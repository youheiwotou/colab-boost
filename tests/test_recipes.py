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


def _write_minimal_recipe(
    base_dir: Path,
    recipe_id: str,
    extra_toml: str = "",
    script_rel: str = "worker.py",
    create_script: bool = True,
) -> Path:
    rdir = base_dir / recipe_id
    rdir.mkdir(parents=True, exist_ok=True)
    if create_script:
        sp = rdir / script_rel
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text("print('ok')\n", encoding="utf-8")
    (rdir / "recipe.toml").write_text(
        f"""
api = 1
id = "{recipe_id}"
version = "0.1.0"
[name]
en = "Recipe {recipe_id}"
[run]
script = "{script_rel}"
{extra_toml}
""",
        encoding="utf-8",
    )
    return rdir


def test_discover_all_recipes_isolates_corrupt_user_recipe_and_skips_pycache(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bundled_dir = project_root() / "recipes"
    user_dir = tmp_path / "user_recipes"
    user_dir.mkdir()

    # Valid user recipe
    _write_minimal_recipe(user_dir, "custom-ok")
    # Corrupt user recipe (broken TOML)
    bad_dir = user_dir / "broken-user"
    bad_dir.mkdir()
    (bad_dir / "recipe.toml").write_text("api = [unclosed", encoding="utf-8")
    # __pycache__ and hidden directory should be skipped silently
    (user_dir / "__pycache__").mkdir()
    (user_dir / ".hidden-draft").mkdir()

    with caplog.at_level("WARNING"):
        recipes = discover_all_recipes(bundled_dir, user_dir)

    assert "llama-cpp-gguf" in recipes
    assert "minimax-h3-studio" in recipes
    assert "custom-ok" in recipes
    assert "broken-user" not in recipes
    assert "__pycache__" not in recipes
    assert any("broken-user" in rec.message for rec in caplog.records)


def test_recipe_loader_checks_symlinks_before_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Symlinked worker script (simulated deterministically so Windows without symlink privilege still tests exact path)
    rdir = _write_minimal_recipe(tmp_path, "sym-script", script_rel="sub/worker.py")
    real_is_symlink = Path.is_symlink

    def fake_script_symlink(self: Path) -> bool:
        if self == rdir / "sub" / "worker.py":
            return True
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_script_symlink)
    with pytest.raises(RecipeValidationError, match="symlink"):
        load_recipe_from_dir(rdir, bundled=False)

    # 2. Symlinked intermediate parent directory in relative script path
    def fake_parent_symlink(self: Path) -> bool:
        if self == rdir / "sub":
            return True
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_parent_symlink)
    with pytest.raises(RecipeValidationError, match="symlink"):
        load_recipe_from_dir(rdir, bundled=False)


