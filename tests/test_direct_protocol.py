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

