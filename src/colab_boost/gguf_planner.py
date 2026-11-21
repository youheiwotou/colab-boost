"""GGUF repository scanner, binary GGUF header parser, and GPU VRAM auto-fitter."""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

import httpx

from colab_boost.models import ACCELERATOR_VRAM_REFERENCE_MIB

RESERVE_MIB = 1024.0
COMPUTE_BASE_MIB = 512.0
COMPUTE_WEIGHT_FRACTION = 0.05
COMPUTE_CTX_MIB_PER_32K = 256.0
VRAM_BUDGET_FRACTION = 0.97
FALLBACK_KV_BYTES_PER_TOKEN = 128 * 1024  # 128 KiB/token conservative fallback
FALLBACK_MODEL_CTX = 32768
MIN_AUTO_CTX = 8192
MIN_LOW_VRAM_AUTO_CTX = 4096
LOW_VRAM_THRESHOLD_MIB = 16384
HEADER_PROBE_BYTES = 4 * 1024 * 1024
MAX_GGUF_STR_BYTES = 1024 * 1024
MAX_NESTED_ARRAY_DEPTH = 16

KV_TYPE_FACTORS: dict[str, float] = {
    "f16": 1.0,
    "q8_0": 34.0 / 64.0,
    "q4_0": 18.0 / 64.0,
}

FULL_PRECISION_QUANTS = frozenset({"F16", "BF16", "F32"})
LOW_BIT_PREFIXES = ("Q3", "Q2", "Q1", "IQ3", "IQ2", "IQ1", "TQ")
REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
SHARD_SUFFIX_RE = re.compile(r"[-_]\d{5}[-_]of[-_]\d{5}$", re.IGNORECASE | re.ASCII)
QUANT_TOKEN_RE = re.compile(
    r"(?:^|[-._])(?:(UD)[-_])?((?:BF16|F16|F32|MXFP4(?:_[A-Z0-9]+)*|(?:IQ|TQ|Q)\d+(?:_[A-Z0-9]+)*))(?=$|[-.])",
    re.IGNORECASE | re.ASCII,
)

_HARDWARE_MATRIX_ORDER: tuple[str, ...] = ("T4", "L4", "A100", "H100", "G4")
_VRAM_GB_LABEL_OVERRIDES: dict[int, int] = {
    23034: 23,
    97887: 96,
}

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
    sliding_window: Optional[int] = None
    header_truncated: bool = False
    header_warning: Optional[str] = None


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
    # Matches `-00001-of-00003` or `_00001_of_00003`
    return SHARD_SUFFIX_RE.sub("", stem)


def extract_quant_label(file_path: str) -> Optional[str]:
    base = file_path.rsplit("/", 1)[-1]
    if not base.lower().endswith(".gguf"):
        return None
    stem = strip_shard_suffix(base[:-5])
    matches = list(QUANT_TOKEN_RE.finditer(stem))
    for match in reversed(matches):
        ud_prefix = match.group(1)
        token = match.group(2).upper()
        if is_quant_token(token):
            return f"UD-{token}" if ud_prefix else token
    return None


def is_mmproj_file(file_path: str) -> bool:
    return "mmproj" in file_path.rsplit("/", 1)[-1].lower()


def _base_quant_name(quant: str) -> str:
    return quant.rsplit("/", 1)[-1]


def group_gguf_files(files: list[tuple[str, int]]) -> tuple[list[GgufVariant], list[tuple[str, int]]]:
    """Groups GGUF files into sorted variants (largest first) and separates mmproj projectors.

    Files with the same quantization label across different subdirectories are kept
    separate (disambiguated by subdirectory prefix) so duplicate builds are not merged
    and double-counted.
    """
    by_dir_quant: dict[tuple[str, str], GgufVariant] = {}
    subdirs_for_label: dict[str, set[str]] = {}
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
        subdir = path.rsplit("/", 1)[0] if "/" in path else ""
        subdirs_for_label.setdefault(label, set()).add(subdir)
        variant = by_dir_quant.setdefault((subdir, label), GgufVariant(quant=label))
        variant.files.append(path)
        variant.size_bytes += int(size)

    for (subdir, label), variant in by_dir_quant.items():
        if len(subdirs_for_label.get(label, ())) > 1 and subdir:
            variant.quant = f"{subdir}/{label}"
        variant.files.sort()

    variants = sorted(by_dir_quant.values(), key=lambda v: (-v.size_bytes, v.quant))
    mmproj.sort(
        key=lambda item: (
            0 if re.search(r"[-_]f16", item[0].lower()) else 1 if "bf16" in item[0].lower() else 2,
            item[0],
        )
    )
    return variants, mmproj