def test_recipe_schema_bounds_defaults_labels_and_endpoint_conflicts(tmp_path: Path) -> None:
    # 1. min > max
    d1 = _write_minimal_recipe(
        tmp_path,
        "bad-bounds",
        """
[[inputs]]
id = "threads"
type = "number"
min = 16
max = 4
[inputs.label]
en = "Threads"
""",
    )
    with pytest.raises(RecipeValidationError, match="min"):
        load_recipe_from_dir(d1, bundled=False)

    # 2. Number default out of range
    d2 = _write_minimal_recipe(
        tmp_path,
        "bad-default-range",
        """
[[inputs]]
id = "threads"
type = "number"
min = 1
max = 8
default = 32
[inputs.label]
en = "Threads"
""",
    )
    with pytest.raises(RecipeValidationError, match="exceeds max"):
        load_recipe_from_dir(d2, bundled=False)

    # 3. Duplicate endpoint id
    d3 = _write_minimal_recipe(
        tmp_path,
        "dup-ep-id",
        """
[[endpoints]]
id = "api"
port = 8000
[endpoints.label]
en = "API 1"
[[endpoints]]
id = "api"
port = 8001
[endpoints.label]
en = "API 2"
""",
    )
    with pytest.raises(RecipeValidationError, match="duplicate endpoint id"):
        load_recipe_from_dir(d3, bundled=False)

    # 4. Duplicate static endpoint port
    d4 = _write_minimal_recipe(
        tmp_path,
        "dup-ep-port",
        """
[[endpoints]]
id = "api1"
port = 8000
[endpoints.label]
en = "API 1"
[[endpoints]]
id = "api2"
port = 8000
[endpoints.label]
en = "API 2"
""",
    )
    with pytest.raises(RecipeValidationError, match="conflicts"):
        load_recipe_from_dir(d4, bundled=False)

    # 5. Missing non-empty 'en' label on stage / endpoint / keep
    d5 = _write_minimal_recipe(
        tmp_path,
        "missing-stage-en",
        """
[[stages]]
id = "boot"
[stages.label]
zh-CN = "启动"
""",
    )
    with pytest.raises(RecipeValidationError, match="en"):
        load_recipe_from_dir(d5, bundled=False)

    d6 = _write_minimal_recipe(
        tmp_path,
        "missing-ep-en",
        """
[[endpoints]]
id = "api"
port = 8001
[endpoints.label]
en = "   "
""",
    )
    with pytest.raises(RecipeValidationError, match="en"):
        load_recipe_from_dir(d6, bundled=False)

    d7 = _write_minimal_recipe(
        tmp_path,
        "missing-keep-en",
        """
[[keep]]
path = "/content/out"
[keep.label]
zh-CN = "输出"
""",
    )
    with pytest.raises(RecipeValidationError, match="en"):
        load_recipe_from_dir(d7, bundled=False)


def test_validate_and_build_env_rejects_float_truncation_control_chars_and_port_clash(
    tmp_path: Path,
) -> None:
    d = _write_minimal_recipe(
        tmp_path,
        "strict-inputs",
        """
[[inputs]]
id = "port_a"
type = "number"
default = 8001
min = 1024
max = 65535
[inputs.label]
en = "Port A"
[[inputs]]
id = "port_b"
type = "number"
default = 8002
min = 1024
max = 65535
[inputs.label]
en = "Port B"
[[inputs]]
id = "note"
type = "text"
[inputs.label]
en = "Note"
[[inputs]]
id = "token"
type = "secret"
[inputs.label]
en = "Token"
[[endpoints]]
id = "ep_a"
port_input = "port_a"
[endpoints.label]
en = "Endpoint A"
[[endpoints]]
id = "ep_b"
port_input = "port_b"
[endpoints.label]
en = "Endpoint B"
""",
    )
    loaded = load_recipe_from_dir(d, bundled=False)

    # Reject float silent truncation (12.9 -> 12) and float string
    for bad_num in (8001.9, 8001.0, "8001.9", True):
        with pytest.raises(RecipeValidationError, match="integer"):
            validate_and_build_env(loaded.manifest, {"port_a": bad_num})

    # Reject control characters in text and secret
    for bad_str in ("abc\ndef", "abc\rdef", "abc\x00def"):
        with pytest.raises(RecipeValidationError, match="NUL or newline"):
            validate_and_build_env(loaded.manifest, {"note": bad_str})
        with pytest.raises(RecipeValidationError, match="NUL or newline"):
            validate_and_build_env(loaded.manifest, {"token": bad_str})

    # Reject oversized text / secret
    with pytest.raises(RecipeValidationError, match="maximum length"):
        validate_and_build_env(loaded.manifest, {"note": "x" * 5000})
    with pytest.raises(RecipeValidationError, match="maximum length"):
        validate_and_build_env(loaded.manifest, {"token": "s" * 5000})

    # Reject runtime port conflict across endpoints
    with pytest.raises(RecipeValidationError, match="conflicts"):
        validate_and_build_env(loaded.manifest, {"port_a": 8010, "port_b": 8010})

    # Valid string-encoded int works
    env, secrets, ports = validate_and_build_env(
        loaded.manifest, {"port_a": "8011", "port_b": 8012, "note": "ok", "token": "sec1234"}
    )
    assert env["BOOST_INPUT_PORT_A"] == "8011"
    assert ports == {"ep_a": 8011, "ep_b": 8012}
    assert secrets == ["sec1234"]


