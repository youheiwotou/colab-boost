#!/usr/bin/env python3
"""Colab Boost VM Worker — Prebuilt CUDA llama.cpp GGUF Deployer.

Runs detached on the Colab runtime:
  1. Probes GPU & VRAM via nvidia-smi.
  2. Downloads prebuilt CUDA llama-server + cudart (with dynamic CUDA version fallback)
     in parallel with GGUF weights, aborting early if the binary install fails.
  3. Parses GGUF metadata via HTTP Range header and selects the optimal quantization,
     KV cache precision, and context length for the assigned GPU VRAM.
  4. Passes API keys via LLAMA_API_KEY env var (never --api-key on the command line),
     enforces LD_LIBRARY_PATH=<bindir>:/usr/lib64-nvidia, and verifies CUDA0 + VRAM usage
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
FALLBACK_CUDA_VERSIONS = ("12.8", "12.4", "13.0", "12.6")
FORBIDDEN_EXTRA_FLAGS = frozenset({"--host", "--port", "--api-key", "--api-key-file"})

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
COMPUTE_CTX_MIB_PER_32K = 256.0
BUDGET_FRAC = 0.97
DEFAULT_KV_BPT = 128 * 1024
DEFAULT_CTX = 32768
MIN_CTX = 8192
MIN_LOW_VRAM_CTX = 4096
LOW_VRAM_THRESHOLD_MIB = 16384
MAX_GGUF_STR_BYTES = 1024 * 1024
MAX_NESTED_ARRAY_DEPTH = 16
KV_FACTORS = {"f16": 1.0, "q8_0": 34.0 / 64.0, "q4_0": 18.0 / 64.0}
FP_QUANTS = frozenset({"F16", "BF16", "F32"})
LOW_BIT = ("Q3", "Q2", "Q1", "IQ3", "IQ2", "IQ1", "TQ")
SHARD_SUFFIX_RE = re.compile(r"[-_]\d{5}[-_]of[-_]\d{5}$", re.IGNORECASE | re.ASCII)
QUANT_TOKEN_RE = re.compile(
    r"(?:^|[-._])(?:(UD)[-_])?((?:BF16|F16|F32|MXFP4(?:_[A-Z0-9]+)*|(?:IQ|TQ|Q)\d+(?:_[A-Z0-9]+)*))(?=$|[-.])",
    re.IGNORECASE | re.ASCII,
)

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
    parent = os.path.dirname(STATUS_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, STATUS_FILE)


def _read_log_tail(log_path: str, max_lines: int = 8, max_chars: int = 360) -> str:
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()[-max_lines:]
        tail = " | ".join(x.strip() for x in lines if x.strip())
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


def abort(msg: str) -> None:
    _terminate_proc(_server_proc[0])
    _server_proc[0] = None
    write_status("failed", message=msg)
    sys.exit(1)


def beat() -> None:
    if not HEARTBEAT_FILE:
        return
    try:
        parent = os.path.dirname(HEARTBEAT_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(HEARTBEAT_FILE, "a", encoding="utf-8"):
            pass
        os.utime(HEARTBEAT_FILE, None)
    except OSError:
        pass


def _is_quant_token(token: str) -> bool:
    u = token.upper()
    if not u or not all(c.isascii() and (c.isalnum() or c == "_") for c in u):
        return False
    if u in FP_QUANTS or u.startswith("MXFP4"):
        return True
    return any(u.startswith(p) and len(u) > len(p) and u[len(p)].isdigit() for p in ("IQ", "TQ", "Q"))


def extract_quant(path: str) -> str | None:
    base = path.rsplit("/", 1)[-1]
    if not base.lower().endswith(".gguf"):
        return None
    stem = SHARD_SUFFIX_RE.sub("", base[:-5])
    matches = list(QUANT_TOKEN_RE.finditer(stem))
    for match in reversed(matches):
        ud = match.group(1)
        u = match.group(2).upper()
        if _is_quant_token(u):
            return f"UD-{u}" if ud else u
    return None


def _base_quant(quant: str) -> str:
    return quant.rsplit("/", 1)[-1]


def group_files(files: list[tuple[str, int]]) -> tuple[list[dict], list[tuple[str, int]]]:
    by_dir_q: dict[tuple[str, str], dict] = {}
    subdirs_for_q: dict[str, set[str]] = {}
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
        subdir = path.rsplit("/", 1)[0] if "/" in path else ""
        subdirs_for_q.setdefault(q, set()).add(subdir)
        entry = by_dir_q.setdefault((subdir, q), {"quant": q, "files": [], "size": 0})
        entry["files"].append(path)
        entry["size"] += sz
    for (subdir, q), v in by_dir_q.items():
        if len(subdirs_for_q.get(q, ())) > 1 and subdir:
            v["quant"] = f"{subdir}/{q}"
        v["files"].sort()
    variants = sorted(by_dir_q.values(), key=lambda x: (-x["size"], x["quant"]))
    mm.sort(key=lambda item: (0 if re.search(r"[-_]f16", item[0].lower()) else 1 if "bf16" in item[0].lower() else 2, item[0]))
    return variants, mm


def parse_gguf_meta(buf: bytes) -> dict:
    scalars = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
    pos = [0]

    def take(n: int) -> bytes:
        if n < 0 or pos[0] + n > len(buf):
            raise EOFError
        out = buf[pos[0] : pos[0] + n]
        pos[0] += n
        return out

    def up(fmt: str):
        return struct.unpack(fmt, take(struct.calcsize(fmt)))[0]

    def rstr() -> str:
        length = up("<Q")
        if length > MAX_GGUF_STR_BYTES:
            raise EOFError
        return take(length).decode("utf-8", "replace")

    def rval(t: int, depth: int = 0):
        if depth > MAX_NESTED_ARRAY_DEPTH:
            raise EOFError
        if t in scalars:
            return up(scalars[t])
        if t == 8:
            return rstr()
        if t == 9:
            et, cnt = up("<I"), up("<Q")
            rem = len(buf) - pos[0]
            if et == 8:
                if cnt > rem // 8:
                    raise EOFError
                for _ in range(cnt):
                    rstr()
                return None
            if et == 9:
                if cnt > rem // 12:
                    raise EOFError
                if cnt <= 4096:
                    return [rval(9, depth + 1) for _ in range(cnt)]
                for _ in range(cnt):
                    rval(9, depth + 1)
                return None
            if et in scalars:
                esz = struct.calcsize(scalars[et])
                if cnt > rem // esz:
                    raise EOFError
                if cnt <= 4096:
                    return [up(scalars[et]) for _ in range(cnt)]
                take(cnt * esz)
                return None
            raise EOFError
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


def _sliding_window(meta: dict) -> int | None:
    arch = meta.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return None
    interval = meta.get(f"{arch}.full_attention_interval")
    if isinstance(interval, int) and interval > 0:
        return None
    sw = meta.get(f"{arch}.attention.sliding_window")
    return int(sw) if isinstance(sw, (int, float)) and sw > 0 else None


def kv_info(meta: dict) -> tuple[int | None, int | None, bool]:
    arch = meta.get("general.architecture")
    mtp = any("nextn_predict_layers" in k or "mtp_num_hidden_layers" in k for k in meta)
    if not isinstance(arch, str) or not arch:
        return None, None, mtp
    raw_ctx = meta.get(f"{arch}.context_length")
    ctx = int(raw_ctx) if isinstance(raw_ctx, (int, float)) and raw_ctx > 0 else None
    blocks = meta.get(f"{arch}.block_count")
    heads = meta.get(f"{arch}.attention.head_count")
    kv_heads = meta.get(f"{arch}.attention.head_count_kv")
    embd = meta.get(f"{arch}.embedding_length")
    interval = meta.get(f"{arch}.full_attention_interval")

    if not isinstance(blocks, (int, float)) or blocks <= 0:
        return None, ctx, mtp

    layers = max(1, int(blocks) // int(interval)) if isinstance(interval, int) and interval > 0 else int(blocks)

    kv_lora_rank = meta.get(f"{arch}.attention.kv_lora_rank")
    if isinstance(kv_lora_rank, (int, float)) and kv_lora_rank > 0:
        rope_dim = meta.get(f"{arch}.rope.dimension_count")
        rope_d = int(rope_dim) if isinstance(rope_dim, (int, float)) and rope_dim > 0 else 0
        return int(layers * (int(kv_lora_rank) + rope_d) * 2), ctx, mtp

    if isinstance(heads, list):
        valid_heads = [int(h) for h in heads if isinstance(h, (int, float)) and h > 0]
        heads = max(valid_heads) if valid_heads else None
    klen = meta.get(f"{arch}.attention.key_length") or (embd // heads if embd and heads else None)
    vlen = meta.get(f"{arch}.attention.value_length") or klen
    if not klen or not vlen:
        return None, ctx, mtp
    if isinstance(kv_heads, list):
        valid_kv = [int(h) for h in kv_heads if isinstance(h, (int, float)) and h > 0]
        if not valid_kv:
            return None, ctx, mtp
        return sum(valid_kv) * int(klen + vlen) * 2, ctx, mtp
    eff_kv = kv_heads or heads
    if not eff_kv:
        return None, ctx, mtp
    return int(layers * int(eff_kv) * int(klen + vlen) * 2), ctx, mtp


def choose_fit(variants: list[dict], vram_mib: int, mm_bytes: int, meta: dict) -> dict:
    kvb, mctx, _ = kv_info(meta)
    kvb_eff = kvb or DEFAULT_KV_BPT
    sw = _sliding_window(meta)
    max_c = CTX if CTX > 0 else (int(mctx) if isinstance(mctx, (int, float)) and mctx > 0 else DEFAULT_CTX)
    min_c = MIN_LOW_VRAM_CTX if 0 < vram_mib <= LOW_VRAM_THRESHOLD_MIB else MIN_CTX
    ladder = [max_c]
    if CTX == 0:
        c = max_c
        while c // 2 >= min_c:
            c //= 2
            ladder.append(c)
    budget = vram_mib * BUDGET_FRAC
    kv_list = [KV_PREF] if KV_PREF != "auto" else ["f16", "q8_0"]

    def attempt(v: dict, ctx_val: int) -> dict | None:
        mib = 1024.0 * 1024.0
        w = v["size"] / mib
        eff_kv_ctx = min(ctx_val, sw) if (sw and sw > 0) else ctx_val
        compute_ctx = COMPUTE_CTX_MIB_PER_32K * (max(0, ctx_val) / 32768.0)
        for kv in kv_list:
            kv_factor = KV_FACTORS.get(kv, 1.0)
            est = w + (mm_bytes / mib) + (kvb_eff * kv_factor * eff_kv_ctx / mib) + RESERVE_MIB + COMPUTE_MIB + COMPUTE_FRAC * w + compute_ctx
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
        non_fp = [v for v in variants if _base_quant(v["quant"]) not in FP_QUANTS] or variants
        normal = [
            v
            for v in non_fp
            if not (
                _base_quant(v["quant"])[3:]
                if _base_quant(v["quant"]).startswith("UD-")
                else _base_quant(v["quant"])
            ).startswith(LOW_BIT)
        ]
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
            target = next((v for v in variants if _base_quant(v["quant"]).lower() == QUANT.lower()), None)
        if not target:
            abort(f"{REPO} has no {QUANT} files (found: {', '.join(v['quant'] for v in variants)})")
        for c_val in ladder:
            got = attempt(target, c_val)
            if got:
                return got
        abort(f"{QUANT} exceeds {vram_mib} MiB GPU memory")
    return {}


def parse_extra_args(raw: str) -> list[str]:
    if not raw or not raw.strip():
        return []
    flags = shlex.split(raw)
    for tok in flags:
        name = tok.split("=", 1)[0].lower()
        if name in FORBIDDEN_EXTRA_FLAGS:
            raise ValueError(f"extra_args cannot override reserved server option {name!r}")
    return flags


def build_server_cmd(
    bindir: str,
    model_path: str,
    ctx: int,
    alias: str,
    kv_type: str,
    port: int,
    *,
    mmproj_path: str | None = None,
    use_mtp: bool = False,
    extra_flags: list[str] | None = None,
) -> list[str]:
    cmd = [
        f"{bindir}/llama-server", "-m", model_path, "-c", str(ctx),
        "-np", "1", "-ngl", "999", "-fa", "on", "--jinja",
        "--alias", alias, "--cache-type-k", kv_type, "--cache-type-v", kv_type,
        "--host", "127.0.0.1", "--port", str(port),
    ]
    if mmproj_path:
        cmd += ["--mmproj", mmproj_path]
    if use_mtp:
        cmd += ["--spec-type", "draft-mtp"]
    if extra_flags:
        cmd += list(extra_flags)
    return cmd


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


def _find_cached_llama_server_dir(target_dir: str) -> str | None:
    if not os.path.isdir(target_dir):
        return None
    if os.path.isfile(os.path.join(target_dir, "llama-server")):
        return target_dir
    for root, _, files in os.walk(target_dir):
        if "llama-server" in files and os.path.isfile(os.path.join(root, "llama-server")):
            return root
    return None


def _select_release_archives(tag: str, asset_names: list[str] | None = None) -> list[list[str]]:
    """Returns prioritized candidate archive sets (llama-server + optional cudart) for a release tag."""
    if asset_names is not None:
        asset_set = set(asset_names)
        pat = re.compile(rf"^llama-{re.escape(tag)}-bin-ubuntu-cuda-(\d+\.\d+)-x64\.tar\.gz$")
        found_versions: list[str] = []
        for name in asset_names:
            m = pat.match(name)
            if m:
                found_versions.append(m.group(1))
        if found_versions:
            pref_rank = {ver: i for i, ver in enumerate(FALLBACK_CUDA_VERSIONS)}

            def _ver_key(ver: str) -> tuple[int, tuple[int, ...]]:
                nums = tuple(-int(p) for p in ver.split("."))
                return (pref_rank.get(ver, len(pref_rank)), nums)

            candidates: list[list[str]] = []
            for ver in sorted(set(found_versions), key=_ver_key):
                main_arc = f"llama-{tag}-bin-ubuntu-cuda-{ver}-x64.tar.gz"
                cudart_arc = f"cudart-llama-{tag}-bin-ubuntu-cuda-{ver}-x64.tar.gz"
                if cudart_arc in asset_set:
                    candidates.append([main_arc, cudart_arc])
                else:
                    candidates.append([main_arc])
            return candidates

    return [
        [
            f"llama-{tag}-bin-ubuntu-cuda-{ver}-x64.tar.gz",
            f"cudart-llama-{tag}-bin-ubuntu-cuda-{ver}-x64.tar.gz",
        ]
        for ver in FALLBACK_CUDA_VERSIONS
    ]


def _fetch_release_asset_names(tag: str) -> list[str] | None:
    try:
        rel = http_get_json(
            f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{tag}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "colab-boost-worker"},
            timeout=15,
        )
        assets = rel.get("assets") if isinstance(rel, dict) else None
        if isinstance(assets, list):
            return [str(a["name"]) for a in assets if isinstance(a, dict) and a.get("name")]
    except Exception:
        pass
    return None


def install_prebuilt_llama(holder: dict) -> None:
    try:
        os.makedirs(LLAMA_BIN_ROOT, exist_ok=True)
        chosen = None
        for tag in discover_llama_tags():
            target_dir = os.path.join(LLAMA_BIN_ROOT, tag)
            cached_dir = _find_cached_llama_server_dir(target_dir)
            if cached_dir is not None:
                chosen = (tag, cached_dir)
                break
            os.makedirs(target_dir, exist_ok=True)
            base = f"https://github.com/ggml-org/llama.cpp/releases/download/{tag}"
            asset_names = _fetch_release_asset_names(tag)
            candidate_sets = _select_release_archives(tag, asset_names)
            downloaded_archives: list[str] | None = None
            for archives in candidate_sets:
                ok = True
                fetched: list[str] = []
                for arc in archives:
                    dst = os.path.join(target_dir, arc)
                    rc = subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", dst, f"{base}/{arc}"]).returncode
                    if rc != 0:
                        ok = False
                        break
                    fetched.append(arc)
                if ok:
                    downloaded_archives = fetched
                    break
                for arc in archives:
                    dst = os.path.join(target_dir, arc)
                    if os.path.exists(dst):
                        try:
                            os.remove(dst)
                        except OSError:
                            pass
            if not downloaded_archives:
                continue
            for arc in downloaded_archives:
                dst = os.path.join(target_dir, arc)
                subprocess.run(["tar", "xzf", dst, "-C", target_dir], check=True)
                os.remove(dst)
            server_dir = _find_cached_llama_server_dir(target_dir)
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


def runtime_env(bindir: str, *, include_api_key: bool = False) -> dict[str, str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("BOOST_")}
    clean.pop("LLAMA_API_KEY", None)
    ld_parts = [bindir, NVIDIA_LIB_DIR, clean.get("LD_LIBRARY_PATH", "")]
    clean["LD_LIBRARY_PATH"] = ":".join(p for p in ld_parts if p)
    if include_api_key and API_KEY:
        clean["LLAMA_API_KEY"] = API_KEY
    return clean


def on_terminate(*_) -> None:
    proc = _server_proc[0]
    if proc is not None and proc.poll() is None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
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

    try:
        extra_flags = parse_extra_args(EXTRA_ARGS)
    except ValueError as exc:
        abort(f"Invalid extra_args: {exc}")

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

    if "error" in llama_box:
        stop_hb.set()
        abort(f"llama.cpp install error: {llama_box['error']}")

    target_files = list(plan["files"]) + ([mm_file] if mm_file else [])
    size_map = dict(raw_files)
    total_bytes = sum(size_map.get(f, 0) for f in target_files)

    write_status("running", "download", f"Downloading {total_bytes / 1e9:.2f} GB via hf_xet...", progress=0.02)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "huggingface_hub[hf_xet]"], check=True)

    if "error" in llama_box:
        stop_hb.set()
        abort(f"llama.cpp install error: {llama_box['error']}")

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
    dl_proc = subprocess.Popen([sys.executable, "-c", dl_code], env=dl_env, start_new_session=True)
    while dl_proc.poll() is None:
        if "error" in llama_box:
            _terminate_proc(dl_proc)
            stop_hb.set()
            abort(f"llama.cpp install error: {llama_box['error']}")
        time.sleep(1.0)
    if dl_proc.returncode != 0:
        stop_hb.set()
        abort(f"GGUF download failed (exit code {dl_proc.returncode})")

    dl_thread.join()
    if "error" in llama_box:
        stop_hb.set()
        abort(f"llama.cpp install error: {llama_box['error']}")

    bindir = llama_box["bindir"]
    if gpu_name:
        devs = subprocess.run([f"{bindir}/llama-server", "--list-devices"], env=runtime_env(bindir), capture_output=True, text=True).stdout
        if "CUDA0" not in devs:
            abort(f"CUDA0 not detected by llama-server (preventing silent CPU fallback): {devs.strip()[-160:]}")

    alias = ALIAS or re.sub(r"[-_]gguf$", "", REPO.rsplit("/", 1)[-1], flags=re.I).lower()
    model_path = os.path.join(MODELS_DIR, plan["files"][0])
    mmproj_path = os.path.join(MODELS_DIR, mm_file) if mm_file else None
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
    with open(log_path, "w", encoding="utf-8"):
        pass

    for idx, (kv_try, mtp_try) in enumerate(launch_attempts):
        plan["kv"] = kv_try
        cmd = build_server_cmd(
            bindir,
            model_path,
            plan["ctx"],
            alias,
            kv_try,
            PORT,
            mmproj_path=mmproj_path,
            use_mtp=mtp_try,
            extra_flags=extra_flags,
        )

        suffix = f" (fallback #{idx}: KV={kv_try}, MTP={'on' if mtp_try else 'off'})" if idx > 0 else ""
        write_status("running", "server", f"Starting llama-server on 127.0.0.1:{PORT}{suffix}...")
        with open(log_path, "a", encoding="utf-8") as srv_log:
            srv_log.write(f"\n===== Attempt #{idx + 1} (KV={kv_try}, MTP={'on' if mtp_try else 'off'}) =====\n")
            srv_log.flush()
            proc = subprocess.Popen(
                cmd,
                env=runtime_env(bindir, include_api_key=True),
                stdout=srv_log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
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
        _terminate_proc(proc)
        _server_proc[0] = None
        proc = None

    if not healthy or proc is None:
        stop_hb.set()
        _terminate_proc(_server_proc[0])
        _server_proc[0] = None
        tail = _read_log_tail(log_path)
        abort(f"llama-server failed to pass /health check{': ' + tail if tail else ''}")

    if gpu_name and gpu_used_mib() < 512:
        stop_hb.set()
        abort("GPU VRAM usage < 512 MiB after model load; aborting suspected CPU fallback")

    write_status("running", "warmup", "Running CUDA kernel JIT warm-up & tok/s benchmark...")
    auth_hdr = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    try:
        res = http_get_json(
            f"http://127.0.0.1:{PORT}/v1/chat/completions",
            headers=auth_hdr,
            data={"messages": [{"role": "user", "content": "Say hello in one short sentence."}], "max_tokens": 48},
            timeout=300,
        )
    except Exception as exc:
        stop_hb.set()
        tail = _read_log_tail(log_path)
        abort(f"CUDA warm-up failed: {exc}{': ' + tail if tail else ''}")
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
            tail = _read_log_tail(log_path)
            abort(f"llama-server terminated unexpectedly{': ' + tail if tail else ''}")
        try:
            slots = http_get_json(f"http://127.0.0.1:{PORT}/slots", headers=auth_hdr, timeout=8)
            if isinstance(slots, list) and any(isinstance(s, dict) and s.get("is_processing") for s in slots):
                beat()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as _exc:
        _terminate_proc(_server_proc[0])
        _server_proc[0] = None
        write_status("failed", message=f"Worker failed: {_exc}")
        sys.exit(1)
