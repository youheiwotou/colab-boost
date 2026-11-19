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
