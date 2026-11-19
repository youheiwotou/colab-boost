"""Shared pytest fixtures for Colab Boost Console tests."""

from __future__ import annotations

from pathlib import Path
import pytest

from colab_boost.config import WorkspacePaths
from colab_boost.orchestrator import ConsoleOrchestrator


@pytest.fixture()
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkspacePaths:
    home = tmp_path / "cboost_home"
    monkeypatch.setenv("COLAB_BOOST_HOME", str(home))
    monkeypatch.setenv("COLAB_BOOST_VAULT_BACKEND", "file")
    paths = WorkspacePaths(base_dir=home)
    paths.ensure_dirs()
    return paths


@pytest.fixture()
def orchestrator(isolated_paths: WorkspacePaths) -> ConsoleOrchestrator:
    orch = ConsoleOrchestrator(
        paths=isolated_paths,
        force_file_vault=True,
        mock_step_delay_s=0.01,
    )
    yield orch
    orch.tunnels.stop_all()
