"""Deterministic Sandbox / Demo Backend (``MockColabBackend``).

Drives every console workflow — multi-account CCU balances, the 40 GB -> 80 GB
A100 VRAM provisioning retry loop, untracked browser runtime cleanup, VM-side
watchdog deadlines, stage-by-stage model deployment, port forwarding, and
Playground chat — without touching Google servers or spending a single CCU.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from colab_boost.backends.base import (
    RESULT_MARKER,
    ColabBackend,
    ColabBackendError,
    ExecResult,
    HardwareProbe,
    RawAssignment,
)
from colab_boost.models import (
    APPROX_CCU_PER_HOUR,
    Accelerator,
    MachineShape,
    UsageSnapshot,
)


@dataclass
class MockVmState:
    assignment: RawAssignment
    account_id: str
    gpu_name: Optional[str]
    vram_total_mib: int
    vram_used_mib: int = 420
    gpu_util_percent: float = 2.0
    created_at_mono: float = field(default_factory=time.monotonic)
    files: dict[str, str] = field(default_factory=dict)
    binary_files: dict[str, bytes] = field(default_factory=dict)


class MockColabBackend(ColabBackend):
    def __init__(self, scenario: str = "demo") -> None:
        self._lock = threading.Lock()
        self.scenario = scenario
        self._a100_counter = 0
        self._pending_flows: dict[str, float] = {}
        self._balances: dict[str, float] = {
            "acc-pro-main": 186.40,
            "acc-backup-lab": 94.20,
        }
        self._vms: dict[str, MockVmState] = {}
        self.reset_scenario(scenario)

    @property
    def is_mock(self) -> bool:
        return True

    def reset_scenario(self, scenario: str = "demo") -> None:
        with self._lock:
            self.scenario = scenario
            self._vms.clear()
            self._a100_counter = 0
            self._balances = {
                "acc-pro-main": 186.40,
                "acc-backup-lab": 94.20,
            }
            if scenario == "demo":
                # 1. Active A100 80GB VM
                ep1 = "m-s-a100-demo80g"
                self._vms[ep1] = MockVmState(
                    assignment=RawAssignment(
                        endpoint=ep1,
                        accelerator=Accelerator.A100,
                        machine_shape=MachineShape.HIGH_RAM,
                        proxy_url=f"https://{ep1}.prod.colab.mock",
                        proxy_token="mock-proxy-token-a100",
                    ),
                    account_id="acc-pro-main",
                    gpu_name="NVIDIA A100-SXM4-80GB",
                    vram_total_mib=81920,
                    vram_used_mib=47920,
                    gpu_util_percent=38.5,
                )
                # 2. Untracked orphan T4 VM ("opened in browser") to demonstrate orphan cleanup!
                ep_orphan = "m-s-t4-browser-orphan"
                self._vms[ep_orphan] = MockVmState(
                    assignment=RawAssignment(
                        endpoint=ep_orphan,
                        accelerator=Accelerator.T4,
                        machine_shape=MachineShape.STANDARD,
                        proxy_url=f"https://{ep_orphan}.prod.colab.mock",
                        proxy_token="mock-proxy-token-orphan",
                    ),
                    account_id="acc-pro-main",
                    gpu_name="Tesla T4",
                    vram_total_mib=15360,
                    vram_used_mib=128,
                    gpu_util_percent=0.0,
                )

    def seed_demo_accounts(self) -> list[dict[str, Any]]:
        if self.scenario == "signed_out":
            return []
        if self.scenario == "single":
            return [
                {
                    "id": "acc-pro-main",
                    "email": "studio.lead@example.com",
                    "display_name": "Studio Lead (Colab Pro+)",
                    "is_active": True,
                    "token_json": json.dumps({"mock_account": "acc-pro-main", "email": "studio.lead@example.com"}),
                }
            ]
        return [
            {
                "id": "acc-pro-main",
                "email": "studio.lead@example.com",
                "display_name": "Studio Lead (Colab Pro+)",
                "is_active": True,
                "token_json": json.dumps({"mock_account": "acc-pro-main", "email": "studio.lead@example.com"}),
            },
            {
                "id": "acc-backup-lab",
                "email": "render.node02@example.com",
                "display_name": "Render Node 02 (Pay-As-You-Go)",
                "is_active": False,
                "token_json": json.dumps({"mock_account": "acc-backup-lab", "email": "render.node02@example.com"}),
            },
        ]

    def begin_oauth_login(self) -> dict[str, str]:
        fid = f"mock-flow-{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._pending_flows[fid] = time.monotonic()
        return {
            "flow_id": fid,
            "auth_url": "https://accounts.google.com/o/oauth2/auth?mock_colab_boost=1",
        }

    def complete_oauth_login(self, flow_id: str, auth_code: str) -> dict[str, str]:
        code = (auth_code or "").strip()
        if not code:
            raise ColabBackendError("invalid_code", "Authorization code cannot be empty.")
        with self._lock:
            if flow_id not in self._pending_flows:
                raise ColabBackendError("flow_expired", "Login flow expired or invalid.")
            self._pending_flows.pop(flow_id, None)
            email = code if "@" in code else f"colab.user.{code[:8].lower()}@gmail.com"
            acc_key = f"acc-{uuid.uuid4().hex[:6]}"
            self._balances.setdefault(acc_key, 100.0)
        token_json = json.dumps({"mock_account": acc_key, "email": email})
        return {
            "email": email,
            "display_name": email.split("@")[0],
            "token_json": token_json,
        }

    def inspect_token(self, token_json: str) -> dict[str, str]:
        try:
            data = json.loads(token_json)
            email = str(data.get("email") or "demo@example.com")
        except Exception as exc:
            raise ColabBackendError("invalid_credential", "Invalid mock token") from exc
        return {
            "email": email,
            "display_name": email.split("@")[0],
            "token_json": token_json,
        }

    def fetch_usage(self, account_id: str, token_json: str) -> UsageSnapshot:
        del token_json
        with self._lock:
            bal = self._balances.setdefault(account_id, 100.0)
            acct_vms = [vm for vm in self._vms.values() if vm.account_id == account_id]
            rate = sum(APPROX_CCU_PER_HOUR.get(vm.assignment.accelerator.value, 1.0) for vm in acct_vms)
            # Simulate tiny live burn tick if runtimes exist
            if rate > 0:
                bal = max(0.0, bal - 0.01)
                self._balances[account_id] = bal
            hours_left = round(bal / rate, 2) if rate > 0.01 else None
            return UsageSnapshot(
                status="live",
                balance_ccu=round(bal, 2),
                burn_rate_hourly=round(rate, 2),
                assignments_count=len(acct_vms),
                hours_remaining=hours_left,
                eligible_gpus=["T4", "L4", "G4", "A100", "H100"],
                fetched_at=datetime.now(timezone.utc),
            )

    def list_assignments(self, account_id: str, token_json: str) -> list[RawAssignment]:
        del token_json
        with self._lock:
            return [
                vm.assignment
                for vm in self._vms.values()
                if vm.account_id == account_id
            ]

    def assign_runtime(
        self,
        account_id: str,
        token_json: str,
        accelerator: Accelerator,
        high_mem: bool = False,
    ) -> RawAssignment:
        del token_json
        with self._lock:
            ep = f"m-s-{accelerator.value.lower()}-{uuid.uuid4().hex[:6]}"
            shape = MachineShape.HIGH_RAM if high_mem else MachineShape.STANDARD
            if accelerator == Accelerator.A100:
                # 1st attempt gives 40 GB, 2nd attempt gives 80 GB — tests min_vram_mib retry!
                self._a100_counter += 1
                is_80g = (self._a100_counter % 2 == 0)
                gpu_name = "NVIDIA A100-SXM4-80GB" if is_80g else "NVIDIA A100-SXM4-40GB"
                vram = 81920 if is_80g else 40960
            elif accelerator == Accelerator.H100:
                gpu_name, vram = "NVIDIA H100 80GB HBM3", 81920
            elif accelerator == Accelerator.L4:
                gpu_name, vram = "NVIDIA L4", 23034
                shape = MachineShape.STANDARD
            elif accelerator == Accelerator.G4:
                gpu_name, vram = "NVIDIA RTX PRO 6000 Blackwell (G4)", 97887
            elif accelerator == Accelerator.T4:
                gpu_name, vram = "Tesla T4", 15360
            else:
                gpu_name, vram = None, 0

            raw = RawAssignment(
                endpoint=ep,
                accelerator=accelerator,
                machine_shape=shape,
                proxy_url=f"https://{ep}.prod.colab.mock",
                proxy_token=f"mock-tok-{ep}",
            )
            self._vms[ep] = MockVmState(
                assignment=raw,
                account_id=account_id,
                gpu_name=gpu_name,
                vram_total_mib=vram,
            )
            # Deduct tiny allocation fee to model real Colab behavior
            self._balances[account_id] = max(0.0, self._balances.get(account_id, 100.0) - 0.05)
            return raw

    def unassign_runtime(self, account_id: str, token_json: str, endpoint: str) -> bool:
        del account_id, token_json
        with self._lock:
            self._vms.pop(endpoint, None)
            return True

    def probe_hardware(self, assignment: RawAssignment) -> HardwareProbe:
        with self._lock:
            vm = self._vms.get(assignment.endpoint)
            if vm is None:
                raise ColabBackendError("runtime_lost", f"Mock runtime {assignment.endpoint} no longer exists.", status_code=404)
            return HardwareProbe(
                gpu_name=vm.gpu_name,
                vram_total_mib=vm.vram_total_mib,
                vram_used_mib=vm.vram_used_mib,
                gpu_util_percent=vm.gpu_util_percent,
                runtime_addr="172.28.0.12:9000",
            )

    def set_vm_vram_usage(self, endpoint: str, used_mib: int, util_pct: float = 24.0) -> None:
        with self._lock:
            vm = self._vms.get(endpoint)
            if vm:
                vm.vram_used_mib = used_mib
                vm.gpu_util_percent = util_pct

    def put_mock_file(self, endpoint: str, remote_path: str, data: bytes) -> None:
        with self._lock:
            vm = self._vms.get(endpoint)
            if vm is None:
                raise ColabBackendError("runtime_lost", f"Mock runtime {endpoint} no longer exists.", status_code=404)
            vm.binary_files[remote_path] = data

    def get_mock_file(self, endpoint: str, remote_path: str) -> bytes:
        with self._lock:
            vm = self._vms.get(endpoint)
            if vm is None:
                raise ColabBackendError("runtime_lost", f"Mock runtime {endpoint} no longer exists.", status_code=404)
            if remote_path not in vm.binary_files:
                raise KeyError(f"Remote file not found: {remote_path}")
            return vm.binary_files[remote_path]

    def execute_python(
        self,
        assignment: RawAssignment,
        code: str,
        *,
        env: Optional[dict[str, str]] = None,
        timeout_s: float = 45.0,
    ) -> ExecResult:
        del env, timeout_s
        with self._lock:
            if assignment.endpoint not in self._vms:
                raise ColabBackendError("runtime_lost", f"Runtime {assignment.endpoint} not found", status_code=404)
        # Support marker-emitting JSON snippets in mock mode cleanly
        if RESULT_MARKER in code:
            return ExecResult(
                ok=True,
                stdout=f'{RESULT_MARKER}{{"ok": true}}\n',
                marker_json={"ok": True},
            )
        return ExecResult(ok=True, stdout="[mock-kernel] executed ok\n")
