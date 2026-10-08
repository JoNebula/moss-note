#!/usr/bin/env python3
"""Create a decoder-only RTN checkpoint using compressed-tensors' INT packer."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil

import torch
from compressed_tensors.compressors.pack_quantized import PackedQuantizationCompressor
from compressed_tensors.quantization import QuantizationConfig, preset_name_to_scheme
from compressed_tensors.quantization.utils import calculate_qparams
from safetensors.torch import load_file, save_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bits", type=int, choices=(4, 8), required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new version directory")
    torch.set_num_threads(6)
    scheme = preset_name_to_scheme(f"W{args.bits}A16", targets=["Linear"])
    scheme.weights.group_size = 128
    scheme.format = "pack-quantized"
    tensors = {}
    for shard in sorted(args.source.glob("*.safetensors")):
        tensors.update(load_file(str(shard), device="cpu"))
    originals = set(tensors)
    converted = []
    # Keep the audio encoder, adaptor, embeddings and tied output head untouched.
    for name in sorted(originals):
        weight = tensors[name]
        if not name.startswith("model.language_model.layers.") or not name.endswith("_proj.weight"):
            continue
        assert weight.ndim == 2 and weight.shape[1] % 128 == 0, name
        groups = weight.float().reshape(weight.shape[0], -1, 128)
        scale, _ = calculate_qparams(groups.amin(-1), groups.amax(-1), scheme.weights)
        packed = PackedQuantizationCompressor.compress(
            {"weight": weight, "weight_scale": scale.to(weight.dtype)}, scheme
        )
        restored = PackedQuantizationCompressor.decompress(packed, scheme)["weight"]
        relative_error = ((weight.float() - restored.float()).norm() / weight.float().norm()).item()
        del tensors[name]
        prefix = name.removesuffix("weight")
        tensors.update({prefix + key: value.contiguous() for key, value in packed.items()})
        converted.append({"name": name, "relative_l2_error": relative_error})
    assert len(converted) == 28 * 7, f"Unexpected decoder linear count: {len(converted)}"
    args.output.mkdir(parents=True)
    for path in args.source.iterdir():
        if path.is_file() and path.name not in {"config.json", "README.md", "manifest.json", "model.safetensors.index.json"} and path.suffix != ".safetensors":
            shutil.copy2(path, args.output / path.name)
    filename = "model.safetensors"
    save_file(tensors, str(args.output / filename), metadata={"format": "pt"})
    (args.output / filename).chmod(0o664)
    config = json.loads((args.source / "config.json").read_text())
    quant = QuantizationConfig(
        config_groups={"decoder": scheme}, format="pack-quantized",
        quantization_status="compressed",
        ignore=["lm_head", "language_model.lm_head", "re:.*whisper_encoder.*", "re:.*vq_adaptor.*", "re:.*embed_tokens.*"],
    ).model_dump(mode="json")
    quant["compression_version"] = importlib.metadata.version("compressed-tensors")
    config["quantization_config"] = quant
    (args.output / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (args.output / "model.safetensors.index.json").write_text(json.dumps({
        "metadata": {"total_size": sum(t.numel() * t.element_size() for t in tensors.values())},
        "weight_map": {name: filename for name in sorted(tensors)},
    }, indent=2) + "\n")
    checksums = {p.name: hashlib.file_digest(p.open("rb"), "sha256").hexdigest() for p in args.output.iterdir() if p.is_file()}
    manifest = {
        "source": "https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize",
        "revision": "704aa4a9c304e8520be88901e0d1960158ef5b15",
        "source_path": str(args.source), "license": "Apache-2.0",
        "algorithm": "RTN symmetric minmax, decoder-only, group_size=128",
        "format": "safetensors/compressed-tensors pack-quantized",
        "weights": f"INT{args.bits} decoder linear; BF16 audio encoder/adaptor/embedding/head",
        "activations": "BF16", "quantization": quant,
        "tool": "scripts/quantize-rtn.py", "torch": torch.__version__,
        "converted_layers": converted, "sha256": checksums,
        "validation": "Packing roundtrip checked; runtime and transcription accuracy require benchmark",
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.output / "README.md").write_text(
        f"# MOSS decoder RTN W{args.bits}A16\n\n"
        "Derived from OpenMOSS-Team/MOSS-Transcribe-Diarize at "
        "704aa4a9c304e8520be88901e0d1960158ef5b15 (Apache-2.0).\n\n"
        "Symmetric INT weight-only RTN, group size 128. Audio encoder, adaptor, "
        "embedding and output head remain BF16. Safetensors compressed-tensors format.\n\n"
        "See manifest.json for checksums, precision, versions and conversion errors. "
        "Run with the project's pinned vLLM; use benchmark-model.py before deployment.\n"
    )
    print(f"Converted {len(converted)} decoder linears: {args.output}", flush=True)


if __name__ == "__main__":
    main()
