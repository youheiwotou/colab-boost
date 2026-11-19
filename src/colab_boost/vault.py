"""Multi-account credential & secret vault.

Stores Google OAuth2 tokens per account (never overwriting global CLI state)
and optional model-hub tokens (Hugging Face, Civitai) in the OS Credential
Manager via ``keyring``, with a local file fallback for headless test sandboxes.
Secrets are never exposed in logs or status responses.
"""

from __future__ import annotations

import json
import os
from typing import Optional

import keyring

from colab_boost.config import WorkspacePaths

VAULT_SERVICE = "colab-boost-console"
COLAB_CLI_SERVICE = "google-colab-cli"
COLAB_CLI_USER = "oauth_token"
ALLOWED_SECRET_SLOTS = frozenset({"hf_token", "civitai_token"})
MIN_REDACT_LENGTH = 4


def redact_secrets(text: str, secrets: list[str]) -> str:
    if not text:
        return text
    out = text
    for secret in secrets:
        if secret and len(secret) >= MIN_REDACT_LENGTH:
            out = out.replace(secret, "***")
    return out


class CredentialVault:
    def __init__(self, paths: WorkspacePaths, *, force_file_backend: bool = False) -> None:
        self.paths = paths
        env_backend = os.environ.get("COLAB_BOOST_VAULT_BACKEND", "").strip().lower()
        self.use_file = force_file_backend or (env_backend == "file")

    def _read_file_store(self) -> dict[str, str]:
        self.paths.ensure_dirs()
        if not self.paths.vault_fallback_file.exists():
            return {}
        try:
            data = json.loads(self.paths.vault_fallback_file.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_file_store(self, data: dict[str, str]) -> None:
        self.paths.ensure_dirs()
        tmp = self.paths.vault_fallback_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.paths.vault_fallback_file)

    def _get(self, slot: str) -> Optional[str]:
        if not self.use_file:
            try:
                val = keyring.get_password(VAULT_SERVICE, slot)
                if val is not None:
                    return val
            except Exception:
                pass
        return self._read_file_store().get(slot)

    def _set(self, slot: str, value: str) -> None:
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
        self._set(f"account:{account_id}", token_json)

    def get_account_token(self, account_id: str) -> Optional[str]:
        return self._get(f"account:{account_id}")

    def has_account_token(self, account_id: str) -> bool:
        return bool(self.get_account_token(account_id))

    def delete_account_token(self, account_id: str) -> None:
        self._delete(f"account:{account_id}")

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

    def all_known_secrets(self) -> list[str]:
        vals = [self.get_secret(slot) for slot in ALLOWED_SECRET_SLOTS]
        return [v for v in vals if v]

    # ---------------- Transient Per-Run Input Secrets (Never written to runs.json) ----------------

    def set_run_secrets(self, run_id: str, secrets: list[str]) -> None:
        cleaned = [s for s in secrets if s and len(s) >= MIN_REDACT_LENGTH]
        if not cleaned:
            self._delete(f"run_secrets:{run_id}")
        else:
            self._set(f"run_secrets:{run_id}", json.dumps(cleaned))

    def get_run_secrets(self, run_id: str) -> list[str]:
        raw = self._get(f"run_secrets:{run_id}")
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return [str(x) for x in parsed if isinstance(x, str) and len(x) >= MIN_REDACT_LENGTH] if isinstance(parsed, list) else []
        except Exception:
            return []

    def delete_run_secrets(self, run_id: str) -> None:
        self._delete(f"run_secrets:{run_id}")
