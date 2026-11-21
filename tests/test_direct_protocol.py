"""Protocol tests for DirectColabBackend (XSSI, XSRF assign/unassign, HTTP errors) & Windows OpenSSH bridge."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Any
import pytest
import requests

from colab_boost.backends.base import ColabBackendError
from colab_boost.backends.direct import XSSI_PREFIX, DirectColabBackend
from colab_boost.models import Accelerator, MachineShape
from colab_boost.ssh_bridge import (
    ensure_isolated_ed25519_keypair,
    format_windows_safe_proxy_command,
)


class _FakeColabServer:
    def __init__(self) -> None:
        self.assigned: dict[str, dict] = {
            "ep-existing-1": {
                "accelerator": "L4",
                "endpoint": "ep-existing-1",
                "variant": 1,
                "machineShape": 0,
                "runtimeProxyInfo": {
                    "token": "tok-existing",
                    "tokenExpiresInSeconds": 3600,
                    "url": "https://ep-existing-1.colab.test",
                },
            }
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        h, p = self.server.server_address
        return f"http://{h}:{p}"

    def _send_xssi(self, handler: BaseHTTPRequestHandler, code: int, obj: dict) -> None:
        body = (XSSI_PREFIX + json.dumps(obj)).encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(body)

    def _handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.startswith("/tun/m/ccu-info"):
                    parent._send_xssi(
                        self,
                        200,
                        {
                            "currentBalance": 142.5,
                            "consumptionRateHourly": 2.82,
                            "assignmentsCount": len(parent.assigned),
                            "eligibleGpus": ["T4", "L4", "A100"],
                        },
                    )
                elif self.path.startswith("/tun/m/assignments"):
                    parent._send_xssi(self, 200, {"assignments": list(parent.assigned.values())})
                elif self.path.startswith("/tun/m/assign"):
                    parent._send_xssi(self, 200, {"token": "xsrf-assign-tok", "acc": "A100", "nbh": "x", "variant": "GPU"})
                elif self.path.startswith("/tun/m/unassign/"):
                    parent._send_xssi(self, 200, {"token": "xsrf-unassign-tok"})
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):  # noqa: N802
                xsrf = self.headers.get("X-Goog-Colab-Token")
                if self.path.startswith("/tun/m/assign"):
                    if xsrf != "xsrf-assign-tok":
                        self.send_response(403)
                        self.end_headers()
                        return
                    if "accelerator=H100" in self.path:
                        parent._send_xssi(self, 400, {"error": "Backend rejected accelerator"})
                        return
                    ep = "ep-new-a100"
                    rec = {
                        "accelerator": "A100",
                        "endpoint": ep,
                        "variant": 1,
                        "machineShape": 1 if "shape=hm" in self.path else 0,
                        "runtimeProxyInfo": {
                            "token": "tok-new-a100",
                            "tokenExpiresInSeconds": 3600,
                            "url": "https://ep-new-a100.colab.test",
                        },
                    }
                    parent.assigned[ep] = rec
                    parent._send_xssi(self, 200, rec)
                elif self.path.startswith("/tun/m/unassign/"):
                    if xsrf != "xsrf-unassign-tok":
                        self.send_response(403)
                        self.end_headers()
                        return
                    ep = self.path.split("/tun/m/unassign/", 1)[1].split("?", 1)[0]
                    parent.assigned.pop(ep, None)
                    parent._send_xssi(self, 200, {})
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *_args):
                return

        return Handler

    def close(self) -> None:
        self.server.shutdown()


def test_direct_colab_backend_xssi_xsrf_assign_and_unassign() -> None:
    fake = _FakeColabServer()
    try:
        backend = DirectColabBackend(
            colab_domain=fake.base_url,
            http_session_factory=lambda _tok: requests.Session(),
            assign_retries=0,
        )
        usage = backend.fetch_usage("acc-1", "{}")
        assert usage.status == "live"
        assert usage.balance_ccu == 142.5
        assert usage.burn_rate_hourly == 2.82
        assert "A100" in usage.eligible_gpus

        # List existing assignment
        items = backend.list_assignments("acc-1", "{}")
        assert len(items) == 1
        assert items[0].endpoint == "ep-existing-1"

        # Allocate new High-RAM A100 via two-step XSRF GET + POST
        created = backend.assign_runtime("acc-1", "{}", Accelerator.A100, high_mem=True)
        assert created.endpoint == "ep-new-a100"
        assert created.accelerator == Accelerator.A100
        assert created.machine_shape == MachineShape.HIGH_RAM

        # HTTP 400 maps to actionable 'not_entitled'
        with pytest.raises(ColabBackendError) as exc_info:
            backend.assign_runtime("acc-1", "{}", Accelerator.H100)
        assert exc_info.value.kind == "not_entitled"

        # Unassign verifies endpoint is gone
        assert backend.unassign_runtime("acc-1", "{}", "ep-new-a100") is True
        assert all(a.endpoint != "ep-new-a100" for a in backend.list_assignments("acc-1", "{}"))
    finally:
        fake.close()


@pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("ssh-keygen"), reason="OpenSSH required")
def test_isolated_ed25519_keygen_and_openssh_proxycommand_in_unicode_dir(tmp_path: Path) -> None:
    # Include Chinese characters and spaces to prove Windows OpenSSH ProxyCommand safety!
    unicode_dir = tmp_path / "中文 空格 目录"
    priv, pub = ensure_isolated_ed25519_keypair(unicode_dir / "ssh")
    assert priv.exists()
    assert pub.startswith("ssh-ed25519 ")

    marker_file = tmp_path / "proxy_invoked.json"
    stub_script = tmp_path / "stub_bridge.py"
    stub_script.write_text(
        f"import json, sys; open({str(marker_file)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    proxy_cmd = format_windows_safe_proxy_command(sys.executable, str(stub_script), "ticket-xyz")
    null_dev = "NUL" if sys.platform == "win32" else "/dev/null"

    subprocess.run(
        [
            "ssh",
            "-F",
            null_dev,
            "-o",
            f"ProxyCommand={proxy_cmd}",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=4",
            "unused-colab-host",
        ],
        capture_output=True,
        timeout=12,
    )
    assert marker_file.exists()
    assert json.loads(marker_file.read_text(encoding="utf-8")) == ["--proxy-ticket", "ticket-xyz"]


def test_direct_jupyter_kernel_websocket_execution_wire_protocol() -> None:
    """Tests DirectColabBackend.execute_python against a real local HTTP + WebSocket Jupyter server."""
    import asyncio
    from websockets.asyncio.server import serve as ws_serve
    from colab_boost.backends.base import RESULT_MARKER, RawAssignment

    received_frames: list[dict] = []
    ws_port_box: list[int] = []
    ws_ready = threading.Event()
    stop_ws_holder: list[asyncio.Event] = []
    ws_loop_holder: list[asyncio.AbstractEventLoop] = []

    def run_ws_server() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ws_loop_holder.append(loop)
        stop_ev = asyncio.Event()
        stop_ws_holder.append(stop_ev)

        async def handler(websocket):
            raw = await websocket.recv()
            msg = json.loads(raw)
            received_frames.append(msg)
            msg_id = msg["header"]["msg_id"]
            sess_id = msg["header"]["session"]
            # Emit stdout with marker + execute_reply + idle status
            await websocket.send(json.dumps({
                "header": {"msg_type": "stream", "session": sess_id},
                "parent_header": {"msg_id": msg_id},
                "content": {"name": "stdout", "text": f"hello\n{RESULT_MARKER}" + '{"gpu": "A100"}\n'},
            }))
            await websocket.send(json.dumps({
                "header": {"msg_type": "execute_reply", "session": sess_id},
                "parent_header": {"msg_id": msg_id},
                "content": {"status": "ok"},
            }))
            await websocket.send(json.dumps({
                "header": {"msg_type": "status", "session": sess_id},
                "parent_header": {"msg_id": msg_id},
                "content": {"execution_state": "idle"},
            }))

        async def main_ws():
            async with ws_serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                ws_port_box.append(port)
                ws_ready.set()
                await stop_ev.wait()

        loop.run_until_complete(main_ws())

    t = threading.Thread(target=run_ws_server, daemon=True)
    t.start()
    assert ws_ready.wait(timeout=5.0)
    ws_port = ws_port_box[0]

    # HTTP server for /api/kernels that points proxy_url to the WS port via monkeypatched _ensure_jupyter_kernel
    backend = DirectColabBackend()
    backend._ensure_jupyter_kernel = lambda _a: "kernel-test-123"  # type: ignore[method-assign]

    raw_assign = RawAssignment(
        endpoint="ep-ws-test",
        accelerator=Accelerator.A100,
        machine_shape=MachineShape.STANDARD,
        proxy_url=f"http://127.0.0.1:{ws_port}",
        proxy_token="proxy-tok-xyz",
    )
    try:
        res = backend.execute_python(
            raw_assign,
            "print('test')",
            env={"BOOST_HF_TOKEN": "hf_secret_abc"},
            timeout_s=10.0,
        )
        assert res.ok is True
        assert res.marker_json == {"gpu": "A100"}
        assert len(received_frames) == 1
        sent = received_frames[0]["content"]
        assert sent["store_history"] is False
        assert "BOOST_HF_TOKEN" in sent["code"]
        assert "_cb_os.environ.pop" in sent["code"]
    finally:
        ws_loop_holder[0].call_soon_threadsafe(stop_ws_holder[0].set)


def test_fetch_usage_never_coerces_missing_empty_or_invalid_balance_to_zero() -> None:
    payloads: list[tuple[int, str]] = [
        (200, XSSI_PREFIX + "{}"),
        (200, ""),
        (200, XSSI_PREFIX + "[]"),
        (200, XSSI_PREFIX + json.dumps({"consumptionRateHourly": 2.5, "assignmentsCount": 1})),
        (200, XSSI_PREFIX + json.dumps({"currentBalance": None})),
        (200, XSSI_PREFIX + json.dumps({"currentBalance": True})),
        (200, XSSI_PREFIX + json.dumps({"currentBalance": "not-a-number"})),
        (200, XSSI_PREFIX + json.dumps({"currentBalance": None, "paidComputeUnitsBalance": 42.25})),
        (200, XSSI_PREFIX + json.dumps({"currentBalance": 0.0, "consumptionRateHourly": 0.0})),
    ]
    idx = [0]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            code, body = payloads[idx[0]]
            idx[0] += 1
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *_args):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        host, port = srv.server_address
        backend = DirectColabBackend(
            colab_domain=f"http://{host}:{port}",
            http_session_factory=lambda _tok: requests.Session(),
        )
        # First 7 invalid/missing/empty cases MUST all return status='unknown' and balance_ccu=None
        for _ in range(7):
            snap = backend.fetch_usage("acc-1", "{}")
            assert snap.status == "unknown"
            assert snap.balance_ccu is None
            assert snap.stale_reason

        # Fallback to paidComputeUnitsBalance when currentBalance is null
        snap_fallback = backend.fetch_usage("acc-1", "{}")
        assert snap_fallback.status == "live"
        assert snap_fallback.balance_ccu == 42.25

        # Legitimate explicit 0.0 balance stays live
        snap_zero = backend.fetch_usage("acc-1", "{}")
        assert snap_zero.status == "live"
        assert snap_zero.balance_ccu == 0.0
    finally:
        srv.shutdown()


def test_unassign_runtime_url_encoding_transient_retry_and_eventual_consistency() -> None:
    special_ep = "ep/with space?x=1"
    seen_paths: list[str] = []
    counts = {"get": 0, "post": 0, "list": 0}

    class Handler(BaseHTTPRequestHandler):
        def _xssi(self, code: int, obj: dict) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write((XSSI_PREFIX + json.dumps(obj)).encode("utf-8"))

        def do_GET(self):  # noqa: N802
            seen_paths.append(self.path)
            if self.path.startswith("/tun/m/unassign/"):
                counts["get"] += 1
                if counts["get"] == 1:
                    self._xssi(503, {"error": "transient get"})
                    return
                self._xssi(200, {"token": f"xsrf-{counts['get']}"})
            elif self.path.startswith("/tun/m/assignments"):
                counts["list"] += 1
                if counts["list"] == 1:
                    self._xssi(503, {"error": "transient list"})
                    return
                if counts["list"] == 2:
                    # Eventual consistency lag: endpoint still listed once right after POST
                    self._xssi(200, {"assignments": [{"endpoint": special_ep, "accelerator": "T4"}]})
                    return
                self._xssi(200, {"assignments": []})
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):  # noqa: N802
            seen_paths.append(self.path)
            if self.path.startswith("/tun/m/unassign/"):
                counts["post"] += 1
                if counts["post"] == 1:
                    self._xssi(502, {"error": "transient post"})
                    return
                self._xssi(200, {})
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_args):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        host, port = srv.server_address
        backend = DirectColabBackend(
            colab_domain=f"http://{host}:{port}",
            http_session_factory=lambda _tok: requests.Session(),
            unassign_retries=3,
            unassign_backoff_s=0.01,
            unassign_verify_polls=3,
            unassign_verify_interval_s=0.01,
        )
        assert backend.unassign_runtime("acc-1", "{}", special_ep) is True
        assert any(p.startswith("/tun/m/unassign/ep%2Fwith%20space%3Fx%3D1?") for p in seen_paths)
        assert counts["get"] == 3  # 1 failed 503 + 1 before failed POST + 1 before success POST
        assert counts["post"] == 2
        assert counts["list"] == 3  # 1 failed 503 + 1 stale poll + 1 empty confirmed
    finally:
        srv.shutdown()


def test_assign_runtime_validates_outcome_and_cleans_up_incomplete_proxy() -> None:
    mode = ["quota"]
    unassigned_eps: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _xssi(self, code: int, obj: dict) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write((XSSI_PREFIX + json.dumps(obj)).encode("utf-8"))

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/tun/m/assign"):
                self._xssi(200, {"token": "xsrf-1"})
            elif self.path.startswith("/tun/m/unassign/"):
                self._xssi(200, {"token": "xsrf-u"})
            elif self.path.startswith("/tun/m/assignments"):
                self._xssi(200, {"assignments": []})
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):  # noqa: N802
            if self.path.startswith("/tun/m/assign"):
                if mode[0] == "quota":
                    self._xssi(200, {"outcome": 2})
                elif mode[0] == "denylisted":
                    self._xssi(200, {"outcome": 4})
                elif mode[0] == "empty":
                    self._xssi(200, {})
                elif mode[0] == "half":
                    self._xssi(200, {"endpoint": "ep-half-broken", "accelerator": "A100", "runtimeProxyInfo": {}})
            elif self.path.startswith("/tun/m/unassign/"):
                ep = self.path.split("/tun/m/unassign/", 1)[1].split("?", 1)[0]
                unassigned_eps.append(ep)
                self._xssi(200, {})
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_args):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        host, port = srv.server_address
        backend = DirectColabBackend(
            colab_domain=f"http://{host}:{port}",
            http_session_factory=lambda _tok: requests.Session(),
            assign_retries=1,
            retry_backoff_s=0.01,
            unassign_backoff_s=0.01,
            unassign_verify_interval_s=0.01,
        )

        mode[0] = "quota"
        with pytest.raises(ColabBackendError) as e_quota:
            backend.assign_runtime("acc-1", "{}", Accelerator.A100)
        assert e_quota.value.kind == "quota_denied"

        mode[0] = "denylisted"
        with pytest.raises(ColabBackendError) as e_deny:
            backend.assign_runtime("acc-1", "{}", Accelerator.A100)
        assert e_deny.value.kind == "denylisted"

        mode[0] = "empty"
        with pytest.raises(ColabBackendError) as e_empty:
            backend.assign_runtime("acc-1", "{}", Accelerator.A100)
        assert e_empty.value.kind == "protocol_error"

        mode[0] = "half"
        with pytest.raises(ColabBackendError) as e_half:
            backend.assign_runtime("acc-1", "{}", Accelerator.A100)
        assert e_half.value.kind == "protocol_error"
        assert "ep-half-broken" in unassigned_eps
    finally:
        srv.shutdown()


def test_assign_runtime_recheck_xsrf_refresh_transient_recheck_and_final_timeout_recovery() -> None:
    # 1. Recheck 503 does not abort retry, and fresh XSRF token from recheck is used on subsequent POST
    get_calls = [0]
    post_xsrfs: list[str] = []

    class RetryHandler(BaseHTTPRequestHandler):
        def _xssi(self, code: int, obj: dict) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write((XSSI_PREFIX + json.dumps(obj)).encode("utf-8"))

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/tun/m/assign"):
                get_calls[0] += 1
                if get_calls[0] == 1:
                    self._xssi(200, {"token": "xsrf-v1"})
                elif get_calls[0] == 2:
                    self._xssi(503, {"error": "recheck blip"})
                else:
                    self._xssi(200, {"token": "xsrf-v2"})

        def do_POST(self):  # noqa: N802
            xsrf = self.headers.get("X-Goog-Colab-Token") or ""
            post_xsrfs.append(xsrf)
            if xsrf != "xsrf-v2":
                self._xssi(503, {"error": "stale or busy"})
                return
            self._xssi(
                200,
                {
                    "endpoint": "ep-recovered-xsrf",
                    "accelerator": "L4",
                    "runtimeProxyInfo": {"url": "https://ep-recovered-xsrf.colab.test", "token": "tok-ok"},
                },
            )

        def log_message(self, *_args):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        host, port = srv.server_address
        backend = DirectColabBackend(
            colab_domain=f"http://{host}:{port}",
            http_session_factory=lambda _tok: requests.Session(),
            assign_retries=2,
            retry_backoff_s=0.01,
        )
        raw = backend.assign_runtime("acc-1", "{}", Accelerator.L4)
        assert raw.endpoint == "ep-recovered-xsrf"
        assert post_xsrfs == ["xsrf-v1", "xsrf-v1", "xsrf-v2"]
    finally:
        srv.shutdown()

    # 2. Final attempt timeout triggers final recheck: recovers valid VM if allocated, or cleans up half-baked VM
    class FakeTimeoutSession:
        def __init__(self, final_recheck_body: dict) -> None:
            self.final_recheck_body = final_recheck_body
            self.get_count = 0
            self.unassign_posts: list[str] = []

        def request(self, method: str, url: str, **_kwargs):
            resp = requests.Response()
            resp.status_code = 200
            if "/tun/m/unassign/" in url:
                if method == "POST":
                    self.unassign_posts.append(url)
                    resp._content = (XSSI_PREFIX + "{}").encode("utf-8")
                else:
                    resp._content = (XSSI_PREFIX + json.dumps({"token": "xsrf-u"})).encode("utf-8")
                return resp
            if "/tun/m/assignments" in url:
                resp._content = (XSSI_PREFIX + json.dumps({"assignments": []})).encode("utf-8")
                return resp
            if method == "GET":
                self.get_count += 1
                if self.get_count == 1:
                    resp._content = (XSSI_PREFIX + json.dumps({"token": "xsrf-init"})).encode("utf-8")
                else:
                    resp._content = (XSSI_PREFIX + json.dumps(self.final_recheck_body)).encode("utf-8")
                return resp
            raise requests.exceptions.Timeout("read timed out")

    # 2a: Final recheck recovers complete VM
    sess_ok = FakeTimeoutSession(
        {
            "endpoint": "ep-after-timeout",
            "accelerator": "A100",
            "runtimeProxyInfo": {"url": "https://ep-after-timeout.colab.test", "token": "tok-after"},
        }
    )
    be_ok = DirectColabBackend(
        http_session_factory=lambda _tok: sess_ok,
        assign_retries=0,
        retry_backoff_s=0.01,
    )
    recovered = be_ok.assign_runtime("acc-1", "{}", Accelerator.A100)
    assert recovered.endpoint == "ep-after-timeout"
    assert sess_ok.get_count == 2

    # 2b: Final recheck finds half-baked VM -> cleans it up and re-raises timeout
    sess_half = FakeTimeoutSession({"endpoint": "ep-leaked-half", "runtimeProxyInfo": {}})
    be_half = DirectColabBackend(
        http_session_factory=lambda _tok: sess_half,
        assign_retries=0,
        retry_backoff_s=0.01,
        unassign_backoff_s=0.01,
        unassign_verify_interval_s=0.01,
    )
    with pytest.raises(ColabBackendError) as exc_timeout:
        be_half.assign_runtime("acc-1", "{}", Accelerator.A100)
    assert exc_timeout.value.kind == "timeout"
    assert any("ep-leaked-half" in u for u in sess_half.unassign_posts)


def test_session_pool_reuse_refresh_persistence_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves AuthorizedSession pooling per account_id, pre/post refresh persistence, not-creds.token handling, and close()."""
    created_sessions: list[Any] = []
    persisted: list[tuple[str, str]] = []
    refresh_calls = [0]

    class FakeCreds:
        def __init__(self, info: dict) -> None:
            self.token = info.get("token")
            self.refresh_token = info.get("refresh_token")
            self.expired = bool(info.get("_expired", False))

        def refresh(self, _req) -> None:
            refresh_calls[0] += 1
            self.token = f"tok-refreshed-{refresh_calls[0]}"
            self.expired = False

        def to_json(self) -> str:
            return json.dumps({"token": self.token, "refresh_token": self.refresh_token})

    class FakeAuthSession:
        def __init__(self, creds: FakeCreds) -> None:
            self.credentials = creds
            self.closed = False
            self.call_count = 0
            created_sessions.append(self)

        def request(self, _method: str, _url: str, **_kwargs):
            self.call_count += 1
            # Simulate AuthorizedSession auto-refreshing token mid-request on call #2
            if self.call_count == 2:
                self.credentials.token = "tok-mid-request-rotated"
            resp = requests.Response()
            resp.status_code = 200
            resp._content = (XSSI_PREFIX + json.dumps({"assignments": []})).encode("utf-8")
            return resp

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "colab_boost.backends.direct.Credentials.from_authorized_user_info",
        staticmethod(lambda info: FakeCreds(info)),
    )
    monkeypatch.setattr("colab_boost.backends.direct.AuthorizedSession", FakeAuthSession)

    backend = DirectColabBackend(on_token_refreshed=lambda aid, tj: persisted.append((aid, tj)))

    # 1. Missing creds.token (even when expired=False) triggers refresh & persistence
    initial_json = json.dumps({"token": "", "refresh_token": "rt-1"})
    assert backend.list_assignments("acc-1", initial_json) == []
    assert len(created_sessions) == 1
    assert refresh_calls[0] == 1
    assert len(persisted) == 1
    assert json.loads(persisted[0][1])["token"] == "tok-refreshed-1"

    # 2. Second request with refreshed token_json REUSES the same pooled AuthorizedSession,
    #    and mid-request token change triggers _on_token_refreshed persistence!
    assert backend.list_assignments("acc-1", persisted[0][1]) == []
    assert len(created_sessions) == 1
    assert len(persisted) == 2
    assert persisted[1][0] == "acc-1"
    assert json.loads(persisted[1][1])["token"] == "tok-mid-request-rotated"

    # 3. Different account gets its own session
    acc2_json = json.dumps({"token": "tok-acc2", "refresh_token": "rt-2"})
    assert backend.list_assignments("acc-2", acc2_json) == []
    assert len(created_sessions) == 2

    # 4. close() closes all pooled sessions cleanly
    backend.close()
    assert all(s.closed for s in created_sessions)


