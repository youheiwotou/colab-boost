"""Workspace & user configuration storage for Colab Boost Console."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Callable, Optional

from colab_boost.models import AppSettings


def _replace_atomic(src: Path, dst: Path) -> None:
    """Replaces ``dst`` with ``src``, retrying brief Windows sharing-violation PermissionError."""
    attempts = 6 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= attempts - 1:
                raise
            time.sleep(0.015 * (attempt + 1))


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    chmod_mode: Optional[int] = None,
    prepare_tmp: Optional[Callable[[Path], None]] = None,
) -> None:
    """Writes bytes via a unique same-directory temp file and atomic ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        if chmod_mode is not None:
            try:
                os.chmod(tmp_path, chmod_mode)
            except OSError:
                pass
        if prepare_tmp is not None:
            prepare_tmp(tmp_path)
        with open(tmp_path, "wb") as fh:
            fh.write(data)
            fh.flush()
        _replace_atomic(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    chmod_mode: Optional[int] = None,
    prepare_tmp: Optional[Callable[[Path], None]] = None,
) -> None:
    """Writes UTF-8 text via a unique same-directory temp file and atomic ``os.replace``."""
    atomic_write_bytes(
        path,
        content.encode(encoding),
        chmod_mode=chmod_mode,
        prepare_tmp=prepare_tmp,
    )


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def user_data_dir() -> Path:
    """Returns the platform-native user data directory for non-editable wheel installs."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data).resolve() / "colab-boost"
        return Path.home() / "AppData" / "Local" / "colab-boost"
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return Path(xdg_data_home).resolve() / "colab-boost"
    return Path.home() / ".local" / "share" / "colab-boost"


def resolve_bundled_recipes_dir(
    *,
    package_dir: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Path:
    """Prefers wheel-embedded ``colab_boost/recipes`` and falls back to ``<repo>/recipes``."""
    pkg_dir = (package_dir if package_dir is not None else Path(__file__).resolve().parent).resolve()
    embedded = pkg_dir / "recipes"
    if embedded.is_dir():
        return embedded
    root = (repo_root if repo_root is not None else project_root()).resolve()
    return root / "recipes"


def resolve_default_data_dir(
    *,
    package_dir: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Path:
    """Uses ``<repo>/data`` inside a source checkout, otherwise falls back to ``user_data_dir()``."""
    pkg_dir = (package_dir if package_dir is not None else Path(__file__).resolve().parent).resolve()
    in_site_packages = any(part in ("site-packages", "dist-packages") for part in pkg_dir.parts)
    root = (repo_root if repo_root is not None else project_root()).resolve()
    if not in_site_packages and (root / "pyproject.toml").is_file():
        return root / "data"
    return user_data_dir()


class WorkspacePaths:
    """Resolves isolated state directories.

    Defaults to ``<project_root>/data`` when running from a source checkout (so all
    receipts, ledger, SSH keys and settings stay neatly inside the project folder),
    falls back to the platform user data directory when installed as a wheel into
    ``site-packages``, and always honors ``COLAB_BOOST_HOME`` or explicit ``base_dir``.
    """

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        env_home = os.environ.get("COLAB_BOOST_HOME", "").strip()
        if base_dir is not None:
            self.home = Path(base_dir).resolve()
        elif env_home:
            self.home = Path(env_home).resolve()
        else:
            self.home = resolve_default_data_dir()

        self.settings_file = self.home / "settings.json"
        self.accounts_file = self.home / "accounts.json"
        self.ledger_file = self.home / "ledger.json"
        self.runs_file = self.home / "runs.json"
        self.vault_fallback_file = self.home / "vault.local.json"
        self.receipts_dir = self.home / "receipts"
        self.keep_dir = self.home / "keep"
        self.ssh_dir = self.home / "ssh"
        self.user_recipes_dir = self.home / "recipes"
        self.bundled_recipes_dir = resolve_bundled_recipes_dir()

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
        atomic_write_text(
            self.paths.settings_file,
            json.dumps(settings.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return settings
