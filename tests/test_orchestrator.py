"""Deep behavioral tests for ConsoleOrchestrator: VRAM retry, cancel cleanup, cost guards & secret redaction."""

from __future__ import annotations

import threading
import time

from colab_boost.backends.base import ColabBackendError
from colab_boost.backends.mock import MockColabBackend
from colab_boost.config import WorkspacePaths
from colab_boost.models import Accelerator, DeployRequest, ProvisionRequest
from colab_boost.orchestrator import ConsoleOrchestrator


def test_a100_min_vram_retry_releases_40gb_and_keeps_80gb(orchestrator: ConsoleOrchestrator) -> None:
    # Stop demo sessions first so we start from a clean count
    orchestrator.stop_all_sessions()
    assert len(orchestrator.list_sessions()) == 0

    # Request A100 with min_vram_mib=70000: Mock allocates 40GB on attempt 1, then 80GB on attempt 2!
    sess = orchestrator.provision_session(
        ProvisionRequest(accelerator=Accelerator.A100, min_vram_mib=70000, high_mem=True)
    )
    assert sess.vram_total_mib == 81920
    assert "80GB" in (sess.gpu_name or "")

    # Only 1 session must remain active, and a vram_retry unassign receipt must exist for the 40GB attempt!
    live = orchestrator.list_sessions()
    assert len(live) == 1
    receipts = orchestrator.list_receipts()
    assert any("vram_retry" in r.reason and r.verified_unassigned for r in receipts)


def test_cancel_mid_allocation_leaves_zero_billed_vms(isolated_paths: WorkspacePaths) -> None:
    entered_assign = threading.Event()
    allow_finish = threading.Event()

    class SlowAssignMock(MockColabBackend):
        def assign_runtime(self, account_id: str, token_json: str, accelerator: Accelerator, high_mem: bool = False):
            entered_assign.set()
            allow_finish.wait(timeout=5.0)
            return super().assign_runtime(account_id, token_json, accelerator, high_mem=high_mem)

    orch = ConsoleOrchestrator(
        paths=isolated_paths,
        mock_backend=SlowAssignMock(scenario="single"),
        force_file_vault=True,
    )
    op = orch.start_provision_async(ProvisionRequest(accelerator=Accelerator.L4))
    assert entered_assign.wait(timeout=3.0)

    # Cancel while assign_runtime is in-flight!
    assert orch.cancel_provision(op.op_id) is True
    allow_finish.set()

    # Wait for thread to settle
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        cur = next(o for o in orch.list_provision_ops() if o.op_id == op.op_id)
        if cur.state == "cancelled":
            break
        time.sleep(0.05)

    assert orch.list_sessions() == []
    assert any(r.reason == "cancelled_cleanup" for r in orch.list_receipts())


def test_stale_usage_never_becomes_zero_and_balance_floor_guard_works(isolated_paths: WorkspacePaths) -> None:
    fail_usage = [False]

    class ToggleUsageMock(MockColabBackend):
        def fetch_usage(self, account_id: str, token_json: str):
            if fail_usage[0]:
                raise ColabBackendError("network", "Simulated outage")
            return super().fetch_usage(account_id, token_json)

    mock = ToggleUsageMock(scenario="demo")
    orch = ConsoleOrchestrator(paths=isolated_paths, mock_backend=mock, force_file_vault=True)

    # 1. Simulated outage marks usage "stale" while preserving non-zero balance, and does NOT trigger floor guard
    orch.update_settings({"guards": {"balance_floor_ccu": 250.0}})
    fail_usage[0] = True
    snap = orch.refresh_account_usage("acc-pro-main")
    assert snap.status == "stale"
    assert snap.balance_ccu is not None and snap.balance_ccu > 100.0
    assert orch.evaluate_cost_guards() == []
    assert len(orch.list_sessions()) == 2  # Managed A100 + Orphan T4 still untouched during stale outage

    # 2. Once usage recovers ("live") and balance (186.4 CCU) <= floor (250.0 CCU), guard stops ALL sessions!
    fail_usage[0] = False
    triggered = orch.evaluate_cost_guards()
    assert len(triggered) == 2
    assert orch.list_sessions() == []


def test_secrets_are_redacted_and_never_written_to_settings_or_accounts(orchestrator: ConsoleOrchestrator) -> None:
    secret_hf = "hf_TopSecretTokenValue12345"
    secret_api = "sk-PrivateModelKey98765"
    orchestrator.vault.set_secret("hf_token", secret_hf)

    run = orchestrator.start_deployment(
        DeployRequest(
            recipe_id="llama-cpp-gguf",
            inputs={
                "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                "api_key": secret_api,
            },
        ),
        run_sync=True,
    )
    assert run.state == "ready"
    dump_str = run.model_dump_json()
    assert secret_hf not in dump_str
    assert secret_api not in dump_str

    for p in (orchestrator.paths.settings_file, orchestrator.paths.accounts_file, orchestrator.paths.ledger_file):
        if p.exists():
            txt = p.read_text(encoding="utf-8")
            assert secret_hf not in txt
            assert secret_api not in txt
