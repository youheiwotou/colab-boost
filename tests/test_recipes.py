"""Validation and security boundary tests for bundled & user recipes."""

from __future__ import annotations

import importlib.util
import json
import py_compile
import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock
import pytest

from colab_boost.config import project_root
from colab_boost.recipe_loader import (
    RecipeValidationError,
    discover_all_recipes,
    load_recipe_from_dir,
    validate_and_build_env,
)


def _load_worker_module(recipe_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    run_dir = tmp_path / f"run_{recipe_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BOOST_RUN_DIR", str(run_dir))
    monkeypatch.setenv("BOOST_STATUS_FILE", str(run_dir / "status.json"))
    monkeypatch.setenv("BOOST_HEARTBEAT_FILE", str(run_dir / "heartbeat"))
    worker_path = project_root() / "recipes" / recipe_id / "worker.py"
    spec = importlib.util.spec_from_file_location(f"test_worker_{recipe_id.replace('-', '_')}", worker_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def test_minimax_h3_hf_transfer_env_gated_on_package(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = _load_worker_module("minimax-h3-studio", monkeypatch, tmp_path)

    monkeypatch.setattr(mod.importlib.util, "find_spec", lambda name: None)
    env_no_pkg = mod._build_hf_download_env("Comfy-Org/MiniMax-H3", "rev1", "vae/a.safetensors", tmp_path)
    assert "HF_HUB_ENABLE_HF_TRANSFER" not in env_no_pkg
    assert env_no_pkg["DL_REPO"] == "Comfy-Org/MiniMax-H3"

    monkeypatch.setattr(mod.importlib.util, "find_spec", lambda name: object() if name == "hf_transfer" else None)
    env_with_pkg = mod._build_hf_download_env("Comfy-Org/MiniMax-H3", "rev1", "vae/a.safetensors", tmp_path)
    assert env_with_pkg["HF_HUB_ENABLE_HF_TRANSFER"] == "1"


def test_minimax_h3_trusted_hf_url_strict_hostname(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = _load_worker_module("minimax-h3-studio", monkeypatch, tmp_path)

    assert mod._is_trusted_hf_url("https://huggingface.co/org/repo/resolve/main/sage.whl") is True
    assert mod._is_trusted_hf_url("https://cdn-lfs.huggingface.co/repos/sage.whl") is True
    assert mod._is_trusted_hf_url("http://huggingface.co/org/repo/sage.whl") is False
    assert mod._is_trusted_hf_url("https://huggingface.co.evil.com/sage.whl") is False
    assert mod._is_trusted_hf_url("https://evil.com/?next=huggingface.co") is False
    assert mod._is_trusted_hf_url("https://evil.com/huggingface.co/sage.whl") is False


def test_minimax_h3_uv_venv_seed_and_sage_build_error_reporting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = _load_worker_module("minimax-h3-studio", monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "WORK", tmp_path / "work")
    monkeypatch.setattr(mod, "COMFY_DIR", tmp_path / "work" / "ComfyUI")
    monkeypatch.setattr(mod, "VENV_DIR", tmp_path / "work" / "venv")
    monkeypatch.setattr(mod, "WHEELS_DIR", tmp_path / "work" / "wheels")
    monkeypatch.setattr(mod, "PYTORCH_MODE", "cu130_291")
    monkeypatch.setattr(mod, "SAGE_MODE", "wheel_or_compile")
    monkeypatch.setattr(mod, "SAGE_WHEEL_URL", "")
    monkeypatch.setattr(mod, "INSTALL_LIPSYNC", False)

    recorded_cmds: list[list[str]] = []

    def fake_shallow(_url: str, target: Path, _commit: str) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / "requirements.txt").write_text("", encoding="utf-8")

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        recorded_cmds.append(list(cmd))
        if "-c" in cmd and "import sageattention" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if "-c" in cmd and "import torch" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if len(cmd) >= 4 and cmd[1:4] == ["-m", "pip", "wheel"]:
            return subprocess.CompletedProcess(cmd, 1, "", "nvcc error: unsupported sm_120")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod, "shallow_checkout_repo", fake_shallow)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    stats = mod.track_a_install_env({"compute_cap": "12.0"}, "/usr/local/bin/uv")

    venv_cmds = [c for c in recorded_cmds if len(c) >= 2 and c[1] == "venv"]
    assert len(venv_cmds) == 1
    assert "--seed" in venv_cmds[0]

    build_dep_cmds = [c for c in recorded_cmds if "ninja" in c]
    assert len(build_dep_cmds) == 1
    assert "pip" in build_dep_cmds[0]

    assert stats["use_sage"] is False
    assert "nvcc error: unsupported sm_120" in stats["sage_fallback_reason"]
    status_doc = json.loads(Path(mod.STATUS_FILE).read_text(encoding="utf-8"))
    assert "nvcc error: unsupported sm_120" in status_doc["message"]


def test_minimax_h3_on_sigterm_does_not_deadlock_when_status_lock_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_worker_module("minimax-h3-studio", monkeypatch, tmp_path)

    fake_proc = MagicMock()
    fake_proc.pid = 424242
    fake_proc.poll.return_value = None
    mod._PROC[0] = fake_proc

    exited_codes: list[int] = []

    class _ExitSentinel(BaseException):
        pass

    def fake_exit(code: int) -> None:
        exited_codes.append(code)
        raise _ExitSentinel

    monkeypatch.setattr(mod.os, "_exit", fake_exit)
    if hasattr(mod.os, "killpg"):
        monkeypatch.setattr(mod.os, "killpg", lambda pid, sig: fake_proc.terminate())

    # Hold _STATUS_LOCK while invoking on_sigterm to verify lock-free execution (no deadlock)
    with mod._STATUS_LOCK:
        with pytest.raises(_ExitSentinel):
            mod.on_sigterm()

    assert exited_codes == [143]
    assert fake_proc.terminate.called
    status_doc = json.loads(Path(mod.STATUS_FILE).read_text(encoding="utf-8"))
    assert status_doc["state"] == "failed"
    assert "Cancelled" in status_doc["message"]


def test_comfyui_civitai_url_and_curl_config_injection_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_worker_module("comfyui-studio", monkeypatch, tmp_path)

    assert mod._is_trusted_civitai_url("https://civitai.com/api/download/models/123") is True
    assert mod._is_trusted_civitai_url("https://cdn.civitai.com/file.safetensors") is True
    assert mod._is_trusted_civitai_url("http://civitai.com/api/download/models/123") is False
    assert mod._is_trusted_civitai_url("https://civitai.com.evil.com/model") is False
    assert mod._is_trusted_civitai_url("https://evil.com/?ref=civitai.com") is False

    cfg = mod._build_curl_auth_config('tok\\"abc')
    assert cfg == 'header = "Authorization: Bearer tok\\\\\\"abc"\n'

    for bad_token in ("abc\nurl = https://evil.com", "abc\rdef", "abc\x00def", "abc\x1bdef"):
        with pytest.raises(ValueError, match="control characters"):
            mod._build_curl_auth_config(bad_token)


def test_worker_terminate_proc_escalates_and_llama_abort_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llama = _load_worker_module("llama-cpp-gguf", monkeypatch, tmp_path)

    # 1. Process that ignores SIGTERM gets escalated to SIGKILL / kill()
    stubborn = MagicMock()
    stubborn.pid = 99999
    stubborn.poll.return_value = None
    stubborn.wait.side_effect = [subprocess.TimeoutExpired(cmd="llama-server", timeout=0.01), 0]

    signals_sent: list[int] = []
    if hasattr(llama.os, "killpg"):
        monkeypatch.setattr(llama.os, "killpg", lambda pid, sig: signals_sent.append(sig))

    llama._terminate_proc(stubborn, grace_s=0.01)
    if hasattr(llama.os, "killpg"):
        assert len(signals_sent) == 2
    else:
        assert stubborn.terminate.called
        assert stubborn.kill.called

    # 2. Already exited process is untouched
    done_proc = MagicMock()
    done_proc.poll.return_value = 0
    llama._terminate_proc(done_proc, grace_s=0.01)
    assert not done_proc.terminate.called
    assert not done_proc.kill.called

    # 3. abort() terminates active server proc and writes failed status.json
    active = MagicMock()
    active.pid = 88888
    active.poll.return_value = None
    active.wait.return_value = 0
    llama._server_proc[0] = active
    with pytest.raises(SystemExit) as exc_info:
        llama.abort("GPU VRAM usage < 512 MiB")
    assert exc_info.value.code == 1
    assert llama._server_proc[0] is None
    status_doc = json.loads(Path(llama.STATUS_FILE).read_text(encoding="utf-8"))
    assert status_doc["state"] == "failed"
    assert "512 MiB" in status_doc["message"]


def test_all_workers_expose_terminate_log_tail_and_top_level_error_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for recipe_id in ("minimax-h3-studio", "llama-cpp-gguf", "comfyui-studio", "faster-whisper-api", "vllm-openai"):
        mod = _load_worker_module(recipe_id, monkeypatch, tmp_path)
        assert callable(mod._terminate_proc)
        assert callable(mod._read_log_tail)

        sample_log = tmp_path / f"{recipe_id}.log"
        sample_log.write_text("line1\nCUDA out of memory\n", encoding="utf-8")
        tail = mod._read_log_tail(str(sample_log))
        assert "CUDA out of memory" in tail

        src = (project_root() / "recipes" / recipe_id / "worker.py").read_text(encoding="utf-8")
        assert "except Exception" in src


def test_minimax_h3_launch_comfy_timeout_terminates_proc_and_surfaces_log_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_worker_module("minimax-h3-studio", monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "WORK", tmp_path / "work")
    monkeypatch.setattr(mod, "COMFY_DIR", tmp_path / "work" / "ComfyUI")
    monkeypatch.setattr(mod, "MODELS_DIR", tmp_path / "work" / "models")
    monkeypatch.setattr(mod, "EXTRA_MODEL_PATHS_YAML", tmp_path / "work" / "extra_model_paths.yaml")
    monkeypatch.setattr(mod, "INPUT_DIR", tmp_path / "work" / "input")
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path / "work" / "output")
    monkeypatch.setattr(mod, "USER_DIR", tmp_path / "work" / "user")
    monkeypatch.setattr(mod, "WHEELS_DIR", tmp_path / "work" / "wheels")
    (tmp_path / "work" / "ComfyUI").mkdir(parents=True, exist_ok=True)

    fake_proc = MagicMock()
    fake_proc.pid = 77777
    fake_proc.poll.return_value = None
    fake_proc.wait.return_value = 0

    def fake_popen(cmd, **kwargs):
        kwargs["stdout"].write("Traceback...\nRuntimeError: CUDA out of memory on G4\n")
        kwargs["stdout"].flush()
        return fake_proc

    terminated: list[object] = []
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mod, "comfy_is_up", lambda: False)
    monkeypatch.setattr(mod, "_terminate_proc", lambda proc, grace_s=5.0: terminated.append(proc))
    # Force deadline to expire immediately on first loop check
    times = iter([0.0, 0.0, 300.0])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times, 300.0))

    with pytest.raises(RuntimeError, match="CUDA out of memory on G4"):
        mod.launch_comfy(use_sage=False)

    assert terminated == [fake_proc]
    assert mod._PROC[0] is None
