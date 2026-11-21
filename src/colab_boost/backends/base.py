"""Abstract backend interface for Colab runtime operations."""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from colab_boost.models import Accelerator, MachineShape, UsageSnapshot

logger = logging.getLogger(__name__)


class ColabBackendError(RuntimeError):
    def __init__(self, kind: str, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code


@dataclass
class RawAssignment:
    endpoint: str
    accelerator: Accelerator
    machine_shape: MachineShape
    proxy_url: str
    proxy_token: str
    token_expires_in_seconds: int = 3600


@dataclass
class ExecResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error_name: Optional[str] = None
    error_value: Optional[str] = None
    marker_json: Optional[dict[str, Any]] = None


@dataclass
class HardwareProbe:
    gpu_name: Optional[str] = None
    vram_total_mib: int = 0
    vram_used_mib: int = 0
    gpu_util_percent: float = 0.0
    runtime_addr: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    probe_ok: bool = True
    probe_error: Optional[str] = None


RESULT_MARKER = "__CBOOST_JSON__ "


class ColabBackend(abc.ABC):
    @property
    @abc.abstractmethod
    def is_mock(self) -> bool:
        ...

    @abc.abstractmethod
    def begin_oauth_login(self) -> dict[str, str]:
        """Returns ``{"flow_id": ..., "auth_url": ...}``."""

    @abc.abstractmethod
    def complete_oauth_login(self, flow_id: str, auth_code: str) -> dict[str, str]:
        """Exchanges ``auth_code`` and returns ``{"email": ..., "display_name": ..., "token_json": ...}``."""

    @abc.abstractmethod
    def inspect_token(self, token_json: str) -> dict[str, str]:
        """Validates/refreshes a stored token JSON and returns ``{"email": ..., "display_name": ..., "token_json": ...}``."""

    @abc.abstractmethod
    def fetch_usage(self, account_id: str, token_json: str) -> UsageSnapshot:
        """Fetches CCU balance & burn rate for an account."""

    @abc.abstractmethod
    def list_assignments(self, account_id: str, token_json: str) -> list[RawAssignment]:
        """Lists all active Colab VM assignments on the account."""

    @abc.abstractmethod
    def assign_runtime(
        self,
        account_id: str,
        token_json: str,
        accelerator: Accelerator,
        high_mem: bool = False,
    ) -> RawAssignment:
        """Allocates a new Colab VM assignment."""

    @abc.abstractmethod
    def unassign_runtime(self, account_id: str, token_json: str, endpoint: str) -> bool:
        """Releases a Colab VM assignment and confirms removal."""

    @abc.abstractmethod
    def execute_python(
        self,
        assignment: RawAssignment,
        code: str,
        *,
        env: Optional[dict[str, str]] = None,
        timeout_s: float = 45.0,
    ) -> ExecResult:
        """Runs Python inside the Colab VM's Jupyter kernel (with store_history=False)."""

    def close(self) -> None:
        """Releases any pooled HTTP sessions or backend resources."""
        return None

    def probe_hardware(self, assignment: RawAssignment) -> HardwareProbe:
        snippet = (
            "import json, os, subprocess\n"
            "info = {'gpu_name': None, 'vram_total_mib': 0, 'vram_used_mib': 0, "
            "'gpu_util_percent': 0.0, 'runtime_addr': os.environ.get('TBE_RUNTIME_ADDR')}\n"
            "try:\n"
            "    out = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,memory.used,utilization.gpu', "
            "'--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=15, check=True).stdout.strip()\n"
            "    if out:\n"
            "        parts = [p.strip() for p in out.splitlines()[0].split(',')]\n"
            "        info['gpu_name'] = parts[0]\n"
            "        info['vram_total_mib'] = int(float(parts[1]))\n"
            "        info['vram_used_mib'] = int(float(parts[2]))\n"
            "        info['gpu_util_percent'] = float(parts[3])\n"
            "except Exception:\n"
            "    pass\n"
            f"print({RESULT_MARKER!r} + json.dumps(info), flush=True)\n"
        )
        res = self.execute_python(assignment, snippet, timeout_s=30.0)
        if not res.ok:
            err_parts = [p for p in (res.error_name, res.error_value) if p]
            err_detail = ": ".join(err_parts) if err_parts else (
                res.stderr.strip()[:300] or "Hardware probe execution failed"
            )
            logger.warning("Hardware probe failed on %s: %s", assignment.endpoint, err_detail)
            return HardwareProbe(
                extra={
                    "probe_status": "error",
                    "probe_error": err_detail,
                    "error_name": res.error_name,
                    "error_value": res.error_value,
                    "stderr": (res.stderr or "")[:500],
                },
                probe_ok=False,
                probe_error=err_detail,
            )
        if not isinstance(res.marker_json, dict):
            err_detail = "Hardware probe returned no structured marker output"
            logger.warning("Hardware probe missing marker output on %s", assignment.endpoint)
            return HardwareProbe(
                extra={
                    "probe_status": "missing_marker",
                    "probe_error": err_detail,
                    "stdout": (res.stdout or "")[:500],
                    "stderr": (res.stderr or "")[:500],
                },
                probe_ok=False,
                probe_error=err_detail,
            )
        data = res.marker_json
        return HardwareProbe(
            gpu_name=data.get("gpu_name"),
            vram_total_mib=int(data.get("vram_total_mib") or 0),
            vram_used_mib=int(data.get("vram_used_mib") or 0),
            gpu_util_percent=float(data.get("gpu_util_percent") or 0.0),
            runtime_addr=data.get("runtime_addr"),
            extra={"probe_status": "ok"},
            probe_ok=True,
            probe_error=None,
        )