def test_llama_worker_env_api_key_extra_args_cache_and_cuda_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BOOST_INPUT_API_KEY", "sk-llama-secret-123")
    mod = _load_worker_module("llama-cpp-gguf", monkeypatch, tmp_path)

    # API key passed via LLAMA_API_KEY env var, NOT --api-key on command line
    cmd = mod.build_server_cmd("/bin/llama", "/m.gguf", 8192, "qwen", "f16", 8000)
    assert "--api-key" not in cmd
    assert "sk-llama-secret-123" not in " ".join(cmd)
    srv_env = mod.runtime_env("/bin/llama", include_api_key=True)
    assert srv_env["LLAMA_API_KEY"] == "sk-llama-secret-123"
    assert "LLAMA_API_KEY" not in mod.runtime_env("/bin/llama", include_api_key=False)

    # extra_args forbids --host / --port / --api-key
    assert mod.parse_extra_args("--temp 0.6 --top-p 0.9") == ["--temp", "0.6", "--top-p", "0.9"]
    for bad in ("--host 0.0.0.0", "--host=0.0.0.0", "--port 8080", "--port=9999", "--api-key leak", "--api-key=leak"):
        with pytest.raises(ValueError, match="cannot override"):
            mod.parse_extra_args(bad)

    # Cache hit works in nested subdirectories
    nested = tmp_path / "b9999" / "build" / "bin"
    nested.mkdir(parents=True)
    (nested / "llama-server").write_text("#!/bin/sh\n", encoding="utf-8")
    assert mod._find_cached_llama_server_dir(str(tmp_path / "b9999")) == str(nested)

    # Dynamic CUDA version selection & fallback
    assets = [
        "llama-b9999-bin-ubuntu-cuda-13.0-x64.tar.gz",
        "llama-b9999-bin-ubuntu-cuda-12.4-x64.tar.gz",
        "cudart-llama-b9999-bin-ubuntu-cuda-12.4-x64.tar.gz",
    ]
    selected = mod._select_release_archives("b9999", assets)
    assert selected[0] == [
        "llama-b9999-bin-ubuntu-cuda-12.4-x64.tar.gz",
        "cudart-llama-b9999-bin-ubuntu-cuda-12.4-x64.tar.gz",
    ]
    assert selected[1] == ["llama-b9999-bin-ubuntu-cuda-13.0-x64.tar.gz"]

    # Underscore quant, non-ASCII rejection, unknown KV_FACTORS.get, and mctx <= 0 guard
    assert mod.extract_quant("model_Q4_K_M.gguf") == "Q4_K_M"
    assert mod.extract_quant("model_Q4_非ASCII.gguf") is None

    # Nested array (elem_type == 9) and 1 MiB string limit in worker parse_gguf_meta
    import struct as _st

    def _pstr(s: str) -> bytes:
        b = s.encode("utf-8")
        return _st.pack("<Q", len(b)) + b

    inner = _st.pack("<IQ", 4, 2) + _st.pack("<II", 7, 9)
    nested_arr = _st.pack("<IQ", 9, 1) + inner
    blob = (
        b"GGUF"
        + _st.pack("<IQQ", 3, 1, 2)
        + _pstr("arr.nested")
        + _st.pack("<I", 9)
        + nested_arr
        + _pstr("general.architecture")
        + _st.pack("<I", 8)
        + _pstr("llama")
    )
    parsed = mod.parse_gguf_meta(blob)
    assert parsed["arr.nested"] == [[7, 9]]
    assert parsed["general.architecture"] == "llama"

    # Oversized >1 MiB string stops cleanly without allocating
    huge_str_blob = b"GGUF" + _st.pack("<IQQ", 3, 1, 1) + _st.pack("<Q", 2 * 1024 * 1024)
    assert mod.parse_gguf_meta(huge_str_blob) == {}

    monkeypatch.setattr(mod, "KV_PREF", "unknown_kv")
    fit = mod.choose_fit(
        [{"quant": "Q4_K_M", "files": ["model_Q4_K_M.gguf"], "size": 4 * (1024**3)}],
        vram_mib=15360,
        mm_bytes=0,
        meta={"general.architecture": "llama", "llama.context_length": -1},
    )
    assert fit["ctx"] == mod.DEFAULT_CTX