def test_inspect_token_bearer_header_refresh_error_and_pending_flow_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves inspect_token wraps refresh errors, sends Bearer header (never access_token query param), and prunes expired flows."""
    seen_tokeninfo_calls: list[dict] = []

    class FakeCreds:
        def __init__(self, token: str, refresh_token: str = "rt-x", fail_refresh: bool = False) -> None:
            self.token = token
            self.refresh_token = refresh_token
            self.expired = not bool(token)
            self._fail_refresh = fail_refresh

        def refresh(self, _req) -> None:
            if self._fail_refresh:
                raise RuntimeError("oauth refresh exploded")
            self.token = "tok-after-refresh"
            self.expired = False

        def to_json(self) -> str:
            return json.dumps({"token": self.token, "refresh_token": self.refresh_token})

    class FakeAuthSession:
        def __init__(self, _creds) -> None:
            self.closed = False

        def get(self, _url: str, **_kwargs):
            # Force fallback to TOKENINFO_URL
            resp = requests.Response()
            resp.status_code = 500
            resp._content = b"{}"
            return resp

        def close(self) -> None:
            self.closed = True

    def fake_requests_get(url: str, **kwargs):
        seen_tokeninfo_calls.append({"url": url, "params": kwargs.get("params"), "headers": kwargs.get("headers")})
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps({"email": "user@example.com"}).encode("utf-8")
        return resp

    monkeypatch.setattr("colab_boost.backends.direct.AuthorizedSession", FakeAuthSession)
    monkeypatch.setattr("colab_boost.backends.direct.requests.get", fake_requests_get)

    backend = DirectColabBackend(pending_flow_ttl_s=1.0)

    # 1. Refresh failure wrapped as ColabBackendError("token_refresh_failed")
    monkeypatch.setattr(
        "colab_boost.backends.direct.Credentials.from_authorized_user_info",
        staticmethod(lambda _info: FakeCreds("", fail_refresh=True)),
    )
    with pytest.raises(ColabBackendError) as err_ref:
        backend.inspect_token("{}")
    assert err_ref.value.kind == "token_refresh_failed"

    # 2. Fallback to TOKENINFO_URL uses Authorization: Bearer header, NOT query params
    monkeypatch.setattr(
        "colab_boost.backends.direct.Credentials.from_authorized_user_info",
        staticmethod(lambda _info: FakeCreds("tok-secret-bearer")),
    )
    info = backend.inspect_token("{}")
    assert info["email"] == "user@example.com"
    assert len(seen_tokeninfo_calls) == 1
    assert seen_tokeninfo_calls[0]["params"] is None
    assert seen_tokeninfo_calls[0]["headers"] == {"Authorization": "Bearer tok-secret-bearer"}

    # 3. _pending_flows TTL cleanup expires stale flows
    with backend._lock:
        backend._pending_flows["oauth-stale"] = (0.0, object())  # type: ignore[assignment]
    with pytest.raises(ColabBackendError) as err_flow:
        backend.complete_oauth_login("oauth-stale", "some-code")
    assert err_flow.value.kind == "flow_expired"
    assert "oauth-stale" not in backend._pending_flows


def test_ensure_jupyter_kernel_403_and_post_errors_and_wrapping() -> None:
    """Proves _ensure_jupyter_kernel maps GET 403 / POST 401,403,404 to runtime_lost and wraps HTTPError/ValueError."""
    from colab_boost.backends.base import RawAssignment

    scenario = ["get_403"]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if scenario[0] == "get_403":
                self.send_response(403)
                self.end_headers()
            elif scenario[0] == "get_bad_json":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"not-valid-json")
            elif scenario[0] == "get_500":
                self.send_response(500)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"[]")

        def do_POST(self):  # noqa: N802
            if scenario[0] == "post_401":
                self.send_response(401)
                self.end_headers()
            elif scenario[0] == "post_403":
                self.send_response(403)
                self.end_headers()
            elif scenario[0] == "post_404":
                self.send_response(404)
                self.end_headers()
            elif scenario[0] == "post_500":
                self.send_response(500)
                self.end_headers()
            elif scenario[0] == "post_bad_json":
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b"{broken")

        def log_message(self, *_args):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        backend = DirectColabBackend()
        raw = RawAssignment("ep-k-err", Accelerator.T4, MachineShape.STANDARD, f"http://{host}:{port}", "tok-k")

        for name, expected_kind, expected_code in [
            ("get_403", "runtime_lost", 403),
            ("post_401", "runtime_lost", 401),
            ("post_403", "runtime_lost", 403),
            ("post_404", "runtime_lost", 404),
            ("get_500", "kernel_http_error", 500),
            ("post_500", "kernel_http_error", 500),
            ("get_bad_json", "protocol_error", None),
            ("post_bad_json", "protocol_error", None),
        ]:
            scenario[0] = name
            with pytest.raises(ColabBackendError) as exc_info:
                backend._ensure_jupyter_kernel(raw)
            assert exc_info.value.kind == expected_kind, f"scenario={name}"
            if expected_code is not None:
                assert exc_info.value.status_code == expected_code
    finally:
        srv.shutdown()


def test_execute_python_hard_timeout_interrupt_env_isolation_and_ws_error_wrapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves continuous output cannot bypass hard wall-clock timeout, interrupt is posted, env does not pollute globals(), and WS errors wrap."""
    import asyncio
    import os
    import time
    from websockets.asyncio.server import serve as ws_serve
    from colab_boost.backends.base import RawAssignment

    ws_port_box: list[int] = []
    ws_ready = threading.Event()
    stop_holder: list[asyncio.Event] = []
    loop_holder: list[asyncio.AbstractEventLoop] = []
    sent_code_box: list[str] = []
    ws_mode = ["endless_stream"]

    def run_ws() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder.append(loop)
        stop_ev = asyncio.Event()
        stop_holder.append(stop_ev)

        async def handler(ws):
            raw = await ws.recv()
            msg = json.loads(raw)
            sent_code_box.append(msg["content"]["code"])
            msg_id = msg["header"]["msg_id"]
            sess_id = msg["header"]["session"]
            if ws_mode[0] == "endless_stream":
                try:
                    for i in range(200):
                        await ws.send(json.dumps({
                            "header": {"msg_type": "stream", "session": sess_id},
                            "parent_header": {"msg_id": msg_id},
                            "content": {"name": "stdout", "text": f"tick {i}\n"},
                        }))
                        await asyncio.sleep(0.04)
                except Exception:
                    pass
            elif ws_mode[0] == "bad_json":
                await ws.send("{not-valid-json")

        async def main_ws():
            async with ws_serve(handler, "127.0.0.1", 0) as server:
                ws_port_box.append(server.sockets[0].getsockname()[1])
                ws_ready.set()
                await stop_ev.wait()

        loop.run_until_complete(main_ws())

    threading.Thread(target=run_ws, daemon=True).start()
    assert ws_ready.wait(timeout=5.0)

    interrupt_calls: list[dict] = []

    def fake_requests_post(url: str, **kwargs):
        interrupt_calls.append({"url": url, "headers": kwargs.get("headers")})
        resp = requests.Response()
        resp.status_code = 204
        resp._content = b""
        return resp

    monkeypatch.setattr("colab_boost.backends.direct.requests.post", fake_requests_post)

    backend = DirectColabBackend()
    backend._ensure_jupyter_kernel = lambda _a: "kern-timeout-42"  # type: ignore[method-assign]
    raw_assign = RawAssignment(
        endpoint="ep-ws-hard",
        accelerator=Accelerator.L4,
        machine_shape=MachineShape.STANDARD,
        proxy_url=f"http://127.0.0.1:{ws_port_box[0]}",
        proxy_token="tok-ws-hard",
    )

    try:
        # 1. Continuous stream output MUST hit hard wall-clock timeout and POST /interrupt
        t0 = time.monotonic()
        with pytest.raises(ColabBackendError) as exc_to:
            backend.execute_python(
                raw_assign,
                "from __future__ import annotations\nseen_keys = sorted(k for k in globals() if k.startswith('_cb_'))\nseen_val = __import__('os').environ.get('CBOOST_TEST_SECRET')\n",
                env={"CBOOST_TEST_SECRET": "secret-123"},
                timeout_s=0.35,
            )
        elapsed = time.monotonic() - t0
        assert exc_to.value.kind == "exec_timeout"
        assert elapsed < 2.0
        assert len(interrupt_calls) == 1
        assert interrupt_calls[0]["url"].endswith("/api/kernels/kern-timeout-42/interrupt")
        assert interrupt_calls[0]["headers"]["Authorization"] == "Bearer tok-ws-hard"

        # 2. Execute the generated env-wrapper code locally and verify zero _cb_* pollution in globals()
        assert len(sent_code_box) == 1
        ns: dict = {}
        os.environ.pop("CBOOST_TEST_SECRET", None)
        exec(compile(sent_code_box[0], "<test-env-wrapper>", "exec"), ns)
        assert ns["seen_keys"] == []  # Zero _cb_* symbols existed in globals() DURING user code execution!
        assert ns["seen_val"] == "secret-123"
        assert "CBOOST_TEST_SECRET" not in os.environ  # Cleanly restored after execution
        assert not any(k.startswith("_cb_") for k in ns)

        # 3. Malformed WS frame wraps as ColabBackendError("protocol_error")
        ws_mode[0] = "bad_json"
        with pytest.raises(ColabBackendError) as exc_proto:
            backend.execute_python(raw_assign, "print(1)", timeout_s=3.0)
        assert exc_proto.value.kind == "protocol_error"

        # 4. Unreachable WS connect wraps as ColabBackendError("ws_connect_failed")
        dead_assign = RawAssignment(
            endpoint="ep-ws-dead",
            accelerator=Accelerator.T4,
            machine_shape=MachineShape.STANDARD,
            proxy_url="http://127.0.0.1:1",
            proxy_token="tok-dead",
        )
        with pytest.raises(ColabBackendError) as exc_conn:
            backend.execute_python(dead_assign, "print(1)", timeout_s=2.0)
        assert exc_conn.value.kind in ("ws_connect_failed", "exec_timeout")
    finally:
        loop_holder[0].call_soon_threadsafe(stop_holder[0].set)


