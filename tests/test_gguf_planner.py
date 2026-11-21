"""Unit & boundary tests for GGUF header parsing and GPU VRAM quantization fitting."""

from __future__ import annotations

import struct
import pytest

from colab_boost.gguf_planner import (
    GgufRepoPlan,
    build_hardware_fit_matrix,
    estimate_vram_mib,
    extract_kv_architecture_params,
    extract_quant_label,
    extract_sliding_window,
    fit_gguf_for_vram,
    group_gguf_files,
    parse_gguf_header_bytes,
    parse_gguf_header_with_status,
    validate_hf_repo_id,
)
from colab_boost.models import ACCELERATOR_VRAM_REFERENCE_MIB

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


def test_underscore_quant_names_and_subdir_disambiguation() -> None:
    assert extract_quant_label("model_Q4_K_M.gguf") == "Q4_K_M"
    assert extract_quant_label("model_UD_Q4_K_XL.gguf") == "UD-Q4_K_XL"
    assert extract_quant_label("qwen2_5_7b_bf16.gguf") == "BF16"
    assert extract_quant_label("model_Q4_K_M_00001_of_00002.gguf") == "Q4_K_M"
    assert extract_quant_label("model_IQ4_XS.gguf") == "IQ4_XS"

    # Same quant label in two distinct subdirectories must NOT merge and double size
    variants, _ = group_gguf_files([
        ("v1/model_Q4_K_M.gguf", 8 * GIB),
        ("v2/model_Q4_K_M.gguf", 8 * GIB),
        ("Q8_0/shard-Q8_0-00001-of-00002.gguf", 10 * GIB),
        ("Q8_0/shard-Q8_0-00002-of-00002.gguf", 10 * GIB),
    ])
    by_q = {v.quant: v for v in variants}
    assert set(by_q.keys()) == {"Q8_0", "v1/Q4_K_M", "v2/Q4_K_M"}
    assert by_q["v1/Q4_K_M"].size_bytes == 8 * GIB
    assert by_q["v2/Q4_K_M"].size_bytes == 8 * GIB
    assert by_q["Q8_0"].size_bytes == 20 * GIB


def test_hardware_fit_matrix_includes_g4_and_distinct_h100() -> None:
    variants, _ = group_gguf_files([
        ("Model-Q8_0.gguf", 30 * GIB),
        ("Model-Q4_K_M.gguf", 16 * GIB),
    ])
    plan = GgufRepoPlan(
        repo="org/model-gguf",
        variants=variants,
        mmproj_files=[],
        preferred_mmproj=None,
        architecture="qwen3",
        model_ctx=32768,
        kv_bytes_per_token_f16=65536,
        has_mtp=False,
        header_truncated=True,
        header_warning="Header truncated at 4 MiB; conservative defaults used.",
    )
    rows = build_hardware_fit_matrix(plan)
    labels = [r["gpu_label"] for r in rows]
    assert "T4 (15 GB)" in labels
    assert "L4 (23 GB)" in labels
    assert "A100 (40 GB)" in labels
    assert "A100 (80 GB)" in labels
    assert "H100 (80 GB)" in labels
    assert "G4 (96 GB)" in labels
    assert not any("A100 / H100" in lbl for lbl in labels)

    g4_row = next(r for r in rows if r["accelerator"] == "G4")
    h100_row = next(r for r in rows if r["accelerator"] == "H100")
    assert g4_row["vram_mib"] == ACCELERATOR_VRAM_REFERENCE_MIB["G4"][0]
    assert h100_row["vram_mib"] == ACCELERATOR_VRAM_REFERENCE_MIB["H100"][0]
    assert g4_row["header_truncated"] is True
    assert "truncated" in g4_row["header_warning"].lower()


def test_nested_array_gguf_header_parsing_preserves_offset() -> None:
    # Build a nested array (elem_type == 9) containing 2 inner arrays of uint32 ([10, 20], [30, 40, 50])
    inner1 = struct.pack("<IQ", 4, 2) + struct.pack("<II", 10, 20)
    inner2 = struct.pack("<IQ", 4, 3) + struct.pack("<III", 30, 40, 50)
    nested_payload = struct.pack("<IQ", 9, 2) + inner1 + inner2

    blob = _build_test_gguf_v3([
        ("custom.nested", 9, nested_payload),
        ("general.architecture", 8, _pack_gguf_str("llama")),
        ("llama.context_length", 4, struct.pack("<I", 131072)),
        ("llama.block_count", 4, struct.pack("<I", 32)),
        ("llama.attention.head_count", 4, struct.pack("<I", 32)),
        ("llama.attention.head_count_kv", 4, struct.pack("<I", 8)),
        ("llama.attention.key_length", 4, struct.pack("<I", 128)),
        ("llama.attention.value_length", 4, struct.pack("<I", 128)),
    ])
    meta, truncated = parse_gguf_header_with_status(blob)
    assert truncated is False
    assert meta["custom.nested"] == [[10, 20], [30, 40, 50]]
    arch, kv_bpt, ctx, has_mtp = extract_kv_architecture_params(meta)
    assert arch == "llama"
    assert ctx == 131072
    assert has_mtp is False
    assert kv_bpt == 32 * 8 * (128 + 128) * 2

    # Truncated buffer sets truncated=True
    _partial, was_trunc = parse_gguf_header_with_status(blob[:60])
    assert was_trunc is True


