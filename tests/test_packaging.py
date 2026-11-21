"""Packaging, wheel resource lookup, and non-editable data_dir fallback tests."""

from __future__ import annotations

import os
from pathlib import Path
import tomllib

import pytest

import colab_boost.config as config
from colab_boost.config import (
    WorkspacePaths,
    project_root,
    resolve_bundled_recipes_dir,
    resolve_default_data_dir,
    user_data_dir,
)
from colab_boost.recipe_loader import discover_all_recipes


def test_pyproject_wheel_force_include_and_dev_dependencies(tmp_path: Path) -> None:
    pyproject_path = project_root() / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    # Wheel force-include bundles recipes/ into colab_boost/recipes
    force_include = (
        data.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("force-include", {})
    )
    assert force_include.get("recipes") == "colab_boost/recipes"

    # Dev dependency group explicitly lists ruff, mypy, websockets (plus test deps)
    dev_deps = data.get("dependency-groups", {}).get("dev", [])
    dep_names = {d.split(">=")[0].split("==")[0].split("[")[0].strip().lower() for d in dev_deps}
    for required in ("ruff", "mypy", "websockets", "pytest", "asyncssh", "pytest-asyncio"):
        assert required in dep_names, f"Missing {required!r} in [dependency-groups].dev"

    # Verify bundled recipes directory actually exists and loads cleanly in source tree
    bundled = resolve_bundled_recipes_dir()
    assert bundled.is_dir()
    loaded = discover_all_recipes(bundled, tmp_path / "empty_user_recipes")
    assert len(loaded) >= 5


def test_resolve_bundled_recipes_dir_prefers_wheel_embedded_over_repo_fallback(tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    repo_recipes = fake_repo / "recipes"
    repo_recipes.mkdir(parents=True)

    fake_pkg = tmp_path / "venv" / "Lib" / "site-packages" / "colab_boost"
    fake_pkg.mkdir(parents=True)

    # 1. Without embedded wheel recipes, falls back to repo_root/recipes
    assert resolve_bundled_recipes_dir(package_dir=fake_pkg, repo_root=fake_repo) == repo_recipes.resolve()

    # 2. Once wheel-embedded colab_boost/recipes exists, it takes priority over repo_root/recipes
    embedded_recipes = fake_pkg / "recipes"
    embedded_recipes.mkdir(parents=True)
    assert resolve_bundled_recipes_dir(package_dir=fake_pkg, repo_root=fake_repo) == embedded_recipes.resolve()


def test_resolve_default_data_dir_source_checkout_vs_wheel_site_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Editable / source checkout with pyproject.toml keeps <repo>/data
    fake_repo = tmp_path / "source_repo"
    fake_pkg = fake_repo / "src" / "colab_boost"
    fake_pkg.mkdir(parents=True)
    (fake_repo / "pyproject.toml").write_text("[project]\nname='colab-boost'\n", encoding="utf-8")

    assert resolve_default_data_dir(package_dir=fake_pkg, repo_root=fake_repo) == (fake_repo / "data").resolve()

    # 2. Non-editable wheel installed into site-packages MUST fall back to user_data_dir (Windows)
    wheel_pkg = tmp_path / "venv" / "Lib" / "site-packages" / "colab_boost"
    wheel_pkg.mkdir(parents=True)
    fake_parent = wheel_pkg.parents[1]
    # Even if an unrelated pyproject.toml existed in a parent dir, site-packages MUST NEVER be written into!
    (fake_parent / "pyproject.toml").write_text("[project]\nname='decoy'\n", encoding="utf-8")

    win_local = tmp_path / "WinLocalAppData"
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(win_local))
    resolved_win = resolve_default_data_dir(package_dir=wheel_pkg, repo_root=fake_parent)
    assert resolved_win == (win_local.resolve() / "colab-boost")
    assert "site-packages" not in resolved_win.parts

    # 3. Non-source install (no pyproject.toml) on POSIX falls back to ~/.local/share/colab-boost
    non_source_root = tmp_path / "frozen_app"
    non_source_pkg = non_source_root / "src" / "colab_boost"
    non_source_pkg.mkdir(parents=True)

    fake_home = tmp_path / "posix_home"
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    resolved_posix = resolve_default_data_dir(package_dir=non_source_pkg, repo_root=non_source_root)
    assert resolved_posix == fake_home / ".local" / "share" / "colab-boost"

    # 4. POSIX with XDG_DATA_HOME set honors $XDG_DATA_HOME/colab-boost
    xdg_dir = tmp_path / "xdg_data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_dir))
    assert user_data_dir() == xdg_dir.resolve() / "colab-boost"


def test_workspace_paths_respects_env_and_default_resolvers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_data = tmp_path / "wheel_user_data"
    custom_recipes = tmp_path / "wheel_recipes"
    custom_recipes.mkdir(parents=True)

    monkeypatch.delenv("COLAB_BOOST_HOME", raising=False)
    monkeypatch.setattr(config, "resolve_default_data_dir", lambda: custom_data)
    monkeypatch.setattr(config, "resolve_bundled_recipes_dir", lambda: custom_recipes)

    wp = WorkspacePaths()
    assert wp.home == custom_data
    assert wp.bundled_recipes_dir == custom_recipes

    # COLAB_BOOST_HOME overrides resolve_default_data_dir
    env_home = tmp_path / "env_override_home"
    monkeypatch.setenv("COLAB_BOOST_HOME", str(env_home))
    wp_env = WorkspacePaths()
    assert wp_env.home == env_home.resolve()
    assert wp_env.bundled_recipes_dir == custom_recipes
    assert os.environ.get("COLAB_BOOST_HOME") == str(env_home)
