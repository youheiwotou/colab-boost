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
import math
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests
import websocket
from google.auth.exceptions import RefreshError
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
PENDING_FLOW_TTL_S = 900.0


@dataclass
class _AccountSessionEntry:
    session: Any
    last_input_json: str
    last_persisted_json: str
    refresh_token: Optional[str] = None


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


def _parse_float_field(data: dict[str, Any], *keys: str) -> Optional[float]:
    """Parses the first valid finite numeric field among ``keys``, rejecting ``None`` and ``bool``."""
    for key in keys:
        if key not in data:
            continue
        raw = data[key]
        if raw is None or isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            val = float(raw)
            if math.isfinite(val):
                return val
        elif isinstance(raw, str):
            text = raw.strip()
            if not text:
                continue
            try:
                val = float(text)
            except ValueError:
                continue
            if math.isfinite(val):
                return val
    return None


def _parse_int_field(data: dict[str, Any], *keys: str) -> Optional[int]:
    val = _parse_float_field(data, *keys)
    if val is None:
        return None
    return int(val)


def _is_transient_error(exc: ColabBackendError) -> bool:
    return exc.kind in ("timeout", "network") or exc.status_code in (502, 503, 504)


def _is_complete_assignment(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    ep = str(item.get("endpoint") or "").strip()
    proxy = item.get("runtimeProxyInfo")
    if not ep or not isinstance(proxy, dict):
        return False
    return bool(str(proxy.get("url") or "").strip() and str(proxy.get("token") or "").strip())


class DirectColabBackend(ColabBackend):
    def __init__(
        self,
        *,
        colab_domain: str = COLAB_DOMAIN,
        http_session_factory: Optional[Any] = None,
        on_token_refreshed: Optional[Callable[[str, str], None]] = None,
        assign_retries: int = 2,
        retry_backoff_s: float = 5.0,
        unassign_retries: int = 2,
        unassign_backoff_s: float = 1.0,
        unassign_verify_polls: int = 3,
        unassign_verify_interval_s: float = 1.0,
        pending_flow_ttl_s: float = PENDING_FLOW_TTL_S,
    ) -> None:
        self.colab_domain = colab_domain.rstrip("/")
        self._http_session_factory = http_session_factory
        self._on_token_refreshed = on_token_refreshed
        self.assign_retries = assign_retries
        self.retry_backoff_s = retry_backoff_s
        self.unassign_retries = unassign_retries
        self.unassign_backoff_s = unassign_backoff_s
        self.unassign_verify_polls = unassign_verify_polls
        self.unassign_verify_interval_s = unassign_verify_interval_s
        self._pending_flow_ttl_s = max(1.0, float(pending_flow_ttl_s))
        self._pending_flows: dict[str, tuple[float, InstalledAppFlow]] = {}
        self._sessions: dict[str, _AccountSessionEntry] = {}
        self._account_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    @property
    def is_mock(self) -> bool:
        return False

    @staticmethod
    def _close_session_quietly(sess: Any) -> None:
        close_fn = getattr(sess, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            entries = list(self._sessions.values())
            self._sessions.clear()
            self._pending_flows.clear()
        for entry in entries:
            self._close_session_quietly(entry.session)

    def _get_account_lock(self, account_id: str) -> threading.Lock:
        with self._lock:
            return self._account_locks.setdefault(account_id, threading.Lock())

    def _prune_pending_flows_locked(self, now: Optional[float] = None) -> None:
        ts = time.monotonic() if now is None else now
        expired = [
            fid
            for fid, (created_at, _flow) in self._pending_flows.items()
            if (ts - created_at) > self._pending_flow_ttl_s
        ]
        for fid in expired:
            self._pending_flows.pop(fid, None)

    # -------------------- OAuth2 Multi-Account Login --------------------

    def begin_oauth_login(self) -> dict[str, str]:
        flow = InstalledAppFlow.from_client_config(COLAB_OAUTH_CLIENT_CONFIG, COLAB_PUBLIC_SCOPES)
        flow.redirect_uri = REMOTE_REDIRECT_URI
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline", token_usage="remote")
        flow_id = f"oauth-{uuid.uuid4().hex[:10]}"
        now = time.monotonic()
        with self._lock:
            self._prune_pending_flows_locked(now)
            self._pending_flows[flow_id] = (now, flow)
        return {"flow_id": flow_id, "auth_url": auth_url}

    def complete_oauth_login(self, flow_id: str, auth_code: str) -> dict[str, str]:
        code = (auth_code or "").strip()
        if not code:
            raise ColabBackendError("invalid_code", "Authorization code cannot be empty.")
        with self._lock:
            self._prune_pending_flows_locked()
            item = self._pending_flows.pop(flow_id, None)
        flow = item[1] if item is not None else None
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

    def _notify_token_refreshed(
        self,
        account_id: Optional[str],
        creds: Any,
        entry: Optional[_AccountSessionEntry] = None,
    ) -> None:
        if not account_id or self._on_token_refreshed is None:
            return
        to_json = getattr(creds, "to_json", None)
        if not callable(to_json):
            return
        try:
            new_json = str(to_json())
        except Exception:
            return
        if entry is not None:
            if new_json == entry.last_persisted_json:
                return
            entry.last_persisted_json = new_json
            rt = getattr(creds, "refresh_token", None)
            if rt:
                entry.refresh_token = str(rt)
        try:
            self._on_token_refreshed(account_id, new_json)
        except Exception:
            pass

    def _credentials_from_json(self, token_json: str, account_id: Optional[str] = None) -> Credentials:
        try:
            info = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(info)
        except Exception as exc:
            raise ColabBackendError("invalid_credential", f"Corrupt credential JSON ({type(exc).__name__}).") from exc
        if (creds.expired or not creds.token) and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise ColabBackendError("token_refresh_failed", f"Token refresh failed ({type(exc).__name__}).") from exc
            self._notify_token_refreshed(account_id, creds)
        return creds

    def _get_or_create_account_session_locked(
        self,
        account_id: str,
        token_json: str,
    ) -> _AccountSessionEntry:
        """Returns a pooled session for ``account_id``. Caller must hold ``_get_account_lock(account_id)``."""
        with self._lock:
            existing = self._sessions.get(account_id)

        if existing is not None:
            if token_json == existing.last_input_json or token_json == existing.last_persisted_json:
                return existing
            if self._http_session_factory is None:
                try:
                    incoming_info = json.loads(token_json)
                except Exception as exc:
                    raise ColabBackendError(
                        "invalid_credential",
                        f"Corrupt credential JSON ({type(exc).__name__}).",
                    ) from exc
                if not isinstance(incoming_info, dict):
                    raise ColabBackendError("invalid_credential", "Corrupt credential JSON (expected object).")
                incoming_rt = incoming_info.get("refresh_token")
                existing_creds = getattr(existing.session, "credentials", None)
                existing_tok = getattr(existing_creds, "token", None)
                incoming_tok = incoming_info.get("token")
                same_identity = (
                    bool(existing.refresh_token)
                    and incoming_rt == existing.refresh_token
                ) or (
                    not existing.refresh_token
                    and not incoming_rt
                    and bool(existing_tok)
                    and incoming_tok == existing_tok
                )
                if same_identity:
                    existing.last_input_json = token_json
                    return existing

        if self._http_session_factory is not None:
            sess = self._http_session_factory(token_json)
            creds = getattr(sess, "credentials", None)
            persisted = ""
            if creds is not None and callable(getattr(creds, "to_json", None)):
                try:
                    persisted = str(creds.to_json())
                except Exception:
                    persisted = ""
            entry = _AccountSessionEntry(
                session=sess,
                last_input_json=token_json,
                last_persisted_json=persisted,
                refresh_token=getattr(creds, "refresh_token", None),
            )
        else:
            creds = self._credentials_from_json(token_json, account_id=account_id)
            sess = AuthorizedSession(creds)
            entry = _AccountSessionEntry(
                session=sess,
                last_input_json=token_json,
                last_persisted_json=creds.to_json(),
                refresh_token=creds.refresh_token,
            )

        old_session: Any = None
        with self._lock:
            prev = self._sessions.get(account_id)
            if prev is not None and prev.session is not entry.session:
                old_session = prev.session
            self._sessions[account_id] = entry
        if old_session is not None:
            self._close_session_quietly(old_session)
        return entry

    def _authorized_session(self, token_json: str, account_id: Optional[str] = None):
        if account_id:
            with self._get_account_lock(account_id):
                return self._get_or_create_account_session_locked(account_id, token_json).session
        if self._http_session_factory is not None:
            return self._http_session_factory(token_json)
        creds = self._credentials_from_json(token_json, account_id=None)
        return AuthorizedSession(creds)

    def inspect_token(self, token_json: str) -> dict[str, str]:
        creds = self._credentials_from_json(token_json)
        if not creds.token and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise ColabBackendError(
                    "token_refresh_failed",
                    f"Token refresh failed ({type(exc).__name__}).",
                ) from exc
        email = ""
        display_name = ""
        sess = AuthorizedSession(creds)
        try:
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
                    r2 = requests.get(
                        TOKENINFO_URL,
                        headers={"Authorization": f"Bearer {creds.token}"},
                        timeout=15,
                    )
                    if r2.ok:
                        email = str(r2.json().get("email") or "")
                except Exception:
                    pass
        finally:
            self._close_session_quietly(sess)
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

        acct_lock: Optional[threading.Lock] = None
        entry: Optional[_AccountSessionEntry] = None
        one_off_sess = False
        token_before: Optional[str] = None

        if account_id:
            acct_lock = self._get_account_lock(account_id)
            with acct_lock:
                entry = self._get_or_create_account_session_locked(account_id, token_json)
                sess = entry.session
                creds = getattr(sess, "credentials", None)
                if creds is not None:
                    if (getattr(creds, "expired", False) or not getattr(creds, "token", None)) and getattr(
                        creds, "refresh_token", None
                    ):
                        try:
                            creds.refresh(Request())
                        except Exception as exc:
                            raise ColabBackendError(
                                "token_refresh_failed",
                                f"Token refresh failed ({type(exc).__name__}).",
                            ) from exc
                        self._notify_token_refreshed(account_id, creds, entry)
                    token_before = getattr(creds, "token", None)
        else:
            sess = self._authorized_session(token_json, account_id=None)
            one_off_sess = self._http_session_factory is None
            creds = getattr(sess, "credentials", None)
            token_before = getattr(creds, "token", None) if creds is not None else None

        try:
            try:
                resp = sess.request(method, url, params=q, headers=req_headers, timeout=timeout)
            except RefreshError as exc:
                raise ColabBackendError(
                    "token_refresh_failed",
                    f"Token refresh failed ({type(exc).__name__}).",
                ) from exc
            except requests.exceptions.Timeout as exc:
                raise ColabBackendError("timeout", f"Colab request timed out: {method} {path}") from exc
            except Exception as exc:
                raise ColabBackendError("network", f"Colab network error ({type(exc).__name__})") from exc
        finally:
            if account_id and acct_lock is not None and creds is not None:
                with acct_lock:
                    token_after = getattr(creds, "token", None)
                    if token_after and token_after != token_before:
                        self._notify_token_refreshed(account_id, creds, entry)
            if one_off_sess:
                self._close_session_quietly(sess)

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
        now = datetime.now(timezone.utc)
        if not isinstance(data, dict) or not data:
            return UsageSnapshot(
                status="unknown",
                balance_ccu=None,
                fetched_at=now,
                stale_reason="ccu-info returned empty or non-object payload",
            )
        bal = _parse_float_field(data, "currentBalance", "paidComputeUnitsBalance")
        if bal is None:
            return UsageSnapshot(
                status="unknown",
                balance_ccu=None,
                fetched_at=now,
                stale_reason="ccu-info missing valid balance field",
            )
        parsed_rate = _parse_float_field(data, "consumptionRateHourly")
        rate = parsed_rate if parsed_rate is not None else 0.0
        parsed_count = _parse_int_field(data, "assignmentsCount")
        count = parsed_count if parsed_count is not None else 0
        raw_eligible = data.get("eligibleGpus")
        eligible = [str(g).upper() for g in raw_eligible if g] if isinstance(raw_eligible, list) else []
        hours_left = round(bal / rate, 2) if rate > 0.01 else None
        return UsageSnapshot(
            status="live",
            balance_ccu=round(bal, 2),
            burn_rate_hourly=round(rate, 2),
            assignments_count=count,
            hours_remaining=hours_left,
            eligible_gpus=eligible,
            fetched_at=now,
        )

    def _parse_assignment_item(self, item: dict[str, Any], default_accel: Accelerator = Accelerator.NONE) -> RawAssignment:
        raw_acc = str(item.get("accelerator") or default_accel.value).upper()
        try:
            accel = Accelerator(raw_acc)
        except ValueError:
            accel = default_accel
        raw_shape = item.get("machineShape", 0)
        shape = MachineShape.HIGH_RAM if raw_shape in (1, "HIGH_RAM", "SHAPE_HIGHMEM", "hm") else MachineShape.STANDARD
        raw_proxy = item.get("runtimeProxyInfo")
        proxy: dict[str, Any] = raw_proxy if isinstance(raw_proxy, dict) else {}
        raw_exp = _parse_int_field(proxy, "tokenExpiresInSeconds")
        return RawAssignment(
            endpoint=str(item.get("endpoint") or "").strip(),
            accelerator=accel,
            machine_shape=shape,
            proxy_url=str(proxy.get("url") or "").strip().rstrip("/"),
            proxy_token=str(proxy.get("token") or "").strip(),
            token_expires_in_seconds=raw_exp if raw_exp and raw_exp > 0 else 3600,
        )

    def _best_effort_unassign(self, account_id: str, token_json: str, endpoint: str) -> None:
        if not endpoint:
            return
        try:
            self.unassign_runtime(account_id, token_json, endpoint)
        except Exception:
            pass

    def _validate_assignment_response(
        self,
        account_id: str,
        token_json: str,
        res: Any,
        *,
        default_accel: Accelerator,
    ) -> RawAssignment:
        if not isinstance(res, dict):
            raise ColabBackendError("protocol_error", "Colab /tun/m/assign returned a non-object response.")

        outcome = res.get("outcome")
        if outcome is not None:
            outcome_str = str(outcome).strip().upper()
            is_ok_outcome = (
                (isinstance(outcome, int) and not isinstance(outcome, bool) and outcome in (0, 1))
                or outcome_str in ("", "0", "1", "SUCCESS", "OUTCOME_SUCCESS", "UNDEFINED", "OUTCOME_UNDEFINED")
            )
            if not is_ok_outcome:
                leaked_ep = str(res.get("endpoint") or "").strip()
                if leaked_ep:
                    self._best_effort_unassign(account_id, token_json, leaked_ep)
                if outcome == 4 or "DENYLIST" in outcome_str:
                    raise ColabBackendError(
                        "denylisted",
                        f"Colab denied runtime allocation: account restricted/denylisted (outcome={outcome}).",
                    )
                if (
                    (isinstance(outcome, int) and not isinstance(outcome, bool) and outcome in (2, 3, 5))
                    or any(tok in outcome_str for tok in ("QUOTA", "DENIED", "EXCEEDED", "LIMIT", "CAPACITY"))
                ):
                    raise ColabBackendError(
                        "quota_denied",
                        f"Colab denied runtime allocation for '{default_accel.value}' due to quota or capacity (outcome={outcome}).",
                    )
                raise ColabBackendError(
                    "assign_denied",
                    f"Colab refused runtime allocation for '{default_accel.value}' (outcome={outcome}).",
                )

        parsed = self._parse_assignment_item(res, default_accel=default_accel)
        if not parsed.endpoint:
            raise ColabBackendError("protocol_error", "Colab /tun/m/assign response missing endpoint.")
        if not parsed.proxy_url or not parsed.proxy_token:
            self._best_effort_unassign(account_id, token_json, parsed.endpoint)
            raise ColabBackendError(
                "protocol_error",
                f"Colab /tun/m/assign returned incomplete runtimeProxyInfo for endpoint '{parsed.endpoint}'.",
            )
        return parsed

    def _final_assign_recheck(
        self,
        account_id: str,
        token_json: str,
        params: dict[str, str],
        accelerator: Accelerator,
    ) -> Optional[RawAssignment]:
        try:
            recheck = self._colab_request(
                token_json,
                "/tun/m/assign",
                account_id=account_id,
                method="GET",
                params=params,
                timeout=30.0,
            )
        except Exception:
            return None

        if not isinstance(recheck, dict):
            return None
        if _is_complete_assignment(recheck):
            try:
                return self._validate_assignment_response(
                    account_id,
                    token_json,
                    recheck,
                    default_accel=accelerator,
                )
            except ColabBackendError:
                ep = str(recheck.get("endpoint") or "").strip()
                if ep:
                    self._best_effort_unassign(account_id, token_json, ep)
                return None

        partial_ep = str(recheck.get("endpoint") or "").strip()
        if partial_ep:
            self._best_effort_unassign(account_id, token_json, partial_ep)
        return None

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
        if not isinstance(first, dict):
            raise ColabBackendError("protocol_error", "Colab /tun/m/assign GET returned a non-object response.")
        if _is_complete_assignment(first):
            return self._validate_assignment_response(account_id, token_json, first, default_accel=accelerator)

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
                return self._validate_assignment_response(account_id, token_json, res, default_accel=accelerator)
            except ColabBackendError as exc:
                last_err = exc
                if exc.kind == "rejected_400" and accelerator != Accelerator.NONE:
                    raise ColabBackendError(
                        "not_entitled",
                        f"Your Google account is not entitled to accelerator '{accelerator.value}' (HTTP 400). Try L4 or T4.",
                        status_code=400,
                    ) from exc
                if exc.kind in ("quota_denied", "denylisted", "assign_denied", "protocol_error"):
                    raise
                if exc.kind == "capacity_412" or (exc.status_code and exc.status_code not in (502, 503, 504)):
                    if exc.kind != "timeout":
                        raise
                if attempt >= self.assign_retries:
                    if exc.kind in ("timeout", "network"):
                        recovered = self._final_assign_recheck(account_id, token_json, params, accelerator)
                        if recovered is not None:
                            return recovered
                    raise
                time.sleep(self.retry_backoff_s * (2**attempt))
                try:
                    recheck = self._colab_request(
                        token_json,
                        "/tun/m/assign",
                        account_id=account_id,
                        method="GET",
                        params=params,
                        timeout=45.0,
                    )
                except ColabBackendError as recheck_exc:
                    if _is_transient_error(recheck_exc):
                        continue
                    raise
                if isinstance(recheck, dict):
                    if _is_complete_assignment(recheck):
                        return self._validate_assignment_response(
                            account_id,
                            token_json,
                            recheck,
                            default_accel=accelerator,
                        )
                    fresh_xsrf = str(recheck.get("token") or "")
                    if fresh_xsrf:
                        xsrf_token = fresh_xsrf
                    elif recheck.get("endpoint"):
                        self._best_effort_unassign(account_id, token_json, str(recheck.get("endpoint") or "").strip())

        raise last_err or ColabBackendError("assign_failed", "Failed to allocate Colab runtime.")

    def _list_assignments_with_retry(self, account_id: str, token_json: str) -> list[RawAssignment]:
        max_retries = max(0, self.unassign_retries)
        for attempt in range(max_retries + 1):
            try:
                return self.list_assignments(account_id, token_json)
            except ColabBackendError as exc:
                if _is_transient_error(exc) and attempt < max_retries:
                    time.sleep(self.unassign_backoff_s * (2**attempt))
                    continue
                raise
        return []

    def unassign_runtime(self, account_id: str, token_json: str, endpoint: str) -> bool:
        if not endpoint:
            return False
        encoded_ep = quote(endpoint, safe="")
        unassign_path = f"/tun/m/unassign/{encoded_ep}"
        max_retries = max(0, self.unassign_retries)

        for attempt in range(max_retries + 1):
            try:
                prep = self._colab_request(token_json, unassign_path, account_id=account_id, method="GET", timeout=25.0)
                xsrf_token = str(prep.get("token") or "") if isinstance(prep, dict) else ""
                headers = {XSRF_HEADER: xsrf_token} if xsrf_token else {}
                self._colab_request(
                    token_json,
                    unassign_path,
                    account_id=account_id,
                    method="POST",
                    headers=headers,
                    timeout=30.0,
                )
                break
            except ColabBackendError as exc:
                if exc.status_code == 404:
                    return True
                if _is_transient_error(exc) and attempt < max_retries:
                    time.sleep(self.unassign_backoff_s * (2**attempt))
                    continue
                raise

        polls = max(1, self.unassign_verify_polls)
        for poll_idx in range(polls):
            remaining = self._list_assignments_with_retry(account_id, token_json)
            if all(a.endpoint != endpoint for a in remaining):
                return True
            if poll_idx + 1 < polls and self.unassign_verify_interval_s > 0:
                time.sleep(self.unassign_verify_interval_s)
        return False

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
                if r.status_code in (401, 403, 404):
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
                    first_kernel = kernels[0]
                    if isinstance(first_kernel, dict):
                        kid = first_kernel.get("id")
                        if kid:
                            return str(kid)

                r_create = requests.post(
                    f"{assignment.proxy_url}/api/kernels",
                    headers=headers,
                    json={"name": "python3"},
                    timeout=25,
                )
                if r_create.status_code in (401, 403, 404):
                    raise ColabBackendError(
                        "runtime_lost",
                        f"Runtime {assignment.endpoint} is no longer reachable (HTTP {r_create.status_code}).",
                        status_code=r_create.status_code,
                    )
                if r_create.status_code in (502, 503, 504) and attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                r_create.raise_for_status()
                created = r_create.json()
                if not isinstance(created, dict) or not created.get("id"):
                    raise ColabBackendError(
                        "protocol_error",
                        f"Jupyter /api/kernels create response missing kernel id on {assignment.endpoint}.",
                    )
                return str(created["id"])
            except ColabBackendError:
                raise
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_err = exc
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ColabBackendError(
                    "kernel_unreachable",
                    f"Failed to reach Jupyter kernel on {assignment.endpoint}: {exc}",
                ) from exc
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                raise ColabBackendError(
                    "kernel_http_error",
                    f"Jupyter kernel API failed on {assignment.endpoint} (HTTP {status or 'unknown'}): {exc}",
                    status_code=status,
                ) from exc
            except (ValueError, KeyError, TypeError) as exc:
                raise ColabBackendError(
                    "protocol_error",
                    f"Invalid Jupyter kernel response on {assignment.endpoint} ({type(exc).__name__}): {exc}",
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise ColabBackendError(
                    "kernel_unreachable",
                    f"Failed to reach Jupyter kernel on {assignment.endpoint}: {exc}",
                ) from exc
        raise ColabBackendError("kernel_unreachable", f"Failed to start Jupyter kernel on {assignment.endpoint}: {last_err}")

    def _best_effort_interrupt_kernel(self, assignment: RawAssignment, kernel_id: str) -> None:
        if not assignment.proxy_url or not assignment.proxy_token or not kernel_id:
            return
        headers = {
            **CLIENT_AGENT_HEADERS,
            "Authorization": f"Bearer {assignment.proxy_token}",
            "X-Colab-Runtime-Proxy-Token": assignment.proxy_token,
        }
        try:
            requests.post(
                f"{assignment.proxy_url}/api/kernels/{quote(kernel_id, safe='')}/interrupt",
                headers=headers,
                timeout=5,
            )
        except Exception:
            pass

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

        # Inject env inside a function local scope and pop the runner before executing user code
        # so __future__ imports work and zero temporary helper symbols ever pollute user globals().
        full_code = code
        if env:
            env_json = json.dumps({str(k): str(v) for k, v in env.items()})
            full_code = (
                "def _cb_run_with_env(_cb_g):\n"
                "    import json as _cb_json, os as _cb_os\n"
                f"    _cb_injected = _cb_json.loads({env_json!r})\n"
                "    _cb_prev_env = {k: _cb_os.environ.get(k) for k in _cb_injected}\n"
                "    _cb_os.environ.update(_cb_injected)\n"
                f"    _cb_code = {code!r}\n"
                "    try:\n"
                "        exec(compile(_cb_code, '<colab-boost>', 'exec'), _cb_g)\n"
                "    finally:\n"
                "        for _cb_k, _cb_old in _cb_prev_env.items():\n"
                "            if _cb_old is None:\n"
                "                _cb_os.environ.pop(_cb_k, None)\n"
                "            else:\n"
                "                _cb_os.environ[_cb_k] = _cb_old\n"
                "globals().pop('_cb_run_with_env')(globals())\n"
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
        timed_out = False
        try:
            try:
                ws.connect(ws_url, header=ws_headers, timeout=20)
                ws.settimeout(1.0)
                ws.send(json.dumps(req_frame))
            except websocket.WebSocketBadStatusException as exc:
                status = getattr(exc, "status_code", None)
                if status in (401, 403, 404):
                    raise ColabBackendError(
                        "runtime_lost",
                        f"Kernel WebSocket rejected on {assignment.endpoint} (HTTP {status}).",
                        status_code=status,
                    ) from exc
                raise ColabBackendError(
                    "ws_connect_failed",
                    f"Kernel WebSocket handshake failed on {assignment.endpoint} (HTTP {status}): {exc}",
                    status_code=status,
                ) from exc
            except (websocket.WebSocketTimeoutException, TimeoutError) as exc:
                raise ColabBackendError(
                    "exec_timeout",
                    f"Kernel WebSocket connect/send timed out on {assignment.endpoint}: {exc}",
                ) from exc
            except (websocket.WebSocketException, OSError) as exc:
                raise ColabBackendError(
                    "ws_connect_failed",
                    f"Kernel WebSocket connection/send error on {assignment.endpoint} ({type(exc).__name__}): {exc}",
                ) from exc

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            err_name: Optional[str] = None
            err_val: Optional[str] = None
            got_reply = False
            got_idle = False
            hard_deadline = time.monotonic() + timeout_s if timeout_s > 0 else None

            while not (got_reply and got_idle):
                if hard_deadline is not None:
                    remaining = hard_deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        raise ColabBackendError(
                            "exec_timeout",
                            f"Kernel execution timed out after {timeout_s:.1f}s",
                        )
                    ws.settimeout(min(1.0, max(0.05, remaining)))
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException:
                    break
                except (websocket.WebSocketException, OSError) as exc:
                    raise ColabBackendError(
                        "exec_ws_error",
                        f"Kernel WebSocket receive failed on {assignment.endpoint} ({type(exc).__name__}): {exc}",
                    ) from exc
                if not raw:
                    break

                try:
                    msg = deserialize_jupyter_ws_message(raw)
                except (ValueError, UnicodeDecodeError, TypeError) as exc:
                    raise ColabBackendError(
                        "protocol_error",
                        f"Failed to deserialize Jupyter WebSocket frame ({type(exc).__name__}): {exc}",
                    ) from exc
                if not isinstance(msg, dict):
                    raise ColabBackendError(
                        "protocol_error",
                        f"Unexpected Jupyter WebSocket frame type: {type(msg).__name__}",
                    )

                mtype = (msg.get("header") or {}).get("msg_type")
                content = msg.get("content") or {}

                # Kernel death/restart broadcasts arrive with an empty parent_header
                if mtype == "status" and content.get("execution_state") in ("dead", "restarting"):
                    kstate = content.get("execution_state")
                    raise ColabBackendError("kernel_dead", f"Jupyter kernel {kstate} before execution finished.")

                parent_id = (msg.get("parent_header") or {}).get("msg_id")
                if parent_id != msg_id:
                    continue

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
            if timed_out:
                self._best_effort_interrupt_kernel(assignment, kernel_id)