def test_base_probe_hardware_reports_error_when_execute_python_fails() -> None:
    """Proves ColabBackend.probe_hardware checks res.ok and records error status rather than silent all-zeros."""
    from colab_boost.backends.base import ExecResult, RawAssignment

    backend = DirectColabBackend()
    raw = RawAssignment("ep-probe", Accelerator.L4, MachineShape.STANDARD, "https://probe.test", "tok")

    # 1. Execution failure (res.ok=False)
    backend.execute_python = lambda *_a, **_kw: ExecResult(  # type: ignore[method-assign]
        ok=False,
        stderr="traceback...",
        error_name="RuntimeError",
        error_value="kernel busy",
    )
    hp_err = backend.probe_hardware(raw)
    assert hp_err.probe_ok is False
    assert hp_err.probe_error and "RuntimeError: kernel busy" in hp_err.probe_error
    assert hp_err.extra["probe_status"] == "error"

    # 2. Missing marker JSON
    backend.execute_python = lambda *_a, **_kw: ExecResult(  # type: ignore[method-assign]
        ok=True,
        stdout="no marker here",
        marker_json=None,
    )
    hp_missing = backend.probe_hardware(raw)
    assert hp_missing.probe_ok is False
    assert hp_missing.extra["probe_status"] == "missing_marker"
    assert hp_missing.probe_error


