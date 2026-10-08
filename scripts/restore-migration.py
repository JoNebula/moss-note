#!/usr/bin/env python3
"""Restore a verified snapshot into a new project-local Thor deployment."""
from __future__ import annotations

import argparse
import configparser
from contextlib import closing
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import sqlite3
import subprocess

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]


def verify_snapshot(snapshot: Path) -> dict:
    manifest = json.loads((snapshot / "manifest.json").read_text())
    source = (snapshot / "data").resolve()
    for name, expected in manifest["sha256"].items():
        path = (source / name).resolve()
        if not path.is_relative_to(source):
            raise ValueError("Snapshot path escapes its data directory")
        with path.open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != expected:
                raise ValueError("Snapshot checksum mismatch")
    return manifest


def restore_data(snapshot: Path, destination: Path) -> dict:
    if destination.exists():
        raise FileExistsError("Never overwrite an existing data directory")
    manifest = verify_snapshot(snapshot)
    old_root = Path(manifest["restore_data_dir"])
    destination = destination.resolve()
    shutil.copytree(snapshot / "data", destination)
    with closing(sqlite3.connect(destination / "moss-note.sqlite3")) as connection, connection:
        for note_id, source, normalized, artifacts in connection.execute(
            "SELECT id, source_path, normalized_path, correction_artifacts_path FROM notes"
        ).fetchall():
            paths = []
            for value in (source, normalized, artifacts):
                if value is None:
                    paths.append(None)
                    continue
                relative = Path(value).resolve().relative_to(old_root.resolve())
                target = (destination / relative).resolve()
                if not target.is_relative_to(destination) or not target.exists():
                    raise ValueError("Restored file is missing or outside the data directory")
                paths.append(str(target))
            connection.execute(
                "UPDATE notes SET source_path=?, normalized_path=?, correction_artifacts_path=? WHERE id=?",
                (*paths, note_id)
            )
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Restored SQLite integrity check failed")
        count = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        if count != manifest["note_count"]:
            raise ValueError("Restored note count differs from snapshot")
    return {"note_count": count, "data_dir": str(destination), "source_data_dir": str(old_root)}


def configure(root: Path, data: Path, source_env: Path, fan: bool) -> None:
    root = root.resolve()
    if (root / ".env").exists():
        raise FileExistsError("Refusing to overwrite deployment configuration")
    values = {key: value for key, value in dotenv_values(source_env).items() if value is not None}
    if not values.get("MOSS_AUTH_USERNAME") or not values.get("MOSS_AUTH_PASSWORD"):
        raise ValueError("Both original authentication settings are required")
    catalogue = json.loads((root / "deploy/model-variants.json").read_text())
    for entry in catalogue.values():
        relative = Path(entry["path"]).relative_to("/data/models")
        entry["path"] = str(root / "models" / relative)
    catalogue_file = root / "deploy/model-variants.local.json"
    catalogue_file.write_text(json.dumps(catalogue, indent=2) + "\n")
    caches = root / "caches"
    values.update({
        "MOSS_DATA_DIR": str(data), "MOSS_MODELS_DIR": str(root / "models"),
        "MOSS_MODEL_PATH": catalogue["bf16"]["path"],
        "MOSS_MODEL_VARIANTS_FILE": str(catalogue_file),
        "MOSS_MODEL_SELECTION_FILE": str(data / "model-selection.json"),
        "MOSS_CACHE_DIR": str(caches / "moss-note"),
        "MOSS_REQUIRE_QWEN": "false", "MOSS_MANAGED_MODELS": "true",
        "MOSS_MAX_FAN_DURING_TRANSCRIPTION": str(fan).lower(),
        "MOSS_CODEX_BINARY": shutil.which("codex") or "/usr/local/bin/codex",
        "HF_HOME": str(caches / "huggingface"), "TORCH_HOME": str(caches / "torch"),
        "PIP_CACHE_DIR": str(caches / "pip"), "UV_CACHE_DIR": str(caches / "uv"),
        "UV_PYTHON_INSTALL_DIR": str(caches / "uv-python"),
        "TRITON_CACHE_DIR": str(caches / "triton"), "CUDA_CACHE_PATH": str(caches / "cuda"),
        "TRITON_PTXAS_BLACKWELL_PATH": str(
            root / ".venv-vllm/lib/python3.12/site-packages/nvidia/cu13/bin/ptxas"
        ),
        "FLASHINFER_WORKSPACE_BASE": str(caches / "flashinfer"),
        "XDG_CACHE_HOME": str(caches), "TMPDIR": str(caches / "moss-note/tmp"),
    })
    with (root / ".env").open("x") as file:
        os.chmod(file.name, 0o600)
        file.write("\n".join(f"{key}={shlex.quote(value)}" for key, value in sorted(values.items())) + "\n")
    local = root / "deploy/local"
    local.mkdir(mode=0o700, exist_ok=False)
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    for name in ("app", "model", "tunnel", "fan"):
        filename = f"moss-note-{name}.service"
        unit = configparser.ConfigParser(interpolation=None)
        unit.optionxform = str
        unit.read(root / "deploy" / filename)
        for section in unit.sections():
            for key, value in unit[section].items():
                unit[section][key] = value.replace("/data/projects/jetson/moss_stt", str(root))
        if name != "fan":
            unit["Service"]["User"] = user
            unit["Service"]["Group"] = group
            unit["Unit"]["RequiresMountsFor"] = str(root)
        if name == "tunnel":
            unit["Service"]["ExecStart"] = unit["Service"]["ExecStart"].replace(
                "/usr/bin/cloudflared", str(root / ".tools/cloudflared")
            )
        with (local / filename).open("x") as file:
            unit.write(file, space_around_delimiters=False)
    (local / "moss-note.sudoers").write_text(
        f"{user} ALL=(root) NOPASSWD: /usr/bin/systemctl restart moss-note-model, "
        "/usr/bin/systemctl start moss-note-fan.service, /usr/bin/systemctl stop moss-note-fan.service\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--source-env", type=Path, required=True)
    parser.add_argument("--enable-fan", action="store_true")
    args = parser.parse_args()
    mount = json.loads(subprocess.check_output(
        ["findmnt", "--json", "--target", str(ROOT), "--output", "SOURCE"], text=True
    ))
    if not mount["filesystems"][0]["source"].startswith("/dev/nvme"):
        parser.error("Project-local Thor storage must be on NVMe")
    if shutil.disk_usage(ROOT).free / shutil.disk_usage(ROOT).total < 0.10:
        parser.error("NVMe has less than 10% free space")
    if (ROOT / ".env").exists():
        parser.error("Deployment is already configured; preserve it and restore a new version explicitly")
    data = ROOT / "data" / args.snapshot.name
    restored = restore_data(args.snapshot, data)
    configure(ROOT, data, args.source_env, args.enable_fan)
    (args.snapshot / "restored.json").write_text(json.dumps(restored, indent=2) + "\n")
    print(f"Restored {restored['note_count']} notes to {data}; generated private local service configuration")


if __name__ == "__main__":
    main()
