#!/usr/bin/env python3
"""Colab Boost VM Worker — Faster-Whisper OpenAI-compatible Audio API Server."""

from __future__ import annotations

import glob
import json
import os
import signal
import site
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.request

ENV = os.environ.get
RUN_DIR = ENV("BOOST_RUN_DIR", "/root/.colab-boost/runs/manual")
STATUS_FILE = ENV("BOOST_STATUS_FILE", os.path.join(RUN_DIR, "status.json"))
HEARTBEAT_FILE = ENV("BOOST_HEARTBEAT_FILE", "")
NVIDIA_LIB_DIR = "/usr/lib64-nvidia"

MODEL_SIZE = (ENV("BOOST_INPUT_MODEL_SIZE") or "large-v3-turbo").strip()
COMPUTE_TYPE = (ENV("BOOST_INPUT_COMPUTE_TYPE") or "float16").strip()
PORT = int(ENV("BOOST_INPUT_PORT") or "8002")
HF_TOKEN = ENV("BOOST_HF_TOKEN") or ""

_proc: list[subprocess.Popen | None] = [None]
_stage = ["install"]

APP_CODE = '''import os, re, shutil, tempfile
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from faster_whisper import WhisperModel

MODEL_NAME = os.environ["WHISPER_MODEL_NAME"]
COMPUTE = os.environ["WHISPER_COMPUTE"]
PORT = int(os.environ["WHISPER_PORT"])
HB = os.environ.get("WHISPER_HB", "")
UPLOAD_CHUNK_BYTES = 1024 * 1024

device = "cuda" if os.system("nvidia-smi >/dev/null 2>&1") == 0 else "cpu"
ctype = COMPUTE if device == "cuda" else "int8"
model = WhisperModel(MODEL_NAME, device=device, compute_type=ctype)
app = FastAPI(title="Colab Boost Faster-Whisper API")


def _safe_audio_suffix(filename: str | None) -> str:
    leaf = os.path.basename((filename or "audio.wav").replace("\\\\", "/"))
    ext = os.path.splitext(leaf)[1]
    cleaned = re.sub(r"[^A-Za-z0-9.]", "", ext)
    if not cleaned.startswith(".") or ".." in cleaned or len(cleaned) < 2:
        return ".wav"
    return cleaned[:16]


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "device": device, "compute_type": ctype}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]}


@app.post("/v1/audio/transcriptions")
def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    word_timestamps: bool = Form(default=True),
):
    if HB:
        try:
            parent = os.path.dirname(HB)
            if parent:
                os.makedirs(parent, exist_ok=True)
            open(HB, "a").close()
            os.utime(HB, None)
        except OSError:
            pass
    suffix = _safe_audio_suffix(file.filename)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp, UPLOAD_CHUNK_BYTES)
        tmp_path = tmp.name
    try:
        segments, info = model.transcribe(tmp_path, language=language or None, word_timestamps=word_timestamps, vad_filter=True)
        seg_list = []
        full_text = []
        for s in segments:
            full_text.append(s.text)
            seg_list.append({
                "start": round(s.start, 3),
                "end": round(s.end, 3),
                "text": s.text.strip(),
                "words": [{"word": w.word, "start": round(w.start, 3), "end": round(w.end, 3)} for w in (s.words or [])],
            })
        return {"text": "".join(full_text).strip(), "language": info.language, "duration": round(info.duration, 3), "segments": seg_list}
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
'''


def build_cuda_ld_library_path(existing: str = "", extra_site_roots: list[str] | None = None) -> str:
    """Builds LD_LIBRARY_PATH including /usr/lib64-nvidia and pip-installed NVIDIA cuDNN/cuBLAS libs."""
    candidates: list[str] = [NVIDIA_LIB_DIR]
    if os.path.isdir("/usr/local/cuda/lib64"):
        candidates.append("/usr/local/cuda/lib64")

    site_roots: list[str] = list(extra_site_roots) if extra_site_roots is not None else []
    if extra_site_roots is None:
        try:
            site_roots.extend(site.getsitepackages())
        except Exception:
            pass
        for key in ("purelib", "platlib"):
            p = sysconfig.get_paths().get(key)
            if p and p not in site_roots:
                site_roots.append(p)

    for root in site_roots:
        for lib_dir in sorted(glob.glob(os.path.join(root, "nvidia", "*", "lib"))):
            if os.path.isdir(lib_dir):
                candidates.append(lib_dir)

    for part in existing.split(":"):
        if part:
            candidates.append(part)

    deduped: list[str] = []
    seen: set[str] = set()
    for p in candidates:
        if p and p not in seen:
            seen.add(p)
            deduped.append(p)
    return ":".join(deduped)


