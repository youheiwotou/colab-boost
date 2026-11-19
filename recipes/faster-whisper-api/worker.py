#!/usr/bin/env python3
"""Colab Boost VM Worker — Faster-Whisper OpenAI-compatible Audio API Server."""

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

MODEL_SIZE = (ENV("BOOST_INPUT_MODEL_SIZE") or "large-v3-turbo").strip()
COMPUTE_TYPE = (ENV("BOOST_INPUT_COMPUTE_TYPE") or "float16").strip()
PORT = int(ENV("BOOST_INPUT_PORT") or "8002")
HF_TOKEN = ENV("BOOST_HF_TOKEN") or ""

_proc: list[subprocess.Popen | None] = [None]

APP_CODE = '''import os, tempfile
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from faster_whisper import WhisperModel

MODEL_NAME = os.environ["WHISPER_MODEL_NAME"]
COMPUTE = os.environ["WHISPER_COMPUTE"]
PORT = int(os.environ["WHISPER_PORT"])
HB = os.environ.get("WHISPER_HB", "")

device = "cuda" if os.system("nvidia-smi >/dev/null 2>&1") == 0 else "cpu"
ctype = COMPUTE if device == "cuda" else "int8"
model = WhisperModel(MODEL_NAME, device=device, compute_type=ctype)
app = FastAPI(title="Colab Boost Faster-Whisper API")

@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "device": device, "compute_type": ctype}

@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]}

@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    word_timestamps: bool = Form(default=True),
):
    if HB:
        try:
            open(HB, "a").close()
            os.utime(HB, None)
        except OSError:
            pass
    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
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


def beat() -> None:
    if HEARTBEAT_FILE:
        try:
            os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
            with open(HEARTBEAT_FILE, "a", encoding="utf-8"):
                pass
            os.utime(HEARTBEAT_FILE, None)
        except OSError:
            pass


def set_status(state: str, stage: str, msg: str, progress: float | None = None, outputs: dict | None = None) -> None:
    if HF_TOKEN and len(HF_TOKEN) >= 4:
        msg = msg.replace(HF_TOKEN, "***")
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
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("BOOST_")}
    if HF_TOKEN:
        clean_env["HF_TOKEN"] = HF_TOKEN
    clean_env.update({
        "WHISPER_MODEL_NAME": MODEL_SIZE,
        "WHISPER_COMPUTE": COMPUTE_TYPE,
        "WHISPER_PORT": str(PORT),
        "WHISPER_HB": HEARTBEAT_FILE,
    })

    log_fh = open(os.path.join(RUN_DIR, "whisper.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, srv_script], env=clean_env, stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    _proc[0] = proc

    t0 = time.time()
    ok = False
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
        set_status("failed", "server", "Faster-Whisper server failed to start")
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
            set_status("failed", "server", "Faster-Whisper process exited")
            return 1


if __name__ == "__main__":
    sys.exit(main())