def test_vllm_worker_env_api_key_and_hf_token_scrubbed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BOOST_INPUT_API_KEY", "sk-vllm-secret-456")
    monkeypatch.setenv("BOOST_HF_TOKEN", "hf_secret_token_999")
    mod = _load_worker_module("vllm-openai", monkeypatch, tmp_path)

    cmd = mod.build_vllm_cmd("/cache/snap", "qwen")
    assert "--api-key" not in cmd
    assert "sk-vllm-secret-456" not in " ".join(cmd)

    srv_env = mod.build_server_env(
        base_env={"PATH": "/usr/bin", "HF_TOKEN": "hf_secret_token_999", "HUGGING_FACE_HUB_TOKEN": "hf_old", "BOOST_X": "1"}
    )
    assert srv_env["VLLM_API_KEY"] == "sk-vllm-secret-456"
    assert "HF_TOKEN" not in srv_env
    assert "HUGGING_FACE_HUB_TOKEN" not in srv_env
    assert "BOOST_X" not in srv_env


def test_faster_whisper_worker_sync_endpoint_streaming_suffix_and_ld_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_worker_module("faster-whisper-api", monkeypatch, tmp_path)

    assert "async def transcribe(" not in mod.APP_CODE
    assert "def transcribe(" in mod.APP_CODE
    assert "shutil.copyfileobj(" in mod.APP_CODE
    assert "await file.read()" not in mod.APP_CODE

    # Verify _safe_audio_suffix strips path separators & traversal
    ns: dict = {"os": __import__("os"), "re": __import__("re")}
    start = mod.APP_CODE.index("def _safe_audio_suffix")
    end = mod.APP_CODE.index("@app.get")
    exec(mod.APP_CODE[start:end], ns)
    safe_suffix = ns["_safe_audio_suffix"]
    assert safe_suffix("clip.mp3") == ".mp3"
    assert safe_suffix("../../etc/passwd.flac") == ".flac"
    assert safe_suffix(r"C:\evil\path\song.wav") == ".wav"
    assert safe_suffix("audio.wav/../../bad") == ".wav"
    assert safe_suffix("noext") == ".wav"

    # Verify LD_LIBRARY_PATH injects /usr/lib64-nvidia and pip nvidia/*/lib
    fake_cudnn = tmp_path / "site" / "nvidia" / "cudnn" / "lib"
    fake_cudnn.mkdir(parents=True)
    ld_path = mod.build_cuda_ld_library_path(existing="/existing/lib", extra_site_roots=[str(tmp_path / "site")])
    assert "/usr/lib64-nvidia" in ld_path
    assert str(fake_cudnn) in ld_path
    assert "/existing/lib" in ld_path


