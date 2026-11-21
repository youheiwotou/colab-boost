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


def test_evaluate_cost_guards_continues_after_single_session_stop_failure(isolated_paths: WorkspacePaths) -> None:
    """One failing stop_session must never prevent evaluate_cost_guards from stopping remaining sessions."""
    stop_attempts: list[str] = []

    class PartialFailStopMock(MockColabBackend):
        def unassign_runtime(self, account_id: str, token_json: str, endpoint: str) -> bool:
            stop_attempts.append(endpoint)
            if len(stop_attempts) == 1:
                raise ColabBackendError("network", "Transient unassign outage on first VM")
            return super().unassign_runtime(account_id, token_json, endpoint)

    orch = ConsoleOrchestrator(
        paths=isolated_paths,
        mock_backend=PartialFailStopMock(scenario="demo"),
        force_file_vault=True,
    )
    orch.update_settings({"guards": {"balance_floor_ccu": 250.0}})

    triggered = orch.evaluate_cost_guards()
    assert len(stop_attempts) == 2
    assert len(triggered) == 1
    assert len(orch.list_sessions()) == 1


def test_provision_failure_and_cancel_during_probe_or_watchdog_never_leak_vms(isolated_paths: WorkspacePaths) -> None:
    """Any exception or cancel after assign_runtime (during probe/watchdog) must immediately unassign and emit events."""
    events: list[dict] = []

    # 1. probe_hardware raises -> allocated VM must be unassigned & provision_updated(failed) emitted
    class ProbeCrashMock(MockColabBackend):
        def probe_hardware(self, assignment):
            raise ColabBackendError("kernel_unreachable", "Simulated probe crash")

    orch1 = ConsoleOrchestrator(
        paths=WorkspacePaths(isolated_paths.home / "probe_crash"),
        mock_backend=ProbeCrashMock(scenario="single"),
        force_file_vault=True,
    )
    orch1.subscribe(events.append)
    try:
        try:
            orch1.provision_session(ProvisionRequest(accelerator=Accelerator.L4))
            raise AssertionError("Expected probe_hardware failure")
        except ColabBackendError as exc:
            assert exc.kind == "kernel_unreachable"
        assert orch1.list_sessions() == []
        assert any(r.reason == "provision_failed_cleanup" and r.verified_unassigned for r in orch1.list_receipts())
        assert any(
            ev["type"] == "provision_updated" and ev["data"].get("state") == "failed"
            for ev in events
        )
    finally:
        orch1.shutdown()

    # 2. Cancel while probe_hardware is in flight -> allocated VM must be immediately unassigned
    in_probe = threading.Event()
    release_probe = threading.Event()

    class SlowProbeMock(MockColabBackend):
        def probe_hardware(self, assignment):
            in_probe.set()
            release_probe.wait(timeout=5.0)
            return super().probe_hardware(assignment)

    orch2 = ConsoleOrchestrator(
        paths=WorkspacePaths(isolated_paths.home / "probe_cancel"),
        mock_backend=SlowProbeMock(scenario="single"),
        force_file_vault=True,
    )
    try:
        op = orch2.start_provision_async(ProvisionRequest(accelerator=Accelerator.T4))
        assert in_probe.wait(timeout=3.0)
        assert orch2.cancel_provision(op.op_id) is True
        release_probe.set()

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            cur = next(o for o in orch2.list_provision_ops() if o.op_id == op.op_id)
            if cur.state == "cancelled":
                break
            time.sleep(0.05)

        assert orch2.list_sessions() == []
        assert any(r.reason == "cancelled_cleanup" and r.verified_unassigned for r in orch2.list_receipts())
    finally:
        orch2.shutdown()

    # 3. _install_vm_watchdog raises -> allocated VM must be unassigned (zero leak)
    orch3 = ConsoleOrchestrator(
        paths=WorkspacePaths(isolated_paths.home / "wd_crash"),
        mock_backend=MockColabBackend(scenario="single"),
        force_file_vault=True,
    )
    try:
        def boom_watchdog(*_args, **_kwargs):
            raise ColabBackendError("watchdog_failed", "Simulated watchdog launch crash")

        orch3._install_vm_watchdog = boom_watchdog  # type: ignore[method-assign]
        try:
            orch3.provision_session(ProvisionRequest(accelerator=Accelerator.T4))
            raise AssertionError("Expected watchdog failure")
        except ColabBackendError as exc:
            assert exc.kind == "watchdog_failed"
        assert orch3.list_sessions() == []
        assert any(r.reason == "provision_failed_cleanup" and r.verified_unassigned for r in orch3.list_receipts())
    finally:
        orch3.shutdown()