def test_oauth_begin_complete_state_validation_expiry_and_exchange_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers begin_oauth_login / complete_oauth_login including state check, unknown/expired flows, and exchange failure."""
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse

    from colab_boost.backends.direct import REMOTE_REDIRECT_URI

    created_flows: list[Any] = []
    fail_exchange = [False]

    class FakeFlow:
        def __init__(self, client_config: dict, scopes: list[str]) -> None:
            self.client_config = client_config
            self.scopes = scopes
            self.redirect_uri = ""
            self.state = "oauth-state-nonce-7788"
            self.oauth2session = SimpleNamespace(scope="initial-scope")
            self.fetched_codes: list[str] = []
            self.credentials = SimpleNamespace(
                to_json=lambda: json.dumps({"token": "tok-oauth-ok", "refresh_token": "rt-oauth-ok"})
            )
            created_flows.append(self)

        def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
            assert kwargs.get("access_type") == "offline"
            assert kwargs.get("prompt") == "consent"
            assert self.redirect_uri == REMOTE_REDIRECT_URI
            url = (
                "https://accounts.google.com/o/oauth2/auth"
                f"?state={self.state}&redirect_uri={self.redirect_uri}&response_type=code"
            )
            return url, self.state

        def fetch_token(self, *, code: str) -> dict[str, Any]:
            self.fetched_codes.append(code)
            assert self.oauth2session.scope is None
            if fail_exchange[0]:
                raise ValueError("state mismatch or invalid_grant")
            return {"access_token": "tok-oauth-ok", "scope": "openid email"}

    monkeypatch.setattr(
        "colab_boost.backends.direct.InstalledAppFlow.from_client_config",
        staticmethod(lambda cfg, scopes: FakeFlow(cfg, scopes)),
    )

    backend = DirectColabBackend(pending_flow_ttl_s=30.0)
    monkeypatch.setattr(
        backend,
        "inspect_token",
        lambda tj: {"email": "oauth.user@example.com", "display_name": "OAuth User", "token_json": tj},
    )

    # 1. Seed a stale flow and verify begin_oauth_login prunes it and embeds state in auth_url
    with backend._lock:
        backend._pending_flows["oauth-expired-old"] = (0.0, FakeFlow({}, []))  # type: ignore[assignment]

    started = backend.begin_oauth_login()
    assert started["flow_id"].startswith("oauth-")
    assert "oauth-expired-old" not in backend._pending_flows
    qs = parse_qs(urlparse(started["auth_url"]).query)
    assert qs["state"] == ["oauth-state-nonce-7788"]
    assert qs["redirect_uri"] == [REMOTE_REDIRECT_URI]

    # 2. Blank auth code rejected before consuming flow
    with pytest.raises(ColabBackendError) as err_blank:
        backend.complete_oauth_login(started["flow_id"], "   ")
    assert err_blank.value.kind == "invalid_code"

    # 3. Unknown flow_id rejected as flow_expired
    with pytest.raises(ColabBackendError) as err_unknown:
        backend.complete_oauth_login("oauth-unknown-999", "code-xyz")
    assert err_unknown.value.kind == "flow_expired"

    # 4. Happy-path exchange succeeds, restores oauth2session.scope, and consumes flow (single-use)
    info = backend.complete_oauth_login(started["flow_id"], "  auth-code-123  ")
    assert info["email"] == "oauth.user@example.com"
    assert created_flows[-1].fetched_codes == ["auth-code-123"]
    assert created_flows[-1].oauth2session.scope == "openid email"

    with pytest.raises(ColabBackendError) as err_replay:
        backend.complete_oauth_login(started["flow_id"], "auth-code-123")
    assert err_replay.value.kind == "flow_expired"

    # 5. Exchange failure wraps cleanly as ColabBackendError("oauth_exchange_failed")
    started2 = backend.begin_oauth_login()
    fail_exchange[0] = True
    with pytest.raises(ColabBackendError) as err_ex:
        backend.complete_oauth_login(started2["flow_id"], "bad-code")
    assert err_ex.value.kind == "oauth_exchange_failed"


def test_inspect_token_userinfo_success_unverified_and_corrupt_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers inspect_token primary userinfo path, unverified fallback failure, defensive refresh wrap, and invalid JSON."""
    backend = DirectColabBackend()

    # 1. Corrupt JSON credential -> invalid_credential
    with pytest.raises(ColabBackendError) as err_bad:
        backend.inspect_token("{not-json")
    assert err_bad.value.kind == "invalid_credential"

    class FakeCreds:
        def __init__(self, token: str, refresh_token: str = "rt-1", fail_refresh: bool = False) -> None:
            self.token = token
            self.refresh_token = refresh_token
            self.expired = False
            self._fail_refresh = fail_refresh

        def refresh(self, _req: Any) -> None:
            if self._fail_refresh:
                raise RuntimeError("refresh endpoint 400")
            self.token = "tok-refreshed"

        def to_json(self) -> str:
            return json.dumps({"token": self.token, "refresh_token": self.refresh_token})

    userinfo_mode = ["ok"]
    tokeninfo_mode = ["fail"]

    class FakeAuthSession:
        def __init__(self, _creds: Any) -> None:
            self.closed = False

        def get(self, _url: str, **_kwargs: Any):
            if userinfo_mode[0] == "raise":
                raise requests.ConnectionError("userinfo down")
            resp = requests.Response()
            resp.status_code = 200 if userinfo_mode[0] == "ok" else 500
            resp._content = (
                json.dumps({"email": "primary@studio.ai", "name": "Primary User"}).encode("utf-8")
                if userinfo_mode[0] == "ok"
                else b"{}"
            )
            return resp

        def close(self) -> None:
            self.closed = True

    def fake_tokeninfo_get(_url: str, **_kwargs: Any):
        if tokeninfo_mode[0] == "raise":
            raise requests.Timeout("tokeninfo timed out")
        resp = requests.Response()
        resp.status_code = 401
        resp._content = b"{}"
        return resp

    monkeypatch.setattr("colab_boost.backends.direct.AuthorizedSession", FakeAuthSession)
    monkeypatch.setattr("colab_boost.backends.direct.requests.get", fake_tokeninfo_get)
    monkeypatch.setattr(
        "colab_boost.backends.direct.Credentials.from_authorized_user_info",
        staticmethod(lambda _info: FakeCreds("tok-primary")),
    )

    # 2. Primary USERINFO_URL returns email & display_name directly
    res = backend.inspect_token("{}")
    assert res["email"] == "primary@studio.ai"
    assert res["display_name"] == "Primary User"

    # 3. Both USERINFO_URL and TOKENINFO_URL fail -> unverified_account
    userinfo_mode[0] = "raise"
    tokeninfo_mode[0] = "raise"
    with pytest.raises(ColabBackendError) as err_unv:
        backend.inspect_token("{}")
    assert err_unv.value.kind == "unverified_account"

    # 4. Defensive refresh inside inspect_token wraps refresh error as token_refresh_failed
    monkeypatch.setattr(
        backend,
        "_credentials_from_json",
        lambda _tj: FakeCreds("", refresh_token="rt-fail", fail_refresh=True),
    )
    with pytest.raises(ColabBackendError) as err_ref:
        backend.inspect_token("{}")
    assert err_ref.value.kind == "token_refresh_failed"


