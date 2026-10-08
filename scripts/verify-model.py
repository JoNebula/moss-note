#!/usr/bin/env python3
"""Validate the pinned upstream snapshot and record its provenance."""
import hashlib
import json
from pathlib import Path
import urllib.request

revision = "704aa4a9c304e8520be88901e0d1960158ef5b15"
model_id = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
directory = Path("/data/models/MOSS-Transcribe-Diarize/20260902-704aa4a9")
with urllib.request.urlopen(
    f"https://huggingface.co/api/models/{model_id}/revision/{revision}?blobs=true",
    timeout=60,
) as response:
    metadata = json.load(response)
assert metadata["sha"] == revision
checksums = {}
for item in metadata["siblings"]:
    path = directory / item["rfilename"]
    assert path.stat().st_size == item["size"], path
    digest = hashlib.sha256()
    git_digest = hashlib.sha1(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            git_digest.update(block)
    if "lfs" in item:
        assert digest.hexdigest() == item["lfs"]["sha256"], path
    else:
        assert git_digest.hexdigest() == item["blobId"], path
    checksums[item["rfilename"]] = digest.hexdigest()
manifest = {
    "source": f"https://huggingface.co/{model_id}",
    "revision": revision,
    "license": "Apache-2.0",
    "format": "Transformers custom code + safetensors",
    "dtype": "BF16",
    "quantization": None,
    "runtime": "/data/projects/jetson/moss_stt/scripts/run-vllm.sh",
    "sha256": checksums,
}
with (directory / "manifest.json").open("x") as output:
    json.dump(manifest, output, indent=2)
    output.write("\n")
print(f"Verified {len(checksums)} upstream files: {directory}")
