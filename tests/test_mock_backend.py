"""Contract-alignment, fault-injection, and mock-tunnel concurrency tests."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from colab_boost.backends.base import ColabBackendError
from colab_boost.backends.mock import PENDING_FLOW_TTL_S, MockColabBackend
from colab_boost.config import WorkspacePaths
from colab_boost.models import HIGH_MEM_ONLY_ACCELERATORS, Accelerator, MachineShape, ProvisionRequest
from colab_boost.orchestrator import ConsoleOrchestrator
from colab_boost.ssh_bridge import SshTunnelManager, find_free_local_port


def test_assign_runtime_high_mem_only_and_tpu_hardware_descriptions() -> None:
    backend = MockColabBackend(scenario="signed_out")

    # L4, V5E1, V6E1 must ALWAYS use HIGH_RAM even when high_mem=False
    for accel in HIGH_MEM_ONLY_ACCELERATORS:
        raw = backend.assign_runtime("acc-1", "{}", accel, high_mem=False)
        assert raw.machine_shape == MachineShape.HIGH_RAM
        probe = backend.probe_hardware(raw)
        assert probe.gpu_name is not None and probe.gpu_name.strip() != ""
        assert probe.vram_total_mib > 0

    # Specifically verify TPU v5e / v6e descriptions are distinct from CPU NONE
    raw_v5 = backend.assign_runtime("acc-1", "{}", Accelerator.V5E1, high_mem=False)
    raw_v6 = backend.assign_runtime("acc-1", "{}", Accelerator.V6E1, high_mem=False)
    raw_cpu = backend.assign_runtime("acc-1", "{}", Accelerator.NONE, high_mem=False)

    probe_v5 = backend.probe_hardware(raw_v5)
    probe_v6 = backend.probe_hardware(raw_v6)
    probe_cpu = backend.probe_hardware(raw_cpu)

    assert "TPU v5e" in (probe_v5.gpu_name or "")
    assert "TPU v6e" in (probe_v6.gpu_name or "")
    assert probe_cpu.gpu_name is None
    assert probe_cpu.vram_total_mib == 0
    assert raw_cpu.machine_shape == MachineShape.STANDARD

    # Standard GPU obeys high_mem flag
    raw_t4_std = backend.assign_runtime("acc-1", "{}", Accelerator.T4, high_mem=False)
    raw_t4_hm = backend.assign_runtime("acc-1", "{}", Accelerator.T4, high_mem=True)
    assert raw_t4_std.machine_shape == MachineShape.STANDARD
    assert raw_t4_hm.machine_shape == MachineShape.HIGH_RAM


def test_unassign_runtime_empty_endpoint_and_account_ownership() -> None:
    backend = MockColabBackend(scenario="demo")

    # Empty / blank endpoint must return False (aligned with DirectColabBackend)
    assert backend.unassign_runtime("acc-pro-main", "{}", "") is False
    assert backend.unassign_runtime("acc-pro-main", "{}", "   ") is False

    # Cross-account unassign must NOT delete another account's runtime
    raw = backend.assign_runtime("acc-pro-main", "{}", Accelerator.T4)
    assert backend.unassign_runtime("acc-backup-lab", "{}", raw.endpoint) is False
    still_there = [a.endpoint for a in backend.list_assignments("acc-pro-main", "{}")]
    assert raw.endpoint in still_there

    # Owning account can unassign cleanly
    assert backend.unassign_runtime("acc-pro-main", "{}", raw.endpoint) is True
    remaining = [a.endpoint for a in backend.list_assignments("acc-pro-main", "{}")]
    assert raw.endpoint not in remaining

    # Idempotent on already-deleted / unknown endpoint (mirrors HTTP 404 -> True)
    assert backend.unassign_runtime("acc-pro-main", "{}", raw.endpoint) is True


def test_fetch_usage_is_readonly_and_unifies_balance_keys() -> None:
    backend = MockColabBackend(scenario="demo")

    # Demo scenario has active A100 + orphan T4 runtimes burning CCU on acc-pro-main.
    # Repeated read-only fetch_usage calls must NOT mutate balance by -0.01!
    snap1 = backend.fetch_usage("acc-pro-main", "{}")
    snap2 = backend.fetch_usage("acc-pro-main", "{}")
    snap3 = backend.fetch_usage("acc-pro-main", "{}")
    assert snap1.burn_rate_hourly is not None and snap1.burn_rate_hourly > 0
    assert snap1.balance_ccu == 186.40
    assert snap2.balance_ccu == 186.40
    assert snap3.balance_ccu == 186.40

    # OAuth login must not leak an orphan random key into _balances
    keys_before = set(backend._balances.keys())
    flow = backend.begin_oauth_login()
    info = backend.complete_oauth_login(flow["flow_id"], "alice@example.com")
    assert set(backend._balances.keys()) == keys_before

    # Fetching usage under orchestrator-assigned account_id binds balance cleanly to account_id
    u = backend.fetch_usage("acc-orch-alice", info["token_json"])
    assert u.balance_ccu == 100.0
    assert "acc-orch-alice" in backend._balances

    # Legacy mock_account balance key compatibility: migrates seamlessly to account_id
    backend._balances["legacy-acc-key"] = 42.50
    legacy_tok = json.dumps({"mock_account": "legacy-acc-key", "email": "legacy@example.com"})
    u_legacy = backend.fetch_usage("acc-unified-1", legacy_tok)
    assert u_legacy.balance_ccu == 42.50
    assert "acc-unified-1" in backend._balances
    assert "legacy-acc-key" not in backend._balances

    # Subsequent assign_runtime deducts 0.05 from the unified account_id ledger entry
    backend.assign_runtime("acc-unified-1", legacy_tok, Accelerator.T4)
    u_after = backend.fetch_usage("acc-unified-1", legacy_tok)
    assert u_after.balance_ccu == 42.45


def test_inspect_token_requires_email_and_pending_flow_ttl() -> None:
    backend = MockColabBackend(scenario="signed_out")
    assert PENDING_FLOW_TTL_S == 900.0

    # Missing or blank email raises unverified_account (aligned with DirectColabBackend)
    for bad_payload in ({}, {"mock_account": "acc-1"}, {"email": ""}, {"email": "   "}):
        with pytest.raises(ColabBackendError) as exc_info:
            backend.inspect_token(json.dumps(bad_payload))
        assert exc_info.value.kind == "unverified_account"

    # Malformed JSON or non-object raises invalid_credential
    for raw_bad in ("not-json", "[]", "123"):
        with pytest.raises(ColabBackendError) as exc_info:
            backend.inspect_token(raw_bad)
        assert exc_info.value.kind == "invalid_credential"

    # Valid token returns email and display_name
    good = backend.inspect_token(json.dumps({"email": "bob@example.com", "display_name": "Bob Lab"}))
    assert good["email"] == "bob@example.com"
    assert good["display_name"] == "Bob Lab"

    # Pending flows expire after 15-minute TTL and are pruned
    stale_flow = backend.begin_oauth_login()["flow_id"]
    fresh_flow = backend.begin_oauth_login()["flow_id"]
    backend._pending_flows[stale_flow] = time.monotonic() - (PENDING_FLOW_TTL_S + 5.0)

    # Trigger prune via begin_oauth_login
    _another = backend.begin_oauth_login()["flow_id"]
    assert stale_flow not in backend._pending_flows
    assert fresh_flow in backend._pending_flows

    with pytest.raises(ColabBackendError) as exp_info:
        backend.complete_oauth_login(stale_flow, "bob@example.com")
    assert exp_info.value.kind == "flow_expired"

    # Fresh flow still completes cleanly
    completed = backend.complete_oauth_login(fresh_flow, "bob@example.com")
    assert completed["email"] == "bob@example.com"


def test_fault_injection_412_400_401_and_orchestrator_integration(isolated_paths: WorkspacePaths) -> None:
    backend = MockColabBackend(scenario="single")

    # Validation checks
    with pytest.raises(ValueError):
        backend.inject_fault("nonexistent_method", RuntimeError("nope"))
    with pytest.raises(ValueError):
        backend.inject_fault("assign_runtime", RuntimeError("nope"), times=0)

    # 1. Persistent HTTP 412 capacity fault until cleared
    err_412 = ColabBackendError("capacity_412", "GPU capacity exhausted", status_code=412)
    backend.inject_fault("assign_runtime", err_412)
    for _ in range(2):
        with pytest.raises(ColabBackendError) as ei:
            backend.assign_runtime("acc-pro-main", "{}", Accelerator.A100)
        assert ei.value.kind == "capacity_412"
        assert ei.value.status_code == 412
    backend.clear_faults("assign_runtime")
    raw_ok = backend.assign_runtime("acc-pro-main", "{}", Accelerator.T4)
    assert raw_ok.accelerator == Accelerator.T4

    # 2. One-shot HTTP 400 not_entitled fault
    err_400 = ColabBackendError("not_entitled", "Account not entitled to H100", status_code=400)
    backend.inject_fault("assign_runtime", err_400, times=1)
    with pytest.raises(ColabBackendError) as ei400:
        backend.assign_runtime("acc-pro-main", "{}", Accelerator.H100)
    assert ei400.value.kind == "not_entitled"
    assert ei400.value.status_code == 400
    # Next call succeeds automatically
    raw_h100 = backend.assign_runtime("acc-pro-main", "{}", Accelerator.H100)
    assert raw_h100.accelerator == Accelerator.H100

    # 3. Counted HTTP 401 auth_error fault + clear_faults()
    err_401 = ColabBackendError("auth_error", "Token revoked", status_code=401)
    backend.inject_fault("fetch_usage", err_401, times=2)
    with pytest.raises(ColabBackendError) as ei401:
        backend.fetch_usage("acc-pro-main", "{}")
    assert ei401.value.kind == "auth_error"
    assert ei401.value.status_code == 401
    backend.clear_faults()
    assert backend.fetch_usage("acc-pro-main", "{}").status == "live"

    # 4. Integration with ConsoleOrchestrator (fault survives reset_scenario in __init__)
    orch_mock = MockColabBackend(scenario="single")
    orch = ConsoleOrchestrator(paths=isolated_paths, mock_backend=orch_mock, force_file_vault=True)
    try:
        orch_mock.inject_fault("fetch_usage", err_401, times=1)
        snap = orch.refresh_account_usage("acc-pro-main")
        assert snap.status == "stale"

        orch_mock.inject_fault("assign_runtime", err_412, times=1)
        with pytest.raises(ColabBackendError) as orch_ei:
            orch.provision_session(ProvisionRequest(accelerator=Accelerator.L4))
        assert orch_ei.value.kind == "capacity_412"
        assert orch.list_sessions() == []
    finally:
        orch.shutdown()


def test_find_free_local_port_respects_free_and_busy_preferred() -> None:
    # Allocate an OS port and keep it bound+listening -> find_free_local_port must pick a different port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        busy_port = int(srv.getsockname()[1])
        picked_while_busy = find_free_local_port(busy_port)
        assert picked_while_busy != busy_port
        assert 1024 <= picked_while_busy <= 65535

    # Once released, preferred port is returned directly (regression guard for Windows connect_ex+bind bug)
    assert find_free_local_port(busy_port) == busy_port


def test_register_mock_tunnel_port_conflict_and_stop_fence(tmp_path: Path) -> None:
    paths = WorkspacePaths(base_dir=tmp_path / "tun_home")
    paths.ensure_dirs()
    mgr = SshTunnelManager(paths, auto_reconnect=False)
    try:
        base_port = find_free_local_port(0)

        # 1. Port 8080 remains forbidden
        with pytest.raises(ValueError, match="8080"):
            mgr.register_mock_tunnel(
                session_name="sess-a",
                account_id="acc-1",
                remote_port=8080,
                local_port=base_port,
            )

        # 2. First registration gets base_port; same (session, remote_port) reuses it
        t1 = mgr.register_mock_tunnel(
            session_name="sess-a",
            account_id="acc-1",
            remote_port=8000,
            local_port=base_port,
        )
        assert t1.local_port == base_port

        t1_again = mgr.register_mock_tunnel(
            session_name="sess-a",
            account_id="acc-1",
            remote_port=8000,
            local_port=base_port,
            label="Updated",
        )
        assert t1_again.id == t1.id
        assert t1_again.local_port == base_port
        assert t1_again.label == "Updated"

        # 3. Second session requesting the SAME occupied local_port must NOT double-bind base_port
        t2 = mgr.register_mock_tunnel(
            session_name="sess-b",
            account_id="acc-1",
            remote_port=8000,
            local_port=base_port,
        )
        assert t2.local_port != base_port
        assert t2.id != t1.id

        # 4. Different remote_port on same session requesting occupied local_port also avoids conflict
        t3 = mgr.register_mock_tunnel(
            session_name="sess-a",
            account_id="acc-1",
            remote_port=8001,
            local_port=base_port,
        )
        assert t3.local_port not in {t1.local_port, t2.local_port}

        # 5. Reserved local port in _reserved_local_ports is also protected from conflict
        reserved_port = find_free_local_port(0)
        with mgr._lock:
            mgr._reserved_local_ports.add(reserved_port)
        try:
            t4 = mgr.register_mock_tunnel(
                session_name="sess-c",
                account_id="acc-1",
                remote_port=9000,
                local_port=reserved_port,
            )
            assert t4.local_port != reserved_port
        finally:
            with mgr._lock:
                mgr._reserved_local_ports.discard(reserved_port)

        # 6. Stop fence prevents resurrecting a tunnel for a stopped session (explicit stale fence)
        stale_sess_fence = mgr._get_fence_token("sess-a")
        mgr.stop_session_tunnels("sess-a")
        with pytest.raises(RuntimeError, match="stopped"):
            mgr.register_mock_tunnel(
                session_name="sess-a",
                account_id="acc-1",
                remote_port=8000,
                local_port=base_port,
                fence=stale_sess_fence,
            )
        assert mgr.list_tunnels("sess-a") == []

        # 7. Global stop_all fence rejects stale registrations too
        stale_global_fence = mgr._get_fence_token("sess-b")
        mgr.stop_all()
        with pytest.raises(RuntimeError, match="stopped"):
            mgr.register_mock_tunnel(
                session_name="sess-b",
                account_id="acc-1",
                remote_port=8000,
                fence=stale_global_fence,
            )
        assert mgr.list_tunnels() == []

        # 8. Concurrent stop race: thread waiting on session lock observes bumped fence and aborts
        sess_lock = mgr._get_session_lock("sess-race")
        race_error: list[BaseException] = []
        started = threading.Event()

        def worker() -> None:
            started.set()
            try:
                mgr.register_mock_tunnel(
                    session_name="sess-race",
                    account_id="acc-1",
                    remote_port=8000,
                )
            except BaseException as exc:
                race_error.append(exc)

        with sess_lock:
            th = threading.Thread(target=worker)
            th.start()
            assert started.wait(timeout=2.0)
            time.sleep(0.05)  # Let worker capture entry_fence and block on sess_lock
            with mgr._lock:
                mgr._session_stop_gen["sess-race"] = mgr._session_stop_gen.get("sess-race", 0) + 1
        th.join(timeout=2.0)
        assert not th.is_alive()
        assert len(race_error) == 1
        assert isinstance(race_error[0], RuntimeError)
        assert mgr.list_tunnels("sess-race") == []

        # 9. Closed manager refuses mock tunnel registration
        mgr.close()
        with pytest.raises(RuntimeError, match="closed"):
            mgr.register_mock_tunnel(
                session_name="sess-after-close",
                account_id="acc-1",
                remote_port=8000,
            )
    finally:
        mgr.close()


def test_orchestrator_fixture_shutdown_teardown(orchestrator: ConsoleOrchestrator) -> None:
    assert not orchestrator._shutdown_ev.is_set()
    orchestrator.shutdown()
    assert orchestrator._shutdown_ev.is_set()
    assert orchestrator.tunnels.list_tunnels() == []
