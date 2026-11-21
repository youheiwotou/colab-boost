"""HTTP endpoint coverage tests for server.py and human-readable / error CLI tests for cli.py."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
import pytest

from colab_boost.backends.base import ColabBackendError
from colab_boost.cli import _is_loopback_host, _parse_kv_pairs, run_client_subcommand
from colab_boost.orchestrator import ConsoleOrchestrator
from colab_boost.recipe_loader import RecipeValidationError
import colab_boost.server as server_mod
from colab_boost.server import (
    _extract_bearer_token,
    _extract_hostname_from_host,
    _is_allowed_origin,
    _map_domain_error,
    create_app,
)


def test_health_accounts_refresh_switch_delete_and_import_cli(orchestrator: ConsoleOrchestrator) -> None:
    app = create_app(orchestrator)
    with TestClient(app) as client:
        # 1. GET /api/health
        h = client.get("/api/health")
        assert h.status_code == 200
        hdata = h.json()
        assert hdata == {"status": "ok", "backend_mode": "mock", "is_mock": True}

        # 2. POST /api/accounts/refresh
        ref = client.post("/api/accounts/refresh")
        assert ref.status_code == 200
        accounts = ref.json()
        assert len(accounts) == 2
        active = next(a for a in accounts if a["is_active"])
        inactive = next(a for a in accounts if not a["is_active"])
        assert active["usage"]["status"] == "live"

        # 3. POST /api/accounts/{id}/switch -> switches active account & closes previous account's tunnels
        sess_name = client.get("/api/sessions").json()[0]["name"]
        tun = client.post("/api/tunnels", json={"session_name": sess_name, "remote_port": 8000})
        assert tun.status_code == 200
        sw = client.post(f"/api/accounts/{inactive['id']}/switch")
        assert sw.status_code == 200
        sw_body = sw.json()
        assert sw_body["active_account_id"] == inactive["id"]
        assert sw_body["closed_tunnels"] >= 1

        # Unknown account switch -> 404
        sw_missing = client.post("/api/accounts/acc-does-not-exist/switch")
        assert sw_missing.status_code == 404
        assert sw_missing.json()["detail"]["kind"] == "not_found"

        # 4. DELETE /api/accounts/{id}
        deleted = client.delete(f"/api/accounts/{inactive['id']}")
        assert deleted.status_code == 200
        assert deleted.json() == {"removed": inactive["id"]}

        # Deleting again -> 404
        del_missing = client.delete(f"/api/accounts/{inactive['id']}")
        assert del_missing.status_code == 404
        assert del_missing.json()["detail"]["kind"] == "not_found"

        # 5. POST /api/accounts/import-cli in mock mode -> 400 mock_mode
        imp = client.post("/api/accounts/import-cli")
        assert imp.status_code == 400
        assert imp.json()["detail"]["kind"] == "mock_mode"


def test_provision_cancel_guards_evaluate_extend_and_tunnel_delete(
    orchestrator: ConsoleOrchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(orchestrator)
    with TestClient(app) as client:
        # 1. POST /api/sessions?async_mode=true + POST /api/provision/{op_id}/cancel
        entered_assign = threading.Event()
        release_assign = threading.Event()
        orig_assign = orchestrator._mock_backend.assign_runtime

        def slow_assign(*args: Any, **kwargs: Any):
            entered_assign.set()
            release_assign.wait(timeout=5.0)
            return orig_assign(*args, **kwargs)

        monkeypatch.setattr(orchestrator._mock_backend, "assign_runtime", slow_assign)

        r_async = client.post("/api/sessions?async_mode=true", json={"name": "cancel-me-vm", "accelerator": "T4"})
        assert r_async.status_code == 200
        op_id = r_async.json()["op_id"]
        assert entered_assign.wait(timeout=5.0)

        r_cancel = client.post(f"/api/provision/{op_id}/cancel")
        assert r_cancel.status_code == 200
        assert r_cancel.json() == {"cancelled": op_id}
        release_assign.set()

        # Wait deterministically for provision thread to finalize cancelled state
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ops = {op.op_id: op for op in orchestrator.list_provision_ops()}
            if ops[op_id].state == "cancelled":
                break
            time.sleep(0.02)
        assert orchestrator._provision_ops[op_id].state == "cancelled"

        # Unknown / inactive op_id -> 404
        r_cancel_404 = client.post("/api/provision/prov-unknown/cancel")
        assert r_cancel_404.status_code == 404

        # 2. POST /api/sessions/{session_name}/extend (valid + unknown)
        sess = next(s for s in client.get("/api/sessions").json() if s["managed"])
        ext_ok = client.post(f"/api/sessions/{sess['name']}/extend", json={"add_minutes": 60})
        assert ext_ok.status_code == 200
        ext_404 = client.post("/api/sessions/no-such-session/extend", json={"add_minutes": 30})
        assert ext_404.status_code == 404

        # 3. POST /api/tunnels + DELETE /api/tunnels/{id} (valid + unknown)
        tun = client.post("/api/tunnels", json={"session_name": sess["name"], "remote_port": 8188})
        assert tun.status_code == 200
        tid = tun.json()["id"]

        del_tun = client.delete(f"/api/tunnels/{tid}")
        assert del_tun.status_code == 200
        del_body = del_tun.json()
        assert del_body["closed"] == tid
        assert "billing" in del_body["billing_notice"].lower()

        del_tun_404 = client.delete(f"/api/tunnels/{tid}")
        assert del_tun_404.status_code == 404

        # 4. POST /api/guards/evaluate (idle -> 0, then raise balance_floor_ccu above live balance -> triggers stop)
        g0 = client.post("/api/guards/evaluate")
        assert g0.status_code == 200
        assert g0.json()["triggered_count"] == 0

        patch_g = client.patch("/api/settings", json={"guards": {"balance_floor_ccu": 500.0}})
        assert patch_g.status_code == 200
        g1 = client.post("/api/guards/evaluate")
        assert g1.status_code == 200
        g1_body = g1.json()
        assert g1_body["triggered_count"] >= 1
        assert len(g1_body["receipts"]) == g1_body["triggered_count"]


def test_secrets_endpoints_deployment_cancel_and_hf_search_plan(
    orchestrator: ConsoleOrchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(orchestrator)
    with TestClient(app) as client:
        # 1. PUT /api/secrets/{slot} & DELETE /api/secrets/{slot}
        put_hf = client.put("/api/secrets/hf_token", json={"value": "hf_endpoint_secret_abc"})
        assert put_hf.status_code == 200
        assert put_hf.json() == {"secrets": {"civitai_token": False, "hf_token": True}}
        assert "hf_endpoint_secret_abc" not in put_hf.text

        del_hf = client.delete("/api/secrets/hf_token")
        assert del_hf.status_code == 200
        assert del_hf.json() == {"secrets": {"civitai_token": False, "hf_token": False}}

        # Invalid secret slot -> 400 invalid_input
        bad_put = client.put("/api/secrets/invalid_slot", json={"value": "abc1234"})
        assert bad_put.status_code == 400
        assert bad_put.json()["detail"]["kind"] == "invalid_input"

        bad_del = client.delete("/api/secrets/invalid_slot")
        assert bad_del.status_code == 400
        assert bad_del.json()["detail"]["kind"] == "invalid_input"

        # 2. GET /api/hf/search: mocked upstream success AND error fallback
        async def fake_search_ok(q: str, *, hf_token=None, limit: int = 10):
            return [{"id": f"org/{q or 'default'}-GGUF", "downloads": 999, "likes": 12, "pipeline_tag": "text-generation"}]

        monkeypatch.setattr(server_mod, "search_hf_gguf_repos", fake_search_ok)
        s_ok = client.get("/api/hf/search", params={"q": "Qwen3"})
        assert s_ok.status_code == 200
        assert s_ok.json()["source"] == "huggingface"
        assert s_ok.json()["items"][0]["id"] == "org/Qwen3-GGUF"

        async def fake_search_err(_q: str, *, hf_token=None, limit: int = 10):
            raise RuntimeError("HF 429 rate limited")

        monkeypatch.setattr(server_mod, "search_hf_gguf_repos", fake_search_err)
        s_fb = client.get("/api/hf/search", params={"q": "qwen3"})
        assert s_fb.status_code == 200
        assert s_fb.json()["source"] == "curated_fallback"
        assert any("Qwen3" in item["id"] for item in s_fb.json()["items"])

        # 3. GET /api/hf/gguf-plan: invalid repo -> 400, upstream failure curated fallback -> 200, unknown -> 502
        bad_repo = client.get("/api/hf/gguf-plan", params={"repo": "../bad repo!!"})
        assert bad_repo.status_code == 400

        async def fake_inspect_err(_repo_id: str, *, hf_token=None):
            raise RuntimeError("HF offline")

        monkeypatch.setattr(server_mod, "inspect_hf_gguf_repo", fake_inspect_err)
        curated_plan = client.get("/api/hf/gguf-plan", params={"repo": "Qwen/Qwen2.5-7B-Instruct-GGUF"})
        assert curated_plan.status_code == 200
        assert curated_plan.json()["source"] == "curated_fallback"

        unknown_plan = client.get("/api/hf/gguf-plan", params={"repo": "some-user/nonexistent-model-gguf"})
        assert unknown_plan.status_code == 502
        assert unknown_plan.json()["detail"]["kind"] == "hf_unreachable"

        # 4. POST /api/deployments/{run_id}/cancel (active run -> cancelled, unknown -> 404)
        orchestrator.mock_step_delay_s = 1.5
        started = client.post(
            "/api/deployments",
            json={"recipe_id": "faster-whisper-api", "provision": {"accelerator": "T4"}},
        )
        assert started.status_code == 200
        run_id = started.json()["run_id"]

        cancelled = client.post(f"/api/deployments/{run_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"

        cancel_404 = client.post("/api/deployments/run-missing/cancel")
        assert cancel_404.status_code == 404

        get_404 = client.get("/api/deployments/run-missing")
        assert get_404.status_code == 404

        sync_404 = client.post("/api/deployments/run-missing/keep/sync")
        assert sync_404.status_code == 404

        dl_404 = client.get("/api/deployments/run-missing/keep/download")
        assert dl_404.status_code == 404

        client.post("/api/sessions/stop-all")


def test_server_helper_edge_cases_and_domain_error_mapping() -> None:
    # _extract_hostname_from_host edge cases (IPv6, invalid ports, forbidden chars)
    assert _extract_hostname_from_host("[::1]:8765") == "::1"
    assert _extract_hostname_from_host("[::1]") == "::1"
    assert _extract_hostname_from_host("[::1") is None
    assert _extract_hostname_from_host("[::1]:notport") is None
    assert _extract_hostname_from_host("127.0.0.1:abc") is None
    assert _extract_hostname_from_host("bad host") is None
    assert _extract_hostname_from_host("user@127.0.0.1") is None
    assert _extract_hostname_from_host("") is None

    # _is_allowed_origin & _extract_bearer_token edge cases
    allowed = {"127.0.0.1", "localhost"}
    assert _is_allowed_origin("null", allowed) is False
    assert _is_allowed_origin("ftp://127.0.0.1", allowed) is False
    assert _is_allowed_origin("http://127.0.0.1:8765", allowed) is True
    assert _extract_bearer_token("Basic abc") is None
    assert _extract_bearer_token("Bearer   ") is None
    assert _extract_bearer_token("Bearer tok-123") == "tok-123"

    # _map_domain_error exhaustive status code mapping
    assert _map_domain_error(RecipeValidationError("port", "bad port")).status_code == 422
    assert _map_domain_error(ColabBackendError("auth_error", "expired")).status_code == 401
    assert _map_domain_error(ColabBackendError("missing_credential", "missing")).status_code == 401
    assert _map_domain_error(ColabBackendError("not_entitled", "no H100")).status_code == 403
    assert _map_domain_error(ColabBackendError("runtime_lost", "gone")).status_code == 404
    assert _map_domain_error(ColabBackendError("session_missing", "gone")).status_code == 404
    assert _map_domain_error(ColabBackendError("capacity_412", "busy")).status_code == 409
    assert _map_domain_error(ColabBackendError("watchdog_failed", "wd")).status_code == 502
    assert _map_domain_error(ColabBackendError("other_kind", "bad")).status_code == 400
    assert _map_domain_error(KeyError("missing")).status_code == 404
    assert _map_domain_error(ValueError("bad")).status_code == 400
    assert _map_domain_error(RuntimeError("boom")).status_code == 500


def test_cli_human_readable_outputs_exec_shell_stop_and_http_errors(
    orchestrator: ConsoleOrchestrator, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(orchestrator)
    with TestClient(app) as client:
        # 1. Human-readable (non --json) `recipes`
        assert run_client_subcommand(["recipes"], client=client) == 0
        rec_out = capsys.readouterr().out
        assert "minimax-h3-studio" in rec_out
        assert "llama-cpp-gguf" in rec_out

        # 2. Human-readable `deploy --wait` (shows Run ID, state, Endpoint)
        rc_dep = run_client_subcommand(
            [
                "deploy",
                "faster-whisper-api",
                "--accel",
                "T4",
                "--input",
                "port=8091",
                "--wait",
                "--poll-interval",
                "0.05",
            ],
            client=client,
        )
        assert rc_dep == 0
        dep_out = capsys.readouterr().out
        assert "Run " in dep_out
        assert "[ready]" in dep_out
        assert "Endpoint:" in dep_out

        # Extract deployed session name
        sess_name = orchestrator.list_runs()[-1].session_name

        # 3. Human-readable `status` (includes Mode, Account, Sessions, Tunnels)
        assert run_client_subcommand(["status"], client=client) == 0
        st_out = capsys.readouterr().out
        assert "Mode     : mock" in st_out
        assert "Account  :" in st_out
        assert "Sessions :" in st_out
        assert "Tunnels  :" in st_out
        assert sess_name in st_out

        # 4. `exec --shell` both --json and human-readable stdout/stderr (ok=True -> 0, ok=False -> 1)
        assert run_client_subcommand(["exec", sess_name, "echo hello-shell", "--shell", "--json"], client=client) == 0
        ex_json = json.loads(capsys.readouterr().out)
        assert ex_json["ok"] is True

        assert run_client_subcommand(["exec", sess_name, "echo hi-plain", "--shell"], client=client) == 0
        captured = capsys.readouterr()
        assert captured.out  # Mock shell prints stdout line

        # Simulate exec returning ok=False with stderr -> exit code 1 & writes to stderr
        with patch.object(
            orchestrator,
            "exec_on_session",
            return_value={"ok": False, "stdout": "partial\n", "stderr": "boom stderr\n", "returncode": 1},
        ):
            assert run_client_subcommand(["exec", sess_name, "false", "--shell"], client=client) == 1
            captured_err = capsys.readouterr()
            assert "partial" in captured_err.out
            assert "boom stderr" in captured_err.err

        # 5. Human-readable `upload` and `download`
        src = tmp_path / "cli_plain.bin"
        src.write_bytes(b"hello-cli-transfer")
        assert run_client_subcommand(["upload", sess_name, str(src), "/content/cli_plain.bin"], client=client) == 0
        up_out = capsys.readouterr().out
        assert "Uploaded 18 bytes" in up_out
        assert "sha256=" in up_out

        dst = tmp_path / "cli_plain_dl.bin"
        assert run_client_subcommand(["download", sess_name, "/content/cli_plain.bin", str(dst)], client=client) == 0
        dl_out = capsys.readouterr().out
        assert "Downloaded 18 bytes" in dl_out
        assert dst.read_bytes() == b"hello-cli-transfer"

        # 6. Human-readable `stop` (verified=True -> exit 0)
        assert run_client_subcommand(["stop", sess_name], client=client) == 0
        stop_out = capsys.readouterr().out
        assert f"Stopped {sess_name}" in stop_out
        assert "verified=True" in stop_out

        # 7. Human-readable `stop-all` (zero_burn_verified=True -> exit 0)
        assert run_client_subcommand(["stop-all"], client=client) == 0
        sa_out = capsys.readouterr().out
        assert "zero_burn_verified=True" in sa_out

        # 8. HTTP 4xx failure (404 unknown session) -> non-zero exit code 1
        assert run_client_subcommand(["stop", "nonexistent-session-xyz"], client=client) == 1
        err_404 = capsys.readouterr().err
        assert "HTTP 404" in err_404

        # 9. HTTP 5xx failure (including non-JSON body fallback in _api_call) -> non-zero exit code 1
        fake_500 = httpx.Response(500, text="Internal Server Exploded")
        with patch.object(client, "request", return_value=fake_500):
            assert run_client_subcommand(["status"], client=client) == 1
            err_500 = capsys.readouterr().err
            assert "HTTP 500" in err_500
            assert "Internal Server Exploded" in err_500

        # 10. Invalid --wait-timeout <= 0 -> exit code 1
        assert run_client_subcommand(["deploy", "faster-whisper-api", "--wait", "--wait-timeout", "0"], client=client) == 1
        assert "--wait-timeout" in capsys.readouterr().err


def test_cli_helper_parsers_and_loopback_checks() -> None:
    assert _parse_kv_pairs(["flag=true", "off=FALSE", "port=8080", "name=hello"]) == {
        "flag": True,
        "off": False,
        "port": 8080,
        "name": "hello",
    }
    with pytest.raises(ValueError, match="KEY=VALUE"):
        _parse_kv_pairs(["no-equals"])
    with pytest.raises(ValueError, match="Empty key"):
        _parse_kv_pairs(["=value"])

    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("[::1]") is True
    assert _is_loopback_host("0.0.0.0") is False
    assert _is_loopback_host("not-an-ip-host") is False
