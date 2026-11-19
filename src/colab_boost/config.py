"""Workspace & user configuration storage for Colab Boost Console."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from colab_boost.models import AppSettings


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


class WorkspacePaths:
    """Resolves isolated state directories.

    Defaults to ``<project_root>/data`` so all receipts, ledger, SSH keys and
    settings stay neatly inside the project folder (never polluting ``~/.ssh/config``),
    or honors ``COLAB_BOOST_HOME`` for isolated tests.
    """

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        env_home = os.environ.get("COLAB_BOOST_HOME", "").strip()
        if base_dir is not None:
            self.home = Path(base_dir).resolve()
        elif env_home:
            self.home = Path(env_home).resolve()
        else:
            self.home = project_root() / "data"

        self.settings_file = self.home / "settings.json"
        self.accounts_file = self.home / "accounts.json"
        self.ledger_file = self.home / "ledger.json"
        self.runs_file = self.home / "runs.json"
        self.vault_fallback_file = self.home / "vault.local.json"
        self.receipts_dir = self.home / "receipts"
        self.keep_dir = self.home / "keep"
        self.ssh_dir = self.home / "ssh"
        self.user_recipes_dir = self.home / "recipes"
        self.bundled_recipes_dir = project_root() / "recipes"

    def ensure_dirs(self) -> None:
        for d in (self.home, self.receipts_dir, self.keep_dir, self.ssh_dir, self.user_recipes_dir):
            d.mkdir(parents=True, exist_ok=True)


class SettingsStore:
    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths

    def load(self) -> AppSettings:
        self.paths.ensure_dirs()
        if not self.paths.settings_file.exists():
            settings = AppSettings()
            env_mode = os.environ.get("COLAB_BOOST_BACKEND", "").strip().lower()
            if env_mode in ("mock", "real"):
                settings.backend_mode = env_mode  # type: ignore[assignment]
            return settings
        try:
            raw = json.loads(self.paths.settings_file.read_text(encoding="utf-8"))
            return AppSettings.model_validate(raw)
        except Exception:
            corrupt = self.paths.settings_file.with_suffix(".json.corrupt")
            try:
                self.paths.settings_file.replace(corrupt)
            except OSError:
                pass
            return AppSettings()

    def save(self, settings: AppSettings) -> AppSettings:
        self.paths.ensure_dirs()
        tmp = self.paths.settings_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(settings.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.paths.settings_file)
        return settings
