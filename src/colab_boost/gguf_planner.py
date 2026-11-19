"""GGUF repository scanner, binary GGUF header parser, and GPU VRAM auto-fitter."""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

import httpx

RESERVE_MIB = 1024.0
COMPUTE_BASE_MIB = 512.0
COMPUTE_WEIGHT_FRACTION = 0.05
VRAM_BUDGET_FRACTION = 0.97
FALLBACK_KV_BYTES_PER_TOKEN = 128 * 1024  # 128 KiB/token conservative fallback
FALLBACK_MODEL_CTX = 32768
MIN_AUTO_CTX = 8192

KV_TYPE_FACTORS: dict[str, float] = {
    "f16": 1.0,
    "q8_0": 34.0 / 64.0,
    "q4_0": 18.0 / 64.0,
}

FULL_PRECISION_QUANTS = frozenset({"F16", "BF16", "F32"})
LOW_BIT_PREFIXES = ("Q3", "Q2", "Q1", "IQ3", "IQ2", "IQ1", "TQ")
REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

GGUF_SCALAR_FORMATS: dict[int, str] = {
    0: "<B",
    1: "<b",
    2: "<H",
    3: "<h",
    4: "<I",
    5: "<i",
    6: "<f",
    7: "<?",
    10: "<Q",
    11: "<q",
    12: "<d",
}


@dataclass
class GgufVariant:
    quant: str
    files: list[str] = field(default_factory=list)
    size_bytes: int = 0

    @property
    def size_gib(self) -> float:
        return round(self.size_bytes / (1024**3), 2)


@dataclass
class GgufRepoPlan:
    repo: str
    variants: list[GgufVariant]
    mmproj_files: list[tuple[str, int]]
    preferred_mmproj: Optional[str]
    architecture: Optional[str]
    model_ctx: Optional[int]
    kv_bytes_per_token_f16: Optional[int]
    has_mtp: bool


def validate_hf_repo_id(repo: str) -> str:
    cleaned = (repo or "").strip()
    if not REPO_ID_RE.match(cleaned) or ".." in cleaned:
        raise ValueError(f"Invalid Hugging Face repository id: {repo!r} (expected 'owner/name')")
    return cleaned


def is_quant_token(token: str) -> bool:
    u = token.upper()
    if not u or not all(c.isascii() and (c.isalnum() or c == "_") for c in u):
        return False
    if u in FULL_PRECISION_QUANTS or u.startswith("MXFP4"):
        return True
    for prefix in ("IQ", "TQ", "Q"):
        if u.startswith(prefix):
            rest = u[len(prefix) :]
            return bool(rest) and rest[0].isdigit()
    return False


def strip_shard_suffix(stem: str) -> str:
    # Matches `-00001-of-00003` (15 chars)
    if len(stem) >= 15:
        tail = stem[-15:]
        if tail[0] == "-" and tail[1:6].isdigit() and tail[6:10] == "-of-" and tail[10:15].isdigit():
            return stem[:-15]
    return stem


def extract_quant_label(file_path: str) -> Optional[str]:
    base = file_path.rsplit("/", 1)[-1]
    if not base.lower().endswith(".gguf"):
        return None
    stem = strip_shard_suffix(base[:-5])
    tokens = re.split(r"[-.]", stem)
    for idx in range(len(tokens) - 1, -1, -1):
        if is_quant_token(tokens[idx]):
            label = tokens[idx].upper()
            if idx > 0 and tokens[idx - 1].lower() == "ud":
                return f"UD-{label}"
            return label
    return None


def is_mmproj_file(file_path: str) -> bool:
    return "mmproj" in file_path.rsplit("/", 1)[-1].lower()


def group_gguf_files(files: list[tuple[str, int]]) -> tuple[list[GgufVariant], list[tuple[str, int]]]:
    """Groups GGUF files into sorted variants (largest first) and separates mmproj projectors."""
    by_quant: dict[str, GgufVariant] = {}
    mmproj: list[tuple[str, int]] = []

    for path, size in files:
        if not path.lower().endswith(".gguf"):
            continue
        if is_mmproj_file(path):
            mmproj.append((path, int(size)))
            continue
        label = extract_quant_label(path)
        if not label:
            continue
        variant = by_quant.setdefault(label, GgufVariant(quant=label))
        variant.files.append(path)
        variant.size_bytes += int(size)

    for v in by_quant.values():
        v.files.sort()
    variants = sorted(by_quant.values(), key=lambda v: (-v.size_bytes, v.quant))
    mmproj.sort(
        key=lambda item: (
            0 if re.search(r"[-_]f16", item[0].lower()) else 1 if "bf16" in item[0].lower() else 2,
            item[0],
        )
    )
    return variants, mmproj


