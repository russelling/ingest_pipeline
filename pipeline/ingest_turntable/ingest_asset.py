"""
ingest_asset.py -- turns one delivery (a file or folder dropped inside an
asset-type folder in the watch folder) into a ShotGrid Asset with published,
pipeline-standard USD, plus a turntable render handed off to the existing
qt_watcher for baking/upload (see generate_turntable.py and README.md
"qt_watcher integration").

Asset typing: the caller (watch_folder.py, or --asset-type on the CLI)
supplies sg_asset_type explicitly -- it comes from which standing type
folder (Prop/, Character/, etc.) the delivery was dropped into. This module
does not guess a type. A delivery's manifest.yml MAY still override it
explicitly with `asset_type:` if a one-off delivery genuinely needs to
differ from its folder (documented as an escape hatch, not the default
path).

Delivery layouts supported:

1. A single 3D file, e.g. `incoming/Prop/HeroChair.fbx`
   -> asset name = "HeroChair", type = "Prop" (from the folder), ext = fbx

2. A folder, optionally with a manifest.yml for explicit naming/overrides:
   incoming/Prop/HeroChair_delivery/
     manifest.yml   (optional)
     HeroChair.fbx
     textures/...
   manifest.yml:
     asset_name: HeroChair
     asset_type: Prop        # optional override of the folder-derived type
     source_file: HeroChair.fbx   # relative to the folder, optional if only
                                    # one accepted-extension file is present
     artist: "vendor_xyz"    # optional, else config.yml qt_watcher.default_artist

3. A folder with no manifest -- asset name is inferred from the folder name,
   type comes from the standing folder it's inside.

Can be run directly for a one-off ingest:
    python ingest_asset.py --path /mnt/projects/incoming/Prop/HeroChair.fbx \
        --asset-type Prop --project-id 123
or imported and called by watch_folder.py for continuous ingestion.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

import sg_utils
import convert_to_usd
import generate_turntable

log = logging.getLogger("ingest_turntable.ingest_asset")


@dataclass
class DeliveryInfo:
    asset_name: str
    asset_type: str
    source_path: Path       # the actual 3D file to convert
    delivery_root: Path     # the file or folder as it appeared in the watch folder
    ingest_extension: str
    artist: Optional[str]


class IngestError(Exception):
    pass


def _find_source_file(folder: Path, accepted_exts: list[str], explicit_name: Optional[str]) -> Path:
    if explicit_name:
        candidate = folder / explicit_name
        if not candidate.exists():
            raise IngestError(f"manifest.yml source_file '{explicit_name}' not found in {folder}")
        return candidate

    candidates = [
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower().lstrip(".") in accepted_exts
    ]
    if not candidates:
        raise IngestError(f"No file with an accepted extension {accepted_exts} found in {folder}")
    if len(candidates) > 1:
        # Prefer USD > Alembic > FBX > OBJ > glTF, since that ordering loses
        # the least fidelity if a vendor bundled more than one format.
        preference = {"usd": 0, "usdc": 0, "usda": 0, "usdz": 0, "abc": 1, "fbx": 2, "obj": 3, "gltf": 4, "glb": 4}
        candidates.sort(key=lambda p: preference.get(p.suffix.lower().lstrip("."), 99))
        log.warning(
            "Multiple ingestible files found in %s, picking %s (found: %s)",
            folder, candidates[0].name, [c.name for c in candidates],
        )
    return candidates[0]


def parse_delivery(path: Path, folder_asset_type: str, config: dict) -> DeliveryInfo:
    accepted_exts = config["ingest"]["accepted_extensions"]

    if path.is_file():
        ext = path.suffix.lower().lstrip(".")
        if ext not in accepted_exts:
            raise IngestError(f"{path} has unsupported extension .{ext}")
        return DeliveryInfo(
            asset_name=path.stem,
            asset_type=folder_asset_type,
            source_path=path,
            delivery_root=path,
            ingest_extension=ext,
            artist=None,
        )

    if path.is_dir():
        manifest_path = path / "manifest.yml"
        if manifest_path.exists():
            with open(manifest_path) as f:
                manifest = yaml.safe_load(f) or {}
            asset_name = manifest.get("asset_name") or path.name
            # The standing folder is authoritative; manifest.yml is an
            # explicit, logged escape hatch for the rare delivery that needs
            # to override it.
            asset_type = manifest.get("asset_type")
            if asset_type and asset_type != folder_asset_type:
                log.warning(
                    "manifest.yml overrides asset_type for '%s': folder says "
                    "'%s', manifest says '%s' -- using manifest value",
                    asset_name, folder_asset_type, asset_type,
                )
            asset_type = asset_type or folder_asset_type
            source_file = _find_source_file(path, accepted_exts, manifest.get("source_file"))
            return DeliveryInfo(
                asset_name=asset_name,
                asset_type=asset_type,
                source_path=source_file,
                delivery_root=path,
                ingest_extension=source_file.suffix.lower().lstrip("."),
                artist=manifest.get("artist"),
            )

        # No manifest -- name from the folder, type from the standing folder.
        source_file = _find_source_file(path, accepted_exts, None)
        return DeliveryInfo(
            asset_name=path.name,
            asset_type=folder_asset_type,
            source_path=source_file,
            delivery_root=path,
            ingest_extension=source_file.suffix.lower().lstrip("."),
            artist=None,
        )

    raise IngestError(f"{path} does not exist")


def ingest_delivery(path: Path, asset_type: str, config: dict, tk=None, sg=None) -> dict:
    """Full pipeline for one delivery: parse -> create/find Asset -> create
    folders -> copy source -> convert to USD -> publish -> render turntable
    frames and hand off to qt_watcher. `asset_type` comes from the standing
    incoming/<Type>/ folder the delivery was found in. Returns a summary
    dict. Raises IngestError / subprocess errors on failure -- caller
    (watch_folder.py) is responsible for routing the delivery to _failed/
    and logging.

    `tk`/`sg` are normally passed in already-bootstrapped from
    watch_folder.py's single startup call to sg_utils.get_sgtk() -- pass
    neither (both None) to have this call bootstrap its own for a one-off
    run (see _cli() below)."""
    info = parse_delivery(path, asset_type, config)
    project_id = config["shotgrid"]["project_id"]
    if project_id is None:
        raise IngestError("shotgrid.project_id is not set in config.yml")

    if tk is None or sg is None:
        tk, sg = sg_utils.get_sgtk(config)

    asset = sg_utils.find_or_create_asset(sg, project_id, info.asset_name, info.asset_type)
    asset["project"] = {"type": "Project", "id": project_id}

    tk.create_filesystem_structure("Asset", asset["id"])

    ingest_task = sg_utils.find_or_create_task(sg, asset, project_id, config["ingest"]["step_code"])

    source_fields = {
        "Asset": asset["code"],
        "sg_asset_type": asset["sg_asset_type"],
        "original_basename": info.source_path.stem,
        "ingest_extension": info.ingest_extension,
    }
    source_dest = Path(tk.templates["asset_ingest_source_file"].apply_fields(source_fields))
    source_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(info.source_path, source_dest)
    log.info("Copied delivery source %s -> %s", info.source_path, source_dest)

    if info.delivery_root.is_dir():
        # Preserve the rest of the delivery (textures, etc.) alongside the
        # copied source file for provenance.
        extras_dir = source_dest.parent / "delivery_contents"
        shutil.copytree(info.delivery_root, extras_dir, dirs_exist_ok=True)

    vendor_delivery_type = sg_utils.find_or_create_published_file_type(sg, "Vendor Delivery")
    sg.create(
        "PublishedFile",
        {
            "project": {"type": "Project", "id": project_id},
            "code": source_dest.name,
            "entity": {"type": "Asset", "id": asset["id"]},
            "task": {"type": "Task", "id": ingest_task["id"]} if ingest_task else None,
            "version_number": 1,
            "path": {"local_path": str(source_dest)},
            "published_file_type": {"type": "PublishedFileType", "id": vendor_delivery_type["id"]},
        },
    )

    usd_version = sg_utils.next_version_number(sg, asset, "USD Asset")
    usd_fields = {"Asset": asset["code"], "sg_asset_type": asset["sg_asset_type"], "version": usd_version}
    usd_dest = Path(tk.templates["asset_usd_publish_file"].apply_fields(usd_fields))

    blender_exe = sg_utils.get_executable(config, "blender")
    convert_to_usd.convert_to_usd(
        source_dest, usd_dest, blender_exe,
        max_import_config=config["ingest"].get("max_import"),
    )

    usd_asset_type = sg_utils.find_or_create_published_file_type(sg, "USD Asset")
    sg.create(
        "PublishedFile",
        {
            "project": {"type": "Project", "id": project_id},
            "code": usd_dest.name,
            "entity": {"type": "Asset", "id": asset["id"]},
            "task": {"type": "Task", "id": ingest_task["id"]} if ingest_task else None,
            "version_number": usd_version,
            "path": {"local_path": str(usd_dest)},
            "published_file_type": {"type": "PublishedFileType", "id": usd_asset_type["id"]},
        },
    )

    turntable_task = sg_utils.find_or_create_task(sg, asset, project_id, config["turntable"]["step_code"])
    artist = info.artist or config["qt_watcher"]["default_artist"]

    flag_path = generate_turntable.render_and_flag(
        usd_path=usd_dest,
        asset=asset,
        project_id=project_id,
        task=turntable_task,
        artist=artist,
        config=config,
        tk=tk,
        sg=sg,
    )

    return {
        "asset": asset,
        "source_path": source_dest,
        "usd_path": usd_dest,
        "flag_path": flag_path,
    }


def _cli():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Ingest a single delivery, outside the watch-folder loop.")
    parser.add_argument("--path", required=True, help="File or folder to ingest")
    parser.add_argument(
        "--asset-type", required=False,
        help="sg_asset_type for this delivery. If omitted, inferred from the "
             "immediate parent folder name (must be one of config.yml "
             "ingest.asset_type_folders).",
    )
    parser.add_argument("--project-id", type=int, help="Overrides shotgrid.project_id from config.yml")
    args = parser.parse_args()

    config = sg_utils.load_config()
    if args.project_id:
        config["shotgrid"]["project_id"] = args.project_id

    path = Path(args.path)
    asset_type = args.asset_type or path.parent.name
    if asset_type not in config["ingest"]["asset_type_folders"]:
        log.error(
            "asset_type '%s' is not in config.yml ingest.asset_type_folders %s "
            "-- pass --asset-type explicitly",
            asset_type, config["ingest"]["asset_type_folders"],
        )
        sys.exit(1)

    try:
        result = ingest_delivery(path, asset_type, config)
    except Exception:
        log.exception("Ingest failed for %s", args.path)
        sys.exit(1)

    log.info("Ingest complete: %s", result)


if __name__ == "__main__":
    _cli()
