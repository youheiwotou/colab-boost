"""Multi-account credential & secret vault.

Stores Google OAuth2 tokens per account (never overwriting global CLI state)
and optional model-hub tokens (Hugging Face, Civitai) in the OS Credential
Manager via ``keyring``, with a local file fallback for headless test sandboxes.
Secrets are never exposed in logs or status responses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Optional

import keyring

from colab_boost.config import WorkspacePaths, atomic_write_text

VAULT_SERVICE = "colab-boost-console"
COLAB_CLI_SERVICE = "google-colab-cli"
COLAB_CLI_USER = "oauth_token"
ALLOWED_SECRET_SLOTS = frozenset({"hf_token", "civitai_token"})
MIN_REDACT_LENGTH = 4
_TOKEN_JSON_SECRET_KEYS = frozenset({"token", "access_token", "refresh_token", "id_token", "client_secret"})

_WINDOWS_USER_SID_LOCK = threading.Lock()
_WINDOWS_USER_SID: Optional[str] = None


def _restrict_windows_acl(path: Path) -> None:
    """Best-effort Windows ACL tightening so fallback vault file is readable only by the current user."""
    global _WINDOWS_USER_SID
    if sys.platform != "win32":
        return
    try:
        sid: Optional[str]
        with _WINDOWS_USER_SID_LOCK:
            if not _WINDOWS_USER_SID:
                proc = subprocess.Popen(
                    ["whoami", "/user", "/fo", "csv", "/nh"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                out = (proc.stdout.read() if proc.stdout else "").strip()
                if proc.stdout:
                    proc.stdout.close()
                proc.wait(timeout=5)
                if proc.returncode == 0 and out:
                    _WINDOWS_USER_SID = [p.strip('"') for p in out.splitlines()[0].split(",")][-1]
            sid = _WINDOWS_USER_SID

        principal = f"*{sid}" if sid else os.environ.get("USERNAME", "").strip()
        if not principal:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:(F)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except Exception:
        # Preserve compatibility in restricted/sandboxed Windows environments
        pass


def _extract_token_secrets(token_json: Optional[str]) -> list[str]:
    if not token_json:
        return []
    try:
        parsed = json.loads(token_json)
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []
    out: list[str] = []
    for k, v in parsed.items():
        if str(k) in _TOKEN_JSON_SECRET_KEYS and isinstance(v, str) and len(v) >= MIN_REDACT_LENGTH:
            out.append(v)
    return out


def redact_secrets(text: str, secrets: list[str]) -> str:
    if not text or not secrets:
        return text
    out = text
    # Replace longest secrets first so shorter substrings do not partially mask longer tokens
    unique_sorted = sorted(
        {s for s in secrets if isinstance(s, str) and len(s) >= MIN_REDACT_LENGTH},
        key=len,
        reverse=True,
    )
    for secret in unique_sorted:
        out = out.replace(secret, "***")
    return out


def redact_value(value: Any, secrets: list[str]) -> Any:
    """Recursively redacts secrets inside strings, dicts, lists, and tuples."""
    if not secrets or value is None:
        return value
    if isinstance(value, str):
        return redact_secrets(value, secrets)
    if isinstance(value, dict):
        return {
            (redact_secrets(k, secrets) if isinstance(k, str) else k): redact_value(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, secrets) for item in value)
    return value


class CredentialVault:
    def __init__(self, paths: WorkspacePaths, *, force_file_backend: bool = False) -> None:
        self.paths = paths
        env_backend = os.environ.get("COLAB_BOOST_VAULT_BACKEND", "").strip().lower()
        self.use_file = force_file_backend or (env_backend == "file")
        self._lock = threading.RLock()
        self._dynamic_secrets: set[str] = set()
        self._active_run_secrets: dict[str, list[str]] = {}
        self._account_secret_values: dict[str, list[str]] = {}

    def _read_file_store(self) -> dict[str, str]:
        with self._lock:
            self.paths.ensure_dirs()
            if not self.paths.vault_fallback_file.exists():
                return {}
            try:
                data = json.loads(self.paths.vault_fallback_file.read_text(encoding="utf-8"))
                return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            except Exception:
                return {}

    def _write_file_store(self, data: dict[str, str]) -> None:
        with self._lock:
            self.paths.ensure_dirs()
            atomic_write_text(
                self.paths.vault_fallback_file,
                json.dumps(data, indent=2),
                encoding="utf-8",
                chmod_mode=0o600,
                prepare_tmp=_restrict_windows_acl,
            )

    def _get(self, slot: str) -> Optional[str]:
        with self._lock:
            if not self.use_file:
                try:
                    val = keyring.get_password(VAULT_SERVICE, slot)
                    if val is not None:
                        return val
                except Exception:
                    pass
            return self._read_file_store().get(slot)

    def _set(self, slot: str, value: str) -> None:
        with self._lock:
            if not self.use_file:
                try:
                    keyring.set_password(VAULT_SERVICE, slot, value)
                    if keyring.get_password(VAULT_SERVICE, slot) == value:
                        return
                except Exception:
                    pass
            store = self._read_file_store()
            store[slot] = value
            self._write_file_store(store)

    def _delete(self, slot: str) -> None:
        with self._lock:
            if not self.use_file:
                try:
                    keyring.delete_password(VAULT_SERVICE, slot)
                except Exception:
                    pass
            store = self._read_file_store()
            if slot in store:
                del store[slot]
                self._write_file_store(store)

    # ---------------- Google Account OAuth Tokens ----------------

    def set_account_token(self, account_id: str, token_json: str) -> None:
        with self._lock:
            self._set(f"account:{account_id}", token_json)
            extracted = _extract_token_secrets(token_json)
            if extracted:
                self._account_secret_values[account_id] = extracted
            else:
                self._account_secret_values.pop(account_id, None)

    def get_account_token(self, account_id: str) -> Optional[str]:
        with self._lock:
            tok = self._get(f"account:{account_id}")
            if tok:
                extracted = _extract_token_secrets(tok)
                if extracted:
                    self._account_secret_values[account_id] = extracted
            return tok

    def has_account_token(self, account_id: str) -> bool:
        return bool(self.get_account_token(account_id))

    def delete_account_token(self, account_id: str) -> None:
        with self._lock:
            self._delete(f"account:{account_id}")
            self._account_secret_values.pop(account_id, None)

    def read_existing_colab_cli_token(self) -> Optional[str]:
        """Reads an existing ``google-colab-cli`` token from OS keyring or legacy path (read-only!)."""
        if self.use_file:
            return None
        try:
            tok = keyring.get_password(COLAB_CLI_SERVICE, COLAB_CLI_USER)
            if tok:
                return tok
        except Exception:
            pass
        legacy = os.path.expanduser("~/.config/colab-cli/token.json")
        if os.path.isfile(legacy):
            try:
                with open(legacy, encoding="utf-8") as fh:
                    content = fh.read().strip()
                if content:
                    return content
            except OSError:
                pass
        return None

    # ---------------- Model Hub Secrets (HF / Civitai) ----------------

    def set_secret(self, name: str, value: str) -> None:
        if name not in ALLOWED_SECRET_SLOTS:
            raise ValueError(f"Unsupported secret slot: {name!r}")
        cleaned = value.strip()
        if not cleaned:
            self._delete(f"secret:{name}")
        else:
            self._set(f"secret:{name}", cleaned)

    def get_secret(self, name: str) -> Optional[str]:
        if name not in ALLOWED_SECRET_SLOTS:
            raise ValueError(f"Unsupported secret slot: {name!r}")
        return self._get(f"secret:{name}")

    def has_secret(self, name: str) -> bool:
        return bool(self.get_secret(name))

    def delete_secret(self, name: str) -> None:
        if name not in ALLOWED_SECRET_SLOTS:
            raise ValueError(f"Unsupported secret slot: {name!r}")
        self._delete(f"secret:{name}")

    def secret_status(self) -> dict[str, bool]:
        return {slot: self.has_secret(slot) for slot in sorted(ALLOWED_SECRET_SLOTS)}

    def register_secret(self, secret: str) -> None:
        """Registers an ephemeral/dynamic secret value for redaction across logs and outputs."""
        if isinstance(secret, str) and len(secret) >= MIN_REDACT_LENGTH:
            with self._lock:
                self._dynamic_secrets.add(secret)

    def unregister_secret(self, secret: str) -> None:
        if isinstance(secret, str):
            with self._lock:
                self._dynamic_secrets.discard(secret)

    def all_known_secrets(self) -> list[str]:
        with self._lock:
            vals: list[str] = []
            for slot in ALLOWED_SECRET_SLOTS:
                v = self.get_secret(slot)
                if v and len(v) >= MIN_REDACT_LENGTH:
                    vals.append(v)
            vals.extend(self._dynamic_secrets)
            for run_vals in self._active_run_secrets.values():
                vals.extend(run_vals)
            for acc_vals in self._account_secret_values.values():
                vals.extend(acc_vals)
            # Also scan fallback file store for persisted run_secrets / account tokens across restarts
            store = self._read_file_store()
            for k, raw in store.items():
                if k.startswith("run_secrets:"):
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, list):
                            for item in parsed:
                                if isinstance(item, str) and len(item) >= MIN_REDACT_LENGTH:
                                    vals.append(item)
                    except Exception:
                        pass
                elif k.startswith("account:"):
                    vals.extend(_extract_token_secrets(raw))
            return list(dict.fromkeys(s for s in vals if isinstance(s, str) and len(s) >= MIN_REDACT_LENGTH))

    # ---------------- Transient Per-Run Input Secrets (Never written to runs.json) ----------------

    def set_run_secrets(self, run_id: str, secrets: list[str]) -> None:
        cleaned = [s for s in secrets if s and len(s) >= MIN_REDACT_LENGTH]
        with self._lock:
            if not cleaned:
                self._active_run_secrets.pop(run_id, None)
                self._delete(f"run_secrets:{run_id}")
            else:
                self._active_run_secrets[run_id] = list(dict.fromkeys(cleaned))
                self._set(f"run_secrets:{run_id}", json.dumps(self._active_run_secrets[run_id]))

    def get_run_secrets(self, run_id: str) -> list[str]:
        with self._lock:
            raw = self._get(f"run_secrets:{run_id}")
            if not raw:
                return []
            try:
                parsed = json.loads(raw)
                res = (
                    [str(x) for x in parsed if isinstance(x, str) and len(x) >= MIN_REDACT_LENGTH]
                    if isinstance(parsed, list)
                    else []
                )
                if res:
                    self._active_run_secrets[run_id] = list(dict.fromkeys(res))
                return res
            except Exception:
                return []

    def delete_run_secrets(self, run_id: str) -> None:
        with self._lock:
            self._active_run_secrets.pop(run_id, None)
            self._delete(f"run_secrets:{run_id}")