def is_low_bit_quant(quant: str) -> bool:
    core = quant[3:] if quant.startswith("UD-") else quant
    return core.startswith(LOW_BIT_PREFIXES)


class _TruncatedBuffer(Exception):
    pass


def parse_gguf_header_bytes(data: bytes) -> dict[str, Any]:
    """Parses GGUF v2/v3 metadata key-values from a partial byte prefix."""
    pos = 0

    def take(n: int) -> bytes:
        nonlocal pos
        if pos + n > len(data):
            raise _TruncatedBuffer
        chunk = data[pos : pos + n]
        pos += n
        return chunk

    def unpack_one(fmt: str) -> Any:
        return struct.unpack(fmt, take(struct.calcsize(fmt)))[0]

    def read_str() -> str:
        length = unpack_one("<Q")
        if length > 1024 * 1024:
            raise _TruncatedBuffer
        return take(length).decode("utf-8", errors="replace")

    def read_val(vtype: int) -> Any:
        if vtype in GGUF_SCALAR_FORMATS:
            return unpack_one(GGUF_SCALAR_FORMATS[vtype])
        if vtype == 8:
            return read_str()
        if vtype == 9:
            elem_type = unpack_one("<I")
            count = unpack_one("<Q")
            if elem_type == 8:
                for _ in range(count):
                    read_str()
                return None
            if elem_type in GGUF_SCALAR_FORMATS and count <= 4096:
                return [read_val(elem_type) for _ in range(count)]
            elem_size = struct.calcsize(GGUF_SCALAR_FORMATS.get(elem_type, "<B"))
            take(count * elem_size)
            return None
        raise _TruncatedBuffer

    meta: dict[str, Any] = {}
    try:
        if take(4) != b"GGUF":
            return meta
        version = unpack_one("<I")
        if version < 2:
            return meta
        _tensor_count = unpack_one("<Q")
        kv_count = unpack_one("<Q")
        for _ in range(kv_count):
            key = read_str()
            vtype = unpack_one("<I")
            meta[key] = read_val(vtype)
    except _TruncatedBuffer:
        pass
    return meta


