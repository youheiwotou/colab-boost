"""Central coordinator for accounts, cost-guarded GPU sessions, deployments, tunnels & receipts."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from colab_boost.backends.base import (
    RESULT_MARKER,
    ColabBackend,
    ColabBackendError,
    HardwareProbe,
    RawAssignment,
)
from colab_boost.backends.direct import DirectColabBackend
from colab_boost.backends.mock import MockColabBackend
from colab_boost.config import SettingsStore, WorkspacePaths
from colab_boost.models import (
    APPROX_CCU_PER_HOUR,
    AccountRecord,
    AppSettings,
    BackendMode,
    DeployRequest,
    DeploymentRun,
    KeptArtifactInfo,
    ProvisionOperation,
    ProvisionRequest,
    RecipeKeepSpec,
    RecipeManifest,
    SessionInfo,
    TunnelInfo,
    UnassignReceipt,
    UsageSnapshot,
    WatchdogState,
)
from colab_boost.recipe_loader import (
    LoadedRecipe,
    discover_all_recipes,
    validate_and_build_env,
)
from colab_boost.ssh_bridge import SshTunnelManager
from colab_boost.vault import CredentialVault, redact_secrets

logger = logging.getLogger(__name__)


def _path_is_within(candidate: Path, root: Path) -> bool:
    """Case-aware, drive-safe containment check (True if candidate == root or inside root)."""
    try:
        c = os.path.normcase(os.path.normpath(str(candidate)))
        r = os.path.normcase(os.path.normpath(str(root)))
        return os.path.commonpath([c, r]) == r
    except ValueError:
        return False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _watchdog_source() -> str:
    p = Path(__file__).resolve().parent / "vm_assets" / "watchdog.py"
    return p.read_text(encoding="utf-8")


def _sanitize_keep_slug(path: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", path.strip("/"))
    return cleaned[:64] or "kept_artifacts"


def _build_keep_winddown_hooks(
    run_id: str, session_name: str, keep_specs: list[RecipeKeepSpec]
) -> list[dict[str, Any]]:
    if not keep_specs:
        return []
    spec_items = [{"path": kp.path, "slug": _sanitize_keep_slug(kp.path)} for kp in keep_specs]
    hook_script = (
        "import json, os, shutil, tarfile, time\n"
        f"specs = {spec_items!r}\n"
        "keep_root = os.environ.get('BOOST_KEEP_STAGE_DIR', '/root/.colab-boost/keep')\n"
        f"stage_root = os.path.join(keep_root, {run_id!r})\n"
        "os.makedirs(stage_root, exist_ok=True)\n"
        "drive_mount = os.environ.get('BOOST_DRIVE_MOUNT', '/content/drive/MyDrive')\n"
        f"drive_base = os.path.join(drive_mount, 'ColabBoost', {session_name!r}, {run_id!r})\n"
        "has_drive = os.path.isdir(drive_mount)\n"
        "if has_drive:\n"
        "    os.makedirs(drive_base, exist_ok=True)\n"
        "def _tar_filter(ti):\n"
        "    if ti.issym() and (ti.linkname.startswith('/') or '..' in ti.linkname.split('/')):\n"
        "        return None\n"
        "    if not (ti.isfile() or ti.isdir() or ti.issym()):\n"
        "        return None\n"
        "    return ti\n"
        f"manifest = {{'run_id': {run_id!r}, 'saved_at': time.time(), 'items': []}}\n"
        "for item in specs:\n"
        "    p = item['path']\n"
        "    slug = item['slug']\n"
        "    entry = {'path': p, 'exists': os.path.lexists(p), 'file_count': 0, 'total_bytes': 0, 'drive_synced': False}\n"
        "    if os.path.lexists(p):\n"
        "        if os.path.isfile(p) and not os.path.islink(p):\n"
        "            entry['file_count'] = 1\n"
        "            entry['total_bytes'] = os.path.getsize(p)\n"
        "        elif os.path.isdir(p) and not os.path.islink(p):\n"
        "            for r, _, fs in os.walk(p, followlinks=False):\n"
        "                for fn in fs:\n"
        "                    fp = os.path.join(r, fn)\n"
        "                    if os.path.islink(fp):\n"
        "                        continue\n"
        "                    try:\n"
        "                        if os.path.isfile(fp):\n"
        "                            entry['file_count'] += 1\n"
        "                            entry['total_bytes'] += os.path.getsize(fp)\n"
        "                    except OSError:\n"
        "                        pass\n"
        "        arc_path = os.path.join(stage_root, slug + '.tar.gz')\n"
        "        tmp_arc = arc_path + '.tmp'\n"
        "        try:\n"
        "            with tarfile.open(tmp_arc, 'w:gz') as tf:\n"
        "                tf.add(p, arcname=os.path.basename(p.rstrip('/')) or slug, recursive=True, filter=_tar_filter)\n"
        "            os.replace(tmp_arc, arc_path)\n"
        "            entry['archive'] = arc_path\n"
        "            if has_drive:\n"
        "                shutil.copy2(arc_path, os.path.join(drive_base, slug + '.tar.gz'))\n"
        "                entry['drive_synced'] = True\n"
        "        except Exception as exc:\n"
        "            try: os.remove(tmp_arc)\n"
        "            except OSError: pass\n"
        "            entry['error'] = str(exc)\n"
        "    manifest['items'].append(entry)\n"
        "with open(os.path.join(stage_root, 'keep_manifest.json'), 'w', encoding='utf-8') as fh:\n"
        "    json.dump(manifest, fh)\n"
    )
    return [{"argv": ["python3", "-c", hook_script], "timeout_s": 120}]


class ConsoleOrchestrator:
    def __init__(
        self,
        paths: Optional[WorkspacePaths] = None,
        *,
        direct_backend: Optional[ColabBackend] = None,
        mock_backend: Optional[MockColabBackend] = None,
        force_file_vault: bool = False,
        mock_step_delay_s: float = 0.45,
    ) -> None:
        self.paths = paths or WorkspacePaths()
        self.paths.ensure_dirs()
        self.settings_store = SettingsStore(self.paths)
        self.settings: AppSettings = self.settings_store.load()
        self.vault = CredentialVault(self.paths, force_file_backend=force_file_vault)
        self.tunnels = SshTunnelManager(self.paths)
        self.tunnels.relocalize_labels(self.settings.language)

        self._direct_backend: ColabBackend = direct_backend or DirectColabBackend(
            on_token_refreshed=self._on_real_token_refreshed
        )
        if mock_backend is not None and not self.paths.settings_file.exists():
            self.settings.mock_scenario = mock_backend.scenario  # type: ignore[assignment]
        self._mock_backend: MockColabBackend = mock_backend or MockColabBackend(self.settings.mock_scenario)
        self.mock_step_delay_s = mock_step_delay_s

        self._lock = threading.RLock()
        self._shutdown_ev = threading.Event()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

        # Real & mock account registries
        self._real_accounts: dict[str, AccountRecord] = self._load_real_accounts()
        self._mock_accounts: dict[str, AccountRecord] = {}
        self._mock_tokens: dict[str, str] = {}

        # Managed session ledger (keyed by endpoint)
        self._real_ledger: dict[str, dict[str, Any]] = self._load_real_ledger()
        self._mock_ledger: dict[str, dict[str, Any]] = {}

        # Live operations & deployment runs (separated between real & mock modes)
        self._provision_ops: dict[str, ProvisionOperation] = {}
        self._provision_cancels: dict[str, threading.Event] = {}
        self._provision_done: dict[str, threading.Event] = {}
        self._inflight_stops: dict[str, tuple[threading.Event, list[UnassignReceipt]]] = {}
        self._real_runs: dict[str, DeploymentRun] = self._load_real_runs()
        self._mock_runs: dict[str, DeploymentRun] = {}
        self._run_cancels: dict[str, threading.Event] = {}
        self._receipts: list[UnassignReceipt] = self._load_recent_receipts()

        self._init_mock_state()
        if self.settings.backend_mode == BackendMode.REAL:
            self._reconcile_real_runs()

    def shutdown(self) -> None:
        """Stops local background pollers and SSH tunnels without terminating remote Colab VMs."""
        self._shutdown_ev.set()
        self.tunnels.stop_all()

    def _localized_label(self, labels: dict[str, str], fallback: str = "") -> str:
        lang = self.settings.language
        return labels.get(lang) or labels.get("en") or labels.get("zh-CN") or fallback

    def _on_real_token_refreshed(self, account_id: str, refreshed_token_json: str) -> None:
        self.vault.set_account_token(account_id, refreshed_token_json)

    # -------------------- Persistence Helpers --------------------

    @property
    def backend(self) -> ColabBackend:
        return self._mock_backend if self.settings.backend_mode == BackendMode.MOCK else self._direct_backend

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    def _emit(self, event_type: str, payload: Optional[dict[str, Any]] = None) -> None:
        with self._lock:
            subs = list(self._listeners)
        msg = {"type": event_type, "timestamp": _utc_now().isoformat(), "data": payload or {}}
        for cb in subs:
            try:
                cb(msg)
            except Exception:
                pass

    def _load_real_accounts(self) -> dict[str, AccountRecord]:
        if not self.paths.accounts_file.exists():
            return {}
        try:
            raw = json.loads(self.paths.accounts_file.read_text(encoding="utf-8"))
            out: dict[str, AccountRecord] = {}
            for item in raw if isinstance(raw, list) else []:
                rec = AccountRecord.model_validate(item)
                rec.has_credential = self.vault.has_account_token(rec.id)
                out[rec.id] = rec
            return out
        except Exception:
            return {}

    def _save_real_accounts(self) -> None:
        items = [
            rec.model_dump(mode="json", exclude={"usage"})
            for rec in self._real_accounts.values()
        ]
        tmp = self.paths.accounts_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.paths.accounts_file)

    def _load_real_ledger(self) -> dict[str, dict[str, Any]]:
        if not self.paths.ledger_file.exists():
            return {}
        try:
            data = json.loads(self.paths.ledger_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_real_ledger(self) -> None:
        tmp = self.paths.ledger_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._real_ledger, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.paths.ledger_file)

    def _load_real_runs(self, limit: int = 50) -> dict[str, DeploymentRun]:
        if not self.paths.runs_file.exists():
            return {}
        try:
            raw = json.loads(self.paths.runs_file.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return {}
            out: dict[str, DeploymentRun] = {}
            for item in raw[-limit:]:
                run = DeploymentRun.model_validate(item)
                out[run.run_id] = run
            return out
        except Exception:
            return {}

    def _save_real_runs(self) -> None:
        items = [r.model_dump(mode="json") for r in list(self._real_runs.values())[-50:]]
        tmp = self.paths.runs_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.paths.runs_file)

    def _load_recent_receipts(self, limit: int = 50) -> list[UnassignReceipt]:
        files = sorted(self.paths.receipts_dir.glob("receipt_*.json"), reverse=True)[:limit]
        out: list[UnassignReceipt] = []
        for fp in files:
            try:
                out.append(UnassignReceipt.model_validate_json(fp.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def _record_receipt(self, receipt: UnassignReceipt) -> UnassignReceipt:
        self.paths.ensure_dirs()
        stamp = receipt.timestamp.strftime("%Y%m%d_%H%M%S_%f")
        fp = self.paths.receipts_dir / f"receipt_{stamp}_{receipt.receipt_id}.json"
        fp.write_text(
            json.dumps(receipt.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        with self._lock:
            self._receipts.insert(0, receipt)
            self._receipts = self._receipts[:100]
        self._emit("receipt_recorded", receipt.model_dump(mode="json"))
        return receipt

    def _init_mock_state(self) -> None:
        with self._lock:
            self._mock_backend.reset_scenario(self.settings.mock_scenario)
            self._mock_accounts.clear()
            self._mock_tokens.clear()
            self._mock_ledger.clear()
            self._mock_runs.clear()
            now = _utc_now()
            for item in self._mock_backend.seed_demo_accounts():
                aid = item["id"]
                self._mock_tokens[aid] = item["token_json"]
                usage = self._mock_backend.fetch_usage(aid, item["token_json"])
                self._mock_accounts[aid] = AccountRecord(
                    id=aid,
                    email=item["email"],
                    display_name=item["display_name"],
                    added_at=now,
                    is_active=item["is_active"],
                    has_credential=True,
                    usage=usage,
                )
            if self.settings.mock_scenario == "demo":
                # Register the demo A100 VM in mock ledger while leaving T4 as untracked browser orphan
                self._mock_ledger["m-s-a100-demo80g"] = {
                    "name": "boost-a100-qwen",
                    "account_id": "acc-pro-main",
                    "created_at": now.isoformat(),
                    "gpu_name": "NVIDIA A100-SXM4-80GB",
                    "vram_total_mib": 81920,
                    "watchdog": {
                        "active": True,
                        "deadline_epoch": time.time() + 7200,
                        "idle_timeout_s": 1800,
                    },
                }

    # -------------------- Settings & Mode Switching --------------------

    def get_settings(self) -> AppSettings:
        with self._lock:
            return self.settings.model_copy(deep=True)

    def update_settings(self, patch: dict[str, Any]) -> AppSettings:
        with self._lock:
            merged = self.settings.model_dump(mode="json")
            if "guards" in patch and isinstance(patch["guards"], dict):
                merged["guards"] = {**merged["guards"], **patch["guards"]}
                patch = {k: v for k, v in patch.items() if k != "guards"}
            merged.update(patch)
            prev_mode = self.settings.backend_mode
            prev_scenario = self.settings.mock_scenario
            prev_lang = self.settings.language
            new_settings = AppSettings.model_validate(merged)
            self.settings = self.settings_store.save(new_settings)
            if prev_mode != new_settings.backend_mode:
                self.tunnels.stop_all()
            if prev_lang != new_settings.language:
                self.tunnels.relocalize_labels(new_settings.language)
                for run_map in (self._real_runs, self._mock_runs):
                    for r in run_map.values():
                        for ep in r.endpoints:
                            if ep.labels:
                                ep.label = self._localized_label(ep.labels, ep.label)
                self._save_real_runs()
            if new_settings.backend_mode == BackendMode.MOCK and (
                prev_mode != BackendMode.MOCK or prev_scenario != new_settings.mock_scenario
            ):
                self._init_mock_state()
            switched_to_real = new_settings.backend_mode == BackendMode.REAL and prev_mode != BackendMode.REAL
        if switched_to_real:
            self._reconcile_real_runs()
        self._emit("settings_updated", self.settings.model_dump(mode="json"))
        return self.get_settings()

    # -------------------- Accounts & Usage --------------------

    def _accounts_dict(self) -> dict[str, AccountRecord]:
        return self._mock_accounts if self.backend.is_mock else self._real_accounts

    def _ledger_dict(self) -> dict[str, dict[str, Any]]:
        return self._mock_ledger if self.backend.is_mock else self._real_ledger

    def _runs_dict(self) -> dict[str, DeploymentRun]:
        return self._mock_runs if self.backend.is_mock else self._real_runs

    def _persist_ledger_if_real(self) -> None:
        if not self.backend.is_mock:
            self._save_real_ledger()

    def _persist_runs_if_real(self) -> None:
        if not self.backend.is_mock:
            self._save_real_runs()

    def _get_token_for_account(self, account_id: str) -> str:
        if self.backend.is_mock:
            tok = self._mock_tokens.get(account_id)
        else:
            tok = self.vault.get_account_token(account_id)
        if not tok:
            raise ColabBackendError("missing_credential", f"No stored credential for account '{account_id}'.")
        return tok

    def list_accounts(self, refresh_usage: bool = False) -> list[AccountRecord]:
        with self._lock:
            accts = list(self._accounts_dict().values())
        if refresh_usage:
            for acc in accts:
                self.refresh_account_usage(acc.id)
            with self._lock:
                accts = list(self._accounts_dict().values())
        return [a.model_copy(deep=True) for a in accts]

    def active_account(self) -> Optional[AccountRecord]:
        with self._lock:
            accts = self._accounts_dict()
            for a in accts.values():
                if a.is_active:
                    return a.model_copy(deep=True)
            if accts:
                first = next(iter(accts.values()))
                first.is_active = True
                return first.model_copy(deep=True)
            return None

    def refresh_account_usage(self, account_id: str) -> UsageSnapshot:
        with self._lock:
            acc = self._accounts_dict().get(account_id)
            if not acc:
                raise KeyError(f"Unknown account: {account_id}")
            prev = acc.usage.model_copy(deep=True)
        try:
            tok = self._get_token_for_account(account_id)
            fresh = self.backend.fetch_usage(account_id, tok)
        except Exception as exc:
            # Never overwrite unknown/failed usage with 0! Mark stale honestly.
            stale = UsageSnapshot(
                status="stale" if prev.balance_ccu is not None else "unknown",
                balance_ccu=prev.balance_ccu,
                burn_rate_hourly=prev.burn_rate_hourly,
                assignments_count=prev.assignments_count,
                hours_remaining=prev.hours_remaining,
                eligible_gpus=prev.eligible_gpus,
                fetched_at=prev.fetched_at,
                stale_reason=f"{type(exc).__name__}: {exc}",
            )
            with self._lock:
                if account_id in self._accounts_dict():
                    self._accounts_dict()[account_id].usage = stale
            return stale

        with self._lock:
            if account_id in self._accounts_dict():
                self._accounts_dict()[account_id].usage = fresh
        return fresh

    def begin_account_login(self) -> dict[str, str]:
        return self.backend.begin_oauth_login()

    def complete_account_login(self, flow_id: str, code: str) -> AccountRecord:
        info = self.backend.complete_oauth_login(flow_id, code)
        return self._register_authenticated_account(info["email"], info["display_name"], info["token_json"])

    def import_colab_cli_credential(self) -> AccountRecord:
        if self.backend.is_mock:
            raise ColabBackendError("mock_mode", "Switch to Real Colab Mode before importing local colab-cli credentials.")
        raw_tok = self.vault.read_existing_colab_cli_token()
        if not raw_tok:
            raise ColabBackendError("no_cli_token", "No existing google-colab-cli OAuth token found in OS Credential Manager.")
        info = self.backend.inspect_token(raw_tok)
        return self._register_authenticated_account(info["email"], info["display_name"], info["token_json"])

    def _register_authenticated_account(self, email: str, display_name: str, token_json: str) -> AccountRecord:
        with self._lock:
            accts = self._accounts_dict()
            existing = next((a for a in accts.values() if a.email.lower() == email.lower()), None)
            aid = existing.id if existing else f"acc-{uuid.uuid4().hex[:8]}"
            for a in accts.values():
                a.is_active = False
            if self.backend.is_mock:
                self._mock_tokens[aid] = token_json
            else:
                self.vault.set_account_token(aid, token_json)
            rec = AccountRecord(
                id=aid,
                email=email,
                display_name=display_name or email.split("@")[0],
                added_at=existing.added_at if existing else _utc_now(),
                is_active=True,
                has_credential=True,
            )
            accts[aid] = rec
            if not self.backend.is_mock:
                self._save_real_accounts()
        self.refresh_account_usage(aid)
        self._emit("accounts_changed", {"active_account_id": aid})
        with self._lock:
            return self._accounts_dict()[aid].model_copy(deep=True)

    def switch_active_account(self, account_id: str) -> dict[str, Any]:
        with self._lock:
            accts = self._accounts_dict()
            if account_id not in accts:
                raise KeyError(f"Unknown account: {account_id}")
            prev_active = next((a.id for a in accts.values() if a.is_active), None)
            for a in accts.values():
                a.is_active = (a.id == account_id)
            if not self.backend.is_mock:
                self._save_real_accounts()

        closed_tunnels = 0
        if prev_active and prev_active != account_id:
            for t in self.tunnels.list_tunnels():
                if t.account_id == prev_active:
                    if self.tunnels.stop_tunnel(t.id):
                        closed_tunnels += 1

        self.refresh_account_usage(account_id)
        self._emit("accounts_changed", {"active_account_id": account_id, "closed_tunnels": closed_tunnels})
        return {"active_account_id": account_id, "closed_tunnels": closed_tunnels}

    def remove_account(self, account_id: str) -> bool:
        with self._lock:
            accts = self._accounts_dict()
            removed = accts.pop(account_id, None)
            if not removed:
                return False
            if self.backend.is_mock:
                self._mock_tokens.pop(account_id, None)
            else:
                self.vault.delete_account_token(account_id)
            if removed.is_active and accts:
                next(iter(accts.values())).is_active = True
            if not self.backend.is_mock:
                self._save_real_accounts()
        for t in self.tunnels.list_tunnels():
            if t.account_id == account_id:
                self.tunnels.stop_tunnel(t.id)
        self._emit("accounts_changed", {"removed_account_id": account_id})
        return True

    # -------------------- Sessions, Orphans & Cost Guards --------------------

    def list_sessions(self, include_all_accounts: bool = True) -> list[SessionInfo]:
        sessions, _ = self.list_sessions_with_errors(include_all_accounts=include_all_accounts)
        return sessions

    def list_sessions_with_errors(
        self, include_all_accounts: bool = True
    ) -> tuple[list[SessionInfo], list[dict[str, str]]]:
        with self._lock:
            accts = list(self._accounts_dict().values())
            ledger = self._ledger_dict()

        target_accounts = accts if include_all_accounts else [a for a in accts if a.is_active]
        all_tunnels = self.tunnels.list_tunnels()
        sessions: list[SessionInfo] = []
        failed_accounts: list[dict[str, str]] = []

        for acc in target_accounts:
            fetch_started = _utc_now()
            try:
                tok = self._get_token_for_account(acc.id)
                assignments = self.backend.list_assignments(acc.id, tok)
            except Exception as exc:
                logger.warning("Failed listing assignments for account %s (%s): %s", acc.id, acc.email, exc)
                failed_accounts.append(
                    {
                        "account_id": acc.id,
                        "email": acc.email,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue

            live_endpoints = {a.endpoint for a in assignments}
            # Prune vanished endpoints from ledger only if created before fetch_started
            with self._lock:
                stale_eps: list[str] = []
                for ep, meta in ledger.items():
                    if meta.get("account_id") != acc.id or ep in live_endpoints:
                        continue
                    try:
                        created_dt = datetime.fromisoformat(str(meta.get("created_at") or ""))
                        if created_dt >= fetch_started:
                            continue
                    except ValueError:
                        pass
                    stale_eps.append(ep)
                for ep in stale_eps:
                    ledger.pop(ep, None)
                if stale_eps:
                    self._persist_ledger_if_real()
                ledger_snap = {ep: dict(meta) for ep, meta in ledger.items()}

            for raw in assignments:
                meta = ledger_snap.get(raw.endpoint)
                managed = meta is not None
                name = str(meta["name"]) if meta and meta.get("name") else f"orphan-{raw.endpoint[-8:]}"
                if not self.backend.is_mock:
                    self.tunnels.update_session_credentials(name, raw.proxy_url, raw.proxy_token)
                created_at: Optional[datetime] = None
                if meta and meta.get("created_at"):
                    try:
                        created_at = datetime.fromisoformat(meta["created_at"])
                    except ValueError:
                        created_at = None

                burn = APPROX_CCU_PER_HOUR.get(raw.accelerator.value, 1.0)
                spent = 0.0
                if created_at is not None:
                    elapsed_h = max(0.0, (_utc_now() - created_at).total_seconds() / 3600.0)
                    spent = round(elapsed_h * burn, 2)

                wd_raw = (meta or {}).get("watchdog") or {}
                dl = wd_raw.get("deadline_epoch")
                winddown = bool(dl and (dl - 300) <= time.time() < dl)
                untracked_msg = (
                    "Untracked runtime (not started by console — no VM watchdog)"
                    if self.settings.language == "en"
                    else "未受管实例（非本控制台创建，未挂载自动关机看门狗）"
                )
                wd = WatchdogState(
                    active=bool(wd_raw.get("active", False)),
                    deadline_epoch=dl,
                    idle_timeout_s=wd_raw.get("idle_timeout_s"),
                    winddown_active=winddown,
                    keep_paths=list(wd_raw.get("keep_paths") or []),
                    warning=None if managed else untracked_msg,
                )

                gpu_name = (meta or {}).get("gpu_name")
                vram_total = (meta or {}).get("vram_total_mib")
                vram_used = (meta or {}).get("vram_used_mib")
                gpu_util = (meta or {}).get("gpu_util_percent")
                if self.backend.is_mock:
                    try:
                        hp = self.backend.probe_hardware(raw)
                        gpu_name = hp.gpu_name
                        vram_total = hp.vram_total_mib or None
                        vram_used = hp.vram_used_mib
                        gpu_util = hp.gpu_util_percent
                    except Exception:
                        pass

                sess_tunnels = [t for t in all_tunnels if t.session_name == name]
                with self._lock:
                    active_run_id = next(
                        (
                            r.run_id
                            for r in self._runs_dict().values()
                            if r.session_name == name and r.state in ("starting", "running", "ready")
                        ),
                        None,
                    )

                sessions.append(
                    SessionInfo(
                        name=name,
                        endpoint=raw.endpoint,
                        account_id=acc.id,
                        account_email=acc.email,
                        accelerator=raw.accelerator,
                        machine_shape=raw.machine_shape,
                        gpu_name=gpu_name,
                        vram_total_mib=vram_total,
                        vram_used_mib=vram_used,
                        gpu_util_percent=gpu_util,
                        managed=managed,
                        status="running",
                        created_at=created_at,
                        estimated_burn_hourly=burn,
                        estimated_spent_ccu=spent,
                        runtime_url=raw.proxy_url,
                        watchdog=wd,
                        active_run_id=active_run_id,
                        tunnels=sess_tunnels,
                    )
                )

        return sessions, failed_accounts

    def _install_vm_watchdog(
        self,
        assignment: RawAssignment,
        probe: HardwareProbe,
        auto_stop_minutes: int,
        idle_stop_minutes: int,
        hooks: Optional[list[dict[str, Any]]] = None,
        keep_paths: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        now_epoch = time.time()
        deadline = (now_epoch + auto_stop_minutes * 60.0) if auto_stop_minutes > 0 else None
        idle_s = (idle_stop_minutes * 60) if idle_stop_minutes > 0 else None
        hook_list = list(hooks or [])
        kept = list(keep_paths or [])
        wd_cfg = {
            "deadline_epoch": deadline,
            "idle_timeout_s": idle_s,
            "cancelled": False,
            "runtime_addr": probe.runtime_addr,
            "heartbeat_file": "/root/.colab-boost/heartbeat",
            "log_file": "/root/.colab-boost/watchdog/watchdog.log",
            "poll_s": 15,
            "grace_s": 180,
            "started_at": now_epoch,
            "hooks": hook_list,
        }
        if self.backend.is_mock:
            return {
                "active": bool(deadline or idle_s),
                "deadline_epoch": deadline,
                "idle_timeout_s": idle_s,
                "keep_paths": kept,
            }

        if not deadline and not idle_s:
            return {"active": False, "deadline_epoch": None, "idle_timeout_s": None, "keep_paths": kept}

        src = _watchdog_source()
        cfg_json = json.dumps(wd_cfg)
        hooks_json = json.dumps(hook_list)
        launcher = (
            "import json, os, subprocess, sys\n"
            "wd_dir = '/root/.colab-boost/watchdog'\n"
            "os.makedirs(wd_dir, exist_ok=True)\n"
            f"open(wd_dir + '/watchdog.py', 'w', encoding='utf-8').write({src!r})\n"
            f"open(wd_dir + '/watchdog.json', 'w', encoding='utf-8').write({cfg_json!r})\n"
            "p = subprocess.Popen("
            "[sys.executable, wd_dir + '/watchdog.py', '--config', wd_dir + '/watchdog.json', '--hooks-stdin'], "
            "stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, start_new_session=True)\n"
            "if p.stdin:\n"
            f"    p.stdin.write({hooks_json!r})\n"
            "    p.stdin.close()\n"
            f"print({RESULT_MARKER!r} + json.dumps({{'pid': p.pid}}), flush=True)\n"
        )
        res = self.backend.execute_python(assignment, launcher, timeout_s=30.0)
        ok = bool(res.ok and res.marker_json and res.marker_json.get("pid"))
        return {
            "active": ok,
            "deadline_epoch": deadline if ok else None,
            "idle_timeout_s": idle_s if ok else None,
            "keep_paths": kept,
        }

    def provision_session(
        self, req: ProvisionRequest, *, cancel_ev: Optional[threading.Event] = None
    ) -> SessionInfo:
        op_id, effective_cancel = self._create_provision_op(req, cancel_ev=cancel_ev)
        return self._run_provision_sync(op_id, req, effective_cancel)

    def start_provision_async(self, req: ProvisionRequest) -> ProvisionOperation:
        op_id, cancel_ev = self._create_provision_op(req)
        with self._lock:
            op = self._provision_ops[op_id].model_copy(deep=True)
        threading.Thread(target=self._run_provision_thread, args=(op_id, req, cancel_ev), daemon=True).start()
        return op

    def _run_provision_thread(self, op_id: str, req: ProvisionRequest, cancel_ev: threading.Event) -> None:
        try:
            self._run_provision_sync(op_id, req, cancel_ev)
        except Exception:
            pass

    def _create_provision_op(
        self, req: ProvisionRequest, *, cancel_ev: Optional[threading.Event] = None
    ) -> tuple[str, threading.Event]:
        if cancel_ev is not None and cancel_ev.is_set():
            raise ColabBackendError("cancelled", "Provisioning cancelled by user.")

        with self._lock:
            acc = self.active_account() if not req.account_id else self._accounts_dict().get(req.account_id)
        if not acc:
            raise ColabBackendError("no_account", "Sign in to a Google account before starting a Colab session.")

        existing = self.list_sessions(include_all_accounts=True)
        sess_name = (req.name or "").strip() or f"boost-{req.accelerator.value.lower()}-{uuid.uuid4().hex[:4]}"
        max_tries = self.settings.guards.max_vram_retries if req.min_vram_mib > 0 else 1
        op_id = f"prov-{uuid.uuid4().hex[:8]}"
        effective_cancel = cancel_ev if cancel_ev is not None else threading.Event()
        done_ev = threading.Event()
        op = ProvisionOperation(
            op_id=op_id,
            session_name=sess_name,
            account_id=acc.id,
            accelerator=req.accelerator,
            min_vram_mib=req.min_vram_mib,
            attempt=1,
            max_attempts=max_tries,
            state="allocating",
            message=f"Requesting {req.accelerator.value} runtime from Colab...",
            started_at=_utc_now(),
        )

        active_prov_states = ("allocating", "probing", "retrying", "watchdog")
        with self._lock:
            if effective_cancel.is_set():
                raise ColabBackendError("cancelled", "Provisioning cancelled by user.")
            inflight_for_acc = sum(
                1
                for o in self._provision_ops.values()
                if o.account_id == acc.id and o.state in active_prov_states
            )
            managed_count = sum(1 for s in existing if s.managed and s.account_id == acc.id) + inflight_for_acc
            if managed_count >= self.settings.guards.max_managed_sessions:
                raise ColabBackendError(
                    "session_cap",
                    f"Managed session limit ({self.settings.guards.max_managed_sessions}) reached. Stop an existing runtime first.",
                )

            name_taken = any(s.name == sess_name for s in existing) or any(
                o.session_name == sess_name and o.state in active_prov_states
                for o in self._provision_ops.values()
            )
            if name_taken:
                raise ColabBackendError("duplicate_name", f"Session '{sess_name}' already exists; pick another name.")

            self._provision_ops[op_id] = op
            self._provision_cancels[op_id] = effective_cancel
            self._provision_done[op_id] = done_ev

        self._emit("provision_updated", op.model_copy(deep=True).model_dump(mode="json"))
        return op_id, effective_cancel

    def cancel_provision(self, op_id: str) -> bool:
        with self._lock:
            ev = self._provision_cancels.get(op_id)
            if not ev:
                return False
            ev.set()
            return True

    def list_provision_ops(self) -> list[ProvisionOperation]:
        with self._lock:
            return [op.model_copy(deep=True) for op in self._provision_ops.values()]

    def _update_prov(self, op_id: str, **updates: Any) -> None:
        with self._lock:
            op = self._provision_ops.get(op_id)
            if not op:
                return
            for k, v in updates.items():
                setattr(op, k, v)
            snap = op.model_copy(deep=True)
        self._emit("provision_updated", snap.model_dump(mode="json"))

    def _run_provision_sync(self, op_id: str, req: ProvisionRequest, cancel_ev: threading.Event) -> SessionInfo:
        with self._lock:
            op = self._provision_ops[op_id]
            acc_id = op.account_id
            sess_name = op.session_name
            max_tries = op.max_attempts
            acc = self._accounts_dict()[acc_id].model_copy(deep=True)

        tok = self._get_token_for_account(acc_id)
        auto_stop = req.auto_stop_minutes if req.auto_stop_minutes is not None else self.settings.guards.auto_stop_minutes
        idle_stop = req.idle_stop_minutes if req.idle_stop_minutes is not None else self.settings.guards.idle_stop_minutes

        untracked_raw: Optional[RawAssignment] = None
        untracked_started: Optional[datetime] = None

        def _cleanup_untracked(raw_vm: RawAssignment, started_at: datetime, reason: str, est_spent: float = 0.01) -> None:
            verified = False
            try:
                verified = self.backend.unassign_runtime(acc_id, tok, raw_vm.endpoint)
            except Exception as unassign_exc:
                logger.warning("Best-effort unassign failed for %s (%s): %s", raw_vm.endpoint, reason, unassign_exc)
            balance_after: Optional[float] = None
            try:
                after_usage = self.refresh_account_usage(acc_id)
                balance_after = after_usage.balance_ccu
            except Exception as usage_exc:
                logger.warning("Usage refresh failed during cleanup of %s: %s", raw_vm.endpoint, usage_exc)
            self._record_receipt(
                UnassignReceipt(
                    receipt_id=uuid.uuid4().hex[:8],
                    timestamp=_utc_now(),
                    account_id=acc_id,
                    account_email=acc.email,
                    session_name=sess_name,
                    endpoint=raw_vm.endpoint,
                    accelerator=raw_vm.accelerator.value,
                    reason=reason,
                    started_at=started_at,
                    duration_seconds=round((_utc_now() - started_at).total_seconds(), 2),
                    estimated_ccu_spent=est_spent,
                    balance_after_ccu=balance_after,
                    verified_unassigned=verified,
                    backend_mode=self.settings.backend_mode,
                )
            )

        def _abort_if_cancelled(stage_label: str) -> None:
            nonlocal untracked_raw, untracked_started
            if not cancel_ev.is_set():
                return
            if untracked_raw is not None:
                _cleanup_untracked(untracked_raw, untracked_started or _utc_now(), "cancelled_cleanup", est_spent=0.01)
                untracked_raw = None
                untracked_started = None
                self._update_prov(
                    op_id,
                    state="cancelled",
                    message=f"Cancelled during {stage_label}; assigned VM immediately unassigned.",
                    finished_at=_utc_now(),
                )
                raise ColabBackendError("cancelled", "Provisioning cancelled and VM cleaned up.")
            self._update_prov(op_id, state="cancelled", message="Provisioning cancelled; 0 VMs billed.", finished_at=_utc_now())
            raise ColabBackendError("cancelled", "Provisioning cancelled by user.")

        try:
            for attempt in range(1, max_tries + 1):
                _abort_if_cancelled("pre-allocation")

                self._update_prov(
                    op_id,
                    attempt=attempt,
                    state="allocating",
                    message=f"Attempt {attempt}/{max_tries}: allocating {req.accelerator.value}...",
                )
                alloc_started = _utc_now()
                raw = self.backend.assign_runtime(acc_id, tok, req.accelerator, high_mem=req.high_mem)
                untracked_raw = raw
                untracked_started = alloc_started

                # Crucial safety invariant: if cancelled while assign was in flight, immediately release the VM!
                _abort_if_cancelled("allocation")

                self._update_prov(op_id, state="probing", message="Probing GPU VRAM & runtime environment...")
                probe = self.backend.probe_hardware(raw)
                _abort_if_cancelled("hardware probe")

                if req.min_vram_mib > 0 and probe.vram_total_mib < req.min_vram_mib:
                    # Assigned GPU has less VRAM than requested (e.g. 40 GB A100 instead of 80 GB) -> release & retry!
                    _cleanup_untracked(
                        raw,
                        alloc_started,
                        f"vram_retry ({probe.vram_total_mib} < {req.min_vram_mib} MiB)",
                        est_spent=0.02,
                    )
                    untracked_raw = None
                    untracked_started = None
                    if attempt >= max_tries:
                        msg = (
                            f"Released {probe.gpu_name or raw.accelerator.value} ({probe.vram_total_mib} MiB < "
                            f"{req.min_vram_mib} MiB) after {max_tries} attempts; 0 VMs left running."
                        )
                        self._update_prov(op_id, state="failed", error=msg, message=msg, finished_at=_utc_now())
                        raise ColabBackendError("vram_not_met", msg)

                    self._update_prov(
                        op_id,
                        state="retrying",
                        message=f"Got {probe.vram_total_mib} MiB (< {req.min_vram_mib} MiB); released & retrying...",
                    )
                    continue

                self._update_prov(op_id, state="watchdog", message="Arming VM-side auto-stop & idle watchdog...")
                wd_info = self._install_vm_watchdog(raw, probe, auto_stop, idle_stop)
                _abort_if_cancelled("watchdog setup")

                cancelled_before_commit = False
                with self._lock:
                    if cancel_ev.is_set():
                        cancelled_before_commit = True
                    else:
                        self._ledger_dict()[raw.endpoint] = {
                            "name": sess_name,
                            "account_id": acc_id,
                            "created_at": alloc_started.isoformat(),
                            "gpu_name": probe.gpu_name,
                            "vram_total_mib": probe.vram_total_mib,
                            "vram_used_mib": probe.vram_used_mib,
                            "gpu_util_percent": probe.gpu_util_percent,
                            "watchdog": wd_info,
                        }
                        self._persist_ledger_if_real()
                        untracked_raw = None
                        untracked_started = None

                if cancelled_before_commit:
                    _abort_if_cancelled("commit")

                self.refresh_account_usage(acc_id)
                self._update_prov(
                    op_id,
                    state="ready",
                    message=f"{sess_name} ready ({probe.gpu_name or raw.accelerator.value})",
                    finished_at=_utc_now(),
                )
                sessions = self.list_sessions(include_all_accounts=True)
                created = next((s for s in sessions if s.endpoint == raw.endpoint), None)
                if not created:
                    raise ColabBackendError("internal", "Provisioned session missing from session list.")
                self._emit("sessions_changed", {"created": sess_name})
                return created

            raise ColabBackendError("provision_exhausted", "Provisioning attempts exhausted.")
        except Exception as exc:
            # Zero-leak invariant: release any allocated VM that was not yet committed to the ledger!
            if untracked_raw is not None:
                cleanup_reason = "cancelled_cleanup" if cancel_ev.is_set() else "provision_failed_cleanup"
                try:
                    _cleanup_untracked(untracked_raw, untracked_started or _utc_now(), cleanup_reason, est_spent=0.01)
                except Exception as cleanup_exc:
                    logger.warning(
                        "Failed cleaning up allocated VM %s after provision failure: %s",
                        untracked_raw.endpoint,
                        cleanup_exc,
                    )
                untracked_raw = None
                untracked_started = None

            with self._lock:
                op_now = self._provision_ops.get(op_id)
                needs_update = bool(op_now and op_now.state not in ("cancelled", "failed"))
            if needs_update:
                self._update_prov(
                    op_id,
                    state="cancelled" if cancel_ev.is_set() else "failed",
                    error=str(exc),
                    message=str(exc),
                    finished_at=_utc_now(),
                )
            raise
        finally:
            with self._lock:
                self._provision_cancels.pop(op_id, None)
                done_ev = self._provision_done.pop(op_id, None)
            if done_ev is not None:
                done_ev.set()

    def stop_session(
        self,
        identifier: str,
        *,
        account_id: Optional[str] = None,
        reason: str = "manual_stop",
    ) -> UnassignReceipt:
        """Stops a session by name OR endpoint on ANY saved account and writes an UnassignReceipt."""
        sessions = self.list_sessions(include_all_accounts=True)
        target = next(
            (
                s for s in sessions
                if (s.name == identifier or s.endpoint == identifier)
                and (account_id is None or s.account_id == account_id)
            ),
            None,
        )
        if not target:
            with self._lock:
                inflight = self._inflight_stops.get(identifier)
            if inflight is not None:
                inflight[0].wait(timeout=30.0)
                if inflight[1]:
                    return inflight[1][0].model_copy(deep=True)
            raise KeyError(f"Session '{identifier}' not found or already stopped.")

        # Deduplicate concurrent stop calls on the same endpoint
        with self._lock:
            existing_stop = self._inflight_stops.get(target.endpoint)
            if existing_stop is None:
                stop_done = threading.Event()
                res_box: list[UnassignReceipt] = []
                self._inflight_stops[target.endpoint] = (stop_done, res_box)
                is_owner = True
            else:
                stop_done, res_box = existing_stop
                is_owner = False

        if not is_owner:
            stop_done.wait(timeout=30.0)
            if res_box:
                return res_box[0].model_copy(deep=True)
            raise KeyError(f"Session '{identifier}' already stopped.")

        try:
            # Best-effort sync of declared [[keep]] artifacts before releasing the VM (skip on emergency stop / deploy cleanup)
            if reason not in ("deploy_cancelled", "deploy_failed_cleanup", "emergency_stop_all"):
                with self._lock:
                    keep_run_ids = [
                        r.run_id
                        for r in self._runs_dict().values()
                        if r.session_name == target.name
                        and r.state in ("running", "ready")
                        and r.keep_paths
                    ]
                for k_rid in keep_run_ids:
                    try:
                        self.sync_keep_artifacts(k_rid)
                    except Exception:
                        pass

            # Stop any local tunnels & cancel running deployments on this session
            self.tunnels.stop_session_tunnels(target.name)
            with self._lock:
                for rid, run in self._runs_dict().items():
                    if run.session_name == target.name and run.state in ("provisioning", "starting", "running"):
                        ev = self._run_cancels.get(rid)
                        if ev:
                            ev.set()

            tok = self._get_token_for_account(target.account_id)
            verified = self.backend.unassign_runtime(target.account_id, tok, target.endpoint)

            with self._lock:
                self._ledger_dict().pop(target.endpoint, None)
                self._persist_ledger_if_real()

            after_usage = self.refresh_account_usage(target.account_id)
            duration = round((_utc_now() - target.created_at).total_seconds(), 2) if target.created_at else None

            receipt = self._record_receipt(
                UnassignReceipt(
                    receipt_id=uuid.uuid4().hex[:8],
                    timestamp=_utc_now(),
                    account_id=target.account_id,
                    account_email=target.account_email,
                    session_name=target.name,
                    endpoint=target.endpoint,
                    accelerator=target.accelerator.value,
                    reason=reason,
                    started_at=target.created_at,
                    duration_seconds=duration,
                    estimated_ccu_spent=target.estimated_spent_ccu,
                    balance_after_ccu=after_usage.balance_ccu,
                    verified_unassigned=verified,
                    backend_mode=self.settings.backend_mode,
                )
            )
            res_box.append(receipt)
            self._emit("sessions_changed", {"stopped": target.name, "endpoint": target.endpoint})
            return receipt
        finally:
            stop_done.set()
            with self._lock:
                self._inflight_stops.pop(target.endpoint, None)

    def stop_all_sessions(self, reason: str = "emergency_stop_all") -> list[UnassignReceipt]:
        return self.stop_all_sessions_detailed(reason=reason)["receipts"]

    def stop_all_sessions_detailed(
        self, reason: str = "emergency_stop_all", *, drain_timeout_s: float = 10.0
    ) -> dict[str, Any]:
        # 1. Cancel any in-flight provisioning or deployment runs so no new VM slips through after stop_all!
        with self._lock:
            receipts_before = {r.receipt_id for r in self._receipts}
            for ev in self._provision_cancels.values():
                ev.set()
            for ev in self._run_cancels.values():
                ev.set()
            pending_prov_done = list(self._provision_done.values())

        deadline = time.monotonic() + max(0.0, drain_timeout_s)
        for done_ev in pending_prov_done:
            rem = max(0.0, deadline - time.monotonic())
            if rem > 0 and not done_ev.is_set():
                done_ev.wait(timeout=rem)
        timed_out_provisions = sum(1 for done_ev in pending_prov_done if not done_ev.is_set())

        receipts: list[UnassignReceipt] = []
        seen_ids: set[str] = set()
        stop_errors: list[dict[str, str]] = []
        initial_failed_accounts: list[dict[str, str]] = []

        try:
            # 2. Stop every live session across all accounts (never let one failure abort remaining stops)
            sessions, initial_failed_accounts = self.list_sessions_with_errors(include_all_accounts=True)
            for s in sessions:
                try:
                    rc = self.stop_session(s.endpoint, account_id=s.account_id, reason=reason)
                    if rc.receipt_id not in seen_ids:
                        seen_ids.add(rc.receipt_id)
                        receipts.append(rc)
                    if not rc.verified_unassigned:
                        stop_errors.append(
                            {
                                "session_name": s.name,
                                "endpoint": s.endpoint,
                                "account_id": s.account_id,
                                "error": "unassign_not_verified",
                            }
                        )
                except KeyError:
                    continue
                except Exception as exc:
                    logger.warning("stop_all_sessions failed stopping %s (%s): %s", s.name, s.endpoint, exc)
                    stop_errors.append(
                        {
                            "session_name": s.name,
                            "endpoint": s.endpoint,
                            "account_id": s.account_id,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        finally:
            # Always tear down local SSH tunnels even if any stop failed
            self.tunnels.stop_all()

        # Include any cancelled_cleanup receipts produced while draining in-flight allocations
        with self._lock:
            for r in self._receipts:
                if r.receipt_id not in receipts_before and r.receipt_id not in seen_ids:
                    seen_ids.add(r.receipt_id)
                    receipts.append(r.model_copy(deep=True))
                    if not r.verified_unassigned:
                        stop_errors.append(
                            {
                                "session_name": r.session_name,
                                "endpoint": r.endpoint,
                                "account_id": r.account_id,
                                "error": "unassign_not_verified",
                            }
                        )

        # 3. Re-verify zero remaining sessions across all accounts and track any account listing errors
        remaining, verify_failed_accounts = self.list_sessions_with_errors(include_all_accounts=True)
        merged_failed_map: dict[str, dict[str, str]] = {}
        for item in (*initial_failed_accounts, *verify_failed_accounts):
            merged_failed_map[item["account_id"]] = item
        failed_accounts = list(merged_failed_map.values())

        zero_burn_verified = (
            len(remaining) == 0
            and len(failed_accounts) == 0
            and len(stop_errors) == 0
            and timed_out_provisions == 0
        )
        return {
            "stopped_count": len(receipts),
            "remaining_sessions": len(remaining),
            "zero_burn_verified": zero_burn_verified,
            "receipts": receipts,
            "failed_accounts": failed_accounts,
            "stop_errors": stop_errors,
            "timed_out_provisions": timed_out_provisions,
        }

    def extend_session_deadline(self, session_name: str, add_minutes: int = 120) -> SessionInfo:
        if add_minutes <= 0 or add_minutes > 1440:
            raise ValueError("add_minutes must be between 1 and 1440")
        sessions = self.list_sessions(include_all_accounts=True)
        target = next((s for s in sessions if s.name == session_name or s.endpoint == session_name), None)
        if not target:
            raise KeyError(f"Session '{session_name}' not found.")

        base_epoch = max(time.time(), target.watchdog.deadline_epoch or time.time())
        new_deadline = base_epoch + (add_minutes * 60.0)
        default_idle_s = (
            target.watchdog.idle_timeout_s
            if target.watchdog.idle_timeout_s is not None
            else (self.settings.guards.idle_stop_minutes * 60 if self.settings.guards.idle_stop_minutes > 0 else None)
        )

        if not self.backend.is_mock:
            tok = self._get_token_for_account(target.account_id)
            assignments = self.backend.list_assignments(target.account_id, tok)
            raw = next((a for a in assignments if a.endpoint == target.endpoint), None)
            if not raw:
                raise KeyError(f"Runtime '{target.endpoint}' disappeared.")
            wd_src = _watchdog_source()
            patch_code = (
                "import json, os, subprocess, sys, time\n"
                "wd_dir = '/root/.colab-boost/watchdog'\n"
                "os.makedirs(wd_dir, exist_ok=True)\n"
                "cfg_path = wd_dir + '/watchdog.json'\n"
                "pid_path = wd_dir + '/watchdog.pid'\n"
                "script_path = wd_dir + '/watchdog.py'\n"
                "cfg = {}\n"
                "if os.path.exists(cfg_path):\n"
                "    try: cfg = json.load(open(cfg_path, encoding='utf-8'))\n"
                "    except Exception: cfg = {}\n"
                f"cfg['deadline_epoch'] = {new_deadline!r}\n"
                "cfg['cancelled'] = False\n"
                f"cfg.setdefault('idle_timeout_s', {default_idle_s!r})\n"
                "cfg.setdefault('runtime_addr', os.environ.get('TBE_RUNTIME_ADDR'))\n"
                "cfg.setdefault('heartbeat_file', '/root/.colab-boost/heartbeat')\n"
                "cfg.setdefault('log_file', wd_dir + '/watchdog.log')\n"
                "cfg.setdefault('poll_s', 15)\n"
                "cfg.setdefault('grace_s', 180)\n"
                "cfg.setdefault('started_at', time.time())\n"
                "tmp = cfg_path + '.tmp'\n"
                "with open(tmp, 'w', encoding='utf-8') as fh:\n"
                "    json.dump(cfg, fh)\n"
                "os.replace(tmp, cfg_path)\n"
                "pid = 0\n"
                "if os.path.exists(pid_path):\n"
                "    try: pid = int(open(pid_path).read().strip())\n"
                "    except Exception: pid = 0\n"
                "alive = bool(pid and os.path.exists(f'/proc/{pid}'))\n"
                "if not alive:\n"
                f"    open(script_path, 'w', encoding='utf-8').write({wd_src!r})\n"
                "    p = subprocess.Popen([sys.executable, script_path, '--config', cfg_path], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
                "    pid = p.pid\n"
                f"print({RESULT_MARKER!r} + json.dumps({{'pid': pid, 'deadline_epoch': cfg['deadline_epoch']}}), flush=True)\n"
            )
            res = self.backend.execute_python(raw, patch_code, timeout_s=25.0)
            if not res.ok or not res.marker_json or not res.marker_json.get("pid"):
                raise ColabBackendError(
                    "watchdog_failed",
                    f"Failed to arm/extend VM watchdog on '{target.name}': {res.stderr[:200] or res.error_value or 'no pid returned'}",
                )

        with self._lock:
            entry = self._ledger_dict().setdefault(
                target.endpoint,
                {"name": target.name, "account_id": target.account_id, "created_at": _utc_now().isoformat()},
            )
            wd = entry.setdefault("watchdog", {})
            wd["active"] = True
            wd["deadline_epoch"] = new_deadline
            if wd.get("idle_timeout_s") is None and default_idle_s is not None:
                wd["idle_timeout_s"] = default_idle_s
            self._persist_ledger_if_real()

        updated = next(s for s in self.list_sessions(include_all_accounts=True) if s.endpoint == target.endpoint)
        self._emit("sessions_changed", {"extended": target.name})
        return updated

    def evaluate_cost_guards(self) -> list[UnassignReceipt]:
        """Checks live CCU balance floor and per-session spend cap.

        Never triggers on ``unknown`` or ``stale`` usage snapshots.
        """
        guards = self.settings.guards
        triggered: list[UnassignReceipt] = []
        accounts = self.list_accounts(refresh_usage=True)
        sessions = self.list_sessions(include_all_accounts=True)

        for acc in accounts:
            acct_sessions = [s for s in sessions if s.account_id == acc.id]
            if not acct_sessions:
                continue
            if acc.usage.status == "live" and acc.usage.balance_ccu is not None:
                if guards.balance_floor_ccu > 0 and acc.usage.balance_ccu <= guards.balance_floor_ccu:
                    for s in acct_sessions:
                        try:
                            triggered.append(
                                self.stop_session(
                                    s.endpoint,
                                    account_id=acc.id,
                                    reason=f"cost_guard_balance_floor ({acc.usage.balance_ccu} <= {guards.balance_floor_ccu} CCU)",
                                )
                            )
                        except KeyError:
                            continue
                        except Exception as exc:
                            logger.warning(
                                "Cost guard balance_floor failed stopping %s (%s): %s",
                                s.name,
                                s.endpoint,
                                exc,
                            )
                            continue
                    continue

            if guards.session_spend_cap_ccu > 0:
                for s in acct_sessions:
                    if s.managed and s.estimated_spent_ccu >= guards.session_spend_cap_ccu:
                        try:
                            triggered.append(
                                self.stop_session(
                                    s.endpoint,
                                    account_id=acc.id,
                                    reason=f"cost_guard_spend_cap ({s.estimated_spent_ccu} >= {guards.session_spend_cap_ccu} CCU)",
                                )
                            )
                        except KeyError:
                            continue
                        except Exception as exc:
                            logger.warning(
                                "Cost guard spend_cap failed stopping %s (%s): %s",
                                s.name,
                                s.endpoint,
                                exc,
                            )
                            continue
        return triggered

    def list_receipts(self) -> list[UnassignReceipt]:
        with self._lock:
            return [r.model_copy(deep=True) for r in self._receipts]

    # -------------------- Port Forwarding --------------------

    def open_tunnel(
        self,
        session_name: str,
        remote_port: int,
        local_port: int = 0,
        label: str = "Port Forward",
        path: str = "/",
        kind: str = "http",
        labels: Optional[dict[str, str]] = None,
    ) -> TunnelInfo:
        sessions = self.list_sessions(include_all_accounts=True)
        target = next((s for s in sessions if s.name == session_name or s.endpoint == session_name), None)
        if not target:
            raise KeyError(f"Session '{session_name}' is not active; refusing to start SSH tunnel.")

        if self.backend.is_mock:
            tun = self.tunnels.register_mock_tunnel(
                session_name=target.name,
                account_id=target.account_id,
                remote_port=remote_port,
                local_port=local_port,
                label=label,
                labels=labels,
                path=path,
                kind=kind,
            )
            self._emit("tunnels_changed", tun.model_dump(mode="json"))
            return tun

        def fetch_latest_raw() -> RawAssignment:
            tok_cur = self._get_token_for_account(target.account_id)
            items = self.backend.list_assignments(target.account_id, tok_cur)
            found = next((a for a in items if a.endpoint == target.endpoint), None)
            if not found:
                raise ColabBackendError("runtime_lost", f"Runtime {target.endpoint} no longer exists on Colab.")
            return found

        raw = fetch_latest_raw()

        def kill_stale_sshd() -> None:
            try:
                cur_raw = fetch_latest_raw()
            except Exception:
                cur_raw = raw
            self.backend.execute_python(
                cur_raw,
                "import os; os.system('pkill -P $(pgrep -o -x sshd) >/dev/null 2>&1 || true')",
                timeout_s=15.0,
            )

        def refresh_creds() -> tuple[str, str]:
            cur_raw = fetch_latest_raw()
            return cur_raw.proxy_url, cur_raw.proxy_token

        tun = self.tunnels.start_real_tunnel(
            session_name=target.name,
            account_id=target.account_id,
            runtime_url=raw.proxy_url,
            proxy_token=raw.proxy_token,
            remote_port=remote_port,
            local_port=local_port,
            label=label,
            labels=labels,
            path=path,
            kind=kind,
            on_429_kill_stale_sshd=kill_stale_sshd,
            refresh_credentials=refresh_creds,
        )
        self._emit("tunnels_changed", tun.model_dump(mode="json"))
        return tun

    def close_tunnel(self, tunnel_id: str) -> bool:
        ok = self.tunnels.stop_tunnel(tunnel_id)
        if ok:
            self._emit("tunnels_changed", {"closed": tunnel_id})
        return ok

    # -------------------- Recipes, [[keep]] Preservation & Model Deployment --------------------

    def list_recipes(self) -> list[RecipeManifest]:
        loaded = discover_all_recipes(self.paths.bundled_recipes_dir, self.paths.user_recipes_dir)
        return [r.manifest for r in loaded.values()]

    def get_loaded_recipe(self, recipe_id: str) -> LoadedRecipe:
        loaded = discover_all_recipes(self.paths.bundled_recipes_dir, self.paths.user_recipes_dir)
        if recipe_id not in loaded:
            raise KeyError(f"Unknown recipe: {recipe_id!r}")
        return loaded[recipe_id]

    def _localize_run_copy(self, run: DeploymentRun) -> DeploymentRun:
        cp = run.model_copy(deep=True)
        for ep in cp.endpoints:
            if ep.labels:
                ep.label = self._localized_label(ep.labels, ep.label)
        return cp

    def list_runs(self) -> list[DeploymentRun]:
        with self._lock:
            return [self._localize_run_copy(r) for r in self._runs_dict().values()]

    def get_run(self, run_id: str) -> DeploymentRun:
        with self._lock:
            runs = self._runs_dict()
            if run_id not in runs:
                raise KeyError(f"Unknown deployment run: {run_id}")
            return self._localize_run_copy(runs[run_id])

    def _configure_vm_keep_hooks(
        self,
        raw: Optional[RawAssignment],
        endpoint: str,
        session_name: str,
        account_id: str,
        run_id: str,
        keep_specs: list[RecipeKeepSpec],
    ) -> None:
        if not keep_specs:
            return
        keep_paths = [kp.path for kp in keep_specs]
        hooks = _build_keep_winddown_hooks(run_id, session_name, keep_specs)

        with self._lock:
            entry = self._ledger_dict().setdefault(
                endpoint,
                {"name": session_name, "account_id": account_id, "created_at": _utc_now().isoformat()},
            )
            wd = entry.setdefault("watchdog", {})
            merged_paths = list(dict.fromkeys([*(wd.get("keep_paths") or []), *keep_paths]))
            wd["keep_paths"] = merged_paths
            self._persist_ledger_if_real()

        if self.backend.is_mock or raw is None:
            return

        wd_src = _watchdog_source()
        patch_code = (
            "import json, os, subprocess, sys, time\n"
            "wd_dir = '/root/.colab-boost/watchdog'\n"
            "os.makedirs(wd_dir, exist_ok=True)\n"
            "cfg_path = wd_dir + '/watchdog.json'\n"
            "pid_path = wd_dir + '/watchdog.pid'\n"
            "script_path = wd_dir + '/watchdog.py'\n"
            "cfg = {}\n"
            "if os.path.exists(cfg_path):\n"
            "    try: cfg = json.load(open(cfg_path, encoding='utf-8'))\n"
            "    except Exception: cfg = {}\n"
            "existing_hooks = cfg.get('hooks') if isinstance(cfg.get('hooks'), list) else []\n"
            f"for h in {hooks!r}:\n"
            "    if h not in existing_hooks:\n"
            "        existing_hooks.append(h)\n"
            "cfg['hooks'] = existing_hooks\n"
            "tmp = cfg_path + '.tmp'\n"
            "with open(tmp, 'w', encoding='utf-8') as fh:\n"
            "    json.dump(cfg, fh)\n"
            "os.replace(tmp, cfg_path)\n"
            "has_guard = bool(cfg.get('deadline_epoch') or cfg.get('idle_timeout_s'))\n"
            "pid = 0\n"
            "if os.path.exists(pid_path):\n"
            "    try: pid = int(open(pid_path).read().strip())\n"
            "    except Exception: pid = 0\n"
            "alive = bool(pid and os.path.exists(f'/proc/{pid}'))\n"
            "if has_guard and not alive:\n"
            f"    open(script_path, 'w', encoding='utf-8').write({wd_src!r})\n"
            "    p = subprocess.Popen([sys.executable, script_path, '--config', cfg_path], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
            "    pid = p.pid\n"
            f"print({RESULT_MARKER!r} + json.dumps({{'hooks_count': len(existing_hooks), 'pid': pid}}), flush=True)\n"
        )
        self.backend.execute_python(raw, patch_code, timeout_s=25.0)

    def sync_keep_artifacts(self, run_id: str) -> list[KeptArtifactInfo]:
        """Archives declared ``[[keep]]`` directories from the Colab VM into ``<workspace>/data/keep/<run_id>/``."""
        with self._lock:
            runs = self._runs_dict()
            if run_id not in runs:
                raise KeyError(f"Unknown deployment run: {run_id}")
            run = runs[run_id].model_copy(deep=True)

        if not run.keep_paths:
            return []

        dest_dir = self.paths.keep_dir / run_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        now = _utc_now()

        if self.backend.is_mock:
            updated_items: list[KeptArtifactInfo] = []
            for kp in run.keep_paths:
                slug = _sanitize_keep_slug(kp.path)
                arc_path = dest_dir / f"{slug}.tar.gz"
                meta_bytes = json.dumps(
                    {
                        "run_id": run.run_id,
                        "recipe_id": run.recipe_id,
                        "session_name": run.session_name,
                        "remote_path": kp.path,
                        "synced_at": now.isoformat(),
                        "mode": "mock",
                    },
                    indent=2,
                ).encode("utf-8")
                sample_bytes = f"Colab Boost preserved artifact snapshot for {kp.path}\n".encode("utf-8")
                with tarfile.open(arc_path, "w:gz") as tf:
                    ti1 = tarfile.TarInfo(name=f"{slug}/keep_manifest.json")
                    ti1.size = len(meta_bytes)
                    ti1.mtime = int(now.timestamp())
                    tf.addfile(ti1, io.BytesIO(meta_bytes))

                    ti2 = tarfile.TarInfo(name=f"{slug}/sample_output.txt")
                    ti2.size = len(sample_bytes)
                    ti2.mtime = int(now.timestamp())
                    tf.addfile(ti2, io.BytesIO(sample_bytes))

                updated_items.append(
                    KeptArtifactInfo(
                        path=kp.path,
                        label=kp.label,
                        file_count=max(kp.file_count, 2),
                        total_bytes=max(kp.total_bytes, int(arc_path.stat().st_size)),
                        drive_synced=kp.drive_synced,
                        local_archive=str(arc_path),
                        synced_at=now,
                    )
                )
            self._update_run(run_id, [], keep_paths=updated_items)
            return [x.model_copy(deep=True) for x in updated_items]

        tok = self._get_token_for_account(run.account_id)
        assignments = self.backend.list_assignments(run.account_id, tok)
        raw = self._match_assignment_for_run(run, assignments)
        if not raw:
            raise ColabBackendError("runtime_lost", f"Runtime for '{run.session_name}' is no longer active on Colab.")

        spec_list = [{"path": kp.path, "slug": _sanitize_keep_slug(kp.path)} for kp in run.keep_paths]
        inline_limit = 8 * 1024 * 1024
        chunk_bytes = 8 * 1024 * 1024
        fetch_code = (
            "import base64, json, os, shutil, tarfile\n"
            f"specs = {spec_list!r}\n"
            "keep_root = os.environ.get('BOOST_KEEP_STAGE_DIR', '/root/.colab-boost/keep')\n"
            f"stage_root = os.path.join(keep_root, {run.run_id!r})\n"
            "os.makedirs(stage_root, exist_ok=True)\n"
            "stage_manifest = os.path.join(stage_root, 'keep_manifest.json')\n"
            "drive_map = {}\n"
            "if os.path.exists(stage_manifest):\n"
            "    try:\n"
            "        for it in json.load(open(stage_manifest, encoding='utf-8')).get('items', []):\n"
            "            drive_map[it.get('path')] = bool(it.get('drive_synced'))\n"
            "    except Exception:\n"
            "        pass\n"
            "drive_mount = os.environ.get('BOOST_DRIVE_MOUNT', '/content/drive/MyDrive')\n"
            f"drive_root = os.path.join(drive_mount, 'ColabBoost', {run.session_name!r}, {run.run_id!r})\n"
            "has_drive = os.path.isdir(drive_mount)\n"
            "if has_drive:\n"
            "    os.makedirs(drive_root, exist_ok=True)\n"
            "def _tar_filter(ti):\n"
            "    if ti.issym() and (ti.linkname.startswith('/') or '..' in ti.linkname.split('/')):\n"
            "        return None\n"
            "    if not (ti.isfile() or ti.isdir() or ti.issym()):\n"
            "        return None\n"
            "    return ti\n"
            "results = []\n"
            "for sp in specs:\n"
            "    p = sp['path']\n"
            "    slug = sp['slug']\n"
            "    fc = 0\n"
            "    tb = 0\n"
            "    b64 = ''\n"
            "    stage_arc = ''\n"
            "    arc_bytes = 0\n"
            "    err_msg = ''\n"
            "    ds = bool(drive_map.get(p, False))\n"
            "    exists = os.path.lexists(p)\n"
            "    if exists:\n"
            "        if os.path.isfile(p) and not os.path.islink(p):\n"
            "            fc = 1\n"
            "            tb = os.path.getsize(p)\n"
            "        elif os.path.isdir(p) and not os.path.islink(p):\n"
            "            for r, _, fs in os.walk(p, followlinks=False):\n"
            "                for fn in fs:\n"
            "                    fp = os.path.join(r, fn)\n"
            "                    if os.path.islink(fp):\n"
            "                        continue\n"
            "                    try:\n"
            "                        if os.path.isfile(fp):\n"
            "                            fc += 1\n"
            "                            tb += os.path.getsize(fp)\n"
            "                    except OSError:\n"
            "                        pass\n"
            "        stage_arc = os.path.join(stage_root, slug + '.tar.gz')\n"
            "        tmp_arc = stage_arc + '.tmp'\n"
            "        try:\n"
            "            with tarfile.open(tmp_arc, mode='w:gz') as tf:\n"
            "                tf.add(p, arcname=os.path.basename(p.rstrip('/')) or slug, recursive=True, filter=_tar_filter)\n"
            "            os.replace(tmp_arc, stage_arc)\n"
            "            arc_bytes = os.path.getsize(stage_arc)\n"
            "            if has_drive:\n"
            "                try:\n"
            "                    shutil.copy2(stage_arc, os.path.join(drive_root, slug + '.tar.gz'))\n"
            "                    ds = True\n"
            "                except OSError:\n"
            "                    pass\n"
            f"            if arc_bytes <= {inline_limit}:\n"
            "                raw_tar = open(stage_arc, 'rb').read()\n"
            "                b64 = base64.b64encode(raw_tar).decode('ascii')\n"
            "        except Exception as exc:\n"
            "            try: os.remove(tmp_arc)\n"
            "            except OSError: pass\n"
            "            err_msg = str(exc)\n"
            "    results.append({'path': p, 'slug': slug, 'exists': exists, 'file_count': fc, 'total_bytes': tb, 'b64': b64, 'stage_archive': stage_arc, 'archive_bytes': arc_bytes, 'drive_synced': ds, 'error': err_msg})\n"
            f"print({RESULT_MARKER!r} + json.dumps({{'items': results}}), flush=True)\n"
        )
        res = self.backend.execute_python(raw, fetch_code, timeout_s=120.0)
        if not res.ok or not res.marker_json:
            raise ColabBackendError("keep_sync_failed", f"Failed to archive keep paths from VM: {res.stderr[:200]}")

        items_list = [it for it in (res.marker_json.get("items") or []) if isinstance(it, dict)]
        remote_map = {it["path"]: it for it in items_list if "path" in it}
        slug_map = {it["slug"]: it for it in items_list if "slug" in it}
        updated_real: list[KeptArtifactInfo] = []
        for kp in run.keep_paths:
            slug = _sanitize_keep_slug(kp.path)
            ritem = remote_map.get(kp.path) or slug_map.get(slug) or {}
            if ritem.get("error"):
                raise ColabBackendError("keep_sync_failed", f"Failed to archive {kp.path!r}: {ritem['error']}")
            arc_path = dest_dir / f"{slug}.tar.gz"
            tmp_local = dest_dir / f"{slug}.tar.gz.tmp"
            local_arc_str = kp.local_archive
            synced_now = False
            b64_data = str(ritem.get("b64") or "")
            stage_arc = str(ritem.get("stage_archive") or "")
            arc_bytes = int(ritem.get("archive_bytes") or 0)
            if b64_data:
                tmp_local.write_bytes(base64.b64decode(b64_data))
                tmp_local.replace(arc_path)
                local_arc_str = str(arc_path)
                synced_now = True
            elif stage_arc and arc_bytes > 0:
                # Stream multi-chunk archive (> 8 MiB / > 48 MiB) safely without single-frame OOM or extra SSH slots
                written = 0
                try:
                    with open(tmp_local, "wb") as out_fh:
                        while written < arc_bytes:
                            chunk_code = (
                                "import base64, json\n"
                                f"with open({stage_arc!r}, 'rb') as fh:\n"
                                f"    fh.seek({written})\n"
                                f"    data = fh.read({chunk_bytes})\n"
                                f"print({RESULT_MARKER!r} + json.dumps({{'n': len(data), 'b64': base64.b64encode(data).decode('ascii')}}), flush=True)\n"
                            )
                            cres = self.backend.execute_python(raw, chunk_code, timeout_s=90.0)
                            if not cres.ok or not cres.marker_json:
                                raise ColabBackendError(
                                    "keep_sync_failed",
                                    f"Failed streaming archive chunk at offset {written} for {kp.path!r}: {cres.stderr[:180]}",
                                )
                            c_b64 = str(cres.marker_json.get("b64") or "")
                            raw_chunk = base64.b64decode(c_b64) if c_b64 else b""
                            if not raw_chunk:
                                break
                            out_fh.write(raw_chunk)
                            written += len(raw_chunk)
                    if written != arc_bytes:
                        raise ColabBackendError(
                            "keep_sync_failed",
                            f"Incomplete archive stream for {kp.path!r} ({written}/{arc_bytes} bytes).",
                        )
                    tmp_local.replace(arc_path)
                    local_arc_str = str(arc_path)
                    synced_now = True
                except BaseException:
                    tmp_local.unlink(missing_ok=True)
                    raise
            elif int(ritem.get("file_count") or 0) == 0:
                with tarfile.open(tmp_local, "w:gz"):
                    pass
                tmp_local.replace(arc_path)
                local_arc_str = str(arc_path)
                synced_now = True
            updated_real.append(
                KeptArtifactInfo(
                    path=kp.path,
                    label=kp.label,
                    file_count=int(ritem.get("file_count") or 0),
                    total_bytes=int(ritem.get("total_bytes") or 0),
                    drive_synced=bool(ritem.get("drive_synced") or kp.drive_synced),
                    local_archive=local_arc_str,
                    synced_at=now if synced_now else kp.synced_at,
                )
            )
        self._update_run(run_id, [], keep_paths=updated_real)
        return [x.model_copy(deep=True) for x in updated_real]

    def _collect_capability_secrets(self, manifest: RecipeManifest) -> tuple[dict[str, str], list[str]]:
        cap_env: dict[str, str] = {}
        secrets: list[str] = []
        if "hf_token" in manifest.needs:
            hf_tok = self.vault.get_secret("hf_token")
            if hf_tok:
                cap_env["BOOST_HF_TOKEN"] = hf_tok
                secrets.append(hf_tok)
        if "civitai_token" in manifest.needs:
            civ_tok = self.vault.get_secret("civitai_token")
            if civ_tok:
                cap_env["BOOST_CIVITAI_TOKEN"] = civ_tok
                secrets.append(civ_tok)
        return cap_env, secrets

    def _default_endpoint_ports(self, manifest: RecipeManifest) -> dict[str, int]:
        try:
            _, _, ports = validate_and_build_env(manifest, {})
            return ports
        except Exception:
            out: dict[str, int] = {}
            inp_defaults = {inp.id: int(inp.default) for inp in manifest.inputs if inp.type == "number" and isinstance(inp.default, int)}
            for ep in manifest.endpoints:
                p = ep.port if ep.port is not None else inp_defaults.get(ep.port_input or "")
                if p:
                    out[ep.id] = p
            return out

    def _match_assignment_for_run(self, run: DeploymentRun, assignments: list[RawAssignment]) -> Optional[RawAssignment]:
        if run.session_endpoint:
            by_ep = next((a for a in assignments if a.endpoint == run.session_endpoint), None)
            if by_ep:
                return by_ep
        with self._lock:
            ledger = dict(self._real_ledger)
        for ep, meta in ledger.items():
            if meta.get("account_id") == run.account_id and meta.get("name") == run.session_name:
                by_ledger = next((a for a in assignments if a.endpoint == ep), None)
                if by_ledger:
                    return by_ledger
        return next(
            (
                a
                for a in assignments
                if a.endpoint == run.session_name or f"orphan-{a.endpoint[-8:]}" == run.session_name
            ),
            None,
        )

    def _build_poll_snippet(self, vm_run_dir: str, keep_specs: list[RecipeKeepSpec]) -> str:
        if not keep_specs:
            return (
                "import json, os\n"
                f"d = {vm_run_dir!r}\n"
                "ex = os.path.isdir(d)\n"
                "st = json.load(open(d + '/status.json')) if os.path.exists(d + '/status.json') else None\n"
                "pid = int(open(d + '/worker.pid').read().strip()) if os.path.exists(d + '/worker.pid') else 0\n"
                "alive = bool(pid and os.path.exists(f'/proc/{pid}'))\n"
                "lines = open(d + '/run.log', errors='replace').readlines()[-35:] if os.path.exists(d + '/run.log') else []\n"
                f"print({RESULT_MARKER!r} + json.dumps({{'exists': ex, 'status': st, 'alive': alive, 'tail': [x.rstrip() for x in lines]}}), flush=True)\n"
            )
        kpaths = [kp.path for kp in keep_specs]
        return (
            "import json, os\n"
            f"d = {vm_run_dir!r}\n"
            "ex = os.path.isdir(d)\n"
            "st = json.load(open(d + '/status.json')) if os.path.exists(d + '/status.json') else None\n"
            "pid = int(open(d + '/worker.pid').read().strip()) if os.path.exists(d + '/worker.pid') else 0\n"
            "alive = bool(pid and os.path.exists(f'/proc/{pid}'))\n"
            "lines = open(d + '/run.log', errors='replace').readlines()[-35:] if os.path.exists(d + '/run.log') else []\n"
            "ks = {}\n"
            f"for kp in {kpaths!r}:\n"
            "    fc, tb = 0, 0\n"
            "    if os.path.isfile(kp) and not os.path.islink(kp):\n"
            "        fc, tb = 1, os.path.getsize(kp)\n"
            "    elif os.path.isdir(kp) and not os.path.islink(kp):\n"
            "        for r, _, fs in os.walk(kp, followlinks=False):\n"
            "            for fn in fs:\n"
            "                fp = os.path.join(r, fn)\n"
            "                if os.path.islink(fp):\n"
            "                    continue\n"
            "                try:\n"
            "                    if os.path.isfile(fp):\n"
            "                        fc += 1\n"
            "                        tb += os.path.getsize(fp)\n"
            "                except OSError:\n"
            "                    pass\n"
            "    ks[kp] = {'file_count': fc, 'total_bytes': tb}\n"
            f"print({RESULT_MARKER!r} + json.dumps({{'exists': ex, 'status': st, 'alive': alive, 'tail': [x.rstrip() for x in lines], 'keep_stats': ks}}), flush=True)\n"
        )

    def _merge_keep_stats(self, run_id: str, keep_stats: Optional[dict[str, Any]]) -> Optional[list[KeptArtifactInfo]]:
        if not keep_stats:
            return None
        with self._lock:
            cur_run = self._runs_dict().get(run_id)
            if not cur_run or not cur_run.keep_paths:
                return None
            out: list[KeptArtifactInfo] = []
            for kp in cur_run.keep_paths:
                st = keep_stats.get(kp.path) or {}
                cp = kp.model_copy(deep=True)
                cp.file_count = int(st.get("file_count", cp.file_count))
                cp.total_bytes = int(st.get("total_bytes", cp.total_bytes))
                out.append(cp)
            return out

    def _forward_recipe_endpoints(
        self,
        manifest: RecipeManifest,
        session_name: str,
        endpoint_ports: dict[str, int],
        *,
        best_effort: bool = False,
    ) -> list[TunnelInfo]:
        forwarded: list[TunnelInfo] = []
        if not self.settings.auto_forward_endpoints:
            return forwarded
        resolved_ports = dict(endpoint_ports) if endpoint_ports else self._default_endpoint_ports(manifest)
        for ep_spec in manifest.endpoints:
            rport = resolved_ports.get(ep_spec.id) or ep_spec.port
            if not rport:
                continue
            try:
                forwarded.append(
                    self.open_tunnel(
                        session_name,
                        remote_port=rport,
                        local_port=rport,
                        label=self._localized_label(ep_spec.label, ep_spec.id),
                        labels=dict(ep_spec.label),
                        path=ep_spec.path,
                        kind=ep_spec.kind,
                    )
                )
            except Exception:
                if not best_effort:
                    raise
        return forwarded

    def _reconcile_real_runs(self) -> None:
        """Reconciles or re-attaches in-progress (and live ready) real deployments in ``data/runs.json`` across restarts."""
        with self._lock:
            candidates = [
                r.model_copy(deep=True)
                for r in self._real_runs.values()
                if r.state in ("provisioning", "starting", "running") and r.run_id not in self._run_cancels
            ]
            ready_to_restore = [
                r.model_copy(deep=True)
                for r in self._real_runs.values()
                if r.state == "ready" and (bool(r.endpoints) or self.settings.auto_forward_endpoints)
            ]
        if not candidates and not ready_to_restore:
            return

        assignments_cache: dict[str, list[RawAssignment]] = {}
        auth_errors: dict[str, str] = {}
        transient_errors: dict[str, str] = {}

        def _ensure_acct_assignments(acc_id: str) -> None:
            if acc_id in assignments_cache or acc_id in auth_errors or acc_id in transient_errors:
                return
            with self._lock:
                has_acc = acc_id in self._real_accounts
            tok = self.vault.get_account_token(acc_id) if has_acc else None
            if not has_acc or not tok:
                auth_errors[acc_id] = "Account credentials unavailable after console restart."
                return
            try:
                assignments_cache[acc_id] = self._direct_backend.list_assignments(acc_id, tok)
            except ColabBackendError as exc:
                if exc.kind in ("auth_error", "missing_credential"):
                    auth_errors[acc_id] = exc.message
                else:
                    transient_errors[acc_id] = exc.message
            except Exception as exc:
                transient_errors[acc_id] = str(exc)

        for run in candidates:
            try:
                recipe = self.get_loaded_recipe(run.recipe_id)
            except KeyError:
                recipe = None

            stored_input_secrets = self.vault.get_run_secrets(run.run_id)
            cap_secrets: list[str] = []
            if recipe is not None:
                _, cap_secrets = self._collect_capability_secrets(recipe.manifest)
                if not run.keep_paths and recipe.manifest.keep:
                    run.keep_paths = [KeptArtifactInfo(path=k.path, label=k.label) for k in recipe.manifest.keep]
            secrets = list(dict.fromkeys([*stored_input_secrets, *cap_secrets, *self.vault.all_known_secrets()]))

            if recipe is None:
                self._mark_real_run(
                    run.run_id,
                    secrets,
                    state="failed",
                    message=f"Recipe '{run.recipe_id}' is no longer installed.",
                    finished_at=_utc_now(),
                )
                continue

            acc_id = run.account_id
            _ensure_acct_assignments(acc_id)

            if acc_id in auth_errors:
                self._mark_real_run(
                    run.run_id,
                    secrets,
                    state="failed",
                    message=auth_errors[acc_id],
                    finished_at=_utc_now(),
                )
                continue

            if acc_id in transient_errors:
                if recipe is not None and run.session_endpoint:
                    self._spawn_reattached_poller(run, recipe, secrets, initial_raw=None)
                else:
                    self._mark_real_run(
                        run.run_id,
                        secrets,
                        state="failed",
                        message=f"Could not verify runtime after restart: {transient_errors[acc_id]}",
                        finished_at=_utc_now(),
                    )
                continue

            raw = self._match_assignment_for_run(run, assignments_cache.get(acc_id, []))
            if raw is None:
                self._mark_real_run(
                    run.run_id,
                    secrets,
                    state="failed",
                    message="Colab runtime was unassigned while local console was offline.",
                    finished_at=_utc_now(),
                )
                continue

            vm_run_dir = f"/root/.colab-boost/runs/{run.run_id}"
            keep_specs = recipe.manifest.keep if recipe is not None else []
            poll_snippet = self._build_poll_snippet(vm_run_dir, keep_specs)
            try:
                pr = self._direct_backend.execute_python(raw, poll_snippet, timeout_s=25.0)
            except Exception:
                if recipe is not None:
                    self._mark_real_run(run.run_id, secrets, session_endpoint=raw.endpoint)
                    self._spawn_reattached_poller(run, recipe, secrets, initial_raw=raw)
                else:
                    self._mark_real_run(
                        run.run_id,
                        secrets,
                        state="failed",
                        message="Failed to query remote worker after console restart.",
                        finished_at=_utc_now(),
                    )
                continue

            payload = pr.marker_json or {}
            st = payload.get("status") or {}
            tail = payload.get("tail") or []
            alive = bool(payload.get("alive", False))
            exists = bool(payload.get("exists", bool(st or tail or "alive" in payload)))
            merged_keep = self._merge_keep_stats(run.run_id, payload.get("keep_stats")) or run.keep_paths

            if not exists or (not st and not alive):
                self._mark_real_run(
                    run.run_id,
                    secrets,
                    session_endpoint=raw.endpoint,
                    state="failed",
                    message=(
                        "Local console restarted before worker launched on VM; "
                        "check GPU Sessions to reuse or stop the runtime."
                    ),
                    finished_at=_utc_now(),
                )
                continue

            rem_state = str(st.get("state") or ("running" if alive else "failed"))
            stage = str(st.get("stage") or run.stage)
            msg = str(st.get("message") or "Re-attached to worker on Colab VM...")
            prog = st.get("progress")
            outputs = st.get("outputs") or run.outputs

            if rem_state == "ready":
                forwarded: list[TunnelInfo] = []
                if recipe is not None and self.settings.backend_mode == BackendMode.REAL:
                    ports = dict(run.endpoint_ports) if run.endpoint_ports else self._default_endpoint_ports(recipe.manifest)
                    forwarded = self._forward_recipe_endpoints(
                        recipe.manifest, run.session_name, ports, best_effort=True
                    )
                closed_stale = [ep.model_copy(update={"status": "closed"}) for ep in run.endpoints]
                self._mark_real_run(
                    run.run_id,
                    secrets,
                    session_endpoint=raw.endpoint,
                    state="ready",
                    stage=stage,
                    progress=1.0,
                    message=msg,
                    outputs=outputs,
                    endpoints=forwarded if forwarded else closed_stale,
                    keep_paths=merged_keep,
                    log_tail=tail,
                    finished_at=_utc_now(),
                )
                continue

            if rem_state == "failed" or (not alive and rem_state != "ready") or recipe is None:
                fail_msg = (
                    f"Recipe '{run.recipe_id}' is no longer installed."
                    if recipe is None and rem_state not in ("failed",)
                    else (msg if rem_state == "failed" else "Worker exited while local console was offline.")
                )
                self._mark_real_run(
                    run.run_id,
                    secrets,
                    session_endpoint=raw.endpoint,
                    state="failed",
                    stage=stage,
                    message=fail_msg,
                    outputs=outputs,
                    keep_paths=merged_keep,
                    log_tail=tail,
                    finished_at=_utc_now(),
                )
                continue

            # Remote worker is alive & in-progress: update snapshot and spawn live re-attach poller!
            self._mark_real_run(
                run.run_id,
                secrets,
                session_endpoint=raw.endpoint,
                state="running" if rem_state == "running" else "starting",
                stage=stage,
                progress=prog,
                message=msg,
                outputs=outputs,
                keep_paths=merged_keep,
                log_tail=tail,
            )
            self._spawn_reattached_poller(run, recipe, secrets, initial_raw=raw)

        # Restore localhost SSH port forwards for ready deployments whose Colab VMs are still alive
        for run in ready_to_restore:
            _ensure_acct_assignments(run.account_id)
            if run.account_id in transient_errors:
                continue
            raw = (
                None
                if run.account_id in auth_errors
                else self._match_assignment_for_run(run, assignments_cache.get(run.account_id, []))
            )
            if raw is None:
                if any(ep.status in ("starting", "open") for ep in run.endpoints):
                    closed_eps = [ep.model_copy(update={"status": "closed"}) for ep in run.endpoints]
                    self._mark_real_run(run.run_id, [], endpoints=closed_eps)
                continue
            try:
                recipe = self.get_loaded_recipe(run.recipe_id)
            except KeyError:
                recipe = None
            if recipe is not None and self.settings.backend_mode == BackendMode.REAL and self.settings.auto_forward_endpoints:
                live_ids = {t.id for t in self.tunnels.list_tunnels(run.session_name) if t.status == "open"}
                if not run.endpoints or not all(ep.id in live_ids for ep in run.endpoints):
                    ports = dict(run.endpoint_ports) if run.endpoint_ports else self._default_endpoint_ports(recipe.manifest)
                    forwarded = self._forward_recipe_endpoints(
                        recipe.manifest, run.session_name, ports, best_effort=True
                    )
                    if forwarded:
                        self._mark_real_run(run.run_id, [], session_endpoint=raw.endpoint, endpoints=forwarded)
                    elif any(ep.status in ("starting", "open") for ep in run.endpoints):
                        closed_eps = [ep.model_copy(update={"status": "closed"}) for ep in run.endpoints]
                        self._mark_real_run(run.run_id, [], session_endpoint=raw.endpoint, endpoints=closed_eps)

    def _mark_real_run(self, run_id: str, secrets: list[str], **patch: Any) -> None:
        new_state = patch.get("state")
        with self._lock:
            run = self._real_runs.get(run_id)
            if not run:
                return
            for k, v in patch.items():
                if k == "message" and isinstance(v, str):
                    v = redact_secrets(v, secrets)
                elif k == "log_tail" and isinstance(v, list):
                    v = [redact_secrets(str(line), secrets) for line in v]
                setattr(run, k, v)
            self._save_real_runs()
            snap = self._localize_run_copy(run)
        if new_state in ("ready", "failed", "cancelled"):
            self.vault.delete_run_secrets(run_id)
        self._emit("run_updated", snap.model_dump(mode="json"))

    def _spawn_reattached_poller(
        self,
        run: DeploymentRun,
        recipe: LoadedRecipe,
        secrets: list[str],
        *,
        initial_raw: Optional[RawAssignment],
    ) -> None:
        cancel_ev = threading.Event()
        with self._lock:
            if run.run_id in self._run_cancels:
                return
            self._run_cancels[run.run_id] = cancel_ev
            acc = self._real_accounts.get(run.account_id)
            acc_email = acc.email if acc else ""

        endpoint = initial_raw.endpoint if initial_raw else (run.session_endpoint or "")
        ports = dict(run.endpoint_ports) if run.endpoint_ports else self._default_endpoint_ports(recipe.manifest)
        sess_stub = SessionInfo(
            name=run.session_name,
            endpoint=endpoint,
            account_id=run.account_id,
            account_email=acc_email,
            accelerator=initial_raw.accelerator if initial_raw else recipe.manifest.provision.accelerator,
            machine_shape=initial_raw.machine_shape if initial_raw else recipe.manifest.provision.accelerator and "STANDARD",  # type: ignore[arg-type]
        )

        def _worker() -> None:
            try:
                self._poll_real_deployment_loop(
                    run_id=run.run_id,
                    recipe=recipe,
                    session_info=sess_stub,
                    secrets=secrets,
                    endpoint_ports=ports,
                    cancel_ev=cancel_ev,
                    initial_raw=initial_raw,
                    use_direct_backend=True,
                )
            except Exception as exc:
                if self._shutdown_ev.is_set():
                    return
                if run.provisioned_session and run.stop_session_on_failure and sess_stub.endpoint:
                    try:
                        self.stop_session(sess_stub.endpoint, account_id=sess_stub.account_id, reason="deploy_failed_cleanup")
                    except Exception:
                        pass
                st = "cancelled" if cancel_ev.is_set() else "failed"
                self._mark_real_run(run.run_id, secrets, state=st, message=str(exc), finished_at=_utc_now())
            finally:
                with self._lock:
                    self._run_cancels.pop(run.run_id, None)

        threading.Thread(target=_worker, daemon=True).start()

    def start_deployment(self, req: DeployRequest, *, run_sync: bool = False) -> DeploymentRun:
        recipe = self.get_loaded_recipe(req.recipe_id)
        manifest = recipe.manifest
        env_inputs, input_secrets, endpoint_ports = validate_and_build_env(manifest, req.inputs)

        acc = self.active_account()
        if not acc:
            raise ColabBackendError("no_account", "Sign in to a Google account before deploying a model.")

        cap_env, cap_secrets = self._collect_capability_secrets(manifest)
        all_secrets = [*input_secrets, *cap_secrets]

        provisioned_fresh = req.session_name is None
        prov_req = req.provision or ProvisionRequest(
            accelerator=manifest.provision.accelerator,
            min_vram_mib=manifest.provision.min_vram_mib,
            high_mem=manifest.provision.high_mem,
        )
        target_sess_name = req.session_name or (prov_req.name or f"boost-{manifest.id[:10]}-{uuid.uuid4().hex[:4]}")
        prov_req.name = target_sess_name

        run_id = f"run-{uuid.uuid4().hex[:8]}"
        if not self.backend.is_mock and input_secrets:
            self.vault.set_run_secrets(run_id, input_secrets)
        cancel_ev = threading.Event()
        initial_keep = [KeptArtifactInfo(path=k.path, label=k.label) for k in manifest.keep]
        run = DeploymentRun(
            run_id=run_id,
            recipe_id=manifest.id,
            recipe_name=manifest.name,
            account_id=acc.id,
            session_name=target_sess_name,
            provisioned_session=provisioned_fresh,
            stop_session_on_failure=req.stop_session_on_failure,
            endpoint_ports=dict(endpoint_ports),
            state="provisioning" if provisioned_fresh else "starting",
            stage=manifest.stages[0].id if manifest.stages else "init",
            progress=0.0,
            message="Allocating GPU runtime..." if provisioned_fresh else f"Attaching to {target_sess_name}...",
            keep_paths=initial_keep,
            started_at=_utc_now(),
        )
        with self._lock:
            self._runs_dict()[run_id] = run
            self._run_cancels[run_id] = cancel_ev
            self._persist_runs_if_real()
        self._emit("run_updated", run.model_dump(mode="json"))

        worker_args = (
            run_id,
            recipe,
            prov_req,
            provisioned_fresh,
            env_inputs,
            cap_env,
            all_secrets,
            endpoint_ports,
            req.stop_session_on_failure,
            cancel_ev,
        )
        if run_sync:
            self._execute_deployment(*worker_args)
        else:
            threading.Thread(target=self._execute_deployment, args=worker_args, daemon=True).start()
        return self.get_run(run_id)

    def cancel_deployment(self, run_id: str) -> DeploymentRun:
        cleaned_real = False
        with self._lock:
            ev = self._run_cancels.get(run_id)
            run = self._runs_dict().get(run_id)
            if not run:
                raise KeyError(f"Unknown deployment run: {run_id}")
            if ev:
                ev.set()
            if run.state in ("provisioning", "starting", "running"):
                run.state = "cancelled"
                run.message = "Cancelling deployment..."
                self._persist_runs_if_real()
                cleaned_real = not self.backend.is_mock and ev is None
        if cleaned_real:
            self.vault.delete_run_secrets(run_id)
        return self.get_run(run_id)

    def _update_run(self, run_id: str, secrets: list[str], **patch: Any) -> None:
        new_state = patch.get("state")
        is_real = not self.backend.is_mock
        with self._lock:
            run = self._runs_dict().get(run_id)
            if not run:
                return
            for k, v in patch.items():
                if k == "message" and isinstance(v, str):
                    v = redact_secrets(v, secrets)
                elif k == "log_tail" and isinstance(v, list):
                    v = [redact_secrets(str(line), secrets) for line in v]
                setattr(run, k, v)
            self._persist_runs_if_real()
            snap = self._localize_run_copy(run)
        if is_real and new_state in ("ready", "failed", "cancelled"):
            self.vault.delete_run_secrets(run_id)
        self._emit("run_updated", snap.model_dump(mode="json"))

    def _execute_deployment(
        self,
        run_id: str,
        recipe: LoadedRecipe,
        prov_req: ProvisionRequest,
        provisioned_fresh: bool,
        env_inputs: dict[str, str],
        cap_env: dict[str, str],
        secrets: list[str],
        endpoint_ports: dict[str, int],
        stop_on_failure: bool,
        cancel_ev: threading.Event,
    ) -> None:
        manifest = recipe.manifest
        session_info: Optional[SessionInfo] = None
        try:
            if cancel_ev.is_set():
                self._update_run(
                    run_id,
                    secrets,
                    state="cancelled",
                    message="Deployment cancelled before provisioning; 0 VMs billed.",
                    finished_at=_utc_now(),
                )
                return

            if provisioned_fresh:
                session_info = self.provision_session(prov_req, cancel_ev=cancel_ev)
            else:
                sessions = self.list_sessions(include_all_accounts=True)
                session_info = next((s for s in sessions if s.name == prov_req.name), None)
                if not session_info:
                    raise ColabBackendError("session_missing", f"Session '{prov_req.name}' not found.")

            self._update_run(run_id, secrets, session_endpoint=session_info.endpoint)

            if cancel_ev.is_set():
                if provisioned_fresh and session_info:
                    self.stop_session(session_info.endpoint, account_id=session_info.account_id, reason="deploy_cancelled")
                self._update_run(run_id, secrets, state="cancelled", message="Deployment cancelled; runtime released.", finished_at=_utc_now())
                return

            if self.backend.is_mock:
                self._simulate_mock_deployment(run_id, manifest, session_info, env_inputs, secrets, endpoint_ports, cancel_ev)
            else:
                self._drive_real_deployment(
                    run_id, recipe, session_info, env_inputs, cap_env, secrets, endpoint_ports, cancel_ev
                )
        except Exception as exc:
            if self._shutdown_ev.is_set():
                return
            if provisioned_fresh and stop_on_failure and session_info:
                try:
                    self.stop_session(session_info.endpoint, account_id=session_info.account_id, reason="deploy_failed_cleanup")
                except Exception:
                    pass
            state = "cancelled" if cancel_ev.is_set() else "failed"
            self._update_run(run_id, secrets, state=state, message=str(exc), finished_at=_utc_now())
        finally:
            with self._lock:
                self._run_cancels.pop(run_id, None)

    def _simulate_mock_deployment(
        self,
        run_id: str,
        manifest: RecipeManifest,
        session_info: SessionInfo,
        env_inputs: dict[str, str],
        secrets: list[str],
        endpoint_ports: dict[str, int],
        cancel_ev: threading.Event,
    ) -> None:
        logs: list[str] = [
            f"[colab-boost] Attached to {session_info.name} ({session_info.gpu_name or session_info.accelerator.value})"
        ]
        if manifest.keep:
            self._configure_vm_keep_hooks(
                None, session_info.endpoint, session_info.name, session_info.account_id, run_id, manifest.keep
            )
            joined_keep = ", ".join(kp.path for kp in manifest.keep)
            logs.append(f"[colab-boost] Armed [[keep]] wind-down preservation for: {joined_keep}")

        stages = manifest.stages or []
        total = max(1, len(stages))
        repo = env_inputs.get("BOOST_INPUT_REPO") or env_inputs.get("BOOST_INPUT_MODEL_SIZE") or manifest.id

        for idx, st in enumerate(stages):
            if cancel_ev.is_set():
                if self.get_run(run_id).provisioned_session:
                    self.stop_session(session_info.endpoint, account_id=session_info.account_id, reason="deploy_cancelled")
                self._update_run(run_id, secrets, state="cancelled", message="Cancelled by user; runtime stopped.", finished_at=_utc_now())
                return
            frac = round((idx + 0.5) / total, 2)
            label_en = st.label.get("en") or st.id
            logs.append(f"[{time.strftime('%H:%M:%S')}] Stage '{st.id}': {label_en} ({repo})")
            self._update_run(
                run_id,
                secrets,
                state="running",
                stage=st.id,
                progress=frac,
                message=f"{label_en}...",
                log_tail=list(logs[-40:]),
            )
            if cancel_ev.wait(self.mock_step_delay_s):
                if self.get_run(run_id).provisioned_session:
                    self.stop_session(session_info.endpoint, account_id=session_info.account_id, reason="deploy_cancelled")
                self._update_run(run_id, secrets, state="cancelled", message="Cancelled by user; runtime stopped.", finished_at=_utc_now())
                return

        model_alias = str(repo).rsplit("/", 1)[-1].lower().replace("-gguf", "")
        chosen_quant = env_inputs.get("BOOST_INPUT_QUANT", "Q8_0" if (session_info.vram_total_mib or 0) >= 70000 else "Q6_K")
        if chosen_quant == "auto":
            chosen_quant = "Q8_0" if (session_info.vram_total_mib or 0) >= 70000 else "Q4_K_M"
        used_vram = min((session_info.vram_total_mib or 15360) - 2048, 34800 if (session_info.vram_total_mib or 0) >= 40000 else 11200)
        self._mock_backend.set_vm_vram_usage(session_info.endpoint, max(1024, used_vram), 42.0)

        forwarded = self._forward_recipe_endpoints(manifest, session_info.name, endpoint_ports)
        for tun in forwarded:
            logs.append(f"[colab-boost] Tunnel active: {tun.local_url} -> VM:{tun.remote_port}")

        keep_ready = [
            KeptArtifactInfo(path=kp.path, label=kp.label, file_count=2, total_bytes=2048)
            for kp in manifest.keep
        ]
        outputs: dict[str, Any] = {
            "model_id": model_alias,
            "quant": chosen_quant,
            "gpu": session_info.gpu_name or session_info.accelerator.value,
            "vram_used_mib": max(1024, used_vram),
            "vram_total_mib": session_info.vram_total_mib or 0,
            "tok_per_s": 78.4 if "A100" in (session_info.gpu_name or "") else 41.2,
        }
        if manifest.keep:
            outputs["keep_paths"] = [kp.path for kp in manifest.keep]
        logs.append(f"[colab-boost] Deployment READY: {model_alias} ({outputs['tok_per_s']} tok/s)")
        self._update_run(
            run_id,
            secrets,
            state="ready",
            stage=stages[-1].id if stages else "ready",
            progress=1.0,
            message=f"{model_alias} ready (~{outputs['tok_per_s']} tok/s)",
            outputs=outputs,
            endpoints=forwarded,
            keep_paths=keep_ready,
            log_tail=list(logs[-40:]),
            finished_at=_utc_now(),
        )

    def _drive_real_deployment(
        self,
        run_id: str,
        recipe: LoadedRecipe,
        session_info: SessionInfo,
        env_inputs: dict[str, str],
        cap_env: dict[str, str],
        secrets: list[str],
        endpoint_ports: dict[str, int],
        cancel_ev: threading.Event,
    ) -> None:
        tok_init = self._get_token_for_account(session_info.account_id)
        items_init = self.backend.list_assignments(session_info.account_id, tok_init)
        raw = next((a for a in items_init if a.endpoint == session_info.endpoint), None)
        if not raw:
            raise ColabBackendError("runtime_lost", f"Runtime {session_info.endpoint} vanished.")

        if recipe.manifest.keep:
            self._configure_vm_keep_hooks(
                raw,
                session_info.endpoint,
                session_info.name,
                session_info.account_id,
                run_id,
                recipe.manifest.keep,
            )

        vm_run_dir = f"/root/.colab-boost/runs/{run_id}"
        worker_name = Path(recipe.manifest.run.script).name
        worker_env = {
            "BOOST_RECIPE_ID": recipe.manifest.id,
            "BOOST_RUN_ID": run_id,
            "BOOST_RUN_DIR": vm_run_dir,
            "BOOST_STATUS_FILE": f"{vm_run_dir}/status.json",
            "BOOST_HEARTBEAT_FILE": "/root/.colab-boost/heartbeat",
            **env_inputs,
            **cap_env,
        }
        interp = "[sys.executable, '-u']" if worker_name.endswith(".py") else "['bash']"
        launch_code = (
            "import json, os, subprocess, sys\n"
            f"run_dir = {vm_run_dir!r}\n"
            "os.makedirs(run_dir, exist_ok=True)\n"
            f"script_path = run_dir + '/' + {worker_name!r}\n"
            f"open(script_path, 'w', encoding='utf-8').write({recipe.script_text!r})\n"
            "os.chmod(script_path, 0o700)\n"
            "child_env = dict(os.environ)\n"
            "log_fh = open(run_dir + '/run.log', 'w', encoding='utf-8')\n"
            f"p = subprocess.Popen({interp} + [script_path], cwd=run_dir, env=child_env, "
            "stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT, start_new_session=True)\n"
            "open(run_dir + '/worker.pid', 'w').write(str(p.pid))\n"
            f"print({RESULT_MARKER!r} + json.dumps({{'pid': p.pid}}), flush=True)\n"
        )
        res = self.backend.execute_python(raw, launch_code, env=worker_env, timeout_s=30.0)
        if not res.ok or not res.marker_json:
            raise ColabBackendError("worker_launch_failed", f"Failed to start worker on VM: {res.stderr[:240]}")

        self._poll_real_deployment_loop(
            run_id=run_id,
            recipe=recipe,
            session_info=session_info,
            secrets=secrets,
            endpoint_ports=endpoint_ports,
            cancel_ev=cancel_ev,
            initial_raw=raw,
            use_direct_backend=False,
        )

    def _poll_real_deployment_loop(
        self,
        *,
        run_id: str,
        recipe: LoadedRecipe,
        session_info: SessionInfo,
        secrets: list[str],
        endpoint_ports: dict[str, int],
        cancel_ev: threading.Event,
        initial_raw: Optional[RawAssignment],
        use_direct_backend: bool = False,
    ) -> None:
        be = self._direct_backend if use_direct_backend else self.backend
        update_fn = self._mark_real_run if use_direct_backend else self._update_run

        def fetch_fresh_assignment() -> RawAssignment:
            tok_now = self.vault.get_account_token(session_info.account_id) if use_direct_backend else self._get_token_for_account(session_info.account_id)
            if not tok_now:
                raise ColabBackendError("missing_credential", f"No credential for '{session_info.account_id}'.")
            items = be.list_assignments(session_info.account_id, tok_now)
            found = next((a for a in items if a.endpoint == session_info.endpoint), None)
            if not found:
                raise ColabBackendError("runtime_lost", f"Runtime {session_info.endpoint} vanished.")
            self.tunnels.update_session_credentials(session_info.name, found.proxy_url, found.proxy_token)
            return found

        raw = initial_raw or fetch_fresh_assignment()
        raw_fetched_at = time.monotonic()
        vm_run_dir = f"/root/.colab-boost/runs/{run_id}"
        poll_snippet = self._build_poll_snippet(vm_run_dir, recipe.manifest.keep)

        consecutive_poll_errors = 0
        while not self._shutdown_ev.is_set():
            if cancel_ev.wait(6.0):
                kill_code = (
                    "import os, signal\n"
                    f"pfile = {vm_run_dir!r} + '/worker.pid'\n"
                    "if os.path.exists(pfile):\n"
                    "    pid = int(open(pfile).read().strip())\n"
                    "    try: os.killpg(pid, signal.SIGTERM)\n"
                    "    except OSError: pass\n"
                )
                try:
                    be.execute_python(raw, kill_code, timeout_s=15.0)
                except Exception:
                    pass
                with self._lock:
                    r_obj = (self._real_runs if use_direct_backend else self._runs_dict()).get(run_id)
                    was_provisioned = bool(r_obj and r_obj.provisioned_session)
                if was_provisioned:
                    try:
                        self.stop_session(session_info.endpoint, account_id=session_info.account_id, reason="deploy_cancelled")
                    except Exception:
                        pass
                update_fn(run_id, secrets, state="cancelled", message="Cancelled by user.", finished_at=_utc_now())
                return

            if self._shutdown_ev.is_set():
                return

            # Proactively refresh Colab runtime proxy token (~1h TTL) before it expires
            refresh_after = max(60.0, float(raw.token_expires_in_seconds) - 300.0)
            if time.monotonic() - raw_fetched_at >= refresh_after:
                try:
                    raw = fetch_fresh_assignment()
                    raw_fetched_at = time.monotonic()
                except Exception:
                    pass

            try:
                pr = be.execute_python(raw, poll_snippet, timeout_s=25.0)
                consecutive_poll_errors = 0
            except Exception:
                consecutive_poll_errors += 1
                if consecutive_poll_errors >= 3:
                    raise
                try:
                    raw = fetch_fresh_assignment()
                    raw_fetched_at = time.monotonic()
                except ColabBackendError as re_exc:
                    if re_exc.kind == "runtime_lost":
                        raise
                continue

            payload = pr.marker_json or {}
            st = payload.get("status") or {}
            tail = payload.get("tail") or []
            alive = bool(payload.get("alive", True))
            merged_keep = self._merge_keep_stats(run_id, payload.get("keep_stats"))

            state = str(st.get("state") or "starting")
            stage = str(st.get("stage") or "")
            msg = str(st.get("message") or "Starting worker...")
            prog = st.get("progress")
            outputs = st.get("outputs") or {}

            if state == "ready":
                forwarded: list[TunnelInfo] = []
                if self.settings.backend_mode == BackendMode.REAL or not use_direct_backend:
                    forwarded = self._forward_recipe_endpoints(
                        recipe.manifest,
                        session_info.name,
                        endpoint_ports,
                        best_effort=use_direct_backend,
                    )
                extra_ready: dict[str, Any] = {}
                if merged_keep is not None:
                    extra_ready["keep_paths"] = merged_keep
                update_fn(
                    run_id,
                    secrets,
                    state="ready",
                    stage=stage,
                    progress=1.0,
                    message=msg,
                    outputs=outputs,
                    endpoints=forwarded,
                    log_tail=tail,
                    finished_at=_utc_now(),
                    **extra_ready,
                )
                return

            if state == "failed" or (not alive and state != "ready"):
                raise ColabBackendError("worker_failed", msg if state == "failed" else "Worker exited unexpectedly on VM.")

            extra_run: dict[str, Any] = {}
            if merged_keep is not None:
                extra_run["keep_paths"] = merged_keep
            update_fn(
                run_id,
                secrets,
                state="running",
                stage=stage,
                progress=prog,
                message=msg,
                outputs=outputs,
                log_tail=tail,
                **extra_run,
            )

    # -------------------- Agent Direct Execution & File Transfer --------------------

    @staticmethod
    def _validate_remote_path(remote_path: str) -> str:
        cleaned = (remote_path or "").strip()
        if not cleaned.startswith("/") or ".." in cleaned.split("/") or "\x00" in cleaned:
            raise ValueError(f"Invalid remote VM path: {remote_path!r} (must be absolute without '..')")
        return cleaned

    def _validate_local_path(self, local_path: str, *, mode: Literal["read", "write"] = "read") -> Path:
        """Validates host-side upload/download paths against sensitive app, credential, and system directories."""
        raw_text = (local_path or "").strip()
        if not raw_text or "\x00" in raw_text:
            raise ValueError(f"Invalid local path: {local_path!r}")

        # Reject explicit POSIX system paths even when evaluated on Windows
        posix_norm = raw_text.replace("\\", "/")
        for posix_sys in ("/etc", "/proc", "/sys"):
            if posix_norm == posix_sys or posix_norm.startswith(posix_sys + "/"):
                raise ValueError(f"Access to protected system path is forbidden ({mode}): {local_path!r}")

        expanded = Path(raw_text).expanduser()
        abs_candidate = Path(os.path.abspath(str(expanded)))
        real_candidate = Path(os.path.realpath(str(expanded)))
        candidates = (abs_candidate, real_candidate)

        app_home = Path(os.path.realpath(str(self.paths.home)))
        app_home_abs = Path(os.path.abspath(str(self.paths.home)))
        forbidden_roots: list[Path] = []
        for base in (app_home, app_home_abs):
            forbidden_roots.extend(
                [
                    base / "vault.local.json",
                    base / "accounts.json",
                    base / "settings.json",
                    base / "keys",
                    base / "ssh",
                ]
            )

        user_home = Path.home()
        for home_root in (Path(os.path.abspath(str(user_home))), Path(os.path.realpath(str(user_home)))):
            forbidden_roots.extend(
                [
                    home_root / ".ssh",
                    home_root / ".aws",
                    home_root / ".gnupg",
                    home_root / ".config" / "gcloud",
                    home_root / ".gemini",
                ]
            )

        if os.name == "nt":
            win_dirs = {
                r"C:\Windows",
                os.environ.get("SystemRoot", "").strip(),
                os.environ.get("WINDIR", "").strip(),
            }
            for wd in win_dirs:
                if wd:
                    forbidden_roots.append(Path(os.path.abspath(wd)))
        else:
            forbidden_roots.extend([Path("/etc"), Path("/proc"), Path("/sys")])

        for cand in candidates:
            for root in forbidden_roots:
                if _path_is_within(cand, root):
                    raise ValueError(f"Access to protected local path is forbidden ({mode}): {local_path!r}")

        allowed_env = os.environ.get("COLAB_BOOST_ALLOWED_LOCAL_ROOTS", "").strip()
        if allowed_env:
            allowed_roots = [
                Path(os.path.realpath(str(Path(item.strip()).expanduser())))
                for item in allowed_env.split(os.pathsep)
                if item.strip()
            ]
            if allowed_roots and not any(_path_is_within(real_candidate, ar) for ar in allowed_roots):
                raise ValueError(
                    f"Local path is outside COLAB_BOOST_ALLOWED_LOCAL_ROOTS ({mode}): {local_path!r}"
                )

        return real_candidate

    def _resolve_session_and_assignment(self, identifier: str) -> tuple[SessionInfo, RawAssignment]:
        sessions = self.list_sessions(include_all_accounts=True)
        target = next((s for s in sessions if s.name == identifier or s.endpoint == identifier), None)
        if not target:
            raise KeyError(f"Session '{identifier}' not found or already stopped.")
        tok = self._get_token_for_account(target.account_id)
        assignments = self.backend.list_assignments(target.account_id, tok)
        raw = next((a for a in assignments if a.endpoint == target.endpoint), None)
        if not raw:
            raise ColabBackendError("runtime_lost", f"Runtime '{target.endpoint}' is no longer active.")
        return target, raw

    def exec_on_session(
        self,
        identifier: str,
        code: str,
        *,
        shell: bool = False,
        env: Optional[dict[str, str]] = None,
        timeout_s: float = 60.0,
    ) -> dict[str, Any]:
        """Executes Python or Bash directly inside an active Colab VM and redacts known secrets."""
        if not (code or "").strip():
            raise ValueError("code cannot be empty")
        target, raw = self._resolve_session_and_assignment(identifier)
        secrets = self.vault.all_known_secrets()
        if env:
            for k, v in env.items():
                if any(tok in k.lower() for tok in ("token", "secret", "key", "pass")) and len(str(v)) >= 4:
                    secrets.append(str(v))

        if self.backend.is_mock:
            res = self.backend.execute_python(raw, code, env=env, timeout_s=timeout_s)
            return {
                "session_name": target.name,
                "endpoint": target.endpoint,
                "shell": shell,
                "ok": res.ok,
                "returncode": 0 if res.ok else 1,
                "stdout": redact_secrets(res.stdout, secrets),
                "stderr": redact_secrets(res.stderr, secrets),
                "error_name": res.error_name,
                "error_value": redact_secrets(res.error_value or "", secrets) if res.error_value else None,
                "marker_json": res.marker_json,
            }

        if shell:
            wrapper = (
                "import json, subprocess\n"
                f"p = subprocess.run(['bash', '-lc', {code!r}], capture_output=True, text=True)\n"
                f"print({RESULT_MARKER!r} + json.dumps({{'returncode': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}}), flush=True)\n"
            )
            res = self.backend.execute_python(raw, wrapper, env=env, timeout_s=timeout_s)
            m = res.marker_json or {}
            rc = int(m.get("returncode", 0 if res.ok else 1))
            out_s = str(m.get("stdout", res.stdout))
            err_s = str(m.get("stderr", res.stderr))
            return {
                "session_name": target.name,
                "endpoint": target.endpoint,
                "shell": True,
                "ok": res.ok and rc == 0,
                "returncode": rc,
                "stdout": redact_secrets(out_s, secrets),
                "stderr": redact_secrets(err_s, secrets),
                "error_name": res.error_name,
                "error_value": redact_secrets(res.error_value or "", secrets) if res.error_value else None,
                "marker_json": None,
            }

        res = self.backend.execute_python(raw, code, env=env, timeout_s=timeout_s)
        return {
            "session_name": target.name,
            "endpoint": target.endpoint,
            "shell": False,
            "ok": res.ok,
            "returncode": 0 if res.ok else 1,
            "stdout": redact_secrets(res.stdout, secrets),
            "stderr": redact_secrets(res.stderr, secrets),
            "error_name": res.error_name,
            "error_value": redact_secrets(res.error_value or "", secrets) if res.error_value else None,
            "marker_json": res.marker_json,
        }

    def upload_to_session(
        self,
        identifier: str,
        remote_path: str,
        *,
        local_path: Optional[str] = None,
        content_b64: Optional[str] = None,
        chunk_bytes: int = 4 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Uploads a local file (or base64 payload) directly to an active Colab VM with SHA256 verification."""
        rpath = self._validate_remote_path(remote_path)
        if bool(local_path) == bool(content_b64 is not None):
            raise ValueError("Provide exactly one of 'local_path' or 'content_b64'")

        validated_src: Optional[Path] = self._validate_local_path(local_path, mode="read") if local_path else None
        target, raw = self._resolve_session_and_assignment(identifier)
        if validated_src is not None:
            if not validated_src.is_file():
                raise KeyError(f"Local file not found: {validated_src}")
            data_bytes = validated_src.read_bytes()
        else:
            try:
                data_bytes = base64.b64decode(content_b64 or "", validate=True)
            except Exception as exc:
                raise ValueError(f"Invalid base64 payload: {exc}") from exc

        total_bytes = len(data_bytes)
        expected_sha = hashlib.sha256(data_bytes).hexdigest()
        total_chunks = max(1, (total_bytes + chunk_bytes - 1) // chunk_bytes)

        if self.backend.is_mock:
            self._mock_backend.put_mock_file(target.endpoint, rpath, data_bytes)
            return {
                "session_name": target.name,
                "remote_path": rpath,
                "bytes": total_bytes,
                "sha256": expected_sha,
                "chunks": total_chunks,
            }

        part_path = f"{rpath}.cboost.{uuid.uuid4().hex[:8]}.part"
        init_code = (
            "import json, os\n"
            f"p = {part_path!r}\n"
            "os.makedirs(os.path.dirname(p) or '/', exist_ok=True)\n"
            "open(p, 'wb').close()\n"
            f"print({RESULT_MARKER!r} + json.dumps({{'ok': True}}), flush=True)\n"
        )
        init_res = self.backend.execute_python(raw, init_code, timeout_s=30.0)
        if not init_res.ok:
            raise ColabBackendError("upload_failed", f"Failed to initialize remote file {rpath}: {init_res.stderr[:200]}")

        try:
            for idx in range(total_chunks):
                offset = idx * chunk_bytes
                slice_b = data_bytes[offset : offset + chunk_bytes]
                b64_chunk = base64.b64encode(slice_b).decode("ascii")
                chunk_code = (
                    "import base64, json\n"
                    f"data = base64.b64decode({b64_chunk!r})\n"
                    f"with open({part_path!r}, 'r+b') as fh:\n"
                    f"    fh.seek({offset})\n"
                    "    fh.write(data)\n"
                    f"print({RESULT_MARKER!r} + json.dumps({{'written': len(data)}}), flush=True)\n"
                )
                cres = self.backend.execute_python(raw, chunk_code, timeout_s=90.0)
                if not cres.ok:
                    raise ColabBackendError("upload_failed", f"Chunk {idx + 1}/{total_chunks} failed: {cres.stderr[:200]}")

            verify_code = (
                "import hashlib, json, os\n"
                f"part = {part_path!r}\n"
                f"dst = {rpath!r}\n"
                "h = hashlib.sha256()\n"
                "sz = 0\n"
                "with open(part, 'rb') as fh:\n"
                "    for blk in iter(lambda: fh.read(4 * 1024 * 1024), b''):\n"
                "        sz += len(blk)\n"
                "        h.update(blk)\n"
                "sha = h.hexdigest()\n"
                f"if sz != {total_bytes} or sha != {expected_sha!r}:\n"
                "    raise RuntimeError(f'integrity mismatch: size={sz} sha={sha}')\n"
                "os.replace(part, dst)\n"
                f"print({RESULT_MARKER!r} + json.dumps({{'bytes': sz, 'sha256': sha}}), flush=True)\n"
            )
            vres = self.backend.execute_python(raw, verify_code, timeout_s=90.0)
            if not vres.ok:
                raise ColabBackendError("upload_failed", f"Remote verification failed for {rpath}: {vres.stderr[:200]}")
        except Exception:
            try:
                self.backend.execute_python(
                    raw,
                    f"import os\ntry:\n    os.remove({part_path!r})\nexcept OSError:\n    pass\n",
                    timeout_s=15.0,
                )
            except Exception:
                pass
            raise

        return {
            "session_name": target.name,
            "remote_path": rpath,
            "bytes": total_bytes,
            "sha256": expected_sha,
            "chunks": total_chunks,
        }

    def download_from_session(
        self,
        identifier: str,
        remote_path: str,
        *,
        local_path: Optional[str] = None,
        chunk_bytes: int = 4 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Downloads a remote file from an active Colab VM with chunked SHA256 verification."""
        rpath = self._validate_remote_path(remote_path)
        validated_dst: Optional[Path] = self._validate_local_path(local_path, mode="write") if local_path else None
        target, raw = self._resolve_session_and_assignment(identifier)

        if self.backend.is_mock:
            raw_bytes = self._mock_backend.get_mock_file(target.endpoint, rpath)
            sha = hashlib.sha256(raw_bytes).hexdigest()
            chunks = max(1, (len(raw_bytes) + chunk_bytes - 1) // chunk_bytes)
            if validated_dst is not None:
                dst = validated_dst
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".tmp")
                tmp.write_bytes(raw_bytes)
                tmp.replace(dst)
                return {
                    "session_name": target.name,
                    "remote_path": rpath,
                    "local_path": str(dst),
                    "bytes": len(raw_bytes),
                    "sha256": sha,
                    "chunks": chunks,
                }
            if len(raw_bytes) > 16 * 1024 * 1024:
                raise ValueError("Remote file exceeds 16 MiB; provide 'local_path' to stream to disk")
            return {
                "session_name": target.name,
                "remote_path": rpath,
                "bytes": len(raw_bytes),
                "sha256": sha,
                "chunks": chunks,
                "content_b64": base64.b64encode(raw_bytes).decode("ascii"),
            }

        probe_code = (
            "import hashlib, json, os\n"
            f"p = {rpath!r}\n"
            "if not os.path.isfile(p):\n"
            f"    print({RESULT_MARKER!r} + json.dumps({{'exists': False}}), flush=True)\n"
            "else:\n"
            "    h = hashlib.sha256()\n"
            "    sz = 0\n"
            "    with open(p, 'rb') as fh:\n"
            "        for blk in iter(lambda: fh.read(4 * 1024 * 1024), b''):\n"
            "            sz += len(blk)\n"
            "            h.update(blk)\n"
            f"    print({RESULT_MARKER!r} + json.dumps({{'exists': True, 'bytes': sz, 'sha256': h.hexdigest()}}), flush=True)\n"
        )
        pres = self.backend.execute_python(raw, probe_code, timeout_s=60.0)
        if not pres.ok or not pres.marker_json:
            raise ColabBackendError("download_failed", f"Failed to probe remote file {rpath}: {pres.stderr[:200]}")
        if not pres.marker_json.get("exists"):
            raise KeyError(f"Remote file not found: {rpath}")

        total_bytes = int(pres.marker_json["bytes"])
        expected_sha = str(pres.marker_json["sha256"])
        if local_path is None and total_bytes > 16 * 1024 * 1024:
            raise ValueError("Remote file exceeds 16 MiB; provide 'local_path' to stream to disk")

        total_chunks = max(1, (total_bytes + chunk_bytes - 1) // chunk_bytes)
        hasher = hashlib.sha256()
        received = 0
        chunks_buf: list[bytes] = []

        dst: Optional[Path] = None
        tmp_path: Optional[Path] = None
        if validated_dst is not None:
            dst = validated_dst
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = dst.with_name(dst.name + f".{uuid.uuid4().hex[:6]}.tmp")

        try:
            fh = tmp_path.open("wb") if tmp_path is not None else None
            try:
                for idx in range(total_chunks):
                    offset = idx * chunk_bytes
                    read_code = (
                        "import base64, json\n"
                        f"with open({rpath!r}, 'rb') as fh:\n"
                        f"    fh.seek({offset})\n"
                        f"    blk = fh.read({chunk_bytes})\n"
                        f"print({RESULT_MARKER!r} + json.dumps({{'b64': base64.b64encode(blk).decode('ascii')}}), flush=True)\n"
                    )
                    rres = self.backend.execute_python(raw, read_code, timeout_s=90.0)
                    if not rres.ok or not rres.marker_json or "b64" not in rres.marker_json:
                        raise ColabBackendError("download_failed", f"Chunk {idx + 1}/{total_chunks} failed: {rres.stderr[:200]}")
                    blk = base64.b64decode(rres.marker_json["b64"])
                    received += len(blk)
                    hasher.update(blk)
                    if fh is not None:
                        fh.write(blk)
                    else:
                        chunks_buf.append(blk)
            finally:
                if fh is not None:
                    fh.close()

            actual_sha = hasher.hexdigest()
            if received != total_bytes or actual_sha != expected_sha:
                raise ColabBackendError(
                    "download_failed",
                    f"Download integrity mismatch for {rpath}: bytes={received}/{total_bytes} sha={actual_sha[:12]}!={expected_sha[:12]}",
                )
            if tmp_path is not None and dst is not None:
                tmp_path.replace(dst)
                return {
                    "session_name": target.name,
                    "remote_path": rpath,
                    "local_path": str(dst),
                    "bytes": total_bytes,
                    "sha256": actual_sha,
                    "chunks": total_chunks,
                }
            return {
                "session_name": target.name,
                "remote_path": rpath,
                "bytes": total_bytes,
                "sha256": actual_sha,
                "chunks": total_chunks,
                "content_b64": base64.b64encode(b"".join(chunks_buf)).decode("ascii"),
            }
        except Exception:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