def is_low_bit_quant(quant: str) -> bool:
    base = _base_quant_name(quant)
    core = base[3:] if base.startswith("UD-") else base
    return core.startswith(LOW_BIT_PREFIXES)


class _TruncatedBuffer(Exception):
    pass


def parse_gguf_header_with_status(data: bytes) -> tuple[dict[str, Any], bool]:
    """Parses GGUF v2/v3 metadata key-values from a partial byte prefix.

    Returns ``(metadata_dict, was_truncated)``.
    """
    pos = 0

    def take(n: int) -> bytes:
        nonlocal pos
        if n < 0 or pos + n > len(data):
            raise _TruncatedBuffer
        chunk = data[pos : pos + n]
        pos += n
        return chunk

    def unpack_one(fmt: str) -> Any:
        return struct.unpack(fmt, take(struct.calcsize(fmt)))[0]

    def read_str() -> str:
        length = unpack_one("<Q")
        if length > MAX_GGUF_STR_BYTES:
            raise _TruncatedBuffer
        return take(length).decode("utf-8", errors="replace")

    def read_val(vtype: int, depth: int = 0) -> Any:
        if depth > MAX_NESTED_ARRAY_DEPTH:
            raise _TruncatedBuffer
        if vtype in GGUF_SCALAR_FORMATS:
            return unpack_one(GGUF_SCALAR_FORMATS[vtype])
        if vtype == 8:
            return read_str()
        if vtype == 9:
            elem_type = unpack_one("<I")
            count = unpack_one("<Q")
            remaining = len(data) - pos
            if elem_type == 8:
                if count > remaining // 8:
                    raise _TruncatedBuffer
                for _ in range(count):
                    read_str()
                return None
            if elem_type == 9:
                if count > remaining // 12:
                    raise _TruncatedBuffer
                if count <= 4096:
                    return [read_val(9, depth + 1) for _ in range(count)]
                for _ in range(count):
                    read_val(9, depth + 1)
                return None
            if elem_type in GGUF_SCALAR_FORMATS:
                elem_size = struct.calcsize(GGUF_SCALAR_FORMATS[elem_type])
                if count > remaining // elem_size:
                    raise _TruncatedBuffer
                if count <= 4096:
                    return [unpack_one(GGUF_SCALAR_FORMATS[elem_type]) for _ in range(count)]
                take(count * elem_size)
                return None
            raise _TruncatedBuffer
        raise _TruncatedBuffer

    meta: dict[str, Any] = {}
    truncated = False
    try:
        if take(4) != b"GGUF":
            return meta, False
        version = unpack_one("<I")
        if version < 2:
            return meta, False
        _tensor_count = unpack_one("<Q")
        kv_count = unpack_one("<Q")
        for _ in range(kv_count):
            key = read_str()
            vtype = unpack_one("<I")
            meta[key] = read_val(vtype)
    except _TruncatedBuffer:
        truncated = True
    return meta, truncated


def parse_gguf_header_bytes(data: bytes) -> dict[str, Any]:
    """Parses GGUF v2/v3 metadata key-values from a partial byte prefix."""
    meta, _truncated = parse_gguf_header_with_status(data)
    return meta


