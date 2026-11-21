"""Protocol tests for DirectColabBackend (XSSI, XSRF assign/unassign, HTTP errors) & Windows OpenSSH bridge."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
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