def extract_kv_architecture_params(meta: dict[str, Any]) -> tuple[Optional[str], Optional[int], Optional[int], bool]:
    """Returns ``(arch, kv_bytes_per_token_f16, context_length, has_mtp)``."""
    arch = meta.get("general.architecture")
    has_mtp = any("nextn_predict_layers" in k or "mtp_num_hidden_layers" in k for k in meta)
    if not isinstance(arch, str) or not arch:
        return None, None, None, has_mtp

    def get_arch(field_name: str) -> Any:
        return meta.get(f"{arch}.{field_name}")

    ctx = get_arch("context_length")
    ctx_int = int(ctx) if isinstance(ctx, (int, float)) and ctx > 0 else None
    blocks = get_arch("block_count")
    heads = get_arch("attention.head_count")
    kv_heads = get_arch("attention.head_count_kv")
    embd = get_arch("embedding_length")

    if isinstance(heads, list):
        heads = max(heads) if heads else None
    key_len = get_arch("attention.key_length") or (embd // heads if embd and heads else None)
    val_len = get_arch("attention.value_length") or key_len
    if not blocks or not key_len:
        return arch, None, ctx_int, has_mtp

    if isinstance(kv_heads, list):
        per_token = sum(int(h) for h in kv_heads) * int(key_len + val_len) * 2
        return arch, per_token, ctx_int, has_mtp

    eff_kv_heads = kv_heads or heads
    if not eff_kv_heads:
        return arch, None, ctx_int, has_mtp

    interval = get_arch("full_attention_interval")
    eff_layers = int(blocks) // int(interval) if isinstance(interval, int) and interval > 0 else int(blocks)
    per_token = int(eff_layers) * int(eff_kv_heads) * int(key_len + val_len) * 2
    return arch, per_token, ctx_int, has_mtp


def estimate_vram_mib(
    weights_bytes: int,
    mmproj_bytes: int,
    kv_bytes_per_token_f16: int,
    ctx: int,
    kv_type: str,
) -> float:
    mib = 1024.0 * 1024.0
    w_mib = weights_bytes / mib
    mm_mib = mmproj_bytes / mib
    kv_factor = KV_TYPE_FACTORS.get(kv_type, 1.0)
    kv_mib = (kv_bytes_per_token_f16 * kv_factor * ctx) / mib
    return w_mib + mm_mib + kv_mib + RESERVE_MIB + COMPUTE_BASE_MIB + (COMPUTE_WEIGHT_FRACTION * w_mib)


def build_ctx_ladder(requested_ctx: int, model_ctx: Optional[int]) -> tuple[list[int], int]:
    if requested_ctx > 0:
        return [requested_ctx], requested_ctx
    max_ctx = model_ctx if (model_ctx and model_ctx > 0) else FALLBACK_MODEL_CTX
    ladder = [max_ctx]
    cur = max_ctx
    while cur // 2 >= MIN_AUTO_CTX:
        cur //= 2
        ladder.append(cur)
    return ladder, max_ctx


def _try_fit_variant(
    variant: GgufVariant,
    vram_mib: int,
    ctx: int,
    max_ctx: int,
    mmproj_bytes: int,
    kv_bytes_per_token: Optional[int],
    forced_kv: Optional[str],
) -> Optional[dict[str, Any]]:
    if vram_mib <= 0:
        return None
    kvb = kv_bytes_per_token or FALLBACK_KV_BYTES_PER_TOKEN
    budget = vram_mib * VRAM_BUDGET_FRACTION
    kv_candidates = [forced_kv] if forced_kv and forced_kv != "auto" else ["f16", "q8_0"]
    for kv in kv_candidates:
        est = estimate_vram_mib(variant.size_bytes, mmproj_bytes, kvb, ctx, kv)
        if est <= budget:
            return {
                "fits": True,
                "quant": variant.quant,
                "kv": kv,
                "ctx": ctx,
                "estimate_mib": int(math.ceil(est)),
                "budget_mib": int(budget),
                "vram_mib": vram_mib,
                "size_gib": variant.size_gib,
                "ctx_reduced": ctx < max_ctx,
                "kv_reduced": (not forced_kv or forced_kv == "auto") and kv != "f16",
                "files": list(variant.files),
            }
    return None


def fit_gguf_for_vram(
    variants: list[GgufVariant],
    vram_mib: int,
    quant: str = "auto",
    requested_ctx: int = 0,
    model_ctx: Optional[int] = None,
    kv_bytes_per_token: Optional[int] = None,
    mmproj_bytes: int = 0,
    kv_type: str = "auto",
) -> dict[str, Any]:
    """Computes the best quantization/KV/context plan for a given GPU VRAM size."""
    if not variants:
        return {"fits": False, "vram_mib": vram_mib, "reason": "No GGUF files in repository"}

    forced_kv = None if kv_type == "auto" else kv_type
    ladder, max_ctx = build_ctx_ladder(requested_ctx, model_ctx)

    if quant.lower() == "auto":
        non_fp = [v for v in variants if v.quant not in FULL_PRECISION_QUANTS]
        pool = non_fp or list(variants)
        normal = [v for v in pool if not is_low_bit_quant(v.quant)]
        for tier in (normal, pool):
            for ctx in ladder:
                for variant in tier:
                    res = _try_fit_variant(
                        variant, vram_mib, ctx, max_ctx, mmproj_bytes, kv_bytes_per_token, forced_kv
                    )
                    if res:
                        return res
        return {
            "fits": False,
            "vram_mib": vram_mib,
            "reason": f"No quantization fits in {vram_mib} MiB VRAM",
            "fitting_quants": [],
        }

    target = next((v for v in variants if v.quant.lower() == quant.lower()), None)
    if target is None:
        return {
            "fits": False,
            "vram_mib": vram_mib,
            "reason": f"{quant} not found in repository",
            "available_quants": [v.quant for v in variants],
        }
    for ctx in ladder:
        res = _try_fit_variant(
            target, vram_mib, ctx, max_ctx, mmproj_bytes, kv_bytes_per_token, forced_kv
        )
        if res:
            return res

    fitting_alts = [
        v.quant
        for v in variants
        if any(
            _try_fit_variant(v, vram_mib, c, max_ctx, mmproj_bytes, kv_bytes_per_token, forced_kv)
            for c in ladder
        )
    ]
    return {
        "fits": False,
        "vram_mib": vram_mib,
        "reason": f"{target.quant} exceeds {vram_mib} MiB VRAM",
        "fitting_quants": fitting_alts,
    }


def build_hardware_fit_matrix(
    plan: GgufRepoPlan,
    quant: str = "auto",
    requested_ctx: int = 0,
    kv_type: str = "auto",
    use_mmproj: bool = True,
) -> list[dict[str, Any]]:
    mm_bytes = 0
    if use_mmproj and plan.preferred_mmproj:
        mm_bytes = dict(plan.mmproj_files).get(plan.preferred_mmproj, 0)

    targets = [
        ("T4 (15 GB)", "T4", 15360),
        ("L4 (23 GB)", "L4", 23034),
        ("A100 (40 GB)", "A100", 40960),
        ("A100 / H100 (80 GB)", "A100", 81920),
    ]
    rows: list[dict[str, Any]] = []
    for label, accel, vram in targets:
        fit = fit_gguf_for_vram(
            plan.variants,
            vram_mib=vram,
            quant=quant,
            requested_ctx=requested_ctx,
            model_ctx=plan.model_ctx,
            kv_bytes_per_token=plan.kv_bytes_per_token_f16,
            mmproj_bytes=mm_bytes,
            kv_type=kv_type,
        )
        rows.append({
            "gpu_label": label,
            "accelerator": accel,
            **fit,
        })
    return rows


async def search_hf_gguf_repos(query: str, hf_token: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
    q = (query or "").strip()
    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    params: dict[str, Any] = {
        "filter": "gguf",
        "sort": "downloads",
        "direction": "-1",
        "limit": max(1, min(25, limit)),
    }
    if q:
        params["search"] = q
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get("https://huggingface.co/api/models", params=params, headers=headers)
        resp.raise_for_status()
        items = resp.json()

    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        results.append({
            "id": item["id"],
            "downloads": item.get("downloads", 0),
            "likes": item.get("likes", 0),
            "pipeline_tag": item.get("pipeline_tag") or "text-generation",
            "updated_at": item.get("lastModified"),
        })
    return results


async def inspect_hf_gguf_repo(repo: str, hf_token: Optional[str] = None) -> GgufRepoPlan:
    repo_id = validate_hf_repo_id(repo)
    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        tree_resp = await client.get(
            f"https://huggingface.co/api/models/{repo_id}/tree/main",
            params={"recursive": "true"},
            headers=headers,
        )
        tree_resp.raise_for_status()
        tree = tree_resp.json()

        files: list[tuple[str, int]] = []
        for entry in tree:
            if isinstance(entry, dict) and entry.get("type") == "file":
                path = str(entry.get("path") or "")
                if path.lower().endswith(".gguf"):
                    lfs = entry.get("lfs")
                    size = (lfs.get("size") if isinstance(lfs, dict) else None) or entry.get("size") or 0
                    files.append((path, int(size)))

        variants, mmproj = group_gguf_files(files)
        preferred_mm = mmproj[0][0] if mmproj else None

        arch, kv_per_tok, model_ctx, has_mtp = None, None, None, False
        if variants:
            smallest_file = variants[-1].files[0]
            header_url = f"https://huggingface.co/{repo_id}/resolve/main/{quote(smallest_file)}"
            range_headers = {"Range": "bytes=0-4194303", **headers}
            try:
                h_resp = await client.get(header_url, headers=range_headers, timeout=12.0)
                if h_resp.status_code in (200, 206):
                    meta = parse_gguf_header_bytes(h_resp.content[: 4 * 1024 * 1024])
                    arch, kv_per_tok, model_ctx, has_mtp = extract_kv_architecture_params(meta)
            except Exception:
                pass

    return GgufRepoPlan(
        repo=repo_id,
        variants=variants,
        mmproj_files=mmproj,
        preferred_mmproj=preferred_mm,
        architecture=arch,
        model_ctx=model_ctx,
        kv_bytes_per_token_f16=kv_per_tok,
        has_mtp=has_mtp,
    )
