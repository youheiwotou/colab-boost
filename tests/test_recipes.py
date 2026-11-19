"""Validation and security boundary tests for bundled & user recipes."""

from __future__ import annotations

import py_compile
from pathlib import Path
import pytest

from colab_boost.config import project_root
from colab_boost.recipe_loader import (
    RecipeValidationError,
    discover_all_recipes,
    load_recipe_from_dir,
    validate_and_build_env,
)


def test_all_bundled_recipes_validate_and_compile(tmp_path: Path) -> None:
    bundled_dir = project_root() / "recipes"
    recipes = discover_all_recipes(bundled_dir, tmp_path / "empty_user")
    assert set(recipes.keys()) == {
        "llama-cpp-gguf",
        "vllm-openai",
        "comfyui-studio",
        "faster-whisper-api",
        "minimax-h3-studio",
    }
    for r_id, loaded in recipes.items():
        # Compile every worker.py to bytecode to catch syntax errors
        cfile = tmp_path / f"{r_id}.pyc"
        py_compile.compile(str(loaded.script_path), cfile=str(cfile), doraise=True)
        assert cfile.exists()

    h3 = recipes["minimax-h3-studio"]
    assert h3.manifest.provision.accelerator.value == "G4"
    assert h3.manifest.provision.min_vram_mib == 80000
    assert {k.path for k in h3.manifest.keep} == {"/content/minimax-h3/output", "/content/minimax-h3/wheels"}
    env, secrets, ports = validate_and_build_env(
        h3.manifest,
        {
            "download_engine": "aria2c_parallel",
            "sage_mode": "wheel_only",
            "install_lipsync_nodes": True,
            "port": 8190,
        },
    )
    assert env["BOOST_INPUT_DOWNLOAD_ENGINE"] == "aria2c_parallel"
    assert env["BOOST_INPUT_DOWNLOAD_AUDIO_VAE"] == "1"
    assert env["BOOST_INPUT_SAGE_MODE"] == "wheel_only"
    assert env["BOOST_INPUT_INSTALL_LIPSYNC_NODES"] == "1"
    assert "minimax_h3_audio_vae_fp32.safetensors" in h3.script_text
    assert secrets == []
    assert ports["webui"] == 8190


def test_recipe_rejects_port_8080_and_env_collision(tmp_path: Path) -> None:
    # 1. Port 8080 default must be rejected
    bad_port_dir = tmp_path / "bad-port"
    bad_port_dir.mkdir()
    (bad_port_dir / "worker.py").write_text("print('hi')\n", encoding="utf-8")
    (bad_port_dir / "recipe.toml").write_text(
        """
api = 1
id = "bad-port"
version = "0.1.0"
[name]
en = "Bad Port"
[run]
script = "worker.py"
[[endpoints]]
id = "web"
port = 8080
[endpoints.label]
en = "Web"
""",
        encoding="utf-8",
    )
    with pytest.raises(RecipeValidationError, match="8080"):
        load_recipe_from_dir(bad_port_dir, bundled=False)

    # 2. Input ID env collision (`my-key` vs `my_key`) must be rejected
    col_dir = tmp_path / "env-col"
    col_dir.mkdir()
    (col_dir / "worker.py").write_text("print('hi')\n", encoding="utf-8")
    (col_dir / "recipe.toml").write_text(
        """
api = 1
id = "env-col"
version = "0.1.0"
[name]
en = "Collision"
[run]
script = "worker.py"
[[inputs]]
id = "my-key"
type = "text"
[inputs.label]
en = "Key 1"
[[inputs]]
id = "my_key"
type = "text"
[inputs.label]
en = "Key 2"
""",
        encoding="utf-8",
    )
    with pytest.raises(RecipeValidationError, match="collides"):
        load_recipe_from_dir(col_dir, bundled=False)


def test_validate_and_build_env_rejects_runtime_port_8080_and_collects_secrets() -> None:
    bundled_dir = project_root() / "recipes"
    llama = load_recipe_from_dir(bundled_dir / "llama-cpp-gguf", bundled=True)

    with pytest.raises(RecipeValidationError, match="8080"):
        validate_and_build_env(llama.manifest, {"repo": "Qwen/Qwen2.5-7B-Instruct-GGUF", "port": 8080})

    env, secrets, ports = validate_and_build_env(
        llama.manifest,
        {
            "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
            "quant": "Q4_K_M",
            "port": 8010,
            "api_key": "sk-super-secret-999",
        },
    )
    assert env["BOOST_INPUT_REPO"] == "Qwen/Qwen2.5-7B-Instruct-GGUF"
    assert env["BOOST_INPUT_QUANT"] == "Q4_K_M"
    assert env["BOOST_INPUT_API_KEY"] == "sk-super-secret-999"
    assert "sk-super-secret-999" in secrets
    assert ports["openai_api"] == 8010
