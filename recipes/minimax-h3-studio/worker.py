#!/usr/bin/env python3
"""Colab Boost VM Worker — 3-Track Parallel MiniMax-H3 ComfyUI Video Studio.

Eliminates sequential cold-start bottlenecks on Colab G4 Blackwell (96GB) by:
  1. Track A: Shallow single-commit ComfyUI fetch + ``uv`` isolated venv & PyTorch
     2.9.1+cu130 install + instant precompiled SageAttention 2.2 (sm120) wheel install.
  2. Track B (concurrent with Track A): 4-way parallel download of the 39.5 GB
     MiniMax-H3 Ref2VA + Qwen3-VL + Video VAE + CharacterSwap v1.5 LoRA via
     ``hf_xet`` or ``aria2c``, with optional multi-core ``sha256sum`` verification.
  3. Track C (optional): Installs ComfyUI-LatentSyncWrapper + Demucs vocal separator
     for post-swap close-up lip-sync refinement inside the same warm ComfyUI server.
  4. Warm-session fast path: Re-attaches in <2s if ComfyUI & models are already ready.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

ENV = os.environ.get
RUN_DIR = ENV("BOOST_RUN_DIR", "/root/.colab-boost/runs/manual")
STATUS_FILE = ENV("BOOST_STATUS_FILE", os.path.join(RUN_DIR, "status.json"))
HEARTBEAT_FILE = ENV("BOOST_HEARTBEAT_FILE", "")

WORK = Path("/content/minimax-h3")
COMFY_DIR = WORK / "ComfyUI"
MODELS_DIR = WORK / "models"
EXTRA_MODEL_PATHS_YAML = WORK / "extra_model_paths.yaml"
VENV_DIR = WORK / "venv"
WHEELS_DIR = WORK / "wheels"
OUTPUT_DIR = WORK / "output"
INPUT_DIR = WORK / "input"
USER_DIR = WORK / "user"
ENV_MARKER = WORK / "env_ready.json"

COMFY_REPO = "https://github.com/Comfy-Org/ComfyUI.git"
SAGE_REPO = "https://github.com/thu-ml/SageAttention.git"
SAGE_COMMIT = "eb615cf6cf4d221338033340ee2de1c37fbdba4a"
PYTORCH_INDEX = "https://download.pytorch.org/whl/cu130"
TORCH_PACKAGES = ["torch==2.9.1", "torchvision==0.24.1", "torchaudio==2.9.1"]

HF_REPO = "Comfy-Org/MiniMax-H3"
HF_REVISION = "e5eb578a89295337b8ff433a035929ce0279e0b6"
PUBLIC_MODELS = [
    (
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        20970379616,
        "9255f52b6677845ad238f20dfaafa94727053694127ab7f255c048f0f9365779",
    ),
    (
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        15687142551,
        "35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6",
    ),
    (
        "vae/minimax_h3_video_vae_int8_convrot.safetensors",
        2811065184,
        "52a2c8c73583c86e4f41cdcce3a6ad0ea562987bc0bf3d60a0cef5f5c8e60c0e",
    ),
]
AUDIO_VAE_MODEL = (
    "vae/minimax_h3_audio_vae_fp32.safetensors",
    605254808,
    "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48",
)
LORA_REPO = "akatz-ai/MiniMax-H3-Character-Swap-LoRA"
LORA_REV = "96ea3407b230b6f7df71c58c70e31df96bd7e533"
LORA_FILENAME = "h3_character_swap_v1.5.safetensors"
LORA_REL = f"loras/{LORA_FILENAME}"
LORA_BYTES = 155110792
LORA_SHA256 = "8f2c3c3eba161df8d0c6099598b4730f641f5ce01e04cd16d2427cff8a35218f"

COMFY_COMMIT = (ENV("BOOST_INPUT_COMFY_COMMIT") or "fe298e4e20f1ff26d30e8639267bbd0b57811390").strip()
PYTORCH_MODE = (ENV("BOOST_INPUT_PYTORCH_MODE") or "cu130_291").strip()
SAGE_MODE = (ENV("BOOST_INPUT_SAGE_MODE") or "wheel_or_compile").strip()
SAGE_WHEEL_URL = (ENV("BOOST_INPUT_SAGE_WHEEL_URL") or "").strip()
DOWNLOAD_MODELS = (ENV("BOOST_INPUT_DOWNLOAD_H3_MODELS") or "1") == "1"
DOWNLOAD_AUDIO_VAE = (ENV("BOOST_INPUT_DOWNLOAD_AUDIO_VAE") or "1") == "1"
DOWNLOAD_ENGINE = (ENV("BOOST_INPUT_DOWNLOAD_ENGINE") or "hf_xet_parallel").strip()
VERIFY_SHA256 = (ENV("BOOST_INPUT_VERIFY_SHA256") or "skip_lfs_etag").strip()
INSTALL_LIPSYNC = (ENV("BOOST_INPUT_INSTALL_LIPSYNC_NODES") or "0") == "1"
STRICT_G4_GATE = (ENV("BOOST_INPUT_STRICT_G4_GATE") or "1") == "1"
PORT = int(ENV("BOOST_INPUT_PORT") or "8188")
HF_TOKEN = ENV("BOOST_HF_TOKEN") or ""

SECRETS = [s for s in (HF_TOKEN,) if s and len(s) >= 4]
_PROC: list[subprocess.Popen | None] = [None]
_STATUS_LOCK = threading.Lock()
_STAGE = ["hardware"]


def scrub(text: str) -> str:
    for sec in SECRETS:
        text = text.replace(sec, "***")
    return text


def _is_trusted_hf_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme.lower() == "https" and (host == "huggingface.co" or host.endswith(".huggingface.co"))


def _read_log_tail(log_path: str | Path, max_lines: int = 8, max_chars: int = 360) -> str:
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
        tail = " | ".join(line.strip() for line in lines if line.strip())
        return scrub(tail)[-max_chars:]
    except OSError:
        return ""


def _terminate_proc(proc: subprocess.Popen | None, grace_s: float = 5.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        pass
    if proc.poll() is None:
        sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, sigkill)
            else:
                proc.kill()
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass


def _write_status_lockfree(state: str, stage: str, msg: str) -> None:
    """Signal-safe atomic status write that never acquires ``_STATUS_LOCK``."""
    payload = json.dumps(
        {"state": state, "stage": stage, "message": scrub(msg), "updated_at": time.time()}
    ).encode("utf-8")
    tmp = f"{STATUS_FILE}.sig.{os.getpid()}.tmp"
    try:
        parent = os.path.dirname(STATUS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass


def set_status(
    state: str,
    stage: str,
    msg: str,
    progress: float | None = None,
    outputs: dict | None = None,
) -> None:
    _STAGE[0] = stage
    clean_msg = scrub(msg)
    doc: dict = {"state": state, "stage": stage, "message": clean_msg, "updated_at": time.time()}
    if progress is not None:
        doc["progress"] = max(0.0, min(1.0, float(progress)))
    if outputs is not None:
        doc["outputs"] = outputs
    with _STATUS_LOCK:
        print(f"[{time.strftime('%H:%M:%S')}] [{state}:{stage}] {clean_msg}", flush=True)
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, STATUS_FILE)


def beat() -> None:
    if not HEARTBEAT_FILE:
        return
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "a", encoding="utf-8"):
            pass
        os.utime(HEARTBEAT_FILE, None)
    except OSError:
        pass


def clean_child_env(*, include_hf: bool = False) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOOST_")}
    if include_hf and HF_TOKEN:
        env["HF_TOKEN"] = HF_TOKEN
    return env


def on_sigterm(*_) -> None:
    proc = _PROC[0]
    if proc is not None and proc.poll() is None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except OSError:
            pass
    _write_status_lockfree("failed", _STAGE[0], "Cancelled by user")
    os._exit(143)


def inspect_hardware() -> dict:
    raw = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap,driver_version", "--format=csv,noheader,nounits"],
        text=True,
        timeout=15,
    ).strip().splitlines()[0]
    parts = [p.strip() for p in raw.split(",")]
    name, mem_mib, cc, driver = parts[0], int(float(parts[1])), parts[2], parts[3]
    if STRICT_G4_GATE and (cc != "12.0" or mem_mib < 80000):
        raise RuntimeError(
            f"G4 Blackwell gate rejected GPU '{name}' ({mem_mib} MiB, compute_cap={cc}); "
            "disable strict_g4_gate to run on other GPUs."
        )
    return {
        "gpu_name": name,
        "vram_total_mib": mem_mib,
        "compute_cap": cc,
        "driver_version": driver,
    }


def ensure_uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"], check=True)
    return shutil.which("uv") or "uv"


def shallow_checkout_repo(repo_url: str, target_dir: Path, commit: str) -> None:
    """Fetches a single pinned commit with depth=1 (far faster than full git clone)."""
    if (target_dir / ".git").is_dir():
        cur = subprocess.run(["git", "rev-parse", "HEAD"], cwd=target_dir, capture_output=True, text=True).stdout.strip()
        if commit.lower() == "latest" or cur == commit:
            return
    elif target_dir.exists():
        shutil.rmtree(target_dir)

    target_dir.mkdir(parents=True, exist_ok=True)
    if commit.lower() == "latest":
        if not (target_dir / ".git").is_dir():
            subprocess.run(["git", "clone", "--depth", "1", repo_url, str(target_dir)], check=True)
        return

    if not (target_dir / ".git").is_dir():
        subprocess.run(["git", "init", "-q", str(target_dir)], check=True)
        subprocess.run(["git", "remote", "add", "origin", repo_url], cwd=target_dir, check=True)
    rc = subprocess.run(["git", "fetch", "--depth", "1", "origin", commit], cwd=target_dir).returncode
    if rc == 0:
        subprocess.run(["git", "checkout", "-q", "--force", "FETCH_HEAD"], cwd=target_dir, check=True)
    else:
        subprocess.run(["git", "fetch", "origin"], cwd=target_dir, check=True)
        subprocess.run(["git", "checkout", "-q", "--force", commit], cwd=target_dir, check=True)


def target_python() -> Path:
    if PYTORCH_MODE == "reuse_system":
        return Path(sys.executable)
    return VENV_DIR / "bin" / "python"


def download_sage_wheel_from_spec(spec: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if "::" in spec and not spec.startswith(("http://", "https://")):
        repo_id, filename = [p.strip() for p in spec.split("::", 1)]
        dl_env = {**clean_child_env(include_hf=True), "DL_REPO": repo_id, "DL_FILE": filename, "DL_DIR": str(dest_dir)}
        code = (
            "import os; from huggingface_hub import hf_hub_download; "
            "print(hf_hub_download(repo_id=os.environ['DL_REPO'], filename=os.environ['DL_FILE'], local_dir=os.environ['DL_DIR']))"
        )
        out = subprocess.check_output([sys.executable, "-c", code], env=dl_env, text=True).strip().splitlines()[-1]
        return Path(out)

    parsed = urllib.parse.urlparse(spec)
    fname = Path(parsed.path).name or "sageattention.whl"
    if not fname.endswith(".whl"):
        fname += ".whl"
    out_path = dest_dir / fname
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if (HF_TOKEN and _is_trusted_hf_url(spec)) else {}
    req = urllib.request.Request(spec, headers=headers)
    tmp = out_path.with_suffix(".whl.part")
    with urllib.request.urlopen(req, timeout=180) as resp, tmp.open("wb") as fh:
        shutil.copyfileobj(resp, fh, 4 * 1024 * 1024)
    os.replace(tmp, out_path)
    return out_path


def track_a_install_env(hw: dict, uv_bin: str) -> dict:
    """Track A: Clone ComfyUI + install PyTorch/deps via uv + SageAttention + optional Lip-Sync nodes."""
    t0 = time.monotonic()
    stats: dict = {}

    t_c = time.monotonic()
    shallow_checkout_repo(COMFY_REPO, COMFY_DIR, COMFY_COMMIT)
    stats["checkout_comfyui_s"] = round(time.monotonic() - t_c, 2)

    py_bin = target_python()
    if PYTORCH_MODE != "reuse_system" and not py_bin.is_file():
        subprocess.run([uv_bin, "venv", "--seed", "--python", sys.executable, str(VENV_DIR)], check=True)

    pip_base = [uv_bin, "pip", "install", "--python", str(py_bin)]
    has_torch = subprocess.run(
        [str(py_bin), "-c", "import torch, torchvision, torchaudio, safetensors"],
        capture_output=True,
    ).returncode == 0
    if not has_torch and PYTORCH_MODE != "reuse_system":
        t_pt = time.monotonic()
        subprocess.run([*pip_base, *TORCH_PACKAGES, "--index-url", PYTORCH_INDEX], check=True)
        stats["install_pytorch_uv_s"] = round(time.monotonic() - t_pt, 2)

    t_req = time.monotonic()
    subprocess.run(
        [*pip_base, "-r", str(COMFY_DIR / "requirements.txt"), "huggingface_hub[hf_xet]", "accelerate", "safetensors", "imageio-ffmpeg"],
        check=True,
    )
    stats["install_comfy_reqs_uv_s"] = round(time.monotonic() - t_req, 2)

    # SageAttention 2.2: prefer cached/URL wheel (~2s), optionally compile & archive to WHEELS_DIR
    use_sage = False
    if SAGE_MODE != "disabled":
        use_sage = subprocess.run([str(py_bin), "-c", "import sageattention"], capture_output=True).returncode == 0
        if not use_sage:
            cached = sorted(WHEELS_DIR.glob("sageattention*.whl")) if WHEELS_DIR.is_dir() else []
            wheel_path: Path | None = cached[0] if cached else None
            if wheel_path is None and SAGE_WHEEL_URL:
                t_w = time.monotonic()
                wheel_path = download_sage_wheel_from_spec(SAGE_WHEEL_URL, WHEELS_DIR)
                stats["download_sage_wheel_s"] = round(time.monotonic() - t_w, 2)
            if wheel_path is not None:
                t_sw = time.monotonic()
                rc = subprocess.run([*pip_base, "--no-deps", str(wheel_path)]).returncode
                stats["install_sage_wheel_s"] = round(time.monotonic() - t_sw, 2)
                use_sage = rc == 0 and subprocess.run([str(py_bin), "-c", "import sageattention"], capture_output=True).returncode == 0
            elif SAGE_MODE == "wheel_or_compile":
                t_sc = time.monotonic()
                try:
                    sage_dir = WORK / "SageAttention"
                    shallow_checkout_repo(SAGE_REPO, sage_dir, SAGE_COMMIT)
                    subprocess.run([*pip_base, "pip", "ninja", "packaging", "wheel", "setuptools"], check=True)
                    build_env = clean_child_env()
                    build_env.update({
                        "TORCH_CUDA_ARCH_LIST": hw["compute_cap"],
                        "EXT_PARALLEL": "2",
                        "NVCC_APPEND_FLAGS": "--threads 4",
                        "MAX_JOBS": "4",
                    })
                    WHEELS_DIR.mkdir(parents=True, exist_ok=True)
                    build_res = subprocess.run(
                        [str(py_bin), "-m", "pip", "wheel", "--no-build-isolation", "--no-deps", "-w", str(WHEELS_DIR), str(sage_dir)],
                        env=build_env,
                        capture_output=True,
                        text=True,
                    )
                    if build_res.returncode != 0:
                        err_lines = [ln.strip() for ln in (build_res.stderr or build_res.stdout or "").splitlines() if ln.strip()]
                        err_tail = " | ".join(err_lines[-6:])[-240:] if err_lines else f"exit code {build_res.returncode}"
                        raise RuntimeError(f"pip wheel failed ({err_tail})")
                    built = sorted(WHEELS_DIR.glob("sageattention*.whl"))
                    if built:
                        subprocess.run([*pip_base, "--no-deps", str(built[0])], check=True)
                        stats["compiled_sage_wheel"] = built[0].name
                        use_sage = True
                    else:
                        raise RuntimeError("pip wheel succeeded but produced no sageattention*.whl")
                except Exception as exc:
                    reason = scrub(str(exc))[:240]
                    stats["sage_fallback_reason"] = reason
                    set_status(
                        "running",
                        "parallel_setup",
                        f"SageAttention compile failed ({reason}); falling back to PyTorch attention.",
                    )
                    use_sage = False
                stats["compile_sage_s"] = round(time.monotonic() - t_sc, 2)

    if INSTALL_LIPSYNC:
        t_ls = time.monotonic()
        ls_dir = COMFY_DIR / "custom_nodes" / "ComfyUI-LatentSyncWrapper"
        shallow_checkout_repo("https://github.com/ShmuelRonen/ComfyUI-LatentSyncWrapper.git", ls_dir, "latest")
        ls_reqs = ls_dir / "requirements.txt"
        extra_pkgs = ["demucs", "soundfile"]
        if ls_reqs.is_file():
            subprocess.run([*pip_base, "-r", str(ls_reqs), *extra_pkgs], check=False)
        else:
            subprocess.run([*pip_base, *extra_pkgs], check=False)
        stats["install_lipsync_s"] = round(time.monotonic() - t_ls, 2)

    stats["use_sage"] = use_sage
    stats["track_env_s"] = round(time.monotonic() - t0, 2)
    return stats


def _ensure_aria2c() -> str | None:
    found = shutil.which("aria2c")
    if found:
        return found
    rc = subprocess.run(["apt-get", "update", "-qq"], capture_output=True).returncode
    if rc == 0:
        subprocess.run(["apt-get", "install", "-y", "-qq", "aria2"], capture_output=True)
    return shutil.which("aria2c")


def _build_hf_download_env(repo_id: str, revision: str, remote_path: str, dl_root: Path) -> dict[str, str]:
    dl_env = {
        **clean_child_env(include_hf=True),
        "DL_REPO": repo_id,
        "DL_REV": revision,
        "DL_FILE": remote_path,
        "DL_ROOT": str(dl_root),
    }
    if importlib.util.find_spec("hf_transfer") is not None:
        dl_env["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
    return dl_env


def _download_one_model(
    *,
    repo_id: str,
    revision: str,
    remote_path: str,
    local_dest: Path,
    expected_bytes: int,
    aria2_bin: str | None,
) -> tuple[str, float]:
    t0 = time.monotonic()
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    if local_dest.is_file() and local_dest.stat().st_size == expected_bytes:
        return remote_path, 0.0

    if aria2_bin is not None:
        url = f"https://huggingface.co/{repo_id}/resolve/{revision}/{urllib.parse.quote(remote_path)}"
        cmd = [
            aria2_bin,
            "-x", "16",
            "-s", "16",
            "-k", "4M",
            "--summary-interval=0",
            "--file-allocation=none",
            "--continue=true",
            "--allow-overwrite=true",
            "-d", str(local_dest.parent),
            "-o", local_dest.name,
            url,
        ]
        subprocess.run(cmd, env=clean_child_env(), check=True)
    else:
        dl_env = _build_hf_download_env(
            repo_id,
            revision,
            remote_path,
            MODELS_DIR if "/" in remote_path else local_dest.parent,
        )
        code = (
            "import os; from huggingface_hub import hf_hub_download; "
            "hf_hub_download(repo_id=os.environ['DL_REPO'], filename=os.environ['DL_FILE'], "
            "revision=os.environ['DL_REV'], local_dir=os.environ['DL_ROOT'])"
        )
        subprocess.run([sys.executable, "-c", code], env=dl_env, check=True)

    actual = local_dest.stat().st_size if local_dest.is_file() else -1
    if actual != expected_bytes:
        raise RuntimeError(f"Size mismatch for {remote_path}: got {actual}, expected {expected_bytes}")
    return remote_path, round(time.monotonic() - t0, 2)


def track_b_download_models(uv_bin: str) -> dict:
    """Track B: Download all 4 MiniMax-H3 files (39.5 GB) concurrently into isolated ``MODELS_DIR``."""
    t0 = time.monotonic()
    if not DOWNLOAD_MODELS:
        return {"track_models_s": 0.0, "skipped": True}

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    aria2_bin = _ensure_aria2c() if DOWNLOAD_ENGINE == "aria2c_parallel" else None
    if aria2_bin is None:
        subprocess.run([uv_bin, "pip", "install", "--system", "-q", "huggingface_hub[hf_xet]"], check=True)

    model_list = list(PUBLIC_MODELS) + ([AUDIO_VAE_MODEL] if DOWNLOAD_AUDIO_VAE else [])
    jobs = [
        (HF_REPO, HF_REVISION, rel, MODELS_DIR / rel, expected_sz, expected_sha)
        for rel, expected_sz, expected_sha in model_list
    ] + [
        (LORA_REPO, LORA_REV, LORA_FILENAME, MODELS_DIR / LORA_REL, LORA_BYTES, LORA_SHA256),
    ]

    per_file_s: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = {
            pool.submit(
                _download_one_model,
                repo_id=repo,
                revision=rev,
                remote_path=rpath,
                local_dest=dst,
                expected_bytes=exp_sz,
                aria2_bin=aria2_bin,
            ): rpath
            for repo, rev, rpath, dst, exp_sz, _sha in jobs
        }
        for fut in as_completed(futs):
            rpath, elapsed = fut.result()
            per_file_s[rpath] = elapsed

    return {
        "engine": "aria2c_parallel" if aria2_bin else "hf_xet_parallel",
        "per_file_s": per_file_s,
        "track_models_s": round(time.monotonic() - t0, 2),
        "verify_targets": [(str(dst), sha) for _r, _v, _p, dst, _sz, sha in jobs],
    }


def parallel_verify_sha256(targets: list[tuple[str, str]]) -> float:
    """Verifies all model SHA256 hashes concurrently using native multi-core ``sha256sum``."""
    t0 = time.monotonic()
    procs = [
        (path, expected, subprocess.Popen(["sha256sum", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for path, expected in targets
    ]
    for path, expected, proc in procs:
        out, err = proc.communicate(timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"sha256sum failed for {path}: {err.strip()}")
        actual = out.strip().split()[0].lower()
        if actual != expected.lower():
            raise RuntimeError(f"SHA256 mismatch for {path}: {actual} != {expected}")
    return round(time.monotonic() - t0, 2)


def comfy_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/system_stats", timeout=4) as resp:
            return resp.status == 200
    except Exception:
        return False


def write_extra_model_paths_yaml() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    yaml_text = (
        "minimax_h3:\n"
        f"    base_path: {MODELS_DIR}\n"
        "    diffusion_models: diffusion_models\n"
        "    unet: diffusion_models\n"
        "    text_encoders: text_encoders\n"
        "    clip: text_encoders\n"
        "    vae: vae\n"
        "    loras: loras\n"
    )
    EXTRA_MODEL_PATHS_YAML.write_text(yaml_text, encoding="utf-8")


def launch_comfy(use_sage: bool) -> float:
    if comfy_is_up():
        return 0.0
    for d in (INPUT_DIR, OUTPUT_DIR, USER_DIR, WHEELS_DIR, MODELS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    write_extra_model_paths_yaml()
    py_bin = target_python()
    attn_flag = "--use-sage-attention" if use_sage else "--use-pytorch-cross-attention"
    cmd = [
        str(py_bin),
        "main.py",
        "--listen", "127.0.0.1",
        "--port", str(PORT),
        "--input-directory", str(INPUT_DIR),
        "--output-directory", str(OUTPUT_DIR),
        "--user-directory", str(USER_DIR),
        "--extra-model-paths-config", str(EXTRA_MODEL_PATHS_YAML),
        "--disable-auto-launch",
        "--disable-pinned-memory",
        attn_flag,
    ]
    t0 = time.monotonic()
    log_path = os.path.join(RUN_DIR, "comfyui_h3.log")
    with open(log_path, "w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(COMFY_DIR),
            env=clean_child_env(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    _PROC[0] = proc

    deadline = time.monotonic() + 240.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if comfy_is_up():
            return round(time.monotonic() - t0, 2)
        beat()
        time.sleep(2.0)

    _terminate_proc(proc)
    _PROC[0] = None
    tail = _read_log_tail(log_path)
    raise RuntimeError(
        f"MiniMax-H3 ComfyUI failed to pass /system_stats on port {PORT}{': ' + tail if tail else ''}"
    )


def main() -> int:
    os.makedirs(RUN_DIR, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, on_sigterm)
    if PORT == 8080:
        set_status("failed", "hardware", "Port 8080 is reserved by Colab")
        return 1

    stop_hb = threading.Event()

    def hb_loop() -> None:
        while not stop_hb.wait(25):
            beat()

    beat()
    threading.Thread(target=hb_loop, daemon=True).start()

    try:
        t_all = time.monotonic()
        set_status("running", "hardware", "Probing GPU & VRAM hardware...", progress=0.05)
        hw = inspect_hardware()

        # Warm-session 0s reuse path
        if ENV_MARKER.is_file() and comfy_is_up():
            cached = json.loads(ENV_MARKER.read_text(encoding="utf-8"))
            stop_hb.set()
            set_status(
                "ready",
                "server",
                f"Warm MiniMax-H3 ComfyUI reused in 0s ({hw['gpu_name']})",
                progress=1.0,
                outputs={**cached, "warm_reused": True, "port": PORT, "output_dir": str(OUTPUT_DIR)},
            )
            while True:
                time.sleep(30)
                if not comfy_is_up():
                    set_status("failed", "server", "Warm ComfyUI server became unreachable")
                    return 1

        set_status(
            "running",
            "parallel_setup",
            f"GPU {hw['gpu_name']} ({hw['vram_total_mib']} MiB) — launching 3-track parallel setup (uv + 39.5GB models)...",
            progress=0.2,
        )
        uv_bin = ensure_uv()

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_env = pool.submit(track_a_install_env, hw, uv_bin)
            fut_models = pool.submit(track_b_download_models, uv_bin)
            env_stats = fut_env.result()
            model_stats = fut_models.result()

        verify_s = 0.0
        if VERIFY_SHA256 == "parallel_sha256" and model_stats.get("verify_targets"):
            set_status("running", "verify", "Running multi-core parallel sha256sum verification...", progress=0.78)
            verify_s = parallel_verify_sha256(model_stats["verify_targets"])
        else:
            set_status("running", "verify", "Weights verified by pinned byte size & HF LFS revision.", progress=0.78)

        use_sage = bool(env_stats.get("use_sage", False))
        set_status(
            "running",
            "server",
            f"Starting MiniMax-H3 ComfyUI on 127.0.0.1:{PORT} (sage={'on' if use_sage else 'off'})...",
            progress=0.88,
        )
        boot_s = launch_comfy(use_sage=use_sage)
        total_s = round(time.monotonic() - t_all, 2)

        outputs = {
            "gpu": hw["gpu_name"],
            "vram_total_mib": hw["vram_total_mib"],
            "compute_cap": hw["compute_cap"],
            "port": PORT,
            "use_sage": use_sage,
            "download_engine": model_stats.get("engine", "skipped"),
            "track_env_s": env_stats.get("track_env_s", 0.0),
            "track_models_s": model_stats.get("track_models_s", 0.0),
            "verify_sha256_s": verify_s,
            "comfy_boot_s": boot_s,
            "total_setup_s": total_s,
            "output_dir": str(OUTPUT_DIR),
            "wheels_dir": str(WHEELS_DIR),
            "audio_vae_ready": bool(DOWNLOAD_MODELS and DOWNLOAD_AUDIO_VAE),
            "lipsync_installed": INSTALL_LIPSYNC,
        }
        ENV_MARKER.write_text(json.dumps(outputs, indent=2), encoding="utf-8")
        stop_hb.set()
        set_status(
            "ready",
            "server",
            f"MiniMax-H3 Studio ready in {total_s}s (env={outputs['track_env_s']}s || models={outputs['track_models_s']}s)",
            progress=1.0,
            outputs=outputs,
        )

        proc = _PROC[0]
        log_path = os.path.join(RUN_DIR, "comfyui_h3.log")
        while True:
            time.sleep(30)
            if proc is not None and proc.poll() is not None:
                tail = _read_log_tail(log_path)
                set_status(
                    "failed",
                    "server",
                    f"MiniMax-H3 ComfyUI server exited unexpectedly{': ' + tail if tail else ''}",
                )
                return 1
    except Exception as exc:
        stop_hb.set()
        _terminate_proc(_PROC[0])
        _PROC[0] = None
        set_status("failed", _STAGE[0], f"MiniMax-H3 setup failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
