#!/usr/bin/env python3
"""Colab Boost VM Worker — ComfyUI Visual & Video Studio."""

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

COMFY_DIR = "/content/ComfyUI"
CKPT_REPO = (ENV("BOOST_INPUT_CHECKPOINT_REPO") or "").strip()
CKPT_FILE = (ENV("BOOST_INPUT_CHECKPOINT_FILE") or "").strip()
SUBDIR = (ENV("BOOST_INPUT_TARGET_SUBDIR") or "checkpoints").strip()
INSTALL_MGR = (ENV("BOOST_INPUT_INSTALL_MANAGER") or "1") == "1"
PORT = int(ENV("BOOST_INPUT_PORT") or "8188")
HF_TOKEN = ENV("BOOST_HF_TOKEN") or ""
CIVITAI_TOKEN = ENV("BOOST_CIVITAI_TOKEN") or ""
SECRETS = [s for s in (HF_TOKEN, CIVITAI_TOKEN) if s and len(s) >= 4]

_proc: list[subprocess.Popen | None] = [None]


def set_status(state: str, stage: str, msg: str, progress: float | None = None, outputs: dict | None = None) -> None:
    for sec in SECRETS:
        msg = msg.replace(sec, "***")
    doc: dict = {"state": state, "stage": stage, "message": msg, "updated_at": time.time()}
    if progress is not None:
        doc["progress"] = progress
    if outputs is not None:
        doc["outputs"] = outputs
    print(f"[{time.strftime('%H:%M:%S')}] [{state}:{stage}] {msg}", flush=True)
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
        set_status("failed", "setup", "Port 8080 is reserved by Colab")
        return 1

    if CKPT_FILE and (".." in CKPT_FILE or CKPT_FILE.startswith("/")):
        set_status("failed", "setup", "Invalid checkpoint_file path")
        return 1

    stop_hb = threading.Event()

    def hb_loop() -> None:
        while not stop_hb.wait(25):
            beat()

    beat()
    threading.Thread(target=hb_loop, daemon=True).start()

    set_status("running", "setup", "Cloning ComfyUI & installing lightweight deps...", progress=0.15)
    if not os.path.isdir(COMFY_DIR):
        subprocess.run(["git", "clone", "--depth", "1", "https://github.com/comfyanonymous/ComfyUI.git", COMFY_DIR], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", f"{COMFY_DIR}/requirements.txt", "huggingface_hub[hf_xet]"], check=True)

    if INSTALL_MGR:
        mgr_dir = f"{COMFY_DIR}/custom_nodes/ComfyUI-Manager"
        if not os.path.isdir(mgr_dir):
            subprocess.run(["git", "clone", "--depth", "1", "https://github.com/ltdrdata/ComfyUI-Manager.git", mgr_dir], check=True)

    if CKPT_REPO:
        set_status("running", "models", f"Downloading weights from {CKPT_REPO}...", progress=0.5)
        dest = os.path.join(COMFY_DIR, "models", SUBDIR)
        os.makedirs(dest, exist_ok=True)
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("BOOST_")}
        if CKPT_REPO.startswith(("http://", "https://")):
            out_name = os.path.basename(CKPT_FILE) if CKPT_FILE else "civitai_model.safetensors"
            curl_cmd = ["curl", "-fsSL", "-L", "--retry", "3", "-o", os.path.join(dest, out_name)]
            curl_stdin = None
            if CIVITAI_TOKEN and "civitai.com" in CKPT_REPO.lower():
                curl_cmd += ["-K", "-"]
                curl_stdin = f'header = "Authorization: Bearer {CIVITAI_TOKEN}"\n'
            curl_cmd.append(CKPT_REPO)
            subprocess.run(curl_cmd, input=curl_stdin, text=True, env=clean_env, check=True)
        else:
            if HF_TOKEN:
                clean_env["HF_TOKEN"] = HF_TOKEN
            dl_env = {**clean_env, "DL_REPO": CKPT_REPO, "DL_FILE": CKPT_FILE, "DL_DEST": dest}
            dl_py = (
                "import os\n"
                "from huggingface_hub import hf_hub_download, snapshot_download\n"
                "f = os.environ.get('DL_FILE', '')\n"
                "if f:\n"
                "    hf_hub_download(repo_id=os.environ['DL_REPO'], filename=f, local_dir=os.environ['DL_DEST'])\n"
                "else:\n"
                "    snapshot_download(repo_id=os.environ['DL_REPO'], local_dir=os.environ['DL_DEST'])\n"
            )
            subprocess.run([sys.executable, "-c", dl_py], env=dl_env, check=True)
    else:
        set_status("running", "models", "No pre-download requested; skipping straight to server", progress=0.6)

    set_status("running", "server", f"Starting ComfyUI on 127.0.0.1:{PORT}...", progress=0.8)
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("BOOST_")}
    log_fh = open(os.path.join(RUN_DIR, "comfyui.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--listen", "127.0.0.1", "--port", str(PORT)],
        cwd=COMFY_DIR,
        env=clean_env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    _proc[0] = proc

    t0 = time.time()
    up = False
    while time.time() - t0 < 300:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/system_stats", timeout=5) as r:
                if r.status == 200:
                    up = True
                    break
        except Exception:
            pass
        beat()
        time.sleep(2)

    if not up:
        stop_hb.set()
        set_status("failed", "server", "ComfyUI failed to pass /system_stats check")
        return 1

    stop_hb.set()
    set_status("ready", "server", f"ComfyUI Studio live on port {PORT}", progress=1.0, outputs={"port": PORT, "output_dir": "/content/ComfyUI/output"})
    while True:
        time.sleep(30)
        if proc.poll() is not None:
            set_status("failed", "server", "ComfyUI server stopped")
            return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as _exc:
        set_status("failed", "setup", f"Worker failed: {_exc}")
        sys.exit(1)
