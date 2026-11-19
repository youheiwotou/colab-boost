"""Native Direct Colab Backend (Zero WSL, Zero external CLI required).

Talks directly to Google Colab's HTTPS endpoints and the runtime's Jupyter
Kernel WebSocket channel:
  - Per-account OAuth2 credentials via Google's official Colab OAuth client
    and remote copy-paste redirect URI.
  - ``GET /tun/m/ccu-info`` for live Compute Unit balance & burn rate.
  - ``GET /tun/m/assignments``, ``GET+POST /tun/m/assign``, ``GET+POST /tun/m/unassign/{ep}``.
  - ``GET/POST {runtime}/api/kernels`` + ``wss://{runtime}/api/kernels/{id}/channels``
    using Jupyter Messaging Protocol v5.3 with ``store_history=False`` so secrets
    never touch any local history file.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urljoin, urlparse

import requests
import websocket
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from colab_boost.backends.base import (
    RESULT_MARKER,
    ColabBackend,
    ColabBackendError,
    ExecResult,
    RawAssignment,
)
from colab_boost.models import (
     GPU_ACCELERATORS,
    HIGH_MEM_ONLY_ACCELERATORS,
    TPU_ACCELERATORS,
    Accelerator,
    MachineShape,
    UsageSnapshot,
)

XSSI_PREFIX = ")]}'\n"
COLAB_DOMAIN = "https://colab.research.google.com"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
REMOTE_REDIRECT_URI = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"

COLAB_PUBLIC_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/drive.file",
]

# Official Google Cloud SDK / Colab CLI public installed-app client config
COLAB_OAUTH_CLIENT_CONFIG = {
    "installed": {
        "client_id": "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com",
        "project_id": "colab-cli",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "d-FL95Q19q7MQmFpd7hHD0Ty",
        "redirect_uris": ["http://localhost"],
    }
}

CLIENT_AGENT_HEADERS = {
    "Accept": "application/json",
    "X-Colab-Client-Agent": "colab-cli",
}
XSRF_HEADER = "X-Goog-Colab-Token"


def strip_xssi(body: str) -> str:
    if body.startswith(XSSI_PREFIX):
        return body[len(XSSI_PREFIX) :]
    if body.startswith(")]}'"):
        nl = body.find("\n")
        return body[nl + 1 :] if nl != -1 else ""
    return body


def uuid_to_colab_nbh(val: uuid.UUID) -> str:
    raw = str(val).replace("-", "_")
    return raw + ("." * max(0, 44 - len(raw)))


def deserialize_jupyter_ws_message(raw: str | bytes) -> dict[str, Any]:
    """Deserializes Jupyter WebSocket frames across JSON text, Default binary, and v1 binary formats."""
    if isinstance(raw, str):
        return json.loads(raw)

    stripped = raw.lstrip()
    if stripped.startswith(b"{"):
        return json.loads(raw.decode("utf-8", errors="replace"))

    # 1. Default Jupyter WebSocket binary protocol (big-endian uint32 nbufs + uint32 offsets)
    if len(raw) >= 12:
        nbufs = int.from_bytes(raw[:4], byteorder="big")
        if 1 <= nbufs <= 1024 and len(raw) >= 4 * (nbufs + 1):
            offsets = [
                int.from_bytes(raw[4 * (i + 1) : 4 * (i + 2)], byteorder="big")
                for i in range(nbufs)
            ]
            json_start = offsets[0]
            json_stop = offsets[1] if nbufs > 1 else len(raw)
            if 4 * (nbufs + 1) == json_start <= json_stop <= len(raw):
                return json.loads(raw[json_start:json_stop].decode("utf-8"))

    # 2. v1.kernel.websocket.jupyter.org binary protocol (little-endian uint64 count + offsets)
    if len(raw) >= 56:
        offset_count = int.from_bytes(raw[:8], byteorder="little")
        if 6 <= offset_count <= 1024 and len(raw) >= 8 * (offset_count + 1):
            offsets = [
                int.from_bytes(raw[8 * (i + 1) : 8 * (i + 2)], byteorder="little")
                for i in range(offset_count)
            ]
            if 8 * (offset_count + 1) == offsets[0] and offsets[-1] <= len(raw):
                channel = raw[offsets[0] : offsets[1]].decode("utf-8", errors="replace")
                parts = [raw[offsets[i] : offsets[i + 1]] for i in range(1, offset_count - 1)]
                if len(parts) >= 4:
                    return {
                        "channel": channel,
                        "header": json.loads(parts[0].decode("utf-8")),
                        "parent_header": json.loads(parts[1].decode("utf-8")),
                        "metadata": json.loads(parts[2].decode("utf-8")),
                        "content": json.loads(parts[3].decode("utf-8")),
                    }

    return json.loads(raw.decode("utf-8"))


class DirectColabBackend(ColabBackend):
    def __init__(
        self,
        *,
        colab_domain: str = COLAB_DOMAIN,
        http_session_factory: Optional[Any] = None,
        on_token_refreshed: Optional[Callable[[str, str], None]] = None,
        assign_retries: int = 2,
        retry_backoff_s: float = 5.0,
    ) -> None:
        self.colab_domain = colab_domain.rstrip("/")
        self._http_session_factory = http_session_factory
        self._on_token_refreshed = on_token_refreshed
        self.assign_retries = assign_retries
        self.retry_backoff_s = retry_backoff_s
        self._pending_flows: dict[str, InstalledAppFlow] = {}
        self._lock = threading.Lock()

    @property
    def is_mock(self) -> bool:
        return False

    # -------------------- OAuth2 Multi-Account Login --------------------

    def begin_oauth_login(self) -> dict[str, str]:
        flow = InstalledAppFlow.from_client_config(COLAB_OAUTH_CLIENT_CONFIG, COLAB_PUBLIC_SCOPES)
        flow.redirect_uri = REMOTE_REDIRECT_URI
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline", token_usage="remote")
        flow_id = f"oauth-{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._pending_flows[flow_id] = flow
        return {"flow_id": flow_id, "auth_url": auth_url}

    def complete_oauth_login(self, flow_id: str, auth_code: str) -> dict[str, str]:
        code = (auth_code or "").strip()
        if not code:
            raise ColabBackendError("invalid_code", "Authorization code cannot be empty.")
        with self._lock:
            flow = self._pending_flows.pop(flow_id, None)
        if flow is None:
            raise ColabBackendError("flow_expired", "Login flow expired or not found; start login again.")
        try:
            flow.oauth2session.scope = None
            token = flow.fetch_token(code=code)
            flow.oauth2session.scope = token.get("scope")
            creds = flow.credentials
        except Exception as exc:
            raise ColabBackendError("oauth_exchange_failed", f"OAuth code exchange failed ({type(exc).__name__}).") from exc

        return self.inspect_token(creds.to_json())

    def _credentials_from_json(self, token_json: str, account_id: Optional[str] = None) -> Credentials:
        try:
            info = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(info)
        except Exception as exc:
            raise ColabBackendError("invalid_credential", f"Corrupt credential JSON ({type(exc).__name__}).") from exc
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                if account_id and self._on_token_refreshed is not None:
                    try:
                        self._on_token_refreshed(account_id, creds.to_json())
                    except Exception:
                        pass
            except Exception as exc:
                raise ColabBackendError("token_refresh_failed", f"Token refresh failed ({type(exc).__name__}).") from exc
        return creds

    def _authorized_session(self, token_json: str, account_id: Optional[str] = None):
        if self._http_session_factory is not None:
            return self._http_session_factory(token_json)
        creds = self._credentials_from_json(token_json, account_id=account_id)
        return AuthorizedSession(creds)

    def inspect_token(self, token_json: str) -> dict[str, str]:
        creds = self._credentials_from_json(token_json)
        if not creds.token and creds.refresh_token:
            creds.refresh(Request())
        email = ""
        display_name = ""
        sess = AuthorizedSession(creds)
        try:
            r = sess.get(USERINFO_URL, timeout=15)
            if r.ok:
                u = r.json()
                email = str(u.get("email") or "")
                display_name = str(u.get("name") or "")
        except Exception:
            pass
        if not email and creds.token:
            try:
                r2 = requests.get(TOKENINFO_URL, params={"access_token": creds.token}, timeout=15)
                if r2.ok:
                    email = str(r2.json().get("email") or "")
            except Exception:
                pass
        if not email:
            raise ColabBackendError("unverified_account", "Could not verify Google account email for token.")
        return {
            "email": email,
            "display_name": display_name or email.split("@")[0],
            "token_json": creds.to_json(),
        }

    # -------------------- Colab Tunnel REST API --------------------

    def _colab_request(
        self,
        token_json: str,
        path: str,
        *,
        account_id: Optional[str] = None,
        method: str = "GET",
        params: Optional[dict[str, str]] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 45.0,
    ) -> Any:
        url = urljoin(self.colab_domain + "/", path.lstrip("/"))
        q = {"authuser": "0", **(params or {})}
        req_headers = {**CLIENT_AGENT_HEADERS, **(headers or {})}
        sess = self._authorized_session(token_json, account_id=account_id)
        try:
            resp = sess.request(method, url, params=q, headers=req_headers, timeout=timeout)
        except requests.exceptions.Timeout as exc:
            raise ColabBackendError("timeout", f"Colab request timed out: {method} {path}") from exc
        except Exception as exc:
            raise ColabBackendError("network", f"Colab network error ({type(exc).__name__})") from exc

        if not resp.ok:
            code = resp.status_code
            body_preview = strip_xssi(resp.text or "").strip()[:240]
            if code == 400:
                raise ColabBackendError("rejected_400", body_preview or "Backend rejected request (HTTP 400)", status_code=400)
            if code == 412:
                raise ColabBackendError(
                    "capacity_412",
                    "Colab refused allocation (HTTP 412): too many active runtimes or temporary GPU capacity limit.",
                    status_code=412,
                )
            if code in (401, 403):
                raise ColabBackendError(
                    "auth_error",
                    f"Colab authentication rejected (HTTP {code}). Re-authenticate this account.",
                    status_code=code,
                )
            raise ColabBackendError(
                "http_error",
                f"Colab HTTP {code} on {method} {path}: {body_preview or resp.reason}",
                status_code=code,
            )

        cleaned = strip_xssi(resp.text or "").strip()
        if not cleaned:
            return {}
        try:
            return json.loads(cleaned)
        except ValueError as exc:
            raise ColabBackendError("invalid_json", f"Unexpected non-JSON response from {path}") from exc

    def fetch_usage(self, account_id: str, token_json: str) -> UsageSnapshot:
        data = self._colab_request(token_json, "/tun/m/ccu-info", account_id=account_id, timeout=20.0)
        bal = float(data.get("currentBalance", data.get("paidComputeUnitsBalance", 0.0)))
        rate = float(data.get("consumptionRateHourly", 0.0))
        count = int(data.get("assignmentsCount", 0))
        eligible = [str(g).upper() for g in (data.get("eligibleGpus") or []) if g]
        hours_left = round(bal / rate, 2) if rate > 0.01 else None
        return UsageSnapshot(
            status="live",
            balance_ccu=round(bal, 2),
            burn_rate_hourly=round(rate, 2),
            assignments_count=count,
            hours_remaining=hours_left,
            eligible_gpus=eligible,
            fetched_at=datetime.now(timezone.utc),
        )

    def _parse_assignment_item(self, item: dict[str, Any], default_accel: Accelerator = Accelerator.NONE) -> RawAssignment:
        raw_acc = str(item.get("accelerator") or default_accel.value).upper()
        try:
            accel = Accelerator(raw_acc)
        except ValueError:
            accel = default_accel
        raw_shape = item.get("machineShape", 0)
        shape = MachineShape.HIGH_RAM if raw_shape in (1, "HIGH_RAM", "SHAPE_HIGHMEM", "hm") else MachineShape.STANDARD
        proxy = item.get("runtimeProxyInfo") or {}
        return RawAssignment(
            endpoint=str(item.get("endpoint") or ""),
            accelerator=accel,
            machine_shape=shape,
            proxy_url=str(proxy.get("url") or "").rstrip("/"),
            proxy_token=str(proxy.get("token") or ""),
            token_expires_in_seconds=int(proxy.get("tokenExpiresInSeconds") or 3600),
        )

    def list_assignments(self, account_id: str, token_json: str) -> list[RawAssignment]:
        data = self._colab_request(token_json, "/tun/m/assignments", account_id=account_id, timeout=25.0)
        items = data.get("assignments") if isinstance(data, dict) else []
        out: list[RawAssignment] = []
        for item in items or []:
            if isinstance(item, dict) and item.get("endpoint"):
                out.append(self._parse_assignment_item(item))
        return out

    def assign_runtime(
        self,
        account_id: str,
        token_json: str,
        accelerator: Accelerator,
        high_mem: bool = False,
    ) -> RawAssignment:
        nbh = uuid_to_colab_nbh(uuid.uuid4())
        params: dict[str, str] = {"nbh": nbh}
        if accelerator in GPU_ACCELERATORS:
            params["variant"] = "GPU"
            params["accelerator"] = accelerator.value
        elif accelerator in TPU_ACCELERATORS:
            params["variant"] = "TPU"
            params["accelerator"] = accelerator.value
        if high_mem and accelerator not in HIGH_MEM_ONLY_ACCELERATORS:
            params["shape"] = "hm"

        first = self._colab_request(token_json, "/tun/m/assign", account_id=account_id, method="GET", params=params, timeout=60.0)
        if "endpoint" in first and "runtimeProxyInfo" in first:
            return self._parse_assignment_item(first, default_accel=accelerator)

        xsrf_token = str(first.get("token") or "")
        if not xsrf_token:
            raise ColabBackendError("protocol_error", "Colab /tun/m/assign GET returned neither endpoint nor XSRF token.")

        last_err: Optional[Exception] = None
        for attempt in range(self.assign_retries + 1):
            try:
                res = self._colab_request(
                    token_json,
                    "/tun/m/assign",
                    account_id=account_id,
                    method="POST",
                    params=params,
                    headers={XSRF_HEADER: xsrf_token},
                    timeout=120.0,
                )
                return self._parse_assignment_item(res, default_accel=accelerator)
            except ColabBackendError as exc:
                last_err = exc
                if exc.kind == "rejected_400" and accelerator != Accelerator.NONE:
                    raise ColabBackendError(
                        "not_entitled",
                        f"Your Google account is not entitled to accelerator '{accelerator.value}' (HTTP 400). Try L4 or T4.",
                        status_code=400,
                    ) from exc
                if exc.kind == "capacity_412" or (exc.status_code and exc.status_code not in (502, 503, 504)):
                    if exc.kind != "timeout":
                        raise
                if attempt >= self.assign_retries:
                    raise
                time.sleep(self.retry_backoff_s * (2**attempt))
                recheck = self._colab_request(token_json, "/tun/m/assign", account_id=account_id, method="GET", params=params, timeout=45.0)
                if "endpoint" in recheck and "runtimeProxyInfo" in recheck:
                    return self._parse_assignment_item(recheck, default_accel=accelerator)

        raise last_err or ColabBackendError("assign_failed", "Failed to allocate Colab runtime.")

    def unassign_runtime(self, account_id: str, token_json: str, endpoint: str) -> bool:
        if not endpoint:
            return False
        try:
            prep = self._colab_request(token_json, f"/tun/m/unassign/{endpoint}", account_id=account_id, method="GET", timeout=25.0)
            xsrf_token = str(prep.get("token") or "")
            headers = {XSRF_HEADER: xsrf_token} if xsrf_token else {}
            self._colab_request(
                token_json,
                f"/tun/m/unassign/{endpoint}",
                account_id=account_id,
                method="POST",
                headers=headers,
                timeout=30.0,
            )
        except ColabBackendError as exc:
            if exc.status_code == 404:
                return True
            raise

        remaining = self.list_assignments(account_id, token_json)
        return all(a.endpoint != endpoint for a in remaining)

    # -------------------- Jupyter Kernel WebSocket Execution --------------------

    def _ensure_jupyter_kernel(self, assignment: RawAssignment) -> str:
        if not assignment.proxy_url or not assignment.proxy_token:
            raise ColabBackendError("missing_proxy", "Assignment has no runtime proxy URL or token.")
        headers = {
            **CLIENT_AGENT_HEADERS,
            "Authorization": f"Bearer {assignment.proxy_token}",
            "X-Colab-Runtime-Proxy-Token": assignment.proxy_token,
        }
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = requests.get(f"{assignment.proxy_url}/api/kernels", headers=headers, timeout=20)
                if r.status_code in (401, 404):
                    raise ColabBackendError(
                        "runtime_lost",
                        f"Runtime {assignment.endpoint} is no longer reachable (HTTP {r.status_code}).",
                        status_code=r.status_code,
                    )
                if r.status_code in (502, 503, 504) and attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                r.raise_for_status()
                kernels = r.json()
                if isinstance(kernels, list) and kernels:
                    kid = kernels[0].get("id")
                    if kid:
                        return str(kid)

                r_create = requests.post(
                    f"{assignment.proxy_url}/api/kernels",
                    headers=headers,
                    json={"name": "python3"},
                    timeout=25,
                )
                if r_create.status_code in (502, 503, 504) and attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                r_create.raise_for_status()
                return str(r_create.json()["id"])
            except ColabBackendError:
                raise
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_err = exc
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ColabBackendError("kernel_unreachable", f"Failed to reach Jupyter kernel on {assignment.endpoint}: {exc}") from exc
        raise ColabBackendError("kernel_unreachable", f"Failed to start Jupyter kernel on {assignment.endpoint}: {last_err}")

    def execute_python(
        self,
        assignment: RawAssignment,
        code: str,
        *,
        env: Optional[dict[str, str]] = None,
        timeout_s: float = 45.0,
    ) -> ExecResult:
        kernel_id = self._ensure_jupyter_kernel(assignment)
        parsed = urlparse(assignment.proxy_url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        session_id = uuid.uuid4().hex
        query = urlencode({
            "session_id": session_id,
            "token": assignment.proxy_token,
            "colab-runtime-proxy-token": assignment.proxy_token,
        })
        ws_url = f"{ws_scheme}://{parsed.netloc}/api/kernels/{kernel_id}/channels?{query}"
        ws_headers = [
            "X-Colab-Client-Agent: colab-cli",
            f"Authorization: Bearer {assignment.proxy_token}",
            f"X-Colab-Runtime-Proxy-Token: {assignment.proxy_token}",
        ]

        # Inject env safely in memory via compile+exec so __future__ imports,
        # multiline strings, and empty snippets are never corrupted by indentation.
        full_code = code
        if env:
            env_json = json.dumps({str(k): str(v) for k, v in env.items()})
            full_code = (
                "import json as _cb_json, os as _cb_os\n"
                f"_cb_injected = _cb_json.loads({env_json!r})\n"
                "_cb_prev_env = {k: _cb_os.environ.get(k) for k in _cb_injected}\n"
                "_cb_os.environ.update(_cb_injected)\n"
                f"_cb_code = {code!r}\n"
                "try:\n"
                "    exec(compile(_cb_code, '<colab-boost>', 'exec'), globals())\n"
                "finally:\n"
                "    for _cb_k, _cb_old in _cb_prev_env.items():\n"
                "        if _cb_old is None:\n"
                "            _cb_os.environ.pop(_cb_k, None)\n"
                "        else:\n"
                "            _cb_os.environ[_cb_k] = _cb_old\n"
                "    for _cb_sym in ('_cb_json', '_cb_os', '_cb_injected', '_cb_prev_env', '_cb_code', '_cb_k', '_cb_old'):\n"
                "        globals().pop(_cb_sym, None)\n"
            )

        msg_id = uuid.uuid4().hex
        req_frame = {
            "header": {
                "msg_id": msg_id,
                "username": "colab-boost",
                "session": session_id,
                "date": datetime.now(timezone.utc).isoformat(),
                "msg_type": "execute_request",
                "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": full_code,
                "silent": False,
                "store_history": False,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
            "buffers": [],
            "channel": "shell",
        }

        ws = websocket.WebSocket()
        try:
            ws.connect(ws_url, header=ws_headers, timeout=20)
            ws.settimeout(1.0)
            ws.send(json.dumps(req_frame))

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            err_name: Optional[str] = None
            err_val: Optional[str] = None
            got_reply = False
            got_idle = False
            idle_deadline = time.monotonic() + timeout_s if timeout_s > 0 else None

            while not (got_reply and got_idle):
                if idle_deadline is not None and time.monotonic() > idle_deadline:
                    raise ColabBackendError("exec_timeout", f"Kernel execution timed out after {timeout_s:.0f}s")
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException:
                    break
                if not raw:
                    break

                msg = deserialize_jupyter_ws_message(raw)
                mtype = (msg.get("header") or {}).get("msg_type")
                content = msg.get("content") or {}

                # Kernel death/restart broadcasts arrive with an empty parent_header
                if mtype == "status" and content.get("execution_state") in ("dead", "restarting"):
                    kstate = content.get("execution_state")
                    raise ColabBackendError("kernel_dead", f"Jupyter kernel {kstate} before execution finished.")

                parent_id = (msg.get("parent_header") or {}).get("msg_id")
                if parent_id != msg_id:
                    continue

                if idle_deadline is not None:
                    idle_deadline = time.monotonic() + timeout_s

                if mtype == "stream":
                    text = str(content.get("text") or "")
                    if content.get("name") == "stderr":
                        stderr_chunks.append(text)
                    else:
                        stdout_chunks.append(text)
                elif mtype in ("execute_result", "display_data"):
                    plain = (content.get("data") or {}).get("text/plain")
                    if plain:
                        stdout_chunks.append(str(plain) + "\n")
                elif mtype == "error":
                    err_name = str(content.get("ename") or "ExecutionError")
                    err_val = str(content.get("evalue") or "")
                    tb = "\n".join(content.get("traceback") or [])
                    if tb:
                        stderr_chunks.append(tb + "\n")
                elif mtype == "execute_reply":
                    got_reply = True
                    if content.get("status") == "error" and not err_name:
                        err_name = str(content.get("ename") or "ExecutionError")
                        err_val = str(content.get("evalue") or "")
                elif mtype == "status":
                    if content.get("execution_state") == "idle":
                        got_idle = True

            if not (got_reply and got_idle):
                raise ColabBackendError(
                    "exec_disconnected",
                    "Kernel WebSocket connection closed before execution completed.",
                )

            stdout_str = "".join(stdout_chunks)
            stderr_str = "".join(stderr_chunks)
            marker_obj: Optional[dict[str, Any]] = None
            for line in reversed(stdout_str.splitlines()):
                if line.startswith(RESULT_MARKER):
                    try:
                        marker_obj = json.loads(line[len(RESULT_MARKER) :])
                    except ValueError:
                        pass
                    break

            return ExecResult(
                ok=(err_name is None),
                stdout=stdout_str,
                stderr=stderr_str,
                error_name=err_name,
                error_value=err_val,
                marker_json=marker_obj,
            )
        finally:
            try:
                ws.close()
            except Exception:
                pass