def test_minimax_h3_commit_guard_torch_filter_lipsync_truth_and_weight_consistency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_worker_module("minimax-h3-studio", monkeypatch, tmp_path)

    # 1. COMFY_COMMIT whitelist validation
    assert mod.validate_git_commit("latest") == "latest"
    assert mod.validate_git_commit("fe298e4e20f1ff26d30e8639267bbd0b57811390") == "fe298e4e20f1ff26d30e8639267bbd0b57811390"
    for bad_ref in ("--upload-pack=sh", "-f", "main;id", "../HEAD", "short"):
        with pytest.raises(ValueError, match="Invalid git commit"):
            mod.validate_git_commit(bad_ref)
        with pytest.raises(ValueError, match="Invalid git commit"):
            mod.shallow_checkout_repo("https://example.com/repo.git", tmp_path / "repo", bad_ref)

    # 2. Torch filtering preserves torchsde while removing torch/torchvision/torchaudio
    req_in = tmp_path / "requirements.txt"
    req_in.write_text(
        "# comment\ntorch>=2.0\ntorchvision==0.19.0\ntorchaudio\ntorchsde>=0.2.6\neinops\n",
        encoding="utf-8",
    )
    req_out = mod._filter_torch_requirements(req_in, tmp_path / "filtered.txt")
    filtered_text = req_out.read_text(encoding="utf-8")
    assert "torchsde>=0.2.6" in filtered_text
    assert "einops" in filtered_text
    assert "torch>=2.0" not in filtered_text
    assert "torchvision" not in filtered_text
    assert "torchaudio" not in filtered_text

    # 3. INSTALL_LIPSYNC failure reports lipsync_installed=False instead of hardcoded True
    monkeypatch.setattr(mod, "WORK", tmp_path / "work")
    monkeypatch.setattr(mod, "COMFY_DIR", tmp_path / "work" / "ComfyUI")
    monkeypatch.setattr(mod, "VENV_DIR", tmp_path / "work" / "venv")
    monkeypatch.setattr(mod, "WHEELS_DIR", tmp_path / "work" / "wheels")
    monkeypatch.setattr(mod, "PYTORCH_MODE", "reuse_system")
    monkeypatch.setattr(mod, "SAGE_MODE", "disabled")
    monkeypatch.setattr(mod, "INSTALL_LIPSYNC", True)

    def fake_shallow(_url: str, target: Path, _commit: str) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / "requirements.txt").write_text("torch\neinops\n", encoding="utf-8")

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        if "demucs" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "error: failed to build demucs")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod, "shallow_checkout_repo", fake_shallow)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    stats = mod.track_a_install_env({"compute_cap": "12.0"}, "/usr/local/bin/uv")
    assert stats["lipsync_installed"] is False
    assert "failed to build demucs" in stats["lipsync_error"]

    # 4. Exact weight totals match documentation (4 files 39.62 GB, 5 files 40.23 GB)
    core_bytes = sum(sz for _, sz, _ in mod.PUBLIC_MODELS) + mod.LORA_BYTES
    full_bytes = core_bytes + mod.AUDIO_VAE_MODEL[1]
    assert round(core_bytes / 1e9, 2) == 39.62
    assert round(full_bytes / 1e9, 2) == 40.23
    for rel in ("README.md", "recipes/minimax-h3-studio/recipe.toml", "recipes/minimax-h3-studio/worker.py"):
        txt = (project_root() / rel).read_text(encoding="utf-8")
        assert "39.62 GB" in txt
        assert "40.23 GB" in txt
        assert "39.5 GB" not in txt
        assert "39.5GB" not in txt
        assert "40.1GB" not in txt


def test_comfyui_torch_filter_and_curl_atomic_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_worker_module("comfyui-studio", monkeypatch, tmp_path)

    req_in = tmp_path / "requirements.txt"
    req_in.write_text("torch\ntorchvision>=0.18\ntorchaudio\ntorchsde\npillow\n", encoding="utf-8")
    out_path = mod._filter_torch_requirements(str(req_in), str(tmp_path / "no_torch.txt"))
    txt = Path(out_path).read_text(encoding="utf-8")
    assert "torchsde" in txt and "pillow" in txt
    assert "\ntorch\n" not in f"\n{txt}\n"
    assert "torchvision" not in txt and "torchaudio" not in txt

    recorded: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        recorded.append(list(cmd))
        out_idx = cmd.index("-o") + 1
        Path(cmd[out_idx]).write_bytes(b"weights")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    target = tmp_path / "models" / "ckpt.safetensors"
    mod._download_url_with_curl("https://civitai.com/api/download/1", str(target), env={}, civitai_token="tok123")
    assert len(recorded) == 1
    assert "-C" in recorded[0] and "-" in recorded[0]
    assert recorded[0][recorded[0].index("-o") + 1].endswith(".part")
    assert target.read_bytes() == b"weights"
    assert not (tmp_path / "models" / "ckpt.safetensors.part").exists()


def test_windows_batch_scripts_guards_and_arg_forwarding() -> None:
    root = project_root()
    console_bat = (root / "start_console.bat").read_text(encoding="utf-8")
    demo_bat = (root / "start_demo.bat").read_text(encoding="utf-8")

    for content in (console_bat, demo_bat):
        assert "setlocal" in content
        assert "where uv" in content
        assert "pause" in content

    assert 'if "%~1"==""' in console_bat
    assert "uv run colab-boost %*" in console_bat
    assert "uv run colab-boost --open-browser %*" not in console_bat
    assert "--open-browser %*" in demo_bat