def extract_sliding_window(meta: dict[str, Any]) -> Optional[int]:
    """Extracts pure sliding-window size (tokens) when present without global-layer interleaving."""
    arch = meta.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return None
    interval = meta.get(f"{arch}.full_attention_interval")
    if isinstance(interval, int) and interval > 0:
        return None
    sw = meta.get(f"{arch}.attention.sliding_window")
    if isinstance(sw, (int, float)) and sw > 0:
        return int(sw)
    return None


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
    interval = get_arch("full_attention_interval")

    if not isinstance(blocks, (int, float)) or blocks <= 0:
        return arch, None, ctx_int, has_mtp

    eff_layers = max(1, int(blocks) // int(interval)) if isinstance(interval, int) and interval > 0 else int(blocks)

    # DeepSeek MLA (Multi-head Latent Attention) caches compressed latent + decoupled RoPE key
    kv_lora_rank = get_arch("attention.kv_lora_rank")
    if isinstance(kv_lora_rank, (int, float)) and kv_lora_rank > 0:
        rope_dim = get_arch("rope.dimension_count")
        rope_d = int(rope_dim) if isinstance(rope_dim, (int, float)) and rope_dim > 0 else 0
        per_token = int(eff_layers) * (int(kv_lora_rank) + rope_d) * 2
        return arch, per_token, ctx_int, has_mtp

    if isinstance(heads, list):
        valid_heads = [int(h) for h in heads if isinstance(h, (int, float)) and h > 0]
        heads = max(valid_heads) if valid_heads else None
    key_len = get_arch("attention.key_length") or (embd // heads if embd and heads else None)
    val_len = get_arch("attention.value_length") or key_len
    if not key_len or not val_len:
        return arch, None, ctx_int, has_mtp

    if isinstance(kv_heads, list):
        valid_kv_heads = [int(h) for h in kv_heads if isinstance(h, (int, float)) and h > 0]
        if not valid_kv_heads:
            return arch, None, ctx_int, has_mtp
        per_token = sum(valid_kv_heads) * int(key_len + val_len) * 2
        return arch, per_token, ctx_int, has_mtp

    eff_kv_heads = kv_heads or heads
    if not eff_kv_heads:
        return arch, None, ctx_int, has_mtp

    per_token = int(eff_layers) * int(eff_kv_heads) * int(key_len + val_len) * 2
    return arch, per_token, ctx_int, has_mtp


def estimate_vram_mib(
    weights_bytes: int,
    mmproj_bytes: int,
    kv_bytes_per_token_f16: int,
    ctx: int,
    kv_type: str,
    sliding_window: Optional[int] = None,
) -> float:
    mib = 1024.0 * 1024.0
    w_mib = weights_bytes / mib
    mm_mib = mmproj_bytes / mib
    kv_factor = KV_TYPE_FACTORS.get(kv_type, 1.0)
    effective_kv_ctx = min(ctx, sliding_window) if (sliding_window and sliding_window > 0) else ctx
    kv_mib = (kv_bytes_per_token_f16 * kv_factor * effective_kv_ctx) / mib
    compute_ctx_mib = COMPUTE_CTX_MIB_PER_32K * (max(0, ctx) / 32768.0)
    return (
        w_mib
        + mm_mib
        + kv_mib
        + RESERVE_MIB
        + COMPUTE_BASE_MIB
        + (COMPUTE_WEIGHT_FRACTION * w_mib)
        + compute_ctx_mib
    )


def build_ctx_ladder(
    requested_ctx: int,
    model_ctx: Optional[int],
    *,
    min_ctx: int = MIN_AUTO_CTX,
) -> tuple[list[int], int]:
    if requested_ctx > 0:
        return [requested_ctx], requested_ctx
    max_ctx = model_ctx if (model_ctx and model_ctx > 0) else FALLBACK_MODEL_CTX
    ladder = [max_ctx]
    cur = max_ctx
    while cur // 2 >= min_ctx:
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
    sliding_window: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    if vram_mib <= 0:
        return None
    kvb = kv_bytes_per_token or FALLBACK_KV_BYTES_PER_TOKEN
    budget = vram_mib * VRAM_BUDGET_FRACTION
    kv_candidates = [forced_kv] if forced_kv and forced_kv != "auto" else ["f16", "q8_0"]
    for kv in kv_candidates:
        est = estimate_vram_mib(
            variant.size_bytes,
            mmproj_bytes,
            kvb,
            ctx,
            kv,
            sliding_window=sliding_window,
        )
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
    sliding_window: Optional[int] = None,
) -> dict[str, Any]:
    """Computes the best quantization/KV/context plan for a given GPU VRAM size."""
    if not variants:
        return {"fits": False, "vram_mib": vram_mib, "reason": "No GGUF files in repository"}

    forced_kv = None if kv_type == "auto" else kv_type
    min_ctx = MIN_LOW_VRAM_AUTO_CTX if 0 < vram_mib <= LOW_VRAM_THRESHOLD_MIB else MIN_AUTO_CTX
    ladder, max_ctx = build_ctx_ladder(requested_ctx, model_ctx, min_ctx=min_ctx)

    if quant.lower() == "auto":
        non_fp = [v for v in variants if _base_quant_name(v.quant) not in FULL_PRECISION_QUANTS]
        pool = non_fp or list(variants)
        normal = [v for v in pool if not is_low_bit_quant(v.quant)]
        for tier in (normal, pool):
            for ctx in ladder:
                for variant in tier:
                    res = _try_fit_variant(
                        variant,
                        vram_mib,
                        ctx,
                        max_ctx,
                        mmproj_bytes,
                        kv_bytes_per_token,
                        forced_kv,
                        sliding_window=sliding_window,
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
        target = next((v for v in variants if _base_quant_name(v.quant).lower() == quant.lower()), None)
    if target is None:
        return {
            "fits": False,
            "vram_mib": vram_mib,
            "reason": f"{quant} not found in repository",
            "available_quants": [v.quant for v in variants],
        }
    for ctx in ladder:
        res = _try_fit_variant(
            target,
            vram_mib,
            ctx,
            max_ctx,
            mmproj_bytes,
            kv_bytes_per_token,
            forced_kv,
            sliding_window=sliding_window,
        )
        if res:
            return res

    fitting_alts = [
        v.quant
        for v in variants
        if any(
            _try_fit_variant(
                v,
                vram_mib,
                c,
                max_ctx,
                mmproj_bytes,
                kv_bytes_per_token,
                forced_kv,
                sliding_window=sliding_window,
            )
            for c in ladder
        )
    ]
    return {
        "fits": False,
        "vram_mib": vram_mib,
        "reason": f"{target.quant} exceeds {vram_mib} MiB VRAM",
        "fitting_quants": fitting_alts,
    }


def _hardware_matrix_targets() -> list[tuple[str, str, int]]:
    ordered_accels = list(_HARDWARE_MATRIX_ORDER) + [
        accel for accel in ACCELERATOR_VRAM_REFERENCE_MIB if accel not in _HARDWARE_MATRIX_ORDER
    ]
    targets: list[tuple[str, str, int]] = []
    for accel in ordered_accels:
        for vram in ACCELERATOR_VRAM_REFERENCE_MIB.get(accel, []):
            gb = _VRAM_GB_LABEL_OVERRIDES.get(vram, int(round(vram / 1024)))
            targets.append((f"{accel} ({gb} GB)", accel, vram))
    return targets


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

    rows: list[dict[str, Any]] = []
    for label, accel, vram in _hardware_matrix_targets():
        fit = fit_gguf_for_vram(
            plan.variants,
            vram_mib=vram,
            quant=quant,
            requested_ctx=requested_ctx,
            model_ctx=plan.model_ctx,
            kv_bytes_per_token=plan.kv_bytes_per_token_f16,
            mmproj_bytes=mm_bytes,
            kv_type=kv_type,
            sliding_window=plan.sliding_window,
        )
        row: dict[str, Any] = {
            "gpu_label": label,
            "accelerator": accel,
            **fit,
        }
        if plan.header_truncated:
            row["header_truncated"] = True
        if plan.header_warning:
            row["header_warning"] = plan.header_warning
        rows.append(row)
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
        sliding_window: Optional[int] = None
        header_truncated = False
        header_warning: Optional[str] = None
        if variants:
            smallest_file = variants[-1].files[0]
            header_url = f"https://huggingface.co/{repo_id}/resolve/main/{quote(smallest_file)}"
            range_headers = {"Range": f"bytes=0-{HEADER_PROBE_BYTES - 1}", **headers}
            try:
                h_resp = await client.get(header_url, headers=range_headers, timeout=12.0)
                if h_resp.status_code in (200, 206):
                    raw_prefix = h_resp.content[:HEADER_PROBE_BYTES]
                    meta, truncated = parse_gguf_header_with_status(raw_prefix)
                    arch, kv_per_tok, model_ctx, has_mtp = extract_kv_architecture_params(meta)
                    sliding_window = extract_sliding_window(meta)
                    if truncated:
                        header_truncated = True
                        header_warning = (
                            "GGUF metadata header was truncated at the 4 MiB probe limit; "
                            "missing architecture/KV fields fall back to conservative defaults."
                        )
                else:
                    header_warning = (
                        f"GGUF header probe returned HTTP {h_resp.status_code}; "
                        "using conservative default KV/context estimates."
                    )
            except Exception as exc:
                header_warning = (
                    f"GGUF header probe failed ({type(exc).__name__}); "
                    "using conservative default KV/context estimates."
                )

    return GgufRepoPlan(
        repo=repo_id,
        variants=variants,
        mmproj_files=mmproj,
        preferred_mmproj=preferred_mm,
        architecture=arch,
        model_ctx=model_ctx,
        kv_bytes_per_token_f16=kv_per_tok,
        has_mtp=has_mtp,
        sliding_window=sliding_window,
        header_truncated=header_truncated,
        header_warning=header_warning,
    )
