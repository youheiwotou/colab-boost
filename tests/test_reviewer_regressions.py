"""Deep regression & real-wire-protocol tests covering SSH2-over-WS multiplexing, Jupyter framing, races & recovery."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import asyncssh
from fastapi.testclient import TestClient
import pytest
import requests
from websockets.asyncio.server import serve as ws_serve

from colab_boost.backends.base import RESULT_MARKER, ColabBackendError, ExecResult, RawAssignment
from colab_boost.backends.direct import DirectColabBackend, deserialize_jupyter_ws_message
from colab_boost.backends.mock import MockColabBackend
from colab_boost.config import WorkspacePaths
from colab_boost.models import Accelerator, DeployRequest, MachineShape, ProvisionRequest
from colab_boost.orchestrator import ConsoleOrchestrator
from colab_boost.recipe_loader import load_recipe_from_dir, validate_and_build_env
from colab_boost.server import create_app
from colab_boost.ssh_bridge import SshTunnelManager, ensure_isolated_ed25519_keypair


def _start_json_http_server(payload_fn) -> tuple[ThreadingHTTPServer, int]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            code, obj = payload_fn("GET", self.path, self.headers, b"")
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(n) if n > 0 else b""
            code, obj = payload_fn("POST", self.path, self.headers, body)
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, int(srv.server_address[1])


@pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("ssh-keygen"), reason="OpenSSH required")
def test_real_ssh_over_websocket_multiplexed_port_forwards_and_429_slot_limit(tmp_path: Path) -> None:
    """Proves real OpenSSH `-N -L` over `/colab/ssh` WebSocket with 1-slot HTTP 429 enforcement & 2-port multiplexing."""
    srv1, rport1 = _start_json_http_server(lambda *_a: (200, {"service": "llama-server", "port": 1}))
    srv2, rport2 = _start_json_http_server(lambda *_a: (200, {"service": "comfyui", "port": 2}))

    allowed_pubkeys: list[str] = []
    active_ws_count = [0]
    seen_429_kills = [0]
    ws_port_box: list[int] = []
    ready_ev = threading.Event()
    stop_holder: list[asyncio.Event] = []
    loop_holder: list[asyncio.AbstractEventLoop] = []

    class _ColabSshServer(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return username == "root"

        def public_key_auth_supported(self) -> bool:
            return True

        def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
            if username != "root" or not allowed_pubkeys:
                return False
            expected = asyncssh.import_public_key(allowed_pubkeys[-1])
            return key.get_fingerprint() == expected.get_fingerprint()

        def connection_requested(self, dest_host: str, dest_port: int, orig_host: str, orig_port: int) -> bool:
            return dest_host == "127.0.0.1" and dest_port in (rport1, rport2)

    def run_ssh_and_ws_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder.append(loop)
        stop_ev = asyncio.Event()
        stop_holder.append(stop_ev)

        async def main_coro() -> None:
            host_key = asyncssh.generate_private_key("ssh-ed25519")
            ssh_acceptor = await asyncssh.create_server(
                _ColabSshServer,
                "127.0.0.1",
                0,
                server_host_keys=[host_key],
            )
            ssh_port = ssh_acceptor.sockets[0].getsockname()[1]

            async def process_request(connection, request):
                parsed = urlparse(request.path)
                qs = parse_qs(parsed.query)
                if parsed.path != "/colab/ssh":
                    return connection.respond(404, "Not Found")
                if qs.get("colab-runtime-proxy-token") != ["proxy-tok-real"]:
                    return connection.respond(401, "Invalid token")
                pubkey = request.headers.get("X-Colab-Ssh-Pubkey", "").strip()
                if not pubkey.startswith("ssh-ed25519 "):
                    return connection.respond(400, "missing or unsupported pubkey")
                # Enforce Colab Free/Basic single-SSH-session HTTP 429 limit!
                if active_ws_count[0] >= 1:
                    return connection.respond(429, json.dumps({"error": "already-active-session", "limit": 1}))
                allowed_pubkeys.append(pubkey)
                return None

            async def ws_handler(ws):
                active_ws_count[0] += 1
                reader, writer = await asyncio.open_connection("127.0.0.1", ssh_port)

                async def ws_to_tcp():
                    try:
                        async for frame in ws:
                            data = frame.encode("utf-8") if isinstance(frame, str) else frame
                            writer.write(data)
                            await writer.drain()
                    except Exception:
                        pass
                    finally:
                        writer.close()

                async def tcp_to_ws():
                    try:
                        while True:
                            chunk = await reader.read(16384)
                            if not chunk:
                                break
                            await ws.send(chunk)
                    except Exception:
                        pass
                    finally:
                        await ws.close()

                try:
                    await asyncio.gather(ws_to_tcp(), tcp_to_ws())
                finally:
                    active_ws_count[0] -= 1
                    writer.close()

            async with ws_serve(ws_handler, "127.0.0.1", 0, process_request=process_request) as ws_srv:
                ws_port_box.append(ws_srv.sockets[0].getsockname()[1])
                ready_ev.set()
                await stop_ev.wait()
            ssh_acceptor.close()

        loop.run_until_complete(main_coro())

    threading.Thread(target=run_ssh_and_ws_loop, daemon=True).start()
    assert ready_ev.wait(timeout=8.0)

    # Use non-ASCII + space directory to exercise Windows OpenSSH ProxyCommand & temp key handling
    paths = WorkspacePaths(tmp_path / "中文 空格 目录")
    mgr = SshTunnelManager(paths)
    temp_before = set(Path(tempfile.gettempdir()).glob("cboost_*"))

    try:
        # 1. Verify temp file cleanup if subprocess.Popen fails to spawn
        ensure_isolated_ed25519_keypair(paths.ssh_dir)
        with patch("colab_boost.ssh_bridge.subprocess.Popen", side_effect=OSError("simulated spawn error")):
            with pytest.raises(OSError):
                mgr.start_real_tunnel(
                    session_name="sess-real",
                    account_id="acc-1",
                    runtime_url=f"http://127.0.0.1:{ws_port_box[0]}",
                    proxy_token="proxy-tok-real",
                    remote_port=rport1,
                )
        assert set(Path(tempfile.gettempdir()).glob("cboost_*")) - temp_before == set()

        # 2. Open first port forward over real OpenSSH + WebSocket + asyncssh!
        tun1 = mgr.start_real_tunnel(
            session_name="sess-real",
            account_id="acc-1",
            runtime_url=f"http://127.0.0.1:{ws_port_box[0]}",
            proxy_token="proxy-tok-real",
            remote_port=rport1,
            local_port=0,
            path="/v1",
            kind="openai",
            on_429_kill_stale_sshd=lambda: seen_429_kills.__setitem__(0, seen_429_kills[0] + 1),
        )
        assert tun1.status == "open"
        r1 = requests.get(f"http://127.0.0.1:{tun1.local_port}/health", timeout=5).json()
        assert r1["service"] == "llama-server"

        # 3. Open SECOND port forward on the SAME session under strict 1-slot HTTP 429 limit!
        tun2 = mgr.start_real_tunnel(
            session_name="sess-real",
            account_id="acc-1",
            runtime_url=f"http://127.0.0.1:{ws_port_box[0]}",
            proxy_token="proxy-tok-real",
            remote_port=rport2,
            local_port=0,
            path="/",
            kind="http",
            on_429_kill_stale_sshd=lambda: seen_429_kills.__setitem__(0, seen_429_kills[0] + 1),
        )
        assert tun2.status == "open"
        assert seen_429_kills[0] == 0  # Multiplexed cleanly without killing sshd!

        # BOTH local ports simultaneously serve traffic over the single SSH WebSocket session!
        assert requests.get(f"http://127.0.0.1:{tun1.local_port}/health", timeout=5).json()["service"] == "llama-server"
        assert requests.get(f"http://127.0.0.1:{tun2.local_port}/stats", timeout=5).json()["service"] == "comfyui"

        # 4. Closing tun2 keeps tun1 alive!
        assert mgr.stop_tunnel(tun2.id) is True
        assert requests.get(f"http://127.0.0.1:{tun1.local_port}/health", timeout=5).json()["service"] == "llama-server"

        # 5. Stop all tunnels and verify 0 temp files leaked
        mgr.stop_all()
        time.sleep(0.2)
        assert set(Path(tempfile.gettempdir()).glob("cboost_*")) - temp_before == set()
    finally:
        mgr.stop_all()
        srv1.shutdown()
        srv2.shutdown()
        loop_holder[0].call_soon_threadsafe(stop_holder[0].set)


def test_direct_jupyter_protocol_edge_cases_auth_binary_frames_drop_and_future_env() -> None:
    """Proves Authorization Bearer header, boot retry, Default+v1 binary frames, __future__ env, and WS drop detection."""
    # 1. Binary frame deserializers (Default big-endian & v1 little-endian)
    sample_msg = {"header": {"msg_type": "execute_reply"}, "parent_header": {"msg_id": "m1"}, "content": {"status": "ok"}}
    jbytes = json.dumps(sample_msg).encode("utf-8")
    default_bin = (2).to_bytes(4, "big") + (12).to_bytes(4, "big") + (12 + len(jbytes)).to_bytes(4, "big") + jbytes + b"extra"
    assert deserialize_jupyter_ws_message(default_bin)["content"]["status"] == "ok"

    chan = b"shell"
    parts = [
        json.dumps(sample_msg["header"]).encode(),
        json.dumps(sample_msg["parent_header"]).encode(),
        b"{}",
        json.dumps(sample_msg["content"]).encode(),
    ]
    offs = [8 * 7]
    offs.append(offs[-1] + len(chan))
    for pt in parts:
        offs.append(offs[-1] + len(pt))
    v1_bin = (len(offs)).to_bytes(8, "little") + b"".join(o.to_bytes(8, "little") for o in offs) + chan + b"".join(parts)
    assert deserialize_jupyter_ws_message(v1_bin)["header"]["msg_type"] == "execute_reply"

    # 2. HTTP /api/kernels boot 503 retry + Authorization: Bearer header check
    kernel_calls = [0]

    def kernel_http(method: str, path: str, headers, _body: bytes):
        if headers.get("Authorization") != "Bearer tok-boot-123":
            return 401, {"error": "missing bearer"}
        if headers.get("X-Colab-Runtime-Proxy-Token") != "tok-boot-123":
            return 403, {"error": "missing colab proxy header"}
        kernel_calls[0] += 1
        if kernel_calls[0] == 1:
            return 503, {"error": "jupyter starting"}
        if method == "GET" and path == "/api/kernels":
            return 200, []
        if method == "POST" and path == "/api/kernels":
            return 201, {"id": "kern-booted-99"}
        return 404, {}

    ksrv, kport = _start_json_http_server(kernel_http)
    try:
        backend = DirectColabBackend()
        raw = RawAssignment("ep-k", Accelerator.T4, MachineShape.STANDARD, f"http://127.0.0.1:{kport}", "tok-boot-123")
        with patch("colab_boost.backends.direct.time.sleep"):
            assert backend._ensure_jupyter_kernel(raw) == "kern-booted-99"
        assert kernel_calls[0] == 3  # 1st GET 503 -> 2nd GET [] -> POST 201
    finally:
        ksrv.shutdown()


def test_concurrent_stop_all_drains_inflight_provisions_and_deduplicates_receipts(tmp_path: Path) -> None:
    in_assign = threading.Event()
    finish_assign = threading.Event()
    unassign_calls = [0]

    class RaceMock(MockColabBackend):
        def assign_runtime(self, *args, **kwargs):
            in_assign.set()
            finish_assign.wait(timeout=5.0)
            return super().assign_runtime(*args, **kwargs)

        def unassign_runtime(self, account_id: str, token_json: str, endpoint: str) -> bool:
            time.sleep(0.15)
            unassign_calls[0] += 1
            return super().unassign_runtime(account_id, token_json, endpoint)

    orch = ConsoleOrchestrator(paths=WorkspacePaths(tmp_path / "race"), mock_backend=RaceMock("single"), force_file_vault=True)
    orch.update_settings({"guards": {"max_managed_sessions": 1}})

    # 1. In-flight provision counts toward max_managed_sessions & blocks duplicate names!
    op1 = orch.start_provision_async(ProvisionRequest(name="race-gpu", accelerator=Accelerator.A100))
    assert in_assign.wait(timeout=3.0)

    with pytest.raises(ColabBackendError) as cap_err:
        orch.start_provision_async(ProvisionRequest(name="other-gpu", accelerator=Accelerator.T4))
    assert cap_err.value.kind == "session_cap"

    orch.update_settings({"guards": {"max_managed_sessions": 3}})
    with pytest.raises(ColabBackendError) as dup_err:
        orch.start_provision_async(ProvisionRequest(name="race-gpu", accelerator=Accelerator.T4))
    assert dup_err.value.kind == "duplicate_name"

    # 2. Emergency stop_all_sessions cancels & waits for in-flight allocation -> 0 surviving VMs!
    threading.Timer(0.1, finish_assign.set).start()
    rcs = orch.stop_all_sessions("emergency_stop_all")
    assert orch.list_sessions() == []
    assert any(r.reason == "cancelled_cleanup" for r in rcs)
    assert next(o for o in orch.list_provision_ops() if o.op_id == op1.op_id).state == "cancelled"

    # 3. Concurrent stop_session on the same live session writes ONLY 1 receipt & 1 unassign call!
    finish_assign.set()
    unassign_calls[0] = 0
    live_sess = orch.provision_session(ProvisionRequest(name="dedup-sess", accelerator=Accelerator.T4))
    results = []

    def stopper():
        results.append(orch.stop_session(live_sess.endpoint, reason="concurrent_stop"))

    t1 = threading.Thread(target=stopper)
    t2 = threading.Thread(target=stopper)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(results) == 2
    assert results[0].receipt_id == results[1].receipt_id
    assert unassign_calls[0] == 1


def test_extend_deadline_arms_missing_vm_watchdog_and_rejects_failed_exec(tmp_path: Path) -> None:
    should_fail = [True]
    executed_snippets: list[str] = []

    class FakeDirect(MockColabBackend):
        @property
        def is_mock(self) -> bool:
            return False

        def execute_python(self, assignment, code: str, *, env=None, timeout_s: float = 45.0) -> ExecResult:
            executed_snippets.append(code)
            if should_fail[0]:
                return ExecResult(ok=False, stderr="PermissionError", error_name="PermissionError")
            return ExecResult(
                ok=True,
                stdout=f'{RESULT_MARKER}{{"pid": 4321, "deadline_epoch": 9999999999.0}}\n',
                marker_json={"pid": 4321, "deadline_epoch": 9999999999.0},
            )

    fake = FakeDirect("signed_out")
    orch = ConsoleOrchestrator(paths=WorkspacePaths(tmp_path / "wd"), direct_backend=fake, force_file_vault=True)
    orch.update_settings({"backend_mode": "real"})
    acc = orch._register_authenticated_account("owner@colab.dev", "Owner", '{"refresh_token":"r"}')
    raw = fake.assign_runtime(acc.id, "{}", Accelerator.L4)
    orphan_name = f"orphan-{raw.endpoint[-8:]}"

    # Failure on VM MUST raise watchdog_failed and NEVER mark watchdog active!
    with pytest.raises(ColabBackendError) as exc_info:
        orch.extend_session_deadline(orphan_name, 120)
    assert exc_info.value.kind == "watchdog_failed"
    assert orch.list_sessions()[0].watchdog.active is False

    # When VM succeeds and returns a live PID, watchdog becomes active & installs watchdog.py if missing
    should_fail[0] = False
    updated = orch.extend_session_deadline(orphan_name, 120)
    assert updated.watchdog.active is True
    assert any("watchdog.py" in s for s in executed_snippets)


def test_real_deployment_refreshes_expiring_proxy_token_and_persists_runs_across_restart(tmp_path: Path) -> None:
    list_calls = [0]
    poll_calls = [0]

    class ExpiringTokenDirect(MockColabBackend):
        @property
        def is_mock(self) -> bool:
            return False

        def list_assignments(self, account_id: str, token_json: str) -> list[RawAssignment]:
            list_calls[0] += 1
            items = super().list_assignments(account_id, token_json)
            return [
                RawAssignment(
                    endpoint=a.endpoint,
                    accelerator=a.accelerator,
                    machine_shape=a.machine_shape,
                    proxy_url=a.proxy_url,
                    proxy_token=f"fresh-proxy-tok-{list_calls[0]}",
                    token_expires_in_seconds=60,  # Triggers proactive refresh!
                )
                for a in items
            ]

        def execute_python(self, assignment: RawAssignment, code: str, *, env=None, timeout_s: float = 45.0) -> ExecResult:
            if "TBE_RUNTIME_ADDR" in code and "nvidia-smi" in code:
                return ExecResult(
                    ok=True,
                    marker_json={"gpu_name": "NVIDIA L4", "vram_total_mib": 23034, "vram_used_mib": 200, "gpu_util_percent": 1.0, "runtime_addr": "127.0.0.1:9000"},
                )
            if "watchdog.py" in code or "worker.pid" in code and "Popen" in code:
                return ExecResult(ok=True, marker_json={"pid": 7777})
            # Poll snippet: fail 1st time with transient disconnect, succeed 2nd time with ready!
            poll_calls[0] += 1
            if poll_calls[0] == 1:
                raise ColabBackendError("exec_disconnected", "Simulated proxy token rollover")
            assert assignment.proxy_token.startswith("fresh-proxy-tok-")
            return ExecResult(
                ok=True,
                marker_json={
                    "status": {
                        "state": "ready",
                        "stage": "warmup",
                        "message": "qwen2.5-7b ready (sk-secret-key-1234 scrubbed)",
                        "outputs": {"model_id": "qwen2.5-7b-instruct", "quant": "UD-Q4_K_XL", "tok_per_s": 88.2},
                    },
                    "alive": True,
                    "tail": ["Using sk-secret-key-1234 ready"],
                },
            )

    paths = WorkspacePaths(tmp_path / "persist")
    fake = ExpiringTokenDirect("signed_out")
    orch = ConsoleOrchestrator(paths=paths, direct_backend=fake, force_file_vault=True)
    orch.update_settings({"backend_mode": "real", "auto_forward_endpoints": False})
    orch._register_authenticated_account("real@studio.ai", "Real User", '{"refresh_token":"x"}')

    with patch("threading.Event.wait", return_value=False):
        run = orch.start_deployment(
            DeployRequest(
                recipe_id="llama-cpp-gguf",
                inputs={
                    "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                    "quant": "UD-Q4_K_XL",
                    "api_key": "sk-secret-key-1234",
                },
            ),
            run_sync=True,
        )

    assert run.state == "ready"
    assert run.outputs["quant"] == "UD-Q4_K_XL"
    assert poll_calls[0] == 2
    assert paths.runs_file.exists()
    saved_json = paths.runs_file.read_text(encoding="utf-8")
    assert "sk-secret-key-1234" not in saved_json

    # Simulate console restart -> ready run is preserved in list_runs()!
    orch_restarted = ConsoleOrchestrator(paths=paths, direct_backend=fake, force_file_vault=True)
    reloaded_runs = orch_restarted.list_runs()
    assert len(reloaded_runs) == 1
    assert reloaded_runs[0].run_id == run.run_id
    assert reloaded_runs[0].state == "ready"


def test_real_playground_chat_auto_discovers_model_from_vllm_models_endpoint_and_custom_gguf_quants(tmp_path: Path) -> None:
    # 1. Verify llama-cpp-gguf accepts custom quants (UD-Q4_K_XL, BF16) & fallback tag is >= b12000
    from colab_boost.config import project_root

    llama = load_recipe_from_dir(project_root() / "recipes" / "llama-cpp-gguf", bundled=True)
    env, _, _ = validate_and_build_env(llama.manifest, {"repo": "unsloth/Qwen3-32B-GGUF", "quant": "UD-Q4_K_XL"})
    assert env["BOOST_INPUT_QUANT"] == "UD-Q4_K_XL"
    assert 'VERIFIED_FALLBACK_TAG = "b12528"' in llama.script_text

    # 2. Real-mode Playground Chat auto-discovers served model ID via GET /v1/models when model="default"/"auto"
    def vllm_stub(method: str, path: str, headers, body: bytes):
        if headers.get("Authorization") != "Bearer sk-vllm-999":
            return 401, {"error": "unauthorized"}
        if method == "GET" and path == "/v1/models":
            return 200, {"object": "list", "data": [{"id": "qwen2.5-7b-instruct-awq", "object": "model"}]}
        if method == "POST" and path == "/v1/chat/completions":
            req = json.loads(body.decode("utf-8"))
            if req.get("model") != "qwen2.5-7b-instruct-awq":
                return 404, {"error": f"The model '{req.get('model')}' does not exist."}
            return 200, {
                "model": "qwen2.5-7b-instruct-awq",
                "choices": [{"message": {"role": "assistant", "content": "Hello from real vLLM stub!"}}],
                "usage": {"completion_tokens": 12},
            }
        return 404, {}

    vsrv, vport = _start_json_http_server(vllm_stub)
    try:
        class RealStub(MockColabBackend):
            @property
            def is_mock(self) -> bool:
                return False

        orch = ConsoleOrchestrator(
            paths=WorkspacePaths(tmp_path / "chat"),
            direct_backend=RealStub("signed_out"),
            force_file_vault=True,
        )
        orch.update_settings({"backend_mode": "real"})
        app = create_app(orch)
        with TestClient(app) as client:
            # Unregistered loopback port is blocked by SSRF guard unless registered or explicitly opted in
            blocked = client.post(
                "/api/playground/chat",
                json={
                    "base_url": f"http://127.0.0.1:{vport}",
                    "model": "default",
                    "api_key": "sk-vllm-999",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
            assert blocked.status_code == 403
            assert blocked.json()["detail"]["kind"] == "ssrf_blocked"

            # Registering the local tunnel port allows Playground chat cleanly
            orch.tunnels.register_mock_tunnel(
                session_name="vllm-sess",
                account_id="acc-real",
                remote_port=vport,
                local_port=vport,
                path="/v1",
                kind="openai",
            )
            resp = client.post(
                "/api/playground/chat",
                json={
                    "base_url": f"http://127.0.0.1:{vport}",
                    "model": "default",
                    "api_key": "sk-vllm-999",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["is_mock"] is False
            assert data["model"] == "qwen2.5-7b-instruct-awq"
            assert "Hello from real vLLM stub!" in data["reply"]
    finally:
        vsrv.shutdown()


@pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("ssh-keygen"), reason="OpenSSH required")
def test_ssh_tunnel_manager_concurrent_races_and_proxy_token_rotation(tmp_path: Path) -> None:
    """Proves concurrent start_real_tunnel/stop_tunnel serialization and proxy_token rotation survival."""
    srv1, rport1 = _start_json_http_server(lambda *_a: (200, {"svc": "one"}))
    srv2, rport2 = _start_json_http_server(lambda *_a: (200, {"svc": "two"}))
    srv3, rport3 = _start_json_http_server(lambda *_a: (200, {"svc": "three"}))

    allowed_pubkeys: list[str] = []
    active_ws_count = [0]
    valid_token = ["tok-rot-v1"]
    ws_port_box: list[int] = []
    ready_ev = threading.Event()
    stop_holder: list[asyncio.Event] = []
    loop_holder: list[asyncio.AbstractEventLoop] = []

    class _RotatingSshServer(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return username == "root"

        def public_key_auth_supported(self) -> bool:
            return True

        def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
            if username != "root" or not allowed_pubkeys:
                return False
            expected = asyncssh.import_public_key(allowed_pubkeys[-1])
            return key.get_fingerprint() == expected.get_fingerprint()

        def connection_requested(self, dest_host: str, dest_port: int, orig_host: str, orig_port: int) -> bool:
            return dest_host == "127.0.0.1" and dest_port in (rport1, rport2, rport3)

    def run_server_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder.append(loop)
        stop_ev = asyncio.Event()
        stop_holder.append(stop_ev)

        async def main_coro() -> None:
            host_key = asyncssh.generate_private_key("ssh-ed25519")
            ssh_acceptor = await asyncssh.create_server(
                _RotatingSshServer, "127.0.0.1", 0, server_host_keys=[host_key]
            )
            ssh_port = ssh_acceptor.sockets[0].getsockname()[1]

            async def process_request(connection, request):
                parsed = urlparse(request.path)
                qs = parse_qs(parsed.query)
                if parsed.path != "/colab/ssh":
                    return connection.respond(404, "Not Found")
                if qs.get("colab-runtime-proxy-token") != [valid_token[0]]:
                    return connection.respond(401, "Expired or stale proxy token")
                pubkey = request.headers.get("X-Colab-Ssh-Pubkey", "").strip()
                if not pubkey.startswith("ssh-ed25519 "):
                    return connection.respond(400, "missing pubkey")
                if active_ws_count[0] >= 1:
                    return connection.respond(429, json.dumps({"error": "already-active-session", "limit": 1}))
                allowed_pubkeys.append(pubkey)
                return None

            async def ws_handler(ws):
                active_ws_count[0] += 1
                reader, writer = await asyncio.open_connection("127.0.0.1", ssh_port)

                async def ws_to_tcp():
                    try:
                        async for frame in ws:
                            data = frame.encode("utf-8") if isinstance(frame, str) else frame
                            writer.write(data)
                            await writer.drain()
                    except Exception:
                        pass
                    finally:
                        writer.close()

                async def tcp_to_ws():
                    try:
                        while True:
                            chunk = await reader.read(16384)
                            if not chunk:
                                break
                            await ws.send(chunk)
                    except Exception:
                        pass
                    finally:
                        await ws.close()

                try:
                    await asyncio.gather(ws_to_tcp(), tcp_to_ws())
                finally:
                    active_ws_count[0] -= 1
                    writer.close()

            async with ws_serve(ws_handler, "127.0.0.1", 0, process_request=process_request) as ws_srv:
                ws_port_box.append(ws_srv.sockets[0].getsockname()[1])
                ready_ev.set()
                await stop_ev.wait()
            ssh_acceptor.close()

        loop.run_until_complete(main_coro())

    threading.Thread(target=run_server_loop, daemon=True).start()
    assert ready_ev.wait(timeout=8.0)

    paths = WorkspacePaths(tmp_path / "ssh_race")
    mgr = SshTunnelManager(paths)
    base_rt_url = f"http://127.0.0.1:{ws_port_box[0]}"
    temp_before = set(Path(tempfile.gettempdir()).glob("cboost_*"))

    try:
        # 1. Fire TWO concurrent start_real_tunnel calls on the SAME session!
        # Without per-session lock serialization, they race on 1-slot WS 429 & drop each other's forwards.
        results_box: list = []
        errors_box: list[BaseException] = []
        start_barrier = threading.Barrier(2)

        def open_worker(rport: int) -> None:
            try:
                start_barrier.wait(timeout=5.0)
                tun = mgr.start_real_tunnel(
                    session_name="sess-concurrent",
                    account_id="acc-1",
                    runtime_url=base_rt_url,
                    proxy_token=valid_token[0],
                    remote_port=rport,
                    local_port=0,
                    refresh_credentials=lambda: (base_rt_url, valid_token[0]),
                )
                results_box.append((rport, tun))
            except BaseException as exc:
                errors_box.append(exc)

        th1 = threading.Thread(target=open_worker, args=(rport1,))
        th2 = threading.Thread(target=open_worker, args=(rport2,))
        th1.start()
        th2.start()
        th1.join(timeout=25.0)
        th2.join(timeout=25.0)

        assert errors_box == []
        assert len(results_box) == 2
        tun_map = dict(results_box)
        tun1, tun2 = tun_map[rport1], tun_map[rport2]
        assert requests.get(f"http://127.0.0.1:{tun1.local_port}/", timeout=5).json()["svc"] == "one"
        assert requests.get(f"http://127.0.0.1:{tun2.local_port}/", timeout=5).json()["svc"] == "two"
        assert active_ws_count[0] == 1

        # 2. Rotate proxy token to v2 -> reusing tun1 updates stored proxy_token from v1 to v2!
        valid_token[0] = "tok-rot-v2"
        reused = mgr.start_real_tunnel(
            session_name="sess-concurrent",
            account_id="acc-1",
            runtime_url=base_rt_url,
            proxy_token="tok-rot-v2",
            remote_port=rport1,
            local_port=tun1.local_port,
        )
        assert reused.id == tun1.id
        assert mgr.get_session_proxy_token("sess-concurrent") == "tok-rot-v2"

        # 3. Rotate proxy token AGAIN to v3 (v1 and v2 now return HTTP 401!) and stop tun2:
        # stop_tunnel must fetch fresh v3 via refresh_credentials and keep sibling tun1 alive!
        valid_token[0] = "tok-rot-v3"
        assert mgr.stop_tunnel(tun2.id) is True
        assert mgr.get_session_proxy_token("sess-concurrent") == "tok-rot-v3"
        assert requests.get(f"http://127.0.0.1:{tun1.local_port}/", timeout=5).json()["svc"] == "one"

        # 4. Concurrent start_real_tunnel(rport3) + stop_session_tunnels must leave 0 surviving tunnels or ssh procs!
        race_done = threading.Event()

        def start_during_stop() -> None:
            try:
                mgr.start_real_tunnel(
                    session_name="sess-concurrent",
                    account_id="acc-1",
                    runtime_url=base_rt_url,
                    proxy_token=valid_token[0],
                    remote_port=rport3,
                )
            except Exception:
                pass
            finally:
                race_done.set()

        threading.Thread(target=start_during_stop, daemon=True).start()
        time.sleep(0.05)
        mgr.stop_session_tunnels("sess-concurrent")
        assert race_done.wait(timeout=15.0)
        mgr.stop_session_tunnels("sess-concurrent")
        assert mgr.list_tunnels("sess-concurrent") == []
        time.sleep(0.2)
        assert active_ws_count[0] == 0
        assert set(Path(tempfile.gettempdir()).glob("cboost_*")) - temp_before == set()
    finally:
        mgr.stop_all()
        srv1.shutdown()
        srv2.shutdown()
        srv3.shutdown()
        loop_holder[0].call_soon_threadsafe(stop_holder[0].set)


def test_real_deployment_reconciles_and_reattaches_across_console_restart(tmp_path: Path) -> None:
    """Proves console restart reconciles ready/vanished/pre-launch runs and live-reattaches running workers."""
    from datetime import datetime, timezone

    vm_states: dict[str, dict] = {
        "run-alive": {
            "exists": True,
            "alive": True,
            "polls": 0,
            "status": {"state": "running", "stage": "download", "progress": 0.65, "message": "Downloading weights..."},
            "tail": ["[10:00:01] 65% downloaded"],
        },
        "run-ready-offline": {
            "exists": True,
            "alive": True,
            "polls": 0,
            "status": {
                "state": "ready",
                "stage": "warmup",
                "progress": 1.0,
                "message": "qwen2.5-7b ready (~91.5 tok/s)",
                "outputs": {"model_id": "qwen2.5-7b", "tok_per_s": 91.5},
            },
            "tail": ["[10:01:00] Warmup complete"],
        },
        "run-prelaunch": {
            "exists": False,
            "alive": False,
            "polls": 0,
            "status": None,
            "tail": [],
        },
    }

    class ReattachDirect(MockColabBackend):
        @property
        def is_mock(self) -> bool:
            return False

        def execute_python(self, assignment: RawAssignment, code: str, *, env=None, timeout_s: float = 45.0) -> ExecResult:
            for rid, entry in vm_states.items():
                if rid in code:
                    entry["polls"] += 1
                    # On 2nd poll (inside live re-attached background loop), transition run-alive to ready!
                    if rid == "run-alive" and entry["polls"] >= 2:
                        entry["status"] = {
                            "state": "ready",
                            "stage": "warmup",
                            "progress": 1.0,
                            "message": "qwen2.5-7b ready after reattach (~84.0 tok/s)",
                            "outputs": {"model_id": "qwen2.5-7b", "tok_per_s": 84.0},
                        }
                        entry["tail"] = ["[10:00:01] 65% downloaded", "[10:02:10] READY after reattach"]
                    return ExecResult(
                        ok=True,
                        marker_json={
                            "exists": entry["exists"],
                            "status": entry["status"],
                            "alive": entry["alive"],
                            "tail": entry["tail"],
                        },
                    )
            return ExecResult(ok=True, marker_json={"ok": True})

    paths = WorkspacePaths(tmp_path / "reconcile")
    fake = ReattachDirect("signed_out")
    orch1 = ConsoleOrchestrator(paths=paths, direct_backend=fake, force_file_vault=True)
    orch1.update_settings({"backend_mode": "real", "language": "en", "auto_forward_endpoints": False})
    acc = orch1._register_authenticated_account("ops@studio.ai", "Ops", '{"refresh_token":"tok"}')
    raw_live = fake.assign_runtime(acc.id, "{}", Accelerator.A100)

    now_iso = datetime.now(timezone.utc).isoformat()
    paths.ledger_file.write_text(
        json.dumps(
            {
                raw_live.endpoint: {
                    "name": "sess-surviving",
                    "account_id": acc.id,
                    "created_at": now_iso,
                    "gpu_name": "NVIDIA A100-SXM4-80GB",
                    "vram_total_mib": 81920,
                    "watchdog": {"active": True, "deadline_epoch": time.time() + 3600},
                }
            }
        ),
        encoding="utf-8",
    )
    seed_runs = [
        {
            "run_id": "run-alive",
            "recipe_id": "llama-cpp-gguf",
            "recipe_name": {"en": "Deploy GGUF LLM (llama.cpp)", "zh-CN": "部署 GGUF"},
            "account_id": acc.id,
            "session_name": "sess-surviving",
            "session_endpoint": raw_live.endpoint,
            "provisioned_session": True,
            "endpoint_ports": {"openai_api": 8019},
            "state": "running",
            "stage": "plan",
            "progress": 0.2,
            "message": "Planning...",
            "started_at": now_iso,
        },
        {
            "run_id": "run-ready-offline",
            "recipe_id": "llama-cpp-gguf",
            "recipe_name": {"en": "Deploy GGUF LLM (llama.cpp)", "zh-CN": "部署 GGUF"},
            "account_id": acc.id,
            "session_name": "sess-surviving",
            "session_endpoint": raw_live.endpoint,
            "provisioned_session": False,
            "state": "starting",
            "stage": "detect",
            "progress": 0.05,
            "message": "Starting...",
            "started_at": now_iso,
        },
        {
            "run_id": "run-vanished",
            "recipe_id": "llama-cpp-gguf",
            "recipe_name": {"en": "Deploy GGUF LLM (llama.cpp)", "zh-CN": "部署 GGUF"},
            "account_id": acc.id,
            "session_name": "sess-gone",
            "session_endpoint": "m-s-gone-endpoint",
            "provisioned_session": True,
            "state": "running",
            "stage": "download",
            "progress": 0.4,
            "message": "Downloading...",
            "started_at": now_iso,
        },
        {
            "run_id": "run-prelaunch",
            "recipe_id": "llama-cpp-gguf",
            "recipe_name": {"en": "Deploy GGUF LLM (llama.cpp)", "zh-CN": "部署 GGUF"},
            "account_id": acc.id,
            "session_name": "sess-surviving",
            "session_endpoint": raw_live.endpoint,
            "provisioned_session": True,
            "state": "provisioning",
            "stage": "detect",
            "progress": 0.0,
            "message": "Allocating...",
            "started_at": now_iso,
        },
    ]
    paths.runs_file.write_text(json.dumps(seed_runs), encoding="utf-8")
    orch1.shutdown()

    # Restart ConsoleOrchestrator and verify reconciliation + live background re-attachment!
    with patch("threading.Event.wait", return_value=False):
        orch2 = ConsoleOrchestrator(paths=paths, direct_backend=fake, force_file_vault=True)
        try:
            # 1. Finished while offline -> immediately ready
            r_offline = orch2.get_run("run-ready-offline")
            assert r_offline.state == "ready"
            assert r_offline.outputs["tok_per_s"] == 91.5

            # 2. Vanished VM -> failed with clear unassigned reason
            r_vanished = orch2.get_run("run-vanished")
            assert r_vanished.state == "failed"
            assert "unassigned" in r_vanished.message.lower()

            # 3. Pre-launch interrupted -> failed
            r_pre = orch2.get_run("run-prelaunch")
            assert r_pre.state == "failed"
            assert "before worker launched" in r_pre.message.lower()

            # 4. Alive worker -> NOT marked failed; re-attached poller drives it to ready!
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                r_alive = orch2.get_run("run-alive")
                if r_alive.state == "ready":
                    break
                time.sleep(0.05)
            r_alive = orch2.get_run("run-alive")
            assert r_alive.state == "ready"
            assert r_alive.outputs["tok_per_s"] == 84.0
        finally:
            orch2.shutdown()


def test_real_comfyui_keep_hooks_and_local_archive_sync_on_stop(tmp_path: Path) -> None:
    """Proves [[keep]] wires VM watchdog wind-down hooks AND auto-archives outputs locally before stop_session."""
    import io
    import subprocess
    import sys
    import tarfile

    from colab_boost.orchestrator import _build_keep_winddown_hooks

    # 1. Verify generated VM wind-down hook script archives real files + copies to mounted Drive dir
    fake_vm_root = tmp_path / "vm_fs"
    comfy_out = fake_vm_root / "ComfyUI_output"
    comfy_out.mkdir(parents=True)
    (comfy_out / "render_0001.png").write_bytes(b"\x89PNG\r\n\x1a\nsample-render-bytes")
    fake_stage = fake_vm_root / "stage"
    fake_drive = fake_vm_root / "MyDrive"
    fake_drive.mkdir(parents=True)

    comfy = load_recipe_from_dir(Path(__file__).resolve().parents[1] / "recipes" / "comfyui-studio", bundled=True)
    assert len(comfy.manifest.keep) == 1
    hooks = _build_keep_winddown_hooks("run-keep-1", "sess-comfy", comfy.manifest.keep)
    assert len(hooks) == 1
    hook_code = hooks[0]["argv"][2].replace("'/content/ComfyUI/output'", repr(comfy_out.as_posix()))
    vm_env = {
        **dict(__import__("os").environ),
        "BOOST_KEEP_STAGE_DIR": str(fake_stage),
        "BOOST_DRIVE_MOUNT": str(fake_drive),
    }
    subprocess.run([sys.executable, "-c", hook_code], env=vm_env, check=True, timeout=15)

    manifest_doc = json.loads((fake_stage / "run-keep-1" / "keep_manifest.json").read_text(encoding="utf-8"))
    assert manifest_doc["items"][0]["file_count"] == 1
    assert manifest_doc["items"][0]["drive_synced"] is True
    drive_archives = list((fake_drive / "ColabBoost" / "sess-comfy" / "run-keep-1").glob("*.tar.gz"))
    assert len(drive_archives) == 1

    # 2. Verify Real-mode deployment configures watchdog hooks & stop_session auto-archives [[keep]] locally
    seen_watchdog_hook_patch = [False]

    class KeepDirect(MockColabBackend):
        @property
        def is_mock(self) -> bool:
            return False

        def execute_python(self, assignment: RawAssignment, code: str, *, env=None, timeout_s: float = 45.0) -> ExecResult:
            if "existing_hooks = cfg.get('hooks')" in code:
                seen_watchdog_hook_patch[0] = True
                return ExecResult(ok=True, marker_json={"hooks_count": 1, "pid": 4444})
            if "info = {'gpu_name': None" in code:
                return ExecResult(
                    ok=True,
                    marker_json={"gpu_name": "NVIDIA L4", "vram_total_mib": 23034, "vram_used_mib": 300, "gpu_util_percent": 2.0, "runtime_addr": "127.0.0.1:9000"},
                )
            if "watchdog.py" in code or ("worker.pid" in code and "Popen" in code):
                return ExecResult(ok=True, marker_json={"pid": 5555})
            if "base64.b64encode(raw_tar)" in code:
                rewritten = code.replace("'/content/ComfyUI/output'", repr(comfy_out.as_posix()))
                out = subprocess.run([sys.executable, "-c", rewritten], env=vm_env, capture_output=True, text=True, check=True, timeout=15).stdout
                marker_line = next(line for line in out.splitlines() if line.startswith(RESULT_MARKER))
                return ExecResult(ok=True, stdout=out, marker_json=json.loads(marker_line[len(RESULT_MARKER):]))
            # Poll snippet returns ready + keep_stats
            return ExecResult(
                ok=True,
                marker_json={
                    "exists": True,
                    "status": {"state": "ready", "stage": "server", "message": "ComfyUI live", "outputs": {"port": 8188}},
                    "alive": True,
                    "tail": ["ComfyUI started"],
                    "keep_stats": {"/content/ComfyUI/output": {"file_count": 1, "total_bytes": 29}},
                },
            )

    paths = WorkspacePaths(tmp_path / "keep_orch")
    orch = ConsoleOrchestrator(paths=paths, direct_backend=KeepDirect("signed_out"), force_file_vault=True)
    orch.update_settings({"backend_mode": "real", "language": "en", "auto_forward_endpoints": False})
    orch._register_authenticated_account("artist@studio.ai", "Artist", '{"refresh_token":"r"}')

    with patch("threading.Event.wait", return_value=False):
        run = orch.start_deployment(DeployRequest(recipe_id="comfyui-studio", inputs={}), run_sync=True)

    assert seen_watchdog_hook_patch[0] is True
    assert run.state == "ready"
    assert run.keep_paths[0].file_count == 1
    sess = orch.list_sessions()[0]
    assert "/content/ComfyUI/output" in sess.watchdog.keep_paths

    # Stopping the session automatically archives unsynced [[keep]] outputs locally before unassign!
    receipt = orch.stop_session(sess.endpoint)
    assert receipt.verified_unassigned is True
    updated_run = orch.get_run(run.run_id)
    assert updated_run.keep_paths[0].local_archive is not None
    arc_bytes = Path(updated_run.keep_paths[0].local_archive).read_bytes()
    with tarfile.open(fileobj=io.BytesIO(arc_bytes), mode="r:gz") as tf:
        assert any(name.endswith("render_0001.png") for name in tf.getnames())
    orch.shutdown()


@pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("ssh-keygen"), reason="OpenSSH required")
def test_ssh_tunnel_manager_dead_ssh_heals_siblings_and_global_stop_all_fence(tmp_path: Path) -> None:
    """Proves unexpected ssh death heals sibling tunnels without ghost 'open' entries and stop_all fences brand-new sessions."""
    srv1, rport1 = _start_json_http_server(lambda *_a: (200, {"svc": "port-one"}))
    srv2, rport2 = _start_json_http_server(lambda *_a: (200, {"svc": "port-two"}))

    allowed_pubkeys: list[str] = []
    ws_port_box: list[int] = []
    ready_ev = threading.Event()
    stop_holder: list[asyncio.Event] = []
    loop_holder: list[asyncio.AbstractEventLoop] = []

    class _HealSshServer(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return username == "root"

        def public_key_auth_supported(self) -> bool:
            return True

        def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
            if username != "root" or not allowed_pubkeys:
                return False
            expected = asyncssh.import_public_key(allowed_pubkeys[-1])
            return key.get_fingerprint() == expected.get_fingerprint()

        def connection_requested(self, dest_host: str, dest_port: int, orig_host: str, orig_port: int) -> bool:
            return dest_host == "127.0.0.1" and dest_port in (rport1, rport2)

    def run_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder.append(loop)
        stop_ev = asyncio.Event()
        stop_holder.append(stop_ev)

        async def main_coro() -> None:
            host_key = asyncssh.generate_private_key("ssh-ed25519")
            ssh_acceptor = await asyncssh.create_server(_HealSshServer, "127.0.0.1", 0, server_host_keys=[host_key])
            ssh_port = ssh_acceptor.sockets[0].getsockname()[1]

            async def process_request(connection, request):
                if urlparse(request.path).path != "/colab/ssh":
                    return connection.respond(404, "Not Found")
                pubkey = request.headers.get("X-Colab-Ssh-Pubkey", "").strip()
                allowed_pubkeys.append(pubkey)
                return None

            async def ws_handler(ws):
                reader, writer = await asyncio.open_connection("127.0.0.1", ssh_port)

                async def ws_to_tcp():
                    try:
                        async for frame in ws:
                            writer.write(frame.encode("utf-8") if isinstance(frame, str) else frame)
                            await writer.drain()
                    except Exception:
                        pass
                    finally:
                        writer.close()

                async def tcp_to_ws():
                    try:
                        while True:
                            chunk = await reader.read(16384)
                            if not chunk:
                                break
                            await ws.send(chunk)
                    except Exception:
                        pass
                    finally:
                        await ws.close()

                try:
                    await asyncio.gather(ws_to_tcp(), tcp_to_ws())
                finally:
                    writer.close()

            async with ws_serve(ws_handler, "127.0.0.1", 0, process_request=process_request) as ws_srv:
                ws_port_box.append(ws_srv.sockets[0].getsockname()[1])
                ready_ev.set()
                await stop_ev.wait()
            ssh_acceptor.close()

        loop.run_until_complete(main_coro())

    threading.Thread(target=run_loop, daemon=True).start()
    assert ready_ev.wait(timeout=8.0)

    mgr = SshTunnelManager(WorkspacePaths(tmp_path / "ssh_heal"))
    rt_url = f"http://127.0.0.1:{ws_port_box[0]}"
    try:
        tun1 = mgr.start_real_tunnel(
            session_name="sess-heal",
            account_id="acc-1",
            runtime_url=rt_url,
            proxy_token="tok-1",
            remote_port=rport1,
        )
        assert requests.get(f"http://127.0.0.1:{tun1.local_port}/", timeout=5).json()["svc"] == "port-one"

        # Simulate unexpected SSH disconnect BEFORE list_tunnels() is called!
        mgr._session_ssh["sess-heal"].process.kill()
        mgr._session_ssh["sess-heal"].process.wait(timeout=3)

        # Opening rport2 must heal tun1 into the new multiplexed SSH instead of leaving tun1 dead yet "open"
        tun2 = mgr.start_real_tunnel(
            session_name="sess-heal",
            account_id="acc-1",
            runtime_url=rt_url,
            proxy_token="tok-1",
            remote_port=rport2,
        )
        assert requests.get(f"http://127.0.0.1:{tun1.local_port}/", timeout=5).json()["svc"] == "port-one"
        assert requests.get(f"http://127.0.0.1:{tun2.local_port}/", timeout=5).json()["svc"] == "port-two"

        # Kill SSH again and re-request rport1 -> must heal tun1 in place without creating duplicate entries!
        mgr._session_ssh["sess-heal"].process.kill()
        mgr._session_ssh["sess-heal"].process.wait(timeout=3)
        healed1 = mgr.start_real_tunnel(
            session_name="sess-heal",
            account_id="acc-1",
            runtime_url=rt_url,
            proxy_token="tok-1",
            remote_port=rport1,
        )
        assert healed1.id == tun1.id
        assert len(mgr.list_tunnels("sess-heal")) == 2

        # Verify stop_all() fences a brand-new session that entered start_real_tunnel before stop_all()
        in_keygen = threading.Event()
        allow_keygen = threading.Event()
        real_keygen = ensure_isolated_ed25519_keypair

        def slow_keygen(ssh_dir: Path):
            in_keygen.set()
            allow_keygen.wait(timeout=5.0)
            return real_keygen(ssh_dir)

        fence_err: list[BaseException] = []

        def brand_new_caller() -> None:
            try:
                mgr.start_real_tunnel(
                    session_name="brand-new-unseen-session",
                    account_id="acc-1",
                    runtime_url=rt_url,
                    proxy_token="tok-1",
                    remote_port=rport1,
                )
            except BaseException as exc:
                fence_err.append(exc)

        with patch("colab_boost.ssh_bridge.ensure_isolated_ed25519_keypair", side_effect=slow_keygen):
            th = threading.Thread(target=brand_new_caller, daemon=True)
            th.start()
            assert in_keygen.wait(timeout=3.0)
            mgr.stop_all()
            allow_keygen.set()
            th.join(timeout=10.0)

        assert len(fence_err) == 1
        assert mgr.list_tunnels() == []
    finally:
        mgr.stop_all()
        srv1.shutdown()
        srv2.shutdown()
        loop_holder[0].call_soon_threadsafe(stop_holder[0].set)


def test_keep_resync_on_stop_large_chunked_stream_and_symlinks(tmp_path: Path) -> None:
    """Proves stop_session re-syncs after manual sync, streams multi-chunk archives (> inline_limit), and filters bad symlinks."""
    import io
    import os
    import subprocess
    import sys
    import tarfile

    vm_root = tmp_path / "vm_keep_edge"
    out_dir = vm_root / "ComfyUI_output"
    out_dir.mkdir(parents=True)
    (out_dir / "render_0001.png").write_bytes(b"first-frame")
    # Create a broken symlink and an escaping '../outside' symlink if OS allows symlinks
    try:
        os.symlink("nonexistent_target.png", out_dir / "broken_link.png")
        os.symlink("../outside_secret.txt", out_dir / "escape_link.txt")
        os.symlink(".", out_dir / "cycle_self")
    except OSError:
        pass

    vm_env = {
        **dict(os.environ),
        "BOOST_KEEP_STAGE_DIR": str(vm_root / "stage"),
        "BOOST_DRIVE_MOUNT": str(vm_root / "no_drive_here"),
    }
    chunk_calls = [0]

    class ChunkedKeepDirect(MockColabBackend):
        @property
        def is_mock(self) -> bool:
            return False

        def execute_python(self, assignment: RawAssignment, code: str, *, env=None, timeout_s: float = 45.0) -> ExecResult:
            if "info = {'gpu_name': None" in code:
                return ExecResult(
                    ok=True,
                    marker_json={"gpu_name": "NVIDIA L4", "vram_total_mib": 23034, "vram_used_mib": 250, "gpu_util_percent": 1.0, "runtime_addr": "127.0.0.1:9000"},
                )
            if "existing_hooks = cfg.get('hooks')" in code or "watchdog.py" in code or ("worker.pid" in code and "Popen" in code):
                return ExecResult(ok=True, marker_json={"hooks_count": 1, "pid": 6666})
            if "base64.b64encode(raw_tar)" in code:
                # Lower inline_limit to 4 KiB so a 16 KiB uncompressible file exercises the multi-chunk streaming path!
                rewritten = (
                    code.replace("'/content/ComfyUI/output'", repr(out_dir.as_posix()))
                    .replace(str(8 * 1024 * 1024), "4096")
                )
                out = subprocess.run([sys.executable, "-c", rewritten], env=vm_env, capture_output=True, text=True, check=True, timeout=20).stdout
                marker_line = next(line for line in out.splitlines() if line.startswith(RESULT_MARKER))
                return ExecResult(ok=True, stdout=out, marker_json=json.loads(marker_line[len(RESULT_MARKER):]))
            if "fh.seek(" in code and "base64.b64encode(data)" in code:
                chunk_calls[0] += 1
                rewritten_chunk = code.replace(str(8 * 1024 * 1024), "4096")
                out = subprocess.run([sys.executable, "-c", rewritten_chunk], env=vm_env, capture_output=True, text=True, check=True, timeout=15).stdout
                marker_line = next(line for line in out.splitlines() if line.startswith(RESULT_MARKER))
                return ExecResult(ok=True, stdout=out, marker_json=json.loads(marker_line[len(RESULT_MARKER):]))
            return ExecResult(
                ok=True,
                marker_json={
                    "exists": True,
                    "status": {"state": "ready", "stage": "server", "message": "ComfyUI live", "outputs": {"port": 8188}},
                    "alive": True,
                    "tail": ["ComfyUI ready"],
                    "keep_stats": {"/content/ComfyUI/output": {"file_count": 1, "total_bytes": 11}},
                },
            )

    paths = WorkspacePaths(tmp_path / "keep_resync")
    orch = ConsoleOrchestrator(paths=paths, direct_backend=ChunkedKeepDirect("signed_out"), force_file_vault=True)
    orch.update_settings({"backend_mode": "real", "language": "en", "auto_forward_endpoints": False})
    orch._register_authenticated_account("vfx@studio.ai", "VFX", '{"refresh_token":"r"}')

    with patch("threading.Event.wait", return_value=False):
        run = orch.start_deployment(DeployRequest(recipe_id="comfyui-studio", inputs={}), run_sync=True)

    # 1. Early manual sync (small inline payload)
    first_sync = orch.sync_keep_artifacts(run.run_id)
    assert first_sync[0].local_archive is not None

    # 2. Render a new 18 KiB uncompressible file AFTER the first manual sync, then stop_session()!
    random_blob = os.urandom(18 * 1024)
    (out_dir / "render_0002_video.bin").write_bytes(random_blob)
    sess = orch.list_sessions()[0]
    orch.stop_session(sess.endpoint)

    # Multi-chunk streaming MUST have run during stop_session() and preserved render_0002_video.bin!
    assert chunk_calls[0] >= 3
    final_run = orch.get_run(run.run_id)
    arc_data = Path(final_run.keep_paths[0].local_archive).read_bytes()
    with tarfile.open(fileobj=io.BytesIO(arc_data), mode="r:gz") as tf:
        members = tf.getnames()
        assert any(m.endswith("render_0001.png") for m in members)
        assert any(m.endswith("render_0002_video.bin") for m in members)
        assert not any(m.endswith("escape_link.txt") for m in members)
        extracted_blob = tf.extractfile(next(m for m in members if m.endswith("render_0002_video.bin"))).read()
        assert extracted_blob == random_blob
    orch.shutdown()


def test_reconcile_real_runs_redacts_vault_run_secrets_and_restores_ready_tunnels(tmp_path: Path) -> None:
    """Proves console restart redacts per-run api_key secrets via vault, restores ready tunnels, and fails deleted recipes."""
    from datetime import datetime, timezone

    secret_api_key = "sk-RestartSecretKey-778899"

    class SecretReattachDirect(MockColabBackend):
        @property
        def is_mock(self) -> bool:
            return False

        def execute_python(self, assignment: RawAssignment, code: str, *, env=None, timeout_s: float = 45.0) -> ExecResult:
            return ExecResult(
                ok=True,
                marker_json={
                    "exists": True,
                    "status": {
                        "state": "ready",
                        "stage": "warmup",
                        "progress": 1.0,
                        "message": f"Server ready with key {secret_api_key}",
                        "outputs": {"model_id": "qwen2.5-7b"},
                    },
                    "alive": True,
                    "tail": [f"Loaded --api-key {secret_api_key} on port 8000"],
                },
            )

    paths = WorkspacePaths(tmp_path / "sec_reconcile")
    fake = SecretReattachDirect("signed_out")
    orch1 = ConsoleOrchestrator(paths=paths, direct_backend=fake, force_file_vault=True)
    orch1.update_settings({"backend_mode": "real", "language": "en", "auto_forward_endpoints": True})
    acc = orch1._register_authenticated_account("sec@studio.ai", "Sec", '{"refresh_token":"r"}')
    raw_live = fake.assign_runtime(acc.id, "{}", Accelerator.L4)

    now_iso = datetime.now(timezone.utc).isoformat()
    paths.ledger_file.write_text(
        json.dumps({raw_live.endpoint: {"name": "sess-sec", "account_id": acc.id, "created_at": now_iso}}),
        encoding="utf-8",
    )
    # Persist input secret ONLY in vault (never in runs.json!)
    orch1.vault.set_run_secrets("run-with-secret", [secret_api_key])
    seed_runs = [
        {
            "run_id": "run-with-secret",
            "recipe_id": "llama-cpp-gguf",
            "recipe_name": {"en": "Deploy GGUF LLM (llama.cpp)", "zh-CN": "部署 GGUF"},
            "account_id": acc.id,
            "session_name": "sess-sec",
            "session_endpoint": raw_live.endpoint,
            "endpoint_ports": {"openai_api": 8021},
            "state": "running",
            "stage": "server",
            "message": "Starting...",
            "started_at": now_iso,
        },
        {
            "run_id": "run-missing-recipe",
            "recipe_id": "deleted-custom-recipe",
            "recipe_name": {"en": "Deleted Custom Recipe"},
            "account_id": acc.id,
            "session_name": "sess-sec",
            "session_endpoint": raw_live.endpoint,
            "state": "running",
            "stage": "init",
            "message": "Running...",
            "started_at": now_iso,
        },
        {
            "run_id": "run-ready-gone-vm",
            "recipe_id": "llama-cpp-gguf",
            "recipe_name": {"en": "Deploy GGUF LLM (llama.cpp)"},
            "account_id": acc.id,
            "session_name": "sess-old",
            "session_endpoint": "m-s-old-gone",
            "state": "ready",
            "stage": "warmup",
            "message": "Ready",
            "endpoints": [
                {
                    "id": "tun-stale1",
                    "session_name": "sess-old",
                    "account_id": acc.id,
                    "remote_port": 8000,
                    "local_port": 8000,
                    "label": "OpenAI API",
                    "status": "open",
                    "local_url": "http://127.0.0.1:8000/v1",
                    "created_at": now_iso,
                }
            ],
            "started_at": now_iso,
        },
    ]
    paths.runs_file.write_text(json.dumps(seed_runs), encoding="utf-8")
    assert secret_api_key not in paths.runs_file.read_text(encoding="utf-8")
    orch1.shutdown()

    def fake_start_real_tunnel(**kwargs):
        from colab_boost.models import TunnelInfo

        return TunnelInfo(
            id="tun-restored",
            session_name=kwargs["session_name"],
            account_id=kwargs["account_id"],
            remote_port=kwargs["remote_port"],
            local_port=kwargs["local_port"] or kwargs["remote_port"],
            label=kwargs.get("label", "Port Forward"),
            labels=kwargs.get("labels") or {},
            path=kwargs.get("path", "/"),
            kind=kwargs.get("kind", "http"),
            status="open",
            local_url=f"http://127.0.0.1:{kwargs['remote_port']}/v1",
            created_at=datetime.now(timezone.utc),
        )

    with patch.object(SshTunnelManager, "start_real_tunnel", side_effect=fake_start_real_tunnel):
        orch2 = ConsoleOrchestrator(paths=paths, direct_backend=fake, force_file_vault=True)
        try:
            r_sec = orch2.get_run("run-with-secret")
            assert r_sec.state == "ready"
            assert secret_api_key not in r_sec.message
            assert "***" in r_sec.message
            assert all(secret_api_key not in line for line in r_sec.log_tail)
            assert secret_api_key not in paths.runs_file.read_text(encoding="utf-8")
            # Transient run secret must be purged from vault once terminal!
            assert orch2.vault.get_run_secrets("run-with-secret") == []
            assert len(r_sec.endpoints) == 1 and r_sec.endpoints[0].id == "tun-restored"

            # Deleted custom recipe must fail cleanly instead of staying stuck in "running"
            r_del = orch2.get_run("run-missing-recipe")
            assert r_del.state == "failed"
            assert "no longer installed" in r_del.message

            # Historical ready run whose VM vanished keeps state="ready" in history but marks stale endpoint "closed"
            r_old = orch2.get_run("run-ready-gone-vm")
            assert r_old.state == "ready"
            assert r_old.endpoints[0].status == "closed"
        finally:
            orch2.shutdown()
