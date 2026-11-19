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

_proc: list[subprocess.Popen | None] = [None]


def scrub(text: str) -> str:
    for s in SECRETS:
        text = text.replace(s, "***")
    return text


def set_status(state: str, stage: str, message: str, progress: float | None = None, outputs: dict | None = None) -> None:
    doc: dict = {"state": state, "stage": stage, "message": scrub(message), "updated_at": time.time()}
    if progress is not None:
        doc["progress"] = progress
    if outputs is not None:
        doc["outputs"] = outputs
    print(f"[{time.strftime('%H:%M:%S')}] [{state}:{stage}] {doc['message']}", flush=True)
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, STATUS_FILE)


def beat() -> None:
    if HEARTBEAT_FILE:
        try:
            with open(HEARTBEAT_FILE, "a", encoding="utf-8"):
                pass
            os.utime(HEARTBEAT_FILE, None)
        except OSError:
            pass


def on_sigterm(*_) -> None:
    p = _proc[0]
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except OSError:
            pass
    set_status("failed", "server", "Cancelled")
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
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("BOOST_")}
    if HF_TOKEN:
        clean_env["HF_TOKEN"] = HF_TOKEN
    dl_env = {**clean_env, "DL_REPO": REPO}
    subprocess.run(
        [sys.executable, "-c", "import os; from huggingface_hub import snapshot_download; snapshot_download(os.environ['DL_REPO'])"],
        env=dl_env,
        check=True,
    )

    alias = REPO.rsplit("/", 1)[-1].lower()
    cmd = [
        sys.executable, "-m", "vllm.entrypoints.openai.api_server",
        "--model", REPO,
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
    if API_KEY:
        cmd += ["--api-key", API_KEY]

    set_status("running", "server", f"Launching vLLM on port {PORT}...", progress=0.75)
    log_fh = open(os.path.join(RUN_DIR, "vllm.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, env=clean_env, stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
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
        set_status("failed", "server", "vLLM failed to reach /health; inspect vllm.log")
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
            set_status("failed", "server", "vLLM server exited unexpectedly")
            return 1


if __name__ == "__main__":
    sys.exit(main())
