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


def test_atomic_writes_concurrent_vault_lock_and_recursive_output_redaction(isolated_paths: WorkspacePaths) -> None:
    """Concurrent writes to settings/vault/ledger/runs must never raise PermissionError, and outputs are recursively redacted."""
    import json
    from colab_boost.models import BackendMode, DeploymentRun
    from colab_boost.orchestrator import _utc_now

    orch = ConsoleOrchestrator(paths=isolated_paths, force_file_vault=True)
    orch.update_settings({"backend_mode": BackendMode.REAL})
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            for j in range(10):
                orch.settings_store.save(orch.get_settings())
                orch.vault.set_secret("hf_token", f"hf_concurrent_secret_{idx}_{j}")
                orch.vault.set_account_token(
                    f"acc-{idx}",
                    json.dumps({"access_token": f"ya29_acc_{idx}_{j}_token", "refresh_token": f"1//ref_{idx}_{j}"}),
                )
                with orch._lock:
                    orch._real_ledger[f"ep-{idx}"] = {
                        "name": f"sess-{idx}",
                        "account_id": f"acc-{idx}",
                        "created_at": _utc_now().isoformat(),
                    }
                    orch._save_real_ledger()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert errors == []
    # No stray .tmp files left behind
    assert list(isolated_paths.home.glob("*.tmp")) == []

    # Dynamic secret registration + account token extraction + run_secrets aggregation in all_known_secrets()
    orch.vault.register_secret("dyn_ephemeral_token_7788")
    orch.vault.set_run_secrets("run-sec-1", ["sk-run-nested-secret-9900"])
    known = orch.vault.all_known_secrets()
    assert "dyn_ephemeral_token_7788" in known
    assert "sk-run-nested-secret-9900" in known
    assert any(s.startswith("ya29_acc_") for s in known)

    # Recursive redaction of outputs dict in both _update_run and _mark_real_run
    run = DeploymentRun(
        run_id="run-sec-1",
        recipe_id="llama-cpp-gguf",
        recipe_name={"en": "GGUF"},
        account_id="acc-0",
        session_name="sess-0",
        started_at=_utc_now(),
    )
    with orch._lock:
        orch._real_runs[run.run_id] = run
    orch._mark_real_run(
        run.run_id,
        ["sk-run-nested-secret-9900"],
        outputs={
            "nested": {"key": "bearer dyn_ephemeral_token_7788", "list": ["ok", "sk-run-nested-secret-9900"]},
            "count": 3,
        },
    )
    saved = orch.get_run(run.run_id)
    assert "dyn_ephemeral_token_7788" not in json.dumps(saved.outputs)
    assert "sk-run-nested-secret-9900" not in json.dumps(saved.outputs)
    assert saved.outputs["nested"]["key"] == "bearer ***"
    assert saved.outputs["nested"]["list"] == ["ok", "***"]
    assert saved.outputs["count"] == 3
    orch.shutdown()


def test_refresh_usage_unknown_preserves_balance_as_stale_without_false_kill(isolated_paths: WorkspacePaths) -> None:
    """When backend returns status='unknown', previous balance_ccu is preserved with status='stale' and never trips guards."""
    from colab_boost.models import UsageSnapshot

    return_unknown = [False]

    class UnknownUsageMock(MockColabBackend):
        def fetch_usage(self, account_id: str, token_json: str) -> UsageSnapshot:
            if return_unknown[0]:
                return UsageSnapshot(status="unknown", balance_ccu=None, stale_reason="missing balance field")
            return super().fetch_usage(account_id, token_json)

    orch = ConsoleOrchestrator(paths=isolated_paths, mock_backend=UnknownUsageMock("demo"), force_file_vault=True)
    try:
        orch.update_settings({"guards": {"balance_floor_ccu": 250.0}})
        orig_bal = orch.active_account().usage.balance_ccu
        assert orig_bal is not None and orig_bal > 100.0

        return_unknown[0] = True
        snap = orch.refresh_account_usage("acc-pro-main")
        assert snap.status == "stale"
        assert snap.balance_ccu == orig_bal
        assert "missing balance" in (snap.stale_reason or "")
        assert orch.evaluate_cost_guards() == []
        assert len(orch.list_sessions()) == 2
    finally:
        orch.shutdown()