def build_server_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    src = os.environ if base_env is None else base_env
    clean_env = {k: v for k, v in src.items() if not k.startswith("BOOST_")}
    if HF_TOKEN:
        clean_env["HF_TOKEN"] = HF_TOKEN
    clean_env["LD_LIBRARY_PATH"] = build_cuda_ld_library_path(clean_env.get("LD_LIBRARY_PATH", ""))
    clean_env.update({
        "WHISPER_MODEL_NAME": MODEL_SIZE,
        "WHISPER_COMPUTE": COMPUTE_TYPE,
        "WHISPER_PORT": str(PORT),
        "WHISPER_HB": HEARTBEAT_FILE,
    })
    return clean_env


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


def _scrub(text: str) -> str:
    if HF_TOKEN and len(HF_TOKEN) >= 4:
        text = text.replace(HF_TOKEN, "***")
    return text


def _read_log_tail(log_path: str, max_lines: int = 8, max_chars: int = 360) -> str:
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()[-max_lines:]
        tail = " | ".join(line.strip() for line in lines if line.strip())
        return _scrub(tail)[-max_chars:]
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


def set_status(state: str, stage: str, msg: str, progress: float | None = None, outputs: dict | None = None) -> None:
    _stage[0] = stage
    msg = _scrub(msg)
    doc: dict = {"state": state, "stage": stage, "message": msg, "updated_at": time.time()}
    if progress is not None:
        doc["progress"] = progress
    if outputs is not None:
        doc["outputs"] = outputs
    print(f"[{time.strftime('%H:%M:%S')}] [{state}:{stage}] {msg}", flush=True)
    parent = os.path.dirname(STATUS_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, STATUS_FILE)


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
        set_status("failed", "install", "Port 8080 is reserved by Colab")
        return 1

    stop_hb = threading.Event()

    def hb_loop() -> None:
        while not stop_hb.wait(25):
            beat()

    beat()
    threading.Thread(target=hb_loop, daemon=True).start()

    set_status("running", "install", "Installing faster-whisper, fastapi, uvicorn...", progress=0.2)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "faster-whisper", "fastapi", "uvicorn", "python-multipart"], check=True)

    srv_script = os.path.join(RUN_DIR, "whisper_server.py")
    with open(srv_script, "w", encoding="utf-8") as fh:
        fh.write(APP_CODE)

    set_status("running", "download", f"Downloading & warming up {MODEL_SIZE} ({COMPUTE_TYPE})...", progress=0.6)
    clean_env = build_server_env()

    log_path = os.path.join(RUN_DIR, "whisper.log")
    with open(log_path, "w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, srv_script],
            env=clean_env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    _proc[0] = proc

    t0 = time.time()
    ok = False
    info: dict = {}
    while time.time() - t0 < 480:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as r:
                if r.status == 200:
                    info = json.load(r)
                    ok = True
                    break
        except Exception:
            pass
        time.sleep(2.5)

    if not ok:
        stop_hb.set()
        _terminate_proc(proc)
        _proc[0] = None
        tail = _read_log_tail(log_path)
        set_status(
            "failed",
            "server",
            f"Faster-Whisper server failed to start{': ' + tail if tail else ''}",
        )
        return 1

    stop_hb.set()
    set_status(
        "ready",
        "server",
        f"Faster-Whisper {MODEL_SIZE} ready on {info.get('device', 'cuda').upper()}",
        progress=1.0,
        outputs={"model_id": MODEL_SIZE, "device": info.get("device"), "compute_type": info.get("compute_type")},
    )
    while True:
        time.sleep(30)
        if proc.poll() is not None:
            tail = _read_log_tail(log_path)
            set_status(
                "failed",
                "server",
                f"Faster-Whisper process exited{': ' + tail if tail else ''}",
            )
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
