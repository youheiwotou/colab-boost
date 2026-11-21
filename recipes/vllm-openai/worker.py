#!/usr/bin/env python3
"""Colab Boost VM Worker — vLLM OpenAI Server Deployer."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

ENV = os.environ.get
RUN_DIR = ENV("BOOST_RUN_DIR", "/root/.colab-boost/runs/manual")
STATUS_FILE = ENV("BOOST_STATUS_FILE", os.path.join(RUN_DIR, "status.json"))
HEARTBEAT_FILE = ENV("BOOST_HEARTBEAT_FILE", "")

REPO = (ENV("BOOST_INPUT_REPO") or "").strip()
QUANT = (ENV("BOOST_INPUT_QUANTIZATION") or "auto").strip()
DTYPE = (ENV("BOOST_INPUT_DTYPE") or "auto").strip()
MAX_LEN = int(ENV("BOOST_INPUT_MAX_MODEL_LEN") or "16384")
MEM_UTIL = int(ENV("BOOST_INPUT_GPU_MEM_UTIL") or "92") / 100.0
PORT = int(ENV("BOOST_INPUT_PORT") or "8000")
TRUST_REMOTE = (ENV("BOOST_INPUT_TRUST_REMOTE_CODE") or "1") == "1"
API_KEY = ENV("BOOST_INPUT_API_KEY") or ""
HF_TOKEN = ENV("BOOST_HF_TOKEN") or ""
SECRETS = [s for s in (API_KEY, HF_TOKEN) if s and len(s) >= 4]
_HF_TOKEN_ENV_KEYS = frozenset({"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN"})

_proc: list[subprocess.Popen | None] = [None]
_stage = ["detect"]


def scrub(text: str) -> str:
    for s in SECRETS:
        text = text.replace(s, "***")
    return text


def _read_log_tail(log_path: str, max_lines: int = 8, max_chars: int = 360) -> str:
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()[-max_lines:]
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


def set_status(state: str, stage: str, message: str, progress: float | None = None, outputs: dict | None = None) -> None:
    _stage[0] = stage
    doc: dict = {"state": state, "stage": stage, "message": scrub(message), "updated_at": time.time()}
    if progress is not None:
        doc["progress"] = progress
    if outputs is not None:
        doc["outputs"] = outputs
    print(f"[{time.strftime('%H:%M:%S')}] [{state}:{stage}] {doc['message']}", flush=True)
    parent = os.path.dirname(STATUS_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, STATUS_FILE)


def beat() -> None:
    if HEARTBEAT_FILE:
        try:
            parent = os.path.dirname(HEARTBEAT_FILE)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(HEARTBEAT_FILE, "a", encoding="utf-8"):
                pass
            os.utime(HEARTBEAT_FILE, None)
        except OSError:
            pass


def build_download_env(base_env: dict[str, str] | None = None, hf_token: str | None = None) -> dict[str, str]:
    src = os.environ if base_env is None else base_env
    dl_env = {k: v for k, v in src.items() if not k.startswith("BOOST_")}
    token = HF_TOKEN if hf_token is None else hf_token
    if token:
        dl_env["HF_TOKEN"] = token
    dl_env["DL_REPO"] = REPO
    return dl_env


def build_server_env(base_env: dict[str, str] | None = None, api_key: str | None = None) -> dict[str, str]:
    """Builds the persistent vLLM server environment without HF_TOKEN and passing VLLM_API_KEY."""
    src = os.environ if base_env is None else base_env
    server_env = {
        k: v
        for k, v in src.items()
        if not k.startswith("BOOST_") and k not in _HF_TOKEN_ENV_KEYS
    }
    server_env.pop("VLLM_API_KEY", None)
    key = API_KEY if api_key is None else api_key
    if key:
        server_env["VLLM_API_KEY"] = key
    return server_env


def build_vllm_cmd(model_src: str, alias: str) -> list[str]:
    """Builds the vLLM server command line (API key is passed via VLLM_API_KEY env, never --api-key)."""
    cmd = [
        sys.executable, "-m", "vllm.entrypoints.openai.api_server",
        "--model", model_src,
        "--served-model-name", alias,
        "--host", "127.0.0.1",
        "--port", str(PORT),
        "--dtype", DTYPE,
        "--max-model-len", str(MAX_LEN),
        "--gpu-memory-utilization", f"{MEM_UTIL:.2f}",
    ]
    if QUANT != "auto":
        cmd += ["--quantization", QUANT]
    if TRUST_REMOTE:
        cmd.append("--trust-remote-code")
    return cmd


def on_sigterm(*_) -> None:
    p = _proc[0]
    if p and p.poll() is None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(p.pid, signal.SIGTERM)
            else:
                p.terminate()
        except OSError:
            pass
    set_status("failed", _stage[0], "Cancelled")
    os._exit(143)


def main() -> int:
    os.makedirs(RUN_DIR, exist_ok=True)
    signal.signal(signal.SIGTERM, on_sigterm)
    if PORT == 8080:
        set_status("failed", "detect", "Port 8080 is reserved by Colab")
        return 1

    set_status("running", "detect", "Checking GPU hardware...")
    try:
        gpu_line = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()[0]
    except Exception:
        set_status("failed", "detect", "vLLM requires an NVIDIA GPU runtime on Colab")
        return 1

    stop_hb = threading.Event()

    def hb_loop() -> None:
        while not stop_hb.wait(25):
            beat()

    beat()
    threading.Thread(target=hb_loop, daemon=True).start()

    set_status("running", "install", f"GPU: {gpu_line} — installing vLLM & hf_xet...", progress=0.2)
    uv_bin = "uv" if subprocess.run(["which", "uv"], capture_output=True).returncode == 0 else None
    if uv_bin:
        subprocess.run(["uv", "pip", "install", "--system", "-q", "vllm", "huggingface_hub[hf_xet]"], check=True)
    else:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "vllm", "huggingface_hub[hf_xet]"], check=True)

    set_status("running", "download", f"Downloading {REPO}...", progress=0.45)
    dl_env = build_download_env()
    snap_lines = subprocess.check_output(
        [sys.executable, "-c", "import os; from huggingface_hub import snapshot_download; print(snapshot_download(os.environ['DL_REPO']))"],
        env=dl_env,
        text=True,
    ).strip().splitlines()
    model_src = snap_lines[-1].strip() if snap_lines and os.path.isdir(snap_lines[-1].strip()) else REPO

    alias = REPO.rsplit("/", 1)[-1].lower()
    cmd = build_vllm_cmd(model_src, alias)
    server_env = build_server_env()

    set_status("running", "server", f"Launching vLLM on port {PORT}...", progress=0.75)
    log_path = os.path.join(RUN_DIR, "vllm.log")
    with open(log_path, "w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            cmd,
            env=server_env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    _proc[0] = proc

    t0 = time.time()
    ready = False
    while time.time() - t0 < 1200:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            pass
        beat()
        time.sleep(3)

    if not ready:
        stop_hb.set()
        _terminate_proc(proc)
        _proc[0] = None
        tail = _read_log_tail(log_path)
        set_status(
            "failed",
            "server",
            f"vLLM failed to reach /health{': ' + tail if tail else '; inspect vllm.log'}",
        )
        return 1

    stop_hb.set()
    set_status(
        "ready",
        "server",
        f"vLLM serving {alias} (ctx={MAX_LEN})",
        progress=1.0,
        outputs={"model_id": alias, "repo": REPO, "ctx": MAX_LEN, "gpu": gpu_line},
    )
    while True:
        time.sleep(30)
        if proc.poll() is not None:
            tail = _read_log_tail(log_path)
            set_status("failed", "server", f"vLLM server exited unexpectedly{': ' + tail if tail else ''}")
            return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as _exc:
        _terminate_proc(_proc[0])
        _proc[0] = None
        set_status("failed", _stage[0], f"Worker failed: {_exc}")
        sys.exit(1)
