"""Tests for Agent direct execution, chunked file transfer APIs, and CLI subcommands."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from fastapi.testclient import TestClient
import pytest

from colab_boost.backends.base import RESULT_MARKER, ColabBackend, ExecResult, RawAssignment
from colab_boost.cli import run_client_subcommand
from colab_boost.models import Accelerator, BackendMode, MachineShape, UsageSnapshot
from colab_boost.orchestrator import ConsoleOrchestrator
from colab_boost.server import create_app


class _RealExecFileStubBackend(ColabBackend):
    """Executes real Python snippets against an isolated local directory to test real-mode exec/upload/download."""

    def __init__(self, vm_root: Path) -> None:
        self.vm_root = vm_root
        self.vm_root.mkdir(parents=True, exist_ok=True)
        self.assignment = RawAssignment(
            endpoint="m-s-real-agent-01",
            accelerator=Accelerator.G4,
            machine_shape=MachineShape.HIGH_RAM,
            proxy_url="https://m-s-real-agent-01.prod.colab.stub",
            proxy_token="tok-real-agent",
        )

    @property
    def is_mock(self) -> bool:
        return False

    def begin_oauth_login(self) -> dict[str, str]:
        return {"flow_id": "f", "auth_url": "https://example.invalid"}

    def complete_oauth_login(self, flow_id: str, auth_code: str) -> dict[str, str]:
        return {"email": "agent@example.com", "display_name": "Agent", "token_json": "{}"}

    def inspect_token(self, token_json: str) -> dict[str, str]:
        return {"email": "agent@example.com", "display_name": "Agent", "token_json": token_json}

    def fetch_usage(self, account_id: str, token_json: str) -> UsageSnapshot:
        return UsageSnapshot(status="live", balance_ccu=150.0, burn_rate_hourly=1.8, assignments_count=1)

    def list_assignments(self, account_id: str, token_json: str) -> list[RawAssignment]:
        return [self.assignment]

    def assign_runtime(self, account_id: str, token_json: str, accelerator: Accelerator, high_mem: bool = False) -> RawAssignment:
        return self.assignment

    def unassign_runtime(self, account_id: str, token_json: str, endpoint: str) -> bool:
        return True

    def execute_python(
        self,
        assignment: RawAssignment,
        code: str,
        *,
        env: Optional[dict[str, str]] = None,
        timeout_s: float = 45.0,
    ) -> ExecResult:
        import contextlib
        import io

        # Remap '/content' in test snippets to isolated self.vm_root so it executes safely on Windows!
        mapped_code = code.replace("/content/", self.vm_root.as_posix().rstrip("/") + "/")
        buf = io.StringIO()
        ns: dict[str, Any] = {}
        try:
            with contextlib.redirect_stdout(buf):
                exec(compile(mapped_code, "<stub-vm>", "exec"), ns)
        except Exception as exc:
            return ExecResult(ok=False, stdout=buf.getvalue(), stderr=str(exc), error_name=type(exc).__name__, error_value=str(exc))

        out = buf.getvalue()
        marker = None
        for line in reversed(out.splitlines()):
            if line.startswith(RESULT_MARKER):
                marker = json.loads(line[len(RESULT_MARKER) :])
                break
        return ExecResult(ok=True, stdout=out, marker_json=marker)


def test_agent_exec_upload_download_and_cli_subcommands_e2e(orchestrator: ConsoleOrchestrator, tmp_path: Path, capsys) -> None:
    app = create_app(orchestrator)
    with TestClient(app) as client:
        # 1. CLI status & recipes
        assert run_client_subcommand(["status", "--json"], client=client) == 0
        st = json.loads(capsys.readouterr().out)
        assert st["settings"]["backend_mode"] == "mock"
        assert len(st["recipes"]) == 5

        assert run_client_subcommand(["recipes", "--json"], client=client) == 0
        recs = json.loads(capsys.readouterr().out)
        assert any(r["id"] == "minimax-h3-studio" for r in recs)

        # 2. Deploy minimax-h3-studio (G4 >= 80000 MiB) via CLI --wait
        rc = run_client_subcommand(
            [
                "deploy",
                "minimax-h3-studio",
                "--accel",
                "G4",
                "--min-vram",
                "80000",
                "--input",
                "download_audio_vae=true",
                "--wait",
                "--poll-interval",
                "0.1",
                "--json",
            ],
            client=client,
        )
        assert rc == 0
        dep = json.loads(capsys.readouterr().out)
        assert dep["state"] == "ready"
        sess_name = dep["session_name"]
        assert len(dep["endpoints"]) == 1

        # 3. Upload local file via CLI & download back with SHA256 verification
        src_file = tmp_path / "shot_bundle.bin"
        payload = b"MiniMax-H3-AudioVAE-Test-" + bytes(range(256)) * 64
        src_file.write_bytes(payload)
        expected_sha = hashlib.sha256(payload).hexdigest()

        assert run_client_subcommand(["upload", sess_name, str(src_file), "/content/shot_bundle.bin", "--json"], client=client) == 0
        up_res = json.loads(capsys.readouterr().out)
        assert up_res["bytes"] == len(payload)
        assert up_res["sha256"] == expected_sha

        dst_file = tmp_path / "downloaded" / "shot_bundle.bin"
        assert run_client_subcommand(["download", sess_name, "/content/shot_bundle.bin", str(dst_file), "--json"], client=client) == 0
        dl_res = json.loads(capsys.readouterr().out)
        assert dl_res["sha256"] == expected_sha
        assert dst_file.read_bytes() == payload

        # 4. Inline base64 upload/download & path traversal rejection
        bad_up = client.post(
            f"/api/sessions/{sess_name}/upload",
            json={"remote_path": "/content/../etc/passwd", "content_b64": base64.b64encode(b"x").decode()},
        )
        assert bad_up.status_code == 400

        # 5. Exec via CLI + secret redaction check
        orchestrator.vault.set_secret("hf_token", "hf_super_secret_agent_999")
        assert run_client_subcommand(["exec", sess_name, "print('ok')", "--json"], client=client) == 0
        ex_res = json.loads(capsys.readouterr().out)
        assert ex_res["ok"] is True

        # 6. Stop all via CLI & verify zero burn
        assert run_client_subcommand(["stop-all", "--json"], client=client) == 0
        sa = json.loads(capsys.readouterr().out)
        assert sa["zero_burn_verified"] is True
        assert sa["remaining_sessions"] == 0


def test_real_mode_chunked_upload_download_and_secret_redacted_exec(isolated_paths, tmp_path: Path) -> None:
    vm_root = tmp_path / "fake_vm_content"
    stub = _RealExecFileStubBackend(vm_root)
    orch = ConsoleOrchestrator(paths=isolated_paths, direct_backend=stub, force_file_vault=True)
    orch.update_settings({"backend_mode": BackendMode.REAL})
    orch._register_authenticated_account("agent@example.com", "Agent", "{}")
    orch._real_ledger[stub.assignment.endpoint] = {
        "name": "g4-h3-live",
        "account_id": orch.active_account().id,
        "created_at": "2026-11-19T00:00:00+00:00",
    }
    orch.vault.set_secret("hf_token", "hf_secret_token_do_not_leak")

    # 1. Multi-chunk real-mode upload (force 3 chunks via chunk_bytes=1024)
    src = tmp_path / "payload.bin"
    raw = b"H3_REAL_CHUNK_DATA_" * 140  # ~2660 bytes -> 3 chunks
    src.write_bytes(raw)
    up = orch.upload_to_session("g4-h3-live", "/content/sub/payload.bin", local_path=str(src), chunk_bytes=1024)
    assert up["chunks"] == 3
    assert up["bytes"] == len(raw)
    assert (vm_root / "sub" / "payload.bin").read_bytes() == raw

    # 2. Multi-chunk real-mode download with SHA256 check
    dst = tmp_path / "out" / "payload.bin"
    dl = orch.download_from_session("g4-h3-live", "/content/sub/payload.bin", local_path=str(dst), chunk_bytes=1024)
    assert dl["chunks"] == 3
    assert dl["sha256"] == up["sha256"]
    assert dst.read_bytes() == raw

    # 3. Missing remote file raises KeyError (mapped to HTTP 404)
    with pytest.raises(KeyError):
        orch.download_from_session("g4-h3-live", "/content/does_not_exist.bin", local_path=str(dst))

    # 4. Exec redacts vault secrets in stdout
    ex = orch.exec_on_session("g4-h3-live", "print('leak: hf_secret_token_do_not_leak')")
    assert ex["ok"] is True
    assert "hf_secret_token_do_not_leak" not in ex["stdout"]
    assert "***" in ex["stdout"]


def test_local_path_security_boundary_blocks_sensitive_and_honors_whitelist(
    orchestrator: ConsoleOrchestrator, tmp_path: Path, monkeypatch
) -> None:
    import os

    sess_name = orchestrator.list_sessions()[0].name

    # Seed app-internal sensitive files to prove both read (upload) and write (download) are blocked
    orchestrator.paths.vault_fallback_file.write_text('{"secret":"top"}', encoding="utf-8")
    orchestrator.paths.accounts_file.write_text("[]", encoding="utf-8")
    orchestrator.paths.settings_file.write_text("{}", encoding="utf-8")
    keys_dir = orchestrator.paths.home / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    key_file = keys_dir / "id_ed25519"
    key_file.write_text("PRIVATE KEY", encoding="utf-8")

    # Seed a mock remote file for download boundary checks
    safe_src = tmp_path / "safe.bin"
    safe_src.write_bytes(b"ok-bytes")
    orchestrator.upload_to_session(sess_name, "/content/safe.bin", local_path=str(safe_src))

    protected_paths = [
        str(orchestrator.paths.vault_fallback_file),
        str(orchestrator.paths.accounts_file),
        str(orchestrator.paths.settings_file),
        str(key_file),
        str(orchestrator.paths.ssh_dir / "id_ed25519"),
        str(Path.home() / ".ssh" / "id_rsa"),
        str(Path.home() / ".aws" / "credentials"),
        str(Path.home() / ".gnupg" / "secring.gpg"),
        str(Path.home() / ".config" / "gcloud" / "credentials.db"),
        str(Path.home() / ".gemini" / "settings.json"),
        "/etc/passwd",
        "/proc/self/environ",
        "/sys/kernel/notes",
    ]
    if os.name == "nt":
        protected_paths.append(r"C:\Windows\System32\drivers\etc\hosts")

    for bad_path in protected_paths:
        with pytest.raises(ValueError, match="forbidden"):
            orchestrator.upload_to_session(sess_name, "/content/exfil.bin", local_path=bad_path)
        with pytest.raises(ValueError, match="forbidden"):
            orchestrator.download_from_session(sess_name, "/content/safe.bin", local_path=bad_path)

    # Symlink escaping into protected vault file must also be blocked (when OS permits symlinks)
    link_path = tmp_path / "innocent_link.json"
    try:
        os.symlink(orchestrator.paths.vault_fallback_file, link_path)
    except OSError:
        link_path = None
    if link_path is not None:
        with pytest.raises(ValueError, match="forbidden"):
            orchestrator.upload_to_session(sess_name, "/content/exfil.bin", local_path=str(link_path))

    # Optional COLAB_BOOST_ALLOWED_LOCAL_ROOTS whitelist restricts paths to approved directories
    allowed_dir = tmp_path / "allowed"
    denied_dir = tmp_path / "denied"
    allowed_dir.mkdir()
    denied_dir.mkdir()
    ok_file = allowed_dir / "ok.txt"
    ok_file.write_bytes(b"allowed-data")
    outside_file = denied_dir / "outside.txt"
    outside_file.write_bytes(b"denied-data")

    monkeypatch.setenv("COLAB_BOOST_ALLOWED_LOCAL_ROOTS", str(allowed_dir))
    up_ok = orchestrator.upload_to_session(sess_name, "/content/ok.txt", local_path=str(ok_file))
    assert up_ok["bytes"] == len(b"allowed-data")
    with pytest.raises(ValueError, match="COLAB_BOOST_ALLOWED_LOCAL_ROOTS"):
        orchestrator.upload_to_session(sess_name, "/content/out.txt", local_path=str(outside_file))
    with pytest.raises(ValueError, match="COLAB_BOOST_ALLOWED_LOCAL_ROOTS"):
        orchestrator.download_from_session(sess_name, "/content/ok.txt", local_path=str(denied_dir / "out.txt"))


def test_cli_stop_exit_codes_bearer_token_and_non_loopback_host_guard(
    isolated_paths, monkeypatch, capsys
) -> None:
    from colab_boost.backends.mock import MockColabBackend
    from colab_boost.cli import main as cli_main

    # 1. Refusing non-loopback host without COLAB_BOOST_API_TOKEN
    monkeypatch.delenv("COLAB_BOOST_API_TOKEN", raising=False)
    assert cli_main(["--host", "0.0.0.0", "--mock"]) == 2
    assert "COLAB_BOOST_API_TOKEN" in capsys.readouterr().err

    # 2. CLI automatically sends COLAB_BOOST_API_TOKEN Bearer header & exits non-zero on unverified stop/stop-all
    class UnverifiedStopMock(MockColabBackend):
        def unassign_runtime(self, account_id: str, token_json: str, endpoint: str) -> bool:
            return False

    orch = ConsoleOrchestrator(
        paths=isolated_paths,
        mock_backend=UnverifiedStopMock(scenario="single"),
        force_file_vault=True,
    )
    sess = orch.provision_session(__import__("colab_boost.models", fromlist=["ProvisionRequest"]).ProvisionRequest(accelerator=Accelerator.T4))
    app = create_app(orch, api_token="cli-bearer-xyz")

    with TestClient(app) as client:
        # Without token -> CLI fails with exit code 1 (HTTP 401)
        assert run_client_subcommand(["status", "--json"], client=client) == 1
        capsys.readouterr()

        # With token -> status succeeds
        monkeypatch.setenv("COLAB_BOOST_API_TOKEN", "cli-bearer-xyz")
        assert run_client_subcommand(["status", "--json"], client=client) == 0
        capsys.readouterr()

        # Single stop when verified_unassigned=False -> exit code 2
        assert run_client_subcommand(["stop", sess.name, "--json"], client=client) == 2
        stop_out = json.loads(capsys.readouterr().out)
        assert stop_out["verified_unassigned"] is False

        # stop-all when zero_burn_verified=False -> exit code 2
        assert run_client_subcommand(["stop-all", "--json"], client=client) == 2
        sa_out = json.loads(capsys.readouterr().out)
        assert sa_out["zero_burn_verified"] is False
        assert len(sa_out["stop_errors"]) >= 1


def test_provision_request_name_validation_rejects_traversal_and_control_chars(orchestrator: ConsoleOrchestrator) -> None:
    from pydantic import ValidationError
    from colab_boost.models import ProvisionRequest

    invalid_names = [
        "../escape",
        "foo/bar",
        "foo\\bar",
        "a..b",
        "sess?x=1",
        "sess#frag",
        "my session",
        "   ",
        "bad\nname",
        "bad\x00name",
        ".",
    ]
    for bad in invalid_names:
        with pytest.raises(ValidationError):
            ProvisionRequest(name=bad)

    # Empty string normalizes to None; safe names pass
    assert ProvisionRequest(name="").name is None
    assert ProvisionRequest(name="boost-g4_h3.01").name == "boost-g4_h3.01"

    app = create_app(orchestrator)
    with TestClient(app) as client:
        resp = client.post("/api/sessions", json={"name": "../drive-escape", "accelerator": "T4"})
        assert resp.status_code == 422


def test_cli_deploy_partial_override_preserves_high_mem_and_wait_retries_and_timeout(
    orchestrator: ConsoleOrchestrator, capsys
) -> None:
    import httpx
    from unittest.mock import patch

    app = create_app(orchestrator)
    with TestClient(app) as client:
        orchestrator.stop_all_sessions()
        # 1. README example: --accel G4 --min-vram 80000 WITHOUT --high-mem must keep minimax-h3 high_mem=True!
        rc = run_client_subcommand(
            [
                "deploy",
                "minimax-h3-studio",
                "--accel",
                "G4",
                "--min-vram",
                "80000",
                "--wait",
                "--poll-interval",
                "0.05",
                "--json",
            ],
            client=client,
        )
        assert rc == 0
        dep = json.loads(capsys.readouterr().out)
        assert dep["state"] == "ready"
        sess = next(s for s in orchestrator.list_sessions() if s.name == dep["session_name"])
        assert sess.machine_shape == MachineShape.HIGH_RAM

        # 2. Explicit --no-high-mem overrides high_mem=False while preserving recipe's G4 accelerator
        orchestrator.stop_all_sessions()
        rc2 = run_client_subcommand(
            [
                "deploy",
                "minimax-h3-studio",
                "--no-high-mem",
                "--wait",
                "--poll-interval",
                "0.05",
                "--json",
            ],
            client=client,
        )
        assert rc2 == 0
        dep2 = json.loads(capsys.readouterr().out)
        sess2 = next(s for s in orchestrator.list_sessions() if s.name == dep2["session_name"])
        assert sess2.accelerator == Accelerator.G4
        assert sess2.machine_shape == MachineShape.STANDARD

        # 3. deploy --wait retries transient network error during polling and succeeds
        orchestrator.stop_all_sessions()
        orchestrator.mock_step_delay_s = 0.08
        orig_request = client.request
        flaky_gets = [0]

        def flaky_request(method: str, url: str, *args, **kwargs):
            if method.upper() == "GET" and str(url).startswith("/api/deployments/"):
                flaky_gets[0] += 1
                if flaky_gets[0] <= 2:
                    raise httpx.ConnectError("transient network reset")
            return orig_request(method, url, *args, **kwargs)

        with patch.object(client, "request", side_effect=flaky_request):
            rc3 = run_client_subcommand(
                [
                    "deploy",
                    "faster-whisper-api",
                    "--wait",
                    "--poll-interval",
                    "0.05",
                    "--wait-timeout",
                    "5.0",
                    "--json",
                ],
                client=client,
            )
        assert rc3 == 0
        assert flaky_gets[0] >= 3
        dep3 = json.loads(capsys.readouterr().out)
        assert dep3["state"] == "ready"

        # 4. deploy --wait --wait-timeout exits with error when run does not finish in time
        orchestrator.stop_all_sessions()
        orchestrator.mock_step_delay_s = 2.0
        rc4 = run_client_subcommand(
            [
                "deploy",
                "faster-whisper-api",
                "--wait",
                "--poll-interval",
                "0.05",
                "--wait-timeout",
                "0.12",
            ],
            client=client,
        )
        err_out = capsys.readouterr().err
        assert rc4 == 1
        assert "Timed out waiting" in err_out
        orchestrator.stop_all_sessions()


def test_streaming_upload_retry_token_refresh_chunk_validation_shell_timeout_and_marker_redaction(
    isolated_paths, tmp_path: Path
) -> None:
    from unittest.mock import patch

    vm_root = tmp_path / "stream_vm"
    list_calls = [0]
    chunk_attempts = [0]
    shell_snippets: list[str] = []

    class FlakyStreamBackend(_RealExecFileStubBackend):
        def list_assignments(self, account_id: str, token_json: str) -> list[RawAssignment]:
            list_calls[0] += 1
            return [
                RawAssignment(
                    endpoint=self.assignment.endpoint,
                    accelerator=self.assignment.accelerator,
                    machine_shape=self.assignment.machine_shape,
                    proxy_url=self.assignment.proxy_url,
                    proxy_token=f"tok-stream-{list_calls[0]}",
                    token_expires_in_seconds=60,
                )
            ]

        def execute_python(self, assignment: RawAssignment, code: str, *, env=None, timeout_s: float = 45.0) -> ExecResult:
            if "subprocess.run(['bash', '-lc'" in code:
                shell_snippets.append(code)
                return ExecResult(
                    ok=True,
                    marker_json={"returncode": 124, "stdout": "", "stderr": "Command timed out after 7s", "timed_out": True},
                )
            if "fh.seek(" in code and "fh.write(data)" in code:
                chunk_attempts[0] += 1
                if chunk_attempts[0] == 2:
                    # Simulate a transient chunk error on chunk 2 -> must retry & refresh proxy_token!
                    return ExecResult(ok=False, stderr="Transient websocket hiccup")
            return super().execute_python(assignment, code, env=env, timeout_s=timeout_s)

    stub = FlakyStreamBackend(vm_root)
    orch = ConsoleOrchestrator(paths=isolated_paths, direct_backend=stub, force_file_vault=True)
    try:
        orch.update_settings({"backend_mode": BackendMode.REAL})
        orch._register_authenticated_account("stream@example.com", "Stream", "{}")
        orch._real_ledger[stub.assignment.endpoint] = {
            "name": "g4-stream",
            "account_id": orch.active_account().id,
            "created_at": "2026-11-19T00:00:00+00:00",
        }
        orch.vault.set_secret("hf_token", "hf_marker_secret_5566")

        # 1. Invalid chunk_bytes <= 0 rejected
        src = tmp_path / "big_stream.bin"
        raw = b"STREAM_BLOCK_" * 250  # 3250 bytes -> 4 chunks at 1024
        src.write_bytes(raw)
        with pytest.raises(ValueError, match="chunk_bytes"):
            orch.upload_to_session("g4-stream", "/content/bad.bin", local_path=str(src), chunk_bytes=0)
        with pytest.raises(ValueError, match="chunk_bytes"):
            orch.upload_to_session("g4-stream", "/content/bad.bin", local_path=str(src), chunk_bytes=-10)

        # 2. Streaming upload MUST NOT call Path.read_bytes(), retries failed chunk, and matches SHA256
        with patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes must not be called for streaming upload")):
            with patch("colab_boost.orchestrator.time.sleep"):
                up = orch.upload_to_session("g4-stream", "/content/big_stream.bin", local_path=str(src), chunk_bytes=1024)
        assert up["chunks"] == 4
        assert up["bytes"] == len(raw)
        assert up["sha256"] == hashlib.sha256(raw).hexdigest()
        assert chunk_attempts[0] == 5  # 4 chunks + 1 retry
        assert list_calls[0] >= 3  # refreshed token during retry
        assert (vm_root / "big_stream.bin").read_bytes() == raw

        # 3. shell=True passes timeout to remote subprocess.run and surfaces rc=124 on timeout
        sh_res = orch.exec_on_session("g4-stream", "sleep 99", shell=True, timeout_s=7.0)
        assert sh_res["ok"] is False
        assert sh_res["returncode"] == 124
        assert any("timeout=7.0" in s and "TimeoutExpired" in s for s in shell_snippets)

        # 4. exec_on_session recursively redacts secrets inside marker_json
        ex = orch.exec_on_session(
            "g4-stream",
            f"import json; print({RESULT_MARKER!r} + json.dumps({{'nested': {{'tok': 'hf_marker_secret_5566'}}, 'arr': ['hf_marker_secret_5566']}}))",
        )
        assert ex["ok"] is True
        assert "hf_marker_secret_5566" not in json.dumps(ex["marker_json"])
        assert ex["marker_json"]["nested"]["tok"] == "***"
        assert ex["marker_json"]["arr"] == ["***"]
    finally:
        orch.shutdown()


def test_bootstrap_exposes_accelerator_specs_burn_rates_and_ws_ordered_broadcast(
    orchestrator: ConsoleOrchestrator,
) -> None:
    from colab_boost.models import APPROX_CCU_PER_HOUR

    app = create_app(orchestrator)
    with TestClient(app) as client:
        boot = client.get("/api/bootstrap").json()
        assert boot["approx_burn_rates"] == APPROX_CCU_PER_HOUR
        specs = boot["accelerator_specs"]
        spec_by_id = {item["id"]: item for item in specs}
        assert set(spec_by_id.keys()) == {"G4", "A100", "H100", "L4", "T4", "V5E1", "V6E1", "NONE"}
        assert spec_by_id["L4"]["high_mem_only"] is True
        assert spec_by_id["V5E1"]["high_mem_only"] is True
        assert spec_by_id["V6E1"]["high_mem_only"] is True
        assert spec_by_id["G4"]["vram_mib"] == [97887]
        assert spec_by_id["H100"]["approx_ccu_per_hour"] == APPROX_CCU_PER_HOUR["H100"]

        # Verify ordered per-connection WS delivery for rapid bursts of events
        with client.websocket_connect("/ws/events") as ws:
            first = ws.receive_json()
            assert first["type"] == "connected"
            for seq in range(20):
                orchestrator._emit("ordered_burst", {"seq": seq})
            received = [ws.receive_json()["data"]["seq"] for _ in range(20)]
            assert received == list(range(20))


def test_gguf_plan_propagates_header_warning_and_settings_patch_forbids_unknown_fields(
    isolated_paths,
) -> None:
    from unittest.mock import AsyncMock, patch
    from colab_boost.config import SettingsStore
    from colab_boost.gguf_planner import GgufRepoPlan, GgufVariant

    # 1. AppSettings remains backward-compatible with legacy settings.json containing extra keys
    isolated_paths.ensure_dirs()
    isolated_paths.settings_file.write_text(
        json.dumps({"language": "en", "legacy_removed_flag": True, "guards": {"auto_stop_minutes": 90, "old_guard": 7}}),
        encoding="utf-8",
    )
    loaded_settings = SettingsStore(isolated_paths).load()
    assert loaded_settings.language == "en"
    assert loaded_settings.guards.auto_stop_minutes == 90

    orch = ConsoleOrchestrator(paths=isolated_paths, force_file_vault=True)
    app = create_app(orch)
    try:
        with TestClient(app) as client:
            # 2. PATCH /api/settings accepts valid fields & rejects unknown top-level, nested guard, or null fields (422)
            ok_patch = client.patch(
                "/api/settings",
                json={"theme": "light", "guards": {"idle_stop_minutes": 45}},
            )
            assert ok_patch.status_code == 200
            assert ok_patch.json()["theme"] == "light"
            assert ok_patch.json()["guards"]["idle_stop_minutes"] == 45

            bad_top = client.patch("/api/settings", json={"unknown_setting": True})
            assert bad_top.status_code == 422

            bad_nested = client.patch("/api/settings", json={"guards": {"unknown_guard": 12}})
            assert bad_nested.status_code == 422

            bad_null = client.patch("/api/settings", json={"language": None})
            assert bad_null.status_code == 422

            # 3. /api/hf/gguf-plan surfaces header_truncated, header_warning, warnings & sliding_window at top level
            truncated_plan = GgufRepoPlan(
                repo="demo/BigHeader-GGUF",
                variants=[GgufVariant(quant="Q4_K_M", files=["model-Q4_K_M.gguf"], size_bytes=4 * 1024**3)],
                mmproj_files=[],
                preferred_mmproj=None,
                architecture="qwen2",
                model_ctx=32768,
                kv_bytes_per_token_f16=65536,
                has_mtp=False,
                sliding_window=8192,
                header_truncated=True,
                header_warning="GGUF metadata header was truncated at the 4 MiB probe limit.",
            )
            with patch(
                "colab_boost.server.inspect_hf_gguf_repo",
                new=AsyncMock(return_value=truncated_plan),
            ):
                resp = client.get("/api/hf/gguf-plan", params={"repo": "demo/BigHeader-GGUF"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["repo"] == "demo/BigHeader-GGUF"
            assert data["sliding_window"] == 8192
            assert data["header_truncated"] is True
            assert data["header_warning"] == truncated_plan.header_warning
            assert data["warnings"] == [truncated_plan.header_warning]
            assert len(data["hardware_matrix"]) >= 5
            assert all(row.get("header_truncated") is True for row in data["hardware_matrix"])
    finally:
        orch.shutdown()


def test_cli_help_lists_all_subcommands_and_tunnels_receipts_extend_subcommands(
    orchestrator: ConsoleOrchestrator, capsys
) -> None:
    from colab_boost.cli import CLIENT_SUBCOMMANDS, parse_args

    # 1. Main --help epilog clearly lists all 11 client subcommands
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    expected_cmds = {
        "status",
        "recipes",
        "deploy",
        "exec",
        "upload",
        "download",
        "stop",
        "stop-all",
        "tunnels",
        "receipts",
        "extend",
    }
    assert expected_cmds.issubset(CLIENT_SUBCOMMANDS)
    for cmd in expected_cmds:
        assert cmd in help_text

    # 2. Exercise tunnels, extend, and receipts in both text and --json modes
    app = create_app(orchestrator)
    with TestClient(app) as client:
        # Empty tunnels
        assert run_client_subcommand(["tunnels"], client=client) == 0
        assert "No active tunnels." in capsys.readouterr().out

        # Open a tunnel on existing demo session
        sess = orchestrator.list_sessions()[0]
        tun = orchestrator.open_tunnel(sess.name, remote_port=8000, local_port=8000, label="OpenAI API")

        assert run_client_subcommand(["tunnels"], client=client) == 0
        tun_text = capsys.readouterr().out
        assert tun.id in tun_text
        assert sess.name in tun_text

        assert run_client_subcommand(["tunnels", "--json"], client=client) == 0
        tun_json = json.loads(capsys.readouterr().out)
        assert any(t["id"] == tun.id for t in tun_json)

        # Extend session deadline
        prev_deadline = sess.watchdog.deadline_epoch or 0.0
        assert run_client_subcommand(["extend", sess.name, "--minutes", "45"], client=client) == 0
        ext_text = capsys.readouterr().out
        assert f"Extended {sess.name} (+45m)" in ext_text

        assert run_client_subcommand(["extend", sess.name, "--minutes", "30", "--json"], client=client) == 0
        ext_json = json.loads(capsys.readouterr().out)
        assert ext_json["name"] == sess.name
        assert ext_json["watchdog"]["active"] is True
        assert ext_json["watchdog"]["deadline_epoch"] > prev_deadline

        # Invalid --minutes rejected cleanly
        assert run_client_subcommand(["extend", sess.name, "--minutes", "0"], client=client) == 1
        assert "--minutes" in capsys.readouterr().err

        # Stop session to produce a receipt, then list via receipts subcommand
        assert run_client_subcommand(["stop", sess.name, "--json"], client=client) == 0
        stopped_rc = json.loads(capsys.readouterr().out)

        assert run_client_subcommand(["receipts"], client=client) == 0
        rc_text = capsys.readouterr().out
        assert stopped_rc["receipt_id"] in rc_text
        assert sess.name in rc_text

        assert run_client_subcommand(["receipts", "--limit", "1", "--json"], client=client) == 0
        rc_json = json.loads(capsys.readouterr().out)
        assert len(rc_json) == 1
        assert rc_json[0]["receipt_id"] == stopped_rc["receipt_id"]