def test_stop_session_dedup_by_name_closes_ready_endpoints_and_matches_by_endpoint(
    isolated_paths: WorkspacePaths,
) -> None:
    """stop_session deduplicates concurrent calls by session_name, marks ready endpoints closed, and matches active runs by endpoint."""
    unassign_calls = [0]
    in_unassign = threading.Event()
    release_unassign = threading.Event()

    class SlowUnassignMock(MockColabBackend):
        def unassign_runtime(self, account_id: str, token_json: str, endpoint: str) -> bool:
            unassign_calls[0] += 1
            in_unassign.set()
            release_unassign.wait(timeout=5.0)
            return super().unassign_runtime(account_id, token_json, endpoint)

    orch = ConsoleOrchestrator(
        paths=isolated_paths,
        mock_backend=SlowUnassignMock("single"),
        force_file_vault=True,
        mock_step_delay_s=0.01,
    )
    events: list[dict] = []
    orch.subscribe(events.append)
    try:
        run = orch.start_deployment(
            DeployRequest(
                recipe_id="llama-cpp-gguf",
                inputs={"repo": "Qwen/Qwen2.5-7B-Instruct-GGUF"},
                provision=ProvisionRequest(name="reusable-name", accelerator=Accelerator.T4),
            ),
            run_sync=True,
        )
        assert run.state == "ready"
        assert len(run.endpoints) >= 1 and all(ep.status == "open" for ep in run.endpoints)

        # Trigger two concurrent stop_session calls BY NAME (second call starts while first is mid-unassign)
        results: list = []
        errors: list[BaseException] = []

        def _stop_by_name() -> None:
            try:
                results.append(orch.stop_session("reusable-name"))
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_stop_by_name)
        t1.start()
        assert in_unassign.wait(timeout=3.0)
        t2 = threading.Thread(target=_stop_by_name)
        t2.start()
        time.sleep(0.05)
        release_unassign.set()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert errors == []
        assert len(results) == 2
        assert results[0].receipt_id == results[1].receipt_id
        assert unassign_calls[0] == 1

        # Ready run's endpoints must now be marked "closed"
        stopped_run = orch.get_run(run.run_id)
        assert stopped_run.state == "ready"
        assert all(ep.status == "closed" for ep in stopped_run.endpoints)
        assert any(
            ev["type"] == "run_updated"
            and ev["data"].get("run_id") == run.run_id
            and all(ep.get("status") == "closed" for ep in ev["data"].get("endpoints", []))
            for ev in events
        )

        # Provisioning a brand-new session with the SAME name must NOT inherit old endpoint's active_run_id
        new_sess = orch.provision_session(ProvisionRequest(name="reusable-name", accelerator=Accelerator.T4))
        assert new_sess.endpoint != run.session_endpoint
        assert new_sess.active_run_id is None
    finally:
        orch.shutdown()


def test_start_deployment_binds_account_id_and_partial_provision_keeps_recipe_high_mem(
    isolated_paths: WorkspacePaths,
) -> None:
    """start_deployment binds explicit provision.account_id or reused session's account_id, and preserves recipe high_mem."""
    from colab_boost.models import MachineShape

    orch = ConsoleOrchestrator(
        paths=isolated_paths,
        mock_backend=MockColabBackend("demo"),
        force_file_vault=True,
        mock_step_delay_s=0.01,
    )
    try:
        orch.stop_all_sessions()
        accounts = orch.list_accounts()
        secondary = next(a for a in accounts if not a.is_active)

        # 1. Explicit provision.account_id binds run.account_id to secondary (not active) & keeps minimax-h3 high_mem=True
        run1 = orch.start_deployment(
            DeployRequest(
                recipe_id="minimax-h3-studio",
                provision=ProvisionRequest(
                    name="sec-g4-h3",
                    account_id=secondary.id,
                    accelerator=Accelerator.G4,
                    min_vram_mib=80000,
                ),
            ),
            run_sync=True,
        )
        assert run1.state == "ready"
        assert run1.account_id == secondary.id
        sess = next(s for s in orch.list_sessions() if s.name == "sec-g4-h3")
        assert sess.account_id == secondary.id
        assert sess.machine_shape == MachineShape.HIGH_RAM

        # 2. Reusing existing session belonging to secondary account binds run2.account_id to secondary (not active)
        run2 = orch.start_deployment(
            DeployRequest(
                recipe_id="faster-whisper-api",
                session_name="sec-g4-h3",
            ),
            run_sync=True,
        )
        assert run2.state == "ready"
        assert run2.account_id == secondary.id
        assert run2.session_endpoint == sess.endpoint
    finally:
        orch.shutdown()


