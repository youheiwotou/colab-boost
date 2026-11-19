"""Unit & boundary tests for GGUF header parsing and GPU VRAM quantization fitting."""

from __future__ import annotations

import struct
import pytest

from colab_boost.gguf_planner import (
    extract_kv_architecture_params,
    extract_quant_label,
    fit_gguf_for_vram,
    group_gguf_files,
    parse_gguf_header_bytes,
    validate_hf_repo_id,
)

GIB = 1024**3


def _pack_gguf_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def _build_test_gguf_v3(kv_pairs: list[tuple[str, int, bytes]]) -> bytes:
    out = bytearray()
    out += b"GGUF"
    out += struct.pack("<I", 3)  # version 3
    out += struct.pack("<Q", 128)  # tensor count
    out += struct.pack("<Q", len(kv_pairs))
    for key, vtype, raw_val in kv_pairs:
        out += _pack_gguf_str(key)
        out += struct.pack("<I", vtype)
        out += raw_val
    return bytes(out)


def test_quant_labels_and_shards() -> None:
    assert extract_quant_label("Qwen2.5-7B-Instruct-Q4_K_M.gguf") == "Q4_K_M"
    assert extract_quant_label("model.Q8_0.gguf") == "Q8_0"
    assert extract_quant_label("sub/Model-UD-Q4_K_XL.gguf") == "UD-Q4_K_XL"
    assert extract_quant_label("Q8_0/Big-Q8_0-00002-of-00003.gguf") == "Q8_0"
    assert extract_quant_label("m-bf16.gguf") == "BF16"
    assert extract_quant_label("m-IQ4_XS.gguf") == "IQ4_XS"
    assert extract_quant_label("m-MXFP4_MOE.gguf") == "MXFP4_MOE"
    assert extract_quant_label("README.md") is None
    assert extract_quant_label("plain-weights.gguf") is None


def test_group_gguf_shards_and_mmproj_preference() -> None:
    variants, mmproj = group_gguf_files([
        ("Q8_0/M-Q8_0-00002-of-00002.gguf", 7 * GIB),
        ("Q8_0/M-Q8_0-00001-of-00002.gguf", 20 * GIB),
        ("M-Q4_K_M.gguf", 15 * GIB),
        ("mmproj-M-bf16.gguf", 1 * GIB),
        ("mmproj-M-f16.gguf", 1 * GIB),
        ("config.json", 512),
    ])
    assert len(variants) == 2
    assert variants[0].quant == "Q8_0"
    assert variants[0].size_bytes == 27 * GIB
    assert variants[0].files[0].endswith("00001-of-00002.gguf")
    # f16 mmproj must sort ahead of bf16 for T4 compatibility
    assert mmproj[0][0] == "mmproj-M-f16.gguf"


def test_binary_gguf_header_parser_and_hybrid_attention() -> None:
    # Simulate Qwen3.5-style hybrid linear/full attention (64 blocks, interval=4 -> 16 full layers)
    blob = _build_test_gguf_v3([
        ("general.architecture", 8, _pack_gguf_str("qwen35")),
        ("qwen35.context_length", 4, struct.pack("<I", 262144)),
        ("qwen35.block_count", 4, struct.pack("<I", 64)),
        ("qwen35.attention.head_count", 4, struct.pack("<I", 32)),
        ("qwen35.attention.head_count_kv", 4, struct.pack("<I", 4)),
        ("qwen35.attention.key_length", 4, struct.pack("<I", 256)),
        ("qwen35.attention.value_length", 4, struct.pack("<I", 256)),
        ("qwen35.full_attention_interval", 4, struct.pack("<I", 4)),
        ("qwen35.mtp_num_hidden_layers", 4, struct.pack("<I", 1)),
    ])
    meta = parse_gguf_header_bytes(blob)
    arch, kv_bpt, ctx, has_mtp = extract_kv_architecture_params(meta)
    assert arch == "qwen35"
    assert ctx == 262144
    assert has_mtp is True
    # 16 full layers * 4 kv_heads * (256 + 256) * 2 bytes = 65,536 bytes/token
    assert kv_bpt == 65536

    # Truncated buffer midway must still return parsed prefix without raising
    partial_meta = parse_gguf_header_bytes(blob[:90])
    assert partial_meta.get("general.architecture") == "qwen35"
    assert parse_gguf_header_bytes(b"NOT_GGUF") == {}


def test_vram_auto_and_fixed_fitting_across_gpus() -> None:
    variants, _ = group_gguf_files([
        ("Model-BF16.gguf", int(60 * GIB)),
        ("Model-Q8_0.gguf", int(27.1 * GIB)),
        ("Model-Q6_K.gguf", int(21.5 * GIB)),
        ("Model-Q4_K_M.gguf", int(15.8 * GIB)),
        ("Model-IQ2_XS.gguf", int(8.5 * GIB)),
    ])

    # 80 GB A100 fits Q8_0 at 65k context with f16 KV
    fit_80g = fit_gguf_for_vram(
        variants, vram_mib=81920, quant="auto", requested_ctx=0, model_ctx=65536, kv_bytes_per_token=65536
    )
    assert fit_80g["fits"] is True
    assert fit_80g["quant"] == "Q8_0"  # skips BF16 full precision in auto mode!
    assert fit_80g["kv"] == "f16"

    # 40 GB A100 fits Q8_0 or Q6_K nicely
    fit_40g = fit_gguf_for_vram(
        variants, vram_mib=40960, quant="auto", requested_ctx=0, model_ctx=65536, kv_bytes_per_token=65536
    )
    assert fit_40g["fits"] is True
    assert fit_40g["quant"] in ("Q8_0", "Q6_K")

    # 15 GB T4 cannot fit normal quants, falls back to low-bit IQ2_XS tier
    fit_t4 = fit_gguf_for_vram(
        variants, vram_mib=15360, quant="auto", requested_ctx=0, model_ctx=32768, kv_bytes_per_token=65536
    )
    assert fit_t4["fits"] is True
    assert fit_t4["quant"] == "IQ2_XS"

    # Fixed Q8_0 on 15 GB T4 fails and lists fitting alternatives
    fail_fixed = fit_gguf_for_vram(
        variants, vram_mib=15360, quant="Q8_0", requested_ctx=0, model_ctx=32768, kv_bytes_per_token=65536
    )
    assert fail_fixed["fits"] is False
    assert "IQ2_XS" in fail_fixed["fitting_quants"]


def test_repo_id_validation_rejects_bad_inputs() -> None:
    assert validate_hf_repo_id("  Qwen/Qwen2.5-7B-Instruct-GGUF ") == "Qwen/Qwen2.5-7B-Instruct-GGUF"
    for bad in ["", "just-one-part", "../etc/passwd", "owner/../repo", "a/b/c", "owner/repo;rm -rf"]:
        with pytest.raises(ValueError):
            validate_hf_repo_id(bad)
