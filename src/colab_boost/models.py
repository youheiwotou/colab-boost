"""Domain models for Colab Boost Console."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BackendMode(str, Enum):
    MOCK = "mock"
    REAL = "real"


class Accelerator(str, Enum):
    NONE = "NONE"
    T4 = "T4"
    L4 = "L4"
    G4 = "G4"
    A100 = "A100"
    H100 = "H100"
    V5E1 = "V5E1"
    V6E1 = "V6E1"


class MachineShape(str, Enum):
    STANDARD = "STANDARD"
    HIGH_RAM = "HIGH_RAM"


HIGH_MEM_ONLY_ACCELERATORS = frozenset({
    Accelerator.L4,
    Accelerator.V5E1,
    Accelerator.V6E1,
})

GPU_ACCELERATORS = frozenset({
    Accelerator.T4,
    Accelerator.L4,
    Accelerator.G4,
    Accelerator.A100,
    Accelerator.H100,
})

TPU_ACCELERATORS = frozenset({
    Accelerator.V5E1,
    Accelerator.V6E1,
})

# Typical Colab GPU VRAM reference sizes (MiB) for planning / UI matrix
ACCELERATOR_VRAM_REFERENCE_MIB: dict[str, list[int]] = {
    "T4": [15360],
    "G4": [97887],
    "L4": [23034],
    "A100": [40960, 81920],
    "H100": [81920],
}

# Approximate CCU/hr reference burn rates when Colab rate has not updated yet
APPROX_CCU_PER_HOUR: dict[str, float] = {
    "NONE": 0.07,
    "T4": 1.17,
    "G4": 1.80,
    "L4": 2.82,
    "A100": 6.93,
    "H100": 11.78,
    "V5E1": 2.11,
    "V6E1": 3.55,
}


class UsageSnapshot(BaseModel):
    """Account compute-unit balance & burn rate.

    Never coerces unknown data to 0: when fetch fails, ``status`` is ``unknown``
    or ``stale`` so cost guards report honestly.
    """

    status: Literal["live", "stale", "unknown"] = "unknown"
    balance_ccu: Optional[float] = None
    burn_rate_hourly: Optional[float] = None
    assignments_count: Optional[int] = None
    hours_remaining: Optional[float] = None
    eligible_gpus: list[str] = Field(default_factory=list)
    fetched_at: Optional[datetime] = None
    stale_reason: Optional[str] = None


class AccountRecord(BaseModel):
    id: str
    email: str
    display_name: str = ""
    added_at: datetime
    is_active: bool = False
    has_credential: bool = True
    usage: UsageSnapshot = Field(default_factory=UsageSnapshot)


class CostGuardSettings(BaseModel):
    auto_stop_minutes: int = Field(default=120, ge=0, le=1440)
    idle_stop_minutes: int = Field(default=30, ge=0, le=720)
    balance_floor_ccu: float = Field(default=5.0, ge=0.0, le=10000.0)
    session_spend_cap_ccu: float = Field(default=20.0, ge=0.0, le=10000.0)
    max_vram_retries: int = Field(default=4, ge=1, le=8)
    max_managed_sessions: int = Field(default=3, ge=1, le=5)


class AppSettings(BaseModel):
    backend_mode: BackendMode = BackendMode.MOCK
    mock_scenario: Literal["demo", "single", "signed_out"] = "demo"
    language: Literal["zh-CN", "en"] = "zh-CN"
    theme: Literal["dark", "light"] = "dark"
    default_accelerator: Accelerator = Accelerator.A100
    default_high_mem: bool = False
    default_min_vram_mib: int = 0
    guards: CostGuardSettings = Field(default_factory=CostGuardSettings)
    auto_forward_endpoints: bool = True


class WatchdogState(BaseModel):
    active: bool = False
    deadline_epoch: Optional[float] = None
    idle_timeout_s: Optional[int] = None
    winddown_active: bool = False
    keep_paths: list[str] = Field(default_factory=list)
    warning: Optional[str] = None


class SessionInfo(BaseModel):
    name: str
    endpoint: str
    account_id: str
    account_email: str
    accelerator: Accelerator = Accelerator.NONE
    machine_shape: MachineShape = MachineShape.STANDARD
    gpu_name: Optional[str] = None
    vram_total_mib: Optional[int] = None
    vram_used_mib: Optional[int] = None
    gpu_util_percent: Optional[float] = None
    managed: bool = True
    status: Literal["provisioning", "running", "stopping", "lost"] = "running"
    created_at: Optional[datetime] = None
    estimated_burn_hourly: float = 0.0
    estimated_spent_ccu: float = 0.0
    runtime_url: Optional[str] = None
    watchdog: WatchdogState = Field(default_factory=WatchdogState)
    active_run_id: Optional[str] = None
    tunnels: list["TunnelInfo"] = Field(default_factory=list)


class TunnelInfo(BaseModel):
    id: str
    session_name: str
    account_id: str
    remote_port: int
    local_port: int
    label: str = "Port Forward"
    labels: dict[str, str] = Field(default_factory=dict)
    path: str = "/"
    kind: str = "http"
    status: Literal["starting", "open", "error", "closed"] = "open"
    local_url: str = ""
    error: Optional[str] = None
    created_at: datetime


class ProvisionRequest(BaseModel):
    name: Optional[str] = None
    account_id: Optional[str] = None
    accelerator: Accelerator = Accelerator.A100
    high_mem: bool = False
    min_vram_mib: int = Field(default=0, ge=0, le=163840)
    auto_stop_minutes: Optional[int] = Field(default=None, ge=0, le=1440)
    idle_stop_minutes: Optional[int] = Field(default=None, ge=0, le=720)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if len(v) > 64:
            raise ValueError("Session name must be at most 64 characters.")
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in v):
            raise ValueError("Session name must not contain whitespace or control characters.")
        if any(bad in v for bad in ("/", "\\", "..", "?", "#")):
            raise ValueError("Session name must not contain '/', '\\\\', '..', '?', or '#'.")
        if v in (".", ".."):
            raise ValueError("Session name must not be '.' or '..'.")
        return v


class ProvisionOperation(BaseModel):
    op_id: str
    session_name: str
    account_id: str
    accelerator: Accelerator
    min_vram_mib: int = 0
    attempt: int = 1
    max_attempts: int = 4
    state: Literal["allocating", "probing", "retrying", "watchdog", "ready", "cancelled", "failed"] = "allocating"
    message: str = ""
    started_at: datetime
    finished_at: Optional[datetime] = None
    error: Optional[str] = None


class UnassignReceipt(BaseModel):
    """Verifiable proof that a Colab runtime was unassigned and billing stopped."""

    receipt_id: str
    timestamp: datetime
    account_id: str
    account_email: str
    session_name: str
    endpoint: str
    accelerator: str
    reason: str
    started_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    estimated_ccu_spent: Optional[float] = None
    balance_after_ccu: Optional[float] = None
    verified_unassigned: bool = True
    backend_mode: BackendMode = BackendMode.MOCK


# --------------------- Declarative Recipe Models ---------------------


class RecipeInputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["text", "number", "bool", "select", "secret", "hf-gguf", "hf-repo"]
    required: bool = False
    default: Optional[Any] = None
    options: Optional[list[str]] = None
    min: Optional[int] = None
    max: Optional[int] = None
    label: dict[str, str]
    help: dict[str, str] = Field(default_factory=dict)


class RecipeStageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: dict[str, str]


class RecipeEndpointSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    port: Optional[int] = None
    port_input: Optional[str] = None
    path: str = "/"
    health: Optional[str] = None
    kind: str = "http"
    label: dict[str, str]


class RecipeProvisionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accelerator: Accelerator = Accelerator.A100
    min_vram_mib: int = 0
    high_mem: bool = False


class RecipeRunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: str


class RecipeKeepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    label: dict[str, str]


class RecipeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api: int
    id: str
    version: str
    category: Literal["llm", "vision", "audio", "custom"] = "llm"
    tags: list[str] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)
    name: dict[str, str]
    description: dict[str, str] = Field(default_factory=dict)
    provision: RecipeProvisionSpec = Field(default_factory=RecipeProvisionSpec)
    inputs: list[RecipeInputSpec] = Field(default_factory=list)
    run: RecipeRunSpec
    stages: list[RecipeStageSpec] = Field(default_factory=list)
    endpoints: list[RecipeEndpointSpec] = Field(default_factory=list)
    keep: list[RecipeKeepSpec] = Field(default_factory=list)
    bundled: bool = True


class DeployRequest(BaseModel):
    recipe_id: str
    session_name: Optional[str] = None  # Reuse existing session or None to provision fresh
    provision: Optional[ProvisionRequest] = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    stop_session_on_failure: bool = True


class KeptArtifactInfo(BaseModel):
    path: str
    label: dict[str, str] = Field(default_factory=dict)
    file_count: int = 0
    total_bytes: int = 0
    drive_synced: bool = False
    local_archive: Optional[str] = None
    synced_at: Optional[datetime] = None


class DeploymentRun(BaseModel):
    run_id: str
    recipe_id: str
    recipe_name: dict[str, str]
    account_id: str
    session_name: str
    session_endpoint: Optional[str] = None
    provisioned_session: bool = False
    stop_session_on_failure: bool = True
    endpoint_ports: dict[str, int] = Field(default_factory=dict)
    state: Literal["provisioning", "starting", "running", "ready", "failed", "cancelled"] = "starting"
    stage: str = ""
    progress: Optional[float] = None
    message: str = ""
    outputs: dict[str, Any] = Field(default_factory=dict)
    log_tail: list[str] = Field(default_factory=list)
    endpoints: list[TunnelInfo] = Field(default_factory=list)
    keep_paths: list[KeptArtifactInfo] = Field(default_factory=list)
    started_at: datetime
    finished_at: Optional[datetime] = None


SessionInfo.model_rebuild()