def test_stop_all_detailed_reports_account_and_stop_failures_and_skips_keep_sync(isolated_paths: WorkspacePaths) -> None:
    """stop_all_sessions_detailed must never falsely report zero_burn_verified=True on listing/stop errors and must skip keep sync."""
    fail_list_acc = [True]
    target_fail_acc = [""]
    fail_first_unassign = [True]
    keep_sync_calls = [0]

    class FlakyStopAllMock(MockColabBackend):
        def list_assignments(self, account_id: str, token_json: str):
            if fail_list_acc[0] and target_fail_acc[0] and account_id == target_fail_acc[0]:
                raise ColabBackendError("network", "Simulated account list outage")
            return super().list_assignments(account_id, token_json)

        def unassign_runtime(self, account_id: str, token_json: str, endpoint: str) -> bool:
            if fail_first_unassign[0]:
                fail_first_unassign[0] = False
                raise ColabBackendError("network", "Simulated single VM stop failure")
            return super().unassign_runtime(account_id, token_json, endpoint)

    orch = ConsoleOrchestrator(
        paths=isolated_paths,
        mock_backend=FlakyStopAllMock(scenario="demo"),
        force_file_vault=True,
    )
    secondary_acc_id = next(a.id for a in orch.list_accounts() if not a.is_active)
    target_fail_acc[0] = secondary_acc_id
    orig_sync = orch.sync_keep_artifacts

    def spy_sync(run_id: str):
        keep_sync_calls[0] += 1
        return orig_sync(run_id)

    orch.sync_keep_artifacts = spy_sync  # type: ignore[method-assign]

    # Deploy a recipe with [[keep]] and open a tunnel so we verify keep sync is skipped and tunnels are closed
    run = orch.start_deployment(
        DeployRequest(recipe_id="comfyui-studio", inputs={"port": 8189}, provision=ProvisionRequest(accelerator=Accelerator.T4)),
        run_sync=True,
    )
    assert run.state == "ready"
    assert len(orch.tunnels.list_tunnels()) >= 1

    res = orch.stop_all_sessions_detailed("emergency_stop_all")
    assert keep_sync_calls[0] == 0
    assert orch.tunnels.list_tunnels() == []
    assert res["zero_burn_verified"] is False
    assert any(item["account_id"] == secondary_acc_id for item in res["failed_accounts"])
    assert len(res["stop_errors"]) == 1
    assert res["stopped_count"] >= 2  # Remaining sessions still stopped despite first failure

    # Once transient errors clear, a second stop_all verifies clean zero burn
    fail_list_acc[0] = False
    res2 = orch.stop_all_sessions_detailed("emergency_stop_all")
    assert res2["zero_burn_verified"] is True
    assert res2["remaining_sessions"] == 0
    assert res2["failed_accounts"] == []
    assert res2["stop_errors"] == []


def test_cancel_deployment_propagates_to_inflight_provision(isolated_paths: WorkspacePaths) -> None:
    """Cancelling a deployment mid-provisioning must cancel the underlying provision op immediately."""
    in_assign = threading.Event()
    release_assign = threading.Event()
    assign_calls = [0]

    class SlowRetryMock(MockColabBackend):
        def assign_runtime(self, account_id: str, token_json: str, accelerator: Accelerator, high_mem: bool = False):
            assign_calls[0] += 1
            in_assign.set()
            release_assign.wait(timeout=5.0)
            return super().assign_runtime(account_id, token_json, accelerator, high_mem=high_mem)

    orch = ConsoleOrchestrator(
        paths=isolated_paths,
        mock_backend=SlowRetryMock(scenario="single"),
        force_file_vault=True,
    )
    try:
        run = orch.start_deployment(
            DeployRequest(
                recipe_id="llama-cpp-gguf",
                inputs={"repo": "Qwen/Qwen2.5-7B-Instruct-GGUF"},
                provision=ProvisionRequest(accelerator=Accelerator.A100, min_vram_mib=70000),
            ),
            run_sync=False,
        )
        assert in_assign.wait(timeout=3.0)
        orch.cancel_deployment(run.run_id)
        release_assign.set()

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not orch._provision_done and not orch._run_cancels:
                break
            time.sleep(0.05)

        # Must NOT have retried a 2nd VRAM attempt, and 0 VMs must remain
        assert assign_calls[0] == 1
        assert orch.list_sessions() == []
        assert orch.get_run(run.run_id).state == "cancelled"
    finally:
        orch.shutdown()