def test_deepseek_mla_sliding_window_interval_floor_compute_scaling_and_t4_4096() -> None:
    # 1. DeepSeek MLA uses kv_lora_rank (+ rope dim) instead of heads * (klen + vlen)
    mla_meta = {
        "general.architecture": "deepseek2",
        "deepseek2.context_length": 163840,
        "deepseek2.block_count": 61,
        "deepseek2.attention.head_count": 128,
        "deepseek2.attention.head_count_kv": 128,
        "deepseek2.attention.key_length": 192,
        "deepseek2.attention.value_length": 128,
        "deepseek2.attention.kv_lora_rank": 512,
        "deepseek2.rope.dimension_count": 64,
    }
    arch, mla_kv_bpt, mla_ctx, _ = extract_kv_architecture_params(mla_meta)
    assert arch == "deepseek2"
    assert mla_ctx == 163840
    assert mla_kv_bpt == 61 * (512 + 64) * 2  # 70,272 instead of ~5,000,000!
    assert mla_kv_bpt < 100_000

    # 2. blocks // interval == 0 must floor to 1 full layer instead of 0
    deg_meta = {
        "general.architecture": "qwen35",
        "qwen35.block_count": 2,
        "qwen35.full_attention_interval": 4,
        "qwen35.attention.head_count": 8,
        "qwen35.attention.head_count_kv": 2,
        "qwen35.attention.key_length": 128,
        "qwen35.attention.value_length": 128,
    }
    _, deg_kv_bpt, _, _ = extract_kv_architecture_params(deg_meta)
    assert deg_kv_bpt == 1 * 2 * 256 * 2

    # 3. Sliding window caps KV cache tokens at min(ctx, sliding_window)
    swa_meta = {"general.architecture": "mistral", "mistral.attention.sliding_window": 4096}
    assert extract_sliding_window(swa_meta) == 4096
    est_no_swa = estimate_vram_mib(8 * GIB, 0, 131072, 65536, "f16", sliding_window=None)
    est_with_swa = estimate_vram_mib(8 * GIB, 0, 131072, 65536, "f16", sliding_window=4096)
    assert est_with_swa < est_no_swa - 5000

    # 4. Compute buffer scales with context length even when kv_bytes_per_token is 0
    est_8k = estimate_vram_mib(8 * GIB, 0, 0, 8192, "f16")
    est_128k = estimate_vram_mib(8 * GIB, 0, 0, 131072, "f16")
    assert est_128k > est_8k

    # 5. T4 (15360 MiB) can step down auto ctx to 4096 when 8192 exceeds budget
    t4_variants, _ = group_gguf_files([("Tight-Q4_K_M.gguf", int(11.3 * GIB))])
    fit_t4 = fit_gguf_for_vram(
        t4_variants,
        vram_mib=15360,
        quant="auto",
        requested_ctx=0,
        model_ctx=32768,
        kv_bytes_per_token=262144,
        kv_type="f16",
    )
    assert fit_t4["fits"] is True
    assert fit_t4["ctx"] == 4096
    assert fit_t4["ctx_reduced"] is True


async def test_inspect_hf_gguf_repo_records_header_truncation_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    from colab_boost import gguf_planner

    # Build a truncated GGUF prefix (declares 2 KVs but cuts off inside the 2nd KV)
    full_blob = _build_test_gguf_v3([
        ("general.architecture", 8, _pack_gguf_str("llama")),
        ("llama.context_length", 4, struct.pack("<I", 65536)),
    ])
    truncated_blob = full_blob[:-2]

    class _FakeResp:
        def __init__(self, status_code: int, json_data=None, content: bytes = b"") -> None:
            self.status_code = status_code
            self._json = json_data
            self.content = content

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._json

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url: str, **kwargs):
            if "/tree/main" in url:
                return _FakeResp(
                    200,
                    json_data=[
                        {"type": "file", "path": "model_Q4_K_M.gguf", "lfs": {"size": 4 * GIB}},
                    ],
                )
            return _FakeResp(206, content=truncated_blob)

    monkeypatch.setattr(gguf_planner.httpx, "AsyncClient", _FakeClient)
    plan = await gguf_planner.inspect_hf_gguf_repo("owner/truncated-gguf")
    assert plan.architecture == "llama"
    assert plan.header_truncated is True
    assert plan.header_warning is not None
    assert "4 MiB" in plan.header_warning


