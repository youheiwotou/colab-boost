#!/usr/bin/env python3
"""Colab Boost VM Worker — Prebuilt CUDA llama.cpp GGUF Deployer.

Runs detached on the Colab runtime:
  1. Probes GPU & VRAM via nvidia-smi.
  2. Downloads prebuilt CUDA 12.8 llama-server + cudart in parallel with GGUF weights.
  3. Parses GGUF metadata via HTTP Range header and selects the optimal quantization,
     KV cache precision, and context length for the assigned GPU VRAM.
  4. Enforces LD_LIBRARY_PATH=<bindir>:/usr/lib64-nvidia and verifies CUDA0 + VRAM usage
     so silent CPU fallback can never happen.
  5. Warms up CUDA kernels and reports measured generation tok/s.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ENV = os.environ.get
RUN_DIR = ENV("BOOST_RUN_DIR", "/root/.colab-boost/runs/manual")
STATUS_FILE = ENV("BOOST_STATUS_FILE", os.path.join(RUN_DIR, "status.json"))
HEARTBEAT_FILE = ENV("BOOST_HEARTBEAT_FILE", "")

MODELS_DIR = "/content/boost-models/gguf"
LLAMA_BIN_ROOT = "/content/boost-bin/llama-cpp"
NVIDIA_LIB_DIR = "/usr/lib64-nvidia"
VERIFIED_FALLBACK_TAG = "b12528"

REPO = (ENV("BOOST_INPUT_REPO") or "").strip()
QUANT = (ENV("BOOST_INPUT_QUANT") or "auto").strip()
CTX = int(float(ENV("BOOST_INPUT_CTX") or "0"))
KV_PREF = (ENV("BOOST_INPUT_KV") or "auto").strip()
PORT = int(float(ENV("BOOST_INPUT_PORT") or "8000"))
ALIAS = (ENV("BOOST_INPUT_ALIAS") or "").strip()
MMPROJ_PREF = (ENV("BOOST_INPUT_MMPROJ") or "auto").strip()
MTP_PREF = (ENV("BOOST_INPUT_MTP") or "auto").strip()
API_KEY = ENV("BOOST_INPUT_API_KEY") or ""
EXTRA_ARGS = ENV("BOOST_INPUT_EXTRA_ARGS") or ""
HF_TOKEN = ENV("BOOST_HF_TOKEN") or ""

SECRETS = [s for s in (API_KEY, HF_TOKEN) if s and len(s) >= 4]

RESERVE_MIB = 1024.0
COMPUTE_MIB = 512.0
COMPUTE_FRAC = 0.05
BUDGET_FRAC = 0.97
DEFAULT_KV_BPT = 128 * 1024
DEFAULT_CTX = 32768
MIN_CTX = 8192
KV_FACTORS = {"f16": 1.0, "q8_0": 34.0 / 64.0, "q4_0": 18.0 / 64.0}
FP_QUANTS = ("F16", "BF16", "F32")
LOW_BIT = ("Q3", "Q2", "Q1", "IQ3", "IQ2", "IQ1", "TQ")

_stage = ["detect"]
_server_proc: list[subprocess.Popen | None] = [None]


def scrub(text: str) -> str:
    for sec in SECRETS:
        text = text.replace(sec, "***")
    return text


def write_status(state: str, stage: str | None = None, message: str = "",
                 progress: float | None = None, outputs: dict | None = None) -> None:
    if stage:
        _stage[0] = stage
    payload: dict = {
        "state": state,
        "stage": _stage[0],
        "message": scrub(message),
        "updated_at": time.time(),
    }
    if progress is not None:
        payload["progress"] = max(0.0, min(1.0, float(progress)))
    if outputs is not None:
        payload["outputs"] = outputs
    print(f"[{time.strftime('%H:%M:%S')}] [{state}:{_stage[0]}] {payload['message']}", flush=True)
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, STATUS_FILE)


def abort(msg: str) -> None:
    write_status("failed", message=msg)
    sys.exit(1)


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


def extract_quant(path: str) -> str | None:
    base = path.rsplit("/", 1)[-1]
    if not base.lower().endswith(".gguf"):
        return None
    stem = base[:-5]
    if len(stem) >= 15 and stem[-15] == "-" and stem[-14:-9].isdigit() and stem[-9:-5] == "-of-" and stem[-5:].isdigit():
        stem = stem[:-15]
    parts = re.split(r"[-.]", stem)
    for i in range(len(parts) - 1, -1, -1):
        u = parts[i].upper()
        is_q = u in FP_QUANTS or u.startswith("MXFP4") or any(
            u.startswith(p) and len(u) > len(p) and u[len(p)].isdigit() for p in ("IQ", "TQ", "Q")
        )
        if is_q:
            return f"UD-{u}" if (i > 0 and parts[i - 1].lower() == "ud") else u
    return None


def group_files(files: list[tuple[str, int]]) -> tuple[list[dict], list[tuple[str, int]]]:
    by_q: dict[str, dict] = {}
    mm: list[tuple[str, int]] = []
    for path, sz in files:
        if not path.lower().endswith(".gguf"):
            continue
        if "mmproj" in path.rsplit("/", 1)[-1].lower():
            mm.append((path, sz))
            continue
        q = extract_quant(path)
        if not q:
            continue
        entry = by_q.setdefault(q, {"quant": q, "files": [], "size": 0})
        entry["files"].append(path)
        entry["size"] += sz
    for v in by_q.values():
        v["files"].sort()
    variants = sorted(by_q.values(), key=lambda x: (-x["size"], x["quant"]))
    mm.sort(key=lambda item: (0 if re.search(r"[-_]f16", item[0].lower()) else 1 if "bf16" in item[0].lower() else 2, item[0]))
    return variants, mm


def parse_gguf_meta(buf: bytes) -> dict:
    scalars = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
    pos = [0]

    def take(n: int) -> bytes:
        if pos[0] + n > len(buf):
            raise EOFError
        out = buf[pos[0] : pos[0] + n]
        pos[0] += n
        return out

    def up(fmt: str):
        return struct.unpack(fmt, take(struct.calcsize(fmt)))[0]

    def rstr() -> str:
        return take(up("<Q")).decode("utf-8", "replace")

    def rval(t: int):
        if t in scalars:
            return up(scalars[t])
        if t == 8:
            return rstr()
        if t == 9:
            et, cnt = up("<I"), up("<Q")
            if et == 8:
                for _ in range(cnt):
                    rstr()
                return None
            if et in scalars and cnt <= 4096:
                return [rval(et) for _ in range(cnt)]
            take(cnt * struct.calcsize(scalars.get(et, "<B")))
            return None
        raise EOFError

    meta = {}
    try:
        if take(4) != b"GGUF" or up("<I") < 2:
            return meta
        up("<Q")
        for _ in range(up("<Q")):
            k = rstr()
            meta[k] = rval(up("<I"))
    except EOFError:
        pass
    return meta


def kv_info(meta: dict) -> tuple[int | None, int | None, bool]:
    arch = meta.get("general.architecture")
    mtp = any("nextn_predict_layers" in k or "mtp_num_hidden_layers" in k for k in meta)
    if not isinstance(arch, str):
        return None, None, mtp
    ctx = meta.get(f"{arch}.context_length")
    blocks = meta.get(f"{arch}.block_count")
    heads = meta.get(f"{arch}.attention.head_count")
    kv_heads = meta.get(f"{arch}.attention.head_count_kv")
    embd = meta.get(f"{arch}.embedding_length")
    if isinstance(heads, list):
        heads = max(heads) if heads else None
    klen = meta.get(f"{arch}.attention.key_length") or (embd // heads if embd and heads else None)
    vlen = meta.get(f"{arch}.attention.value_length") or klen
    if not blocks or not klen:
        return None, ctx, mtp
    if isinstance(kv_heads, list):
        return sum(int(h) for h in kv_heads) * (klen + vlen) * 2, ctx, mtp
    eff_kv = kv_heads or heads
    if not eff_kv:
        return None, ctx, mtp
    interval = meta.get(f"{arch}.full_attention_interval")
    layers = blocks // interval if isinstance(interval, int) and interval > 0 else blocks
    return int(layers * eff_kv * (klen + vlen) * 2), ctx, mtp


def choose_fit(variants: list[dict], vram_mib: int, mm_bytes: int, meta: dict) -> dict:
    kvb, mctx, _ = kv_info(meta)
    kvb_eff = kvb or DEFAULT_KV_BPT
    max_c = CTX if CTX > 0 else (int(mctx) if mctx else DEFAULT_CTX)
    ladder = [max_c]
    if CTX == 0:
        c = max_c
        while c // 2 >= MIN_CTX:
            c //= 2
            ladder.append(c)
    budget = vram_mib * BUDGET_FRAC
    kv_list = [KV_PREF] if KV_PREF != "auto" else ["f16", "q8_0"]

    def attempt(v: dict, ctx_val: int) -> dict | None:
        mib = 1024.0 * 1024.0
        w = v["size"] / mib
        for kv in kv_list:
            est = w + (mm_bytes / mib) + (kvb_eff * KV_FACTORS[kv] * ctx_val / mib) + RESERVE_MIB + COMPUTE_MIB + COMPUTE_FRAC * w
            if est <= budget:
                return {
                    "quant": v["quant"],
                    "kv": kv,
                    "ctx": ctx_val,
                    "files": v["files"],
                    "estimate_mib": int(math.ceil(est)),
                    "budget_mib": int(budget),
                }
        return None

    if QUANT.lower() == "auto":
        non_fp = [v for v in variants if v["quant"] not in FP_QUANTS] or variants
        normal = [v for v in non_fp if not (v["quant"][3:] if v["quant"].startswith("UD-") else v["quant"]).startswith(LOW_BIT)]
        for pool in (normal, non_fp):
            for c_val in ladder:
                for v in pool:
                    got = attempt(v, c_val)
                    if got:
                        return got
        abort(f"No quantization of {REPO} fits in {vram_mib} MiB VRAM")
    else:
        target = next((v for v in variants if v["quant"].lower() == QUANT.lower()), None)
        if not target:
            abort(f"{REPO} has no {QUANT} files (found: {', '.join(v['quant'] for v in variants)})")
        for c_val in ladder:
            got = attempt(target, c_val)
            if got:
                return got
        abort(f"{QUANT} exceeds {vram_mib} MiB GPU memory")
    return {}


def probe_gpu() -> tuple[str | None, int]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout.strip().splitlines()[0]
        n, m = [p.strip() for p in out.split(",")[:2]]
        return n, int(float(m))
    except Exception:
        return None, 0


def gpu_used_mib() -> int:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout.strip().splitlines()[0]
        return int(float(out.strip()))
    except Exception:
        return 0


def http_get_json(url: str, headers: dict | None = None, data: dict | None = None, timeout: int = 30):
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def discover_llama_tags() -> list[str]:
    tags: list[str] = []
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", "https://github.com/ggml-org/llama.cpp", "b*"],
            capture_output=True, text=True, timeout=45,
        ).stdout
        nums = sorted({int(m.group(1)) for m in re.finditer(r"refs/tags/b(\d+)$", out, re.M)}, reverse=True)
        tags = [f"b{n}" for n in nums[:5]]
    except Exception:
        pass
    if VERIFIED_FALLBACK_TAG not in tags:
        tags.append(VERIFIED_FALLBACK_TAG)
    return tags


def install_prebuilt_llama(holder: dict) -> None:
    try:
        os.makedirs(LLAMA_BIN_ROOT, exist_ok=True)
        chosen = None
        for tag in discover_llama_tags():
            target_dir = os.path.join(LLAMA_BIN_ROOT, tag)
            if os.path.isfile(os.path.join(target_dir, "llama-server")):
                chosen = (tag, target_dir)
                break
            os.makedirs(target_dir, exist_ok=True)
            base = f"https://github.com/ggml-org/llama.cpp/releases/download/{tag}"
            archives = [
                f"llama-{tag}-bin-ubuntu-cuda-12.8-x64.tar.gz",
                f"cudart-llama-{tag}-bin-ubuntu-cuda-12.8-x64.tar.gz",
            ]
            ok = True
            for arc in archives:
                dst = os.path.join(target_dir, arc)
                rc = subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", dst, f"{base}/{arc}"]).returncode
                if rc != 0:
                    ok = False
                    break
            if not ok:
                continue
            for arc in archives:
                dst = os.path.join(target_dir, arc)
                subprocess.run(["tar", "xzf", dst, "-C", target_dir], check=True)
                os.remove(dst)
            server_dir = None
            for root, _, fs in os.walk(target_dir):
                if "llama-server" in fs:
                    server_dir = root
                    break
            if not server_dir:
                continue
            for root, _, fs in os.walk(target_dir):
                if root != server_dir:
                    for fn in fs:
                        if ".so" in fn and not os.path.exists(os.path.join(server_dir, fn)):
                            subprocess.run(["cp", "-a", os.path.join(root, fn), os.path.join(server_dir, fn)], check=True)
            chosen = (tag, server_dir)
            break
        if not chosen:
            raise RuntimeError("Could not download prebuilt CUDA llama.cpp release")
        holder["tag"], holder["bindir"] = chosen
    except Exception as exc:
        holder["error"] = str(exc)


def runtime_env(bindir: str) -> dict[str, str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("BOOST_")}
    ld_parts = [bindir, NVIDIA_LIB_DIR, clean.get("LD_LIBRARY_PATH", "")]
    clean["LD_LIBRARY_PATH"] = ":".join(p for p in ld_parts if p)
    return clean


def on_terminate(*_) -> None:
    proc = _server_proc[0]
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
    write_status("failed", message="Cancelled by user")
    os._exit(143)


def main() -> int:
    os.makedirs(RUN_DIR, exist_ok=True)
    signal.signal(signal.SIGTERM, on_terminate)
    if not REPO:
        abort("Missing model repository")
    if PORT == 8080:
        abort("Port 8080 is reserved by Colab; pick another port (e.g. 8000)")

    stop_hb = threading.Event()

    def hb_loop() -> None:
        while not stop_hb.wait(25):
            beat()

    beat()
    threading.Thread(target=hb_loop, daemon=True).start()

    write_status("running", "detect", "Checking Colab GPU hardware...")
    gpu_name, vram_mib = probe_gpu()
    write_status("running", "detect", f"{gpu_name} ({vram_mib} MiB)" if gpu_name else "CPU-only runtime")

    llama_box: dict = {}
    dl_thread = threading.Thread(target=install_prebuilt_llama, args=(llama_box,), daemon=True)
    dl_thread.start()

    write_status("running", "plan", f"Scanning GGUF files in {REPO}...")
    hf_hdr = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    try:
        tree = http_get_json(f"https://huggingface.co/api/models/{REPO}/tree/main?recursive=true", headers=hf_hdr)
    except urllib.error.HTTPError as exc:
        abort(f"Hugging Face HTTP {exc.code} reading {REPO}")
    raw_files = [
        (e["path"], int((e.get("lfs") or {}).get("size") or e.get("size") or 0))
        for e in tree
        if isinstance(e, dict) and e.get("type") == "file" and str(e.get("path", "")).lower().endswith(".gguf")
    ]
    variants, mm_list = group_files(raw_files)
    if not variants:
        abort(f"No .gguf files found in {REPO}")

    mm_file = None
    if MMPROJ_PREF.lower() != "none" and mm_list:
        cands = [p for p, _ in mm_list if MMPROJ_PREF == "auto" or MMPROJ_PREF.lower() in p.lower()]
        mm_file = cands[0] if cands else None
    mm_bytes = dict(mm_list).get(mm_file, 0) if mm_file else 0

    header_meta = {}
    try:
        h_url = f"https://huggingface.co/{REPO}/resolve/main/{urllib.parse.quote(variants[-1]['files'][0])}"
        h_req = urllib.request.Request(h_url, headers={"Range": "bytes=0-4194303", **hf_hdr})
        with urllib.request.urlopen(h_req, timeout=20) as hr:
            header_meta = parse_gguf_meta(hr.read(4 * 1024 * 1024))
    except Exception:
        pass

    if gpu_name:
        plan = choose_fit(variants, vram_mib, mm_bytes, header_meta)
    else:
        v = variants[-1]
        plan = {"quant": v["quant"], "kv": "f16", "ctx": CTX or 8192, "files": v["files"], "estimate_mib": 0, "budget_mib": 0}

    write_status("running", "plan", f"Selected {plan['quant']} | KV={plan['kv']} | ctx={plan['ctx']}")

    target_files = list(plan["files"]) + ([mm_file] if mm_file else [])
    size_map = dict(raw_files)
    total_bytes = sum(size_map.get(f, 0) for f in target_files)

    write_status("running", "download", f"Downloading {total_bytes / 1e9:.2f} GB via hf_xet...", progress=0.02)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "huggingface_hub[hf_xet]"], check=True)
    os.makedirs(MODELS_DIR, exist_ok=True)
    dl_env = {k: v for k, v in os.environ.items() if not k.startswith("BOOST_")}
    if HF_TOKEN:
        dl_env["HF_TOKEN"] = HF_TOKEN
    dl_env["DL_REPO"] = REPO
    dl_env["DL_DIR"] = MODELS_DIR
    dl_env["DL_FILES_JSON"] = json.dumps(target_files)
    dl_code = (
        "import json, os\n"
        "from huggingface_hub import hf_hub_download\n"
        "for fn in json.loads(os.environ['DL_FILES_JSON']):\n"
        "    hf_hub_download(repo_id=os.environ['DL_REPO'], filename=fn, local_dir=os.environ['DL_DIR'])\n"
    )
    subprocess.run([sys.executable, "-c", dl_code], env=dl_env, check=True)
    dl_thread.join()
    if "error" in llama_box:
        abort(f"llama.cpp install error: {llama_box['error']}")

    bindir = llama_box["bindir"]
    if gpu_name:
        devs = subprocess.run([f"{bindir}/llama-server", "--list-devices"], env=runtime_env(bindir), capture_output=True, text=True).stdout
        if "CUDA0" not in devs:
            abort(f"CUDA0 not detected by llama-server (preventing silent CPU fallback): {devs.strip()[-160:]}")

    alias = ALIAS or re.sub(r"[-_]gguf$", "", REPO.rsplit("/", 1)[-1], flags=re.I).lower()
    model_path = os.path.join(MODELS_DIR, plan["files"][0])
    want_mtp = MTP_PREF == "on" or (MTP_PREF == "auto" and kv_info(header_meta)[2])

    # Build startup fallback ladder (KV f16 -> q8_0, MTP on -> off) for OOM/flag resilience
    launch_attempts: list[tuple[str, bool]] = [(plan["kv"], want_mtp)]
    if want_mtp and MTP_PREF == "auto":
        launch_attempts.append((plan["kv"], False))
    if plan["kv"] == "f16" and KV_PREF == "auto":
        if ("q8_0", False) not in launch_attempts:
            launch_attempts.append(("q8_0", False))

    healthy = False
    proc = None
    log_path = os.path.join(RUN_DIR, "server.log")
    for idx, (kv_try, mtp_try) in enumerate(launch_attempts):
        plan["kv"] = kv_try
        cmd = [
            f"{bindir}/llama-server", "-m", model_path, "-c", str(plan["ctx"]),
            "-np", "1", "-ngl", "999", "-fa", "on", "--jinja",
            "--alias", alias, "--cache-type-k", kv_try, "--cache-type-v", kv_try,
            "--host", "127.0.0.1", "--port", str(PORT),
        ]
        if mm_file:
            cmd += ["--mmproj", os.path.join(MODELS_DIR, mm_file)]
        if mtp_try:
            cmd += ["--spec-type", "draft-mtp"]
        if API_KEY:
            cmd += ["--api-key", API_KEY]
        if EXTRA_ARGS.strip():
            cmd += shlex.split(EXTRA_ARGS)

        suffix = f" (fallback #{idx}: KV={kv_try}, MTP={'on' if mtp_try else 'off'})" if idx > 0 else ""
        write_status("running", "server", f"Starting llama-server on 127.0.0.1:{PORT}{suffix}...")
        srv_log = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(cmd, env=runtime_env(bindir), stdout=srv_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        _server_proc[0] = proc

        t0 = time.time()
        while time.time() - t0 < 900:
            if proc.poll() is not None:
                break
            try:
                if http_get_json(f"http://127.0.0.1:{PORT}/health", timeout=5).get("status") == "ok":
                    healthy = True
                    break
            except Exception:
                pass
            time.sleep(2.5)
        if healthy:
            break

    if not healthy or proc is None:
        tail = ""
        try:
            lines = open(log_path, encoding="utf-8", errors="replace").read().splitlines()[-6:]
            tail = " | ".join(x.strip() for x in lines if x.strip())[-220:]
        except OSError:
            pass
        abort(f"llama-server failed to pass /health check{': ' + tail if tail else ''}")

    if gpu_name and gpu_used_mib() < 512:
        abort("GPU VRAM usage < 512 MiB after model load; aborting suspected CPU fallback")

    write_status("running", "warmup", "Running CUDA kernel JIT warm-up & tok/s benchmark...")
    auth_hdr = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    res = http_get_json(
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        headers=auth_hdr,
        data={"messages": [{"role": "user", "content": "Say hello in one short sentence."}], "max_tokens": 48},
        timeout=300,
    )
    tps = round(float((res.get("timings") or {}).get("predicted_per_second") or 0.0), 1)
    stop_hb.set()

    outputs = {
        "model_id": alias,
        "quant": plan["quant"],
        "ctx": plan["ctx"],
        "kv": plan["kv"],
        "gpu": gpu_name or "CPU",
        "vram_used_mib": gpu_used_mib(),
        "vram_total_mib": vram_mib,
        "tok_per_s": tps,
        "llama_tag": llama_box["tag"],
    }
    write_status("ready", "warmup", f"{alias} ready ({plan['quant']}, ~{tps} tok/s)", progress=1.0, outputs=outputs)

    while True:
        time.sleep(30)
        if proc.poll() is not None:
            abort("llama-server terminated unexpectedly")
        try:
            slots = http_get_json(f"http://127.0.0.1:{PORT}/slots", headers=auth_hdr, timeout=8)
            if isinstance(slots, list) and any(isinstance(s, dict) and s.get("is_processing") for s in slots):
                beat()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