def test_watchdog_budget_cap_extend_heartbeat_zombie_check_and_missing_keyerror(
    isolated_paths: WorkspacePaths,
) -> None:
    """Watchdog encodes stricter spend_cap/burn_rate deadline; extend touches heartbeat, checks /proc/pid/stat, raises KeyError on vanish."""
    import pytest
    from colab_boost.backends.base import ExecResult

    captured_scripts: list[str] = []
    drop_after_extend = [False]

    class WatchdogDirect(MockColabBackend):
        @property
        def is_mock(self) -> bool:
            return False

        def list_assignments(self, account_id: str, token_json: str):
            if drop_after_extend[0]:
                return []
            return super().list_assignments(account_id, token_json)

        def execute_python(self, assignment, code: str, *, env=None, timeout_s: float = 45.0) -> ExecResult:
            captured_scripts.append(code)
            if "info = {'gpu_name': None" in code:
                return ExecResult(
                    ok=True,
                    marker_json={
                        "gpu_name": "NVIDIA H100 80GB",
                        "vram_total_mib": 81920,
                        "vram_used_mib": 100,
                        "gpu_util_percent": 0.0,
                        "runtime_addr": "127.0.0.1:9000",
                    },
                )
            return ExecResult(ok=True, marker_json={"pid": 9001, "deadline_epoch": time.time() + 7200})

    orch = ConsoleOrchestrator(paths=isolated_paths, direct_backend=WatchdogDirect("signed_out"), force_file_vault=True)
    try:
        orch.update_settings(
            {
                "backend_mode": "real",
                "guards": {"auto_stop_minutes": 180, "session_spend_cap_ccu": 11.78},
            }
        )
        orch._register_authenticated_account("wd@studio.ai", "WD", '{"refresh_token":"r"}')
        # H100 approx burn = 11.78 CCU/h -> 11.78 / 11.78 * 3600 = 3600s (< 180*60=10800s!)
        before = time.time()
        sess = orch.provision_session(ProvisionRequest(name="h100-cap", accelerator=Accelerator.H100))
        assert sess.watchdog.active is True
        assert sess.watchdog.deadline_epoch is not None
        assert 3500.0 <= (sess.watchdog.deadline_epoch - before) <= 3700.0
        wd_install_code = next(c for c in captured_scripts if "watchdog.json" in c and "--hooks-stdin" in c)
        assert "tempfile.mkstemp" in wd_install_code
        assert '"budget_cap_s": 3600.0' in wd_install_code
        assert '"remaining_s": 3600.0' in wd_install_code

        # Extend deadline: must touch heartbeat, refresh remaining_s, filter zombie 'Z' in /proc/{pid}/stat
        captured_scripts.clear()
        ext = orch.extend_session_deadline("h100-cap", 60)
        assert ext.watchdog.active is True
        ext_code = captured_scripts[-1]
        assert "os.utime(hb_file, None)" in ext_code
        assert "cfg['remaining_s'] =" in ext_code
        assert "/proc/{pid}/stat" in ext_code
        assert "('Z', 'X', 'x')" in ext_code
        assert "tempfile.mkstemp" in ext_code

        # If session vanishes right after VM extend, must raise KeyError (NOT StopIteration!)
        orig_exec = orch._direct_backend.execute_python

        def _vanish_during_exec(assignment, code: str, *, env=None, timeout_s: float = 45.0) -> ExecResult:
            res = orig_exec(assignment, code, env=env, timeout_s=timeout_s)
            drop_after_extend[0] = True
            return res

        orch._direct_backend.execute_python = _vanish_during_exec  # type: ignore[method-assign]
        with pytest.raises(KeyError, match="disappeared"):
            orch.extend_session_deadline("h100-cap", 30)
    finally:
        orch.shutdown()


def test_update_settings_does_not_hold_lock_during_tunnel_stop_all(isolated_paths: WorkspacePaths) -> None:
    """update_settings must release self._lock before calling blocking tunnels.stop_all()."""
    orch = ConsoleOrchestrator(paths=isolated_paths, force_file_vault=True)
    lock_was_free = [False]
    orig_stop_all = orch.tunnels.stop_all

    def _check_lock_free() -> None:
        done = threading.Event()

        def _probe() -> None:
            # Acquiring orch._lock from a DIFFERENT thread only succeeds if main thread released it
            if orch._lock.acquire(timeout=1.0):
                lock_was_free[0] = True
                orch._lock.release()
            done.set()

        th = threading.Thread(target=_probe)
        th.start()
        done.wait(timeout=2.0)
        th.join(timeout=2.0)
        orig_stop_all()

    orch.tunnels.stop_all = _check_lock_free  # type: ignore[method-assign]
    try:
        orch.update_settings({"backend_mode": "real"})
        assert lock_was_free[0] is True
    finally:
        orch.shutdown()