def test_execute_python_error_traceback_stderr_display_kernel_dead_and_disconnect() -> None:
    """Covers execute_python error/traceback frames, stderr, display_data, execute_reply-only errors, kernel_dead, and early WS close."""
    import asyncio

    from websockets.asyncio.server import serve as ws_serve

    from colab_boost.backends.base import RESULT_MARKER, RawAssignment

    ws_port_box: list[int] = []
    ws_ready = threading.Event()
    stop_holder: list[asyncio.Event] = []
    loop_holder: list[asyncio.AbstractEventLoop] = []
    scenario = ["traceback_error"]

    def run_ws() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder.append(loop)
        stop_ev = asyncio.Event()
        stop_holder.append(stop_ev)

        async def handler(ws):
            raw = await ws.recv()
            msg = json.loads(raw)
            msg_id = msg["header"]["msg_id"]
            sess_id = msg["header"]["session"]
            mode = scenario[0]

            if mode == "traceback_error":
                # Unrelated frame (different parent_header) must be ignored
                await ws.send(json.dumps({
                    "header": {"msg_type": "stream", "session": sess_id},
                    "parent_header": {"msg_id": "other-msg-id"},
                    "content": {"name": "stdout", "text": "IGNORE_ME\n"},
                }))
                # Stderr stream + display_data + malformed RESULT_MARKER line
                await ws.send(json.dumps({
                    "header": {"msg_type": "stream", "session": sess_id},
                    "parent_header": {"msg_id": msg_id},
                    "content": {"name": "stderr", "text": "warn: low vram\n"},
                }))
                await ws.send(json.dumps({
                    "header": {"msg_type": "display_data", "session": sess_id},
                    "parent_header": {"msg_id": msg_id},
                    "content": {"data": {"text/plain": f"disp-out\n{RESULT_MARKER}{{bad-json"}},
                }))
                # Error frame with traceback
                await ws.send(json.dumps({
                    "header": {"msg_type": "error", "session": sess_id},
                    "parent_header": {"msg_id": msg_id},
                    "content": {
                        "ename": "ZeroDivisionError",
                        "evalue": "division by zero",
                        "traceback": ["Traceback (most recent call last):", "  1 / 0", "ZeroDivisionError: division by zero"],
                    },
                }))
                await ws.send(json.dumps({
                    "header": {"msg_type": "execute_reply", "session": sess_id},
                    "parent_header": {"msg_id": msg_id},
                    "content": {"status": "error", "ename": "ZeroDivisionError", "evalue": "division by zero"},
                }))
                await ws.send(json.dumps({
                    "header": {"msg_type": "status", "session": sess_id},
                    "parent_header": {"msg_id": msg_id},
                    "content": {"execution_state": "idle"},
                }))
            elif mode == "reply_only_error":
                await ws.send(json.dumps({
                    "header": {"msg_type": "execute_reply", "session": sess_id},
                    "parent_header": {"msg_id": msg_id},
                    "content": {"status": "error", "ename": "SyntaxError", "evalue": "invalid syntax"},
                }))
                await ws.send(json.dumps({
                    "header": {"msg_type": "status", "session": sess_id},
                    "parent_header": {"msg_id": msg_id},
                    "content": {"execution_state": "idle"},
                }))
            elif mode == "kernel_dead":
                # Broadcast with empty parent_header must immediately raise kernel_dead
                await ws.send(json.dumps({
                    "header": {"msg_type": "status", "session": sess_id},
                    "parent_header": {},
                    "content": {"execution_state": "dead"},
                }))
            elif mode == "early_disconnect":
                await ws.send(json.dumps({
                    "header": {"msg_type": "stream", "session": sess_id},
                    "parent_header": {"msg_id": msg_id},
                    "content": {"name": "stdout", "text": "partial before drop\n"},
                }))
                await ws.close()

        async def main_ws():
            async with ws_serve(handler, "127.0.0.1", 0) as server:
                ws_port_box.append(server.sockets[0].getsockname()[1])
                ws_ready.set()
                await stop_ev.wait()

        loop.run_until_complete(main_ws())

    threading.Thread(target=run_ws, daemon=True).start()
    assert ws_ready.wait(timeout=5.0)

    backend = DirectColabBackend()
    backend._ensure_jupyter_kernel = lambda _a: "kern-err-paths"  # type: ignore[method-assign]
    raw_assign = RawAssignment(
        endpoint="ep-ws-err",
        accelerator=Accelerator.L4,
        machine_shape=MachineShape.STANDARD,
        proxy_url=f"http://127.0.0.1:{ws_port_box[0]}",
        proxy_token="tok-ws-err",
    )

    try:
        # 1. Error + traceback + stderr + display_data + ignored foreign parent_header + bad marker JSON
        scenario[0] = "traceback_error"
        res_tb = backend.execute_python(raw_assign, "1 / 0", timeout_s=5.0)
        assert res_tb.ok is False
        assert res_tb.error_name == "ZeroDivisionError"
        assert res_tb.error_value == "division by zero"
        assert "Traceback (most recent call last):" in res_tb.stderr
        assert "warn: low vram" in res_tb.stderr
        assert "disp-out" in res_tb.stdout
        assert "IGNORE_ME" not in res_tb.stdout
        assert res_tb.marker_json is None

        # 2. execute_reply error without preceding error frame
        scenario[0] = "reply_only_error"
        res_rep = backend.execute_python(raw_assign, "def (", timeout_s=5.0)
        assert res_rep.ok is False
        assert res_rep.error_name == "SyntaxError"
        assert res_rep.error_value == "invalid syntax"

        # 3. Kernel death broadcast -> ColabBackendError("kernel_dead")
        scenario[0] = "kernel_dead"
        with pytest.raises(ColabBackendError) as err_dead:
            backend.execute_python(raw_assign, "import os; os._exit(1)", timeout_s=5.0)
        assert err_dead.value.kind == "kernel_dead"

        # 4. Premature WS close before reply+idle -> ColabBackendError("exec_disconnected")
        scenario[0] = "early_disconnect"
        with pytest.raises(ColabBackendError) as err_disc:
            backend.execute_python(raw_assign, "print('hi')", timeout_s=5.0)
        assert err_disc.value.kind == "exec_disconnected"
    finally:
        loop_holder[0].call_soon_threadsafe(stop_holder[0].set)




