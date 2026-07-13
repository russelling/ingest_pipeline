"""
convert_to_usd.py -- converts an incoming asset source file (FBX / OBJ /
Alembic / glTF / 3ds Max) to pipeline-standard USD.

This module has two lives:

1. Imported by ingest_asset.py in a normal Python process -- call
   `convert_to_usd(source, dest, blender_exe, max_import_config=...)` and it
   shells out to Blender in --background mode to do the actual conversion.

2. Executed *by* Blender itself:
       blender --background --factory-startup --python convert_to_usd.py -- \
           --input <source> --output <dest> \
           [--max-addon-module X --max-import-operator Y --max-filepath-arg Z]
   In this mode `bpy` is importable and the script performs the import/export.
   The --max-* args are only passed (and only required) when importing a
   .max file -- see "3ds Max (.max) source support" below.

Supported input extensions: fbx, obj, abc, gltf, glb, max, usd/usdc/usda/usdz
(the last group is passed through unchanged -- no conversion needed).

## 3ds Max (.max) source support

Blender has no built-in .max importer -- the format is proprietary and
undocumented by Autodesk. This module assumes a third-party Blender add-on
is installed on the machine `blender_exe` points at that can read .max
files, and drives it generically rather than hardcoding one specific
add-on's API:

    config.yml ingest.max_import:
      addon_module:  module name as it appears in Blender's Add-ons list
                     (used with bpy.ops.preferences.addon_enable)
      operator:      dotted bpy.ops path the add-on registers for import,
                     e.g. "import_scene.max"
      filepath_arg:  the operator's file-path kwarg name (almost always
                     "filepath", but not guaranteed)

These three are placeholders in config.yml until you fill in the specific
add-on you're using -- see README.md "3ds Max (.max) source support" for
the full explanation and how to find these three values for your add-on.
`--factory-startup` (used to keep the conversion scene clean/reproducible)
does NOT prevent this from working: it skips auto-loading previously
*enabled* add-ons at startup, but the add-on's files are still on disk and
`addon_enable()` still works to turn it on for this one headless run.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("ingest_turntable.convert_to_usd")

try:
    import bpy  # noqa: F401
    RUNNING_IN_BLENDER = True
except ImportError:
    RUNNING_IN_BLENDER = False


PASSTHROUGH_EXTENSIONS = {"usd", "usdc", "usda", "usdz"}
SUPPORTED_EXTENSIONS = {"fbx", "obj", "abc", "gltf", "glb", "max"} | PASSTHROUGH_EXTENSIONS


# ---------------------------------------------------------------------------
# Driver side (plain Python, called from ingest_asset.py)
# ---------------------------------------------------------------------------
def convert_to_usd(
    source: Path,
    dest: Path,
    blender_exe: str,
    max_import_config: Optional[dict] = None,
) -> Path:
    """Convert `source` (fbx/obj/abc/gltf/glb/max) into a USD file at
    `dest`, by launching Blender headless. `max_import_config` (config.yml
    ingest.max_import) is required only when `source` is a .max file --
    see module docstring "3ds Max (.max) source support". Returns `dest`.
    Raises CalledProcessError on failure -- caller is responsible for
    logging / moving the delivery to the _failed/ folder."""
    ext = source.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported source extension '.{ext}' for {source}")

    dest.parent.mkdir(parents=True, exist_ok=True)

    if ext in PASSTHROUGH_EXTENSIONS:
        # Already USD -- just copy it into the publish location so the rest
        # of the pipeline (turntable render) always reads from the same
        # template-resolved path.
        import shutil
        shutil.copy2(source, dest)
        log.info("Source is already USD, copied %s -> %s", source, dest)
        return dest

    cmd = [
        blender_exe,
        "--background",
        "--factory-startup",
        "--python",
        str(Path(__file__).resolve()),
        "--",
        "--input",
        str(source),
        "--output",
        str(dest),
    ]

    if ext == "max":
        if not max_import_config:
            raise ValueError(
                "Ingesting a .max file requires config.yml's ingest.max_import "
                "to be filled in (addon_module / operator / filepath_arg) -- "
                "see README.md '3ds Max (.max) source support'."
            )
        for key, flag in (
            ("addon_module", "--max-addon-module"),
            ("operator", "--max-import-operator"),
            ("filepath_arg", "--max-filepath-arg"),
        ):
            value = max_import_config.get(key)
            if not value or str(value).startswith("PLACEHOLDER"):
                raise ValueError(
                    f"config.yml ingest.max_import.{key} is not set -- "
                    f"see README.md '3ds Max (.max) source support'."
                )
            cmd += [flag, str(value)]

    log.info("Running Blender USD conversion: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("Blender conversion failed:\nSTDOUT:\n%s\nSTDERR:\n%s", result.stdout, result.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)

    if not dest.exists():
        raise RuntimeError(f"Blender reported success but {dest} was not created")

    log.info("Converted %s -> %s", source, dest)
    return dest


# ---------------------------------------------------------------------------
# Blender side (executed inside `blender --background --python`)
# ---------------------------------------------------------------------------
def _parse_blender_args():
    # Blender puts its own args before "--"; only what's after belongs to us.
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-addon-module", default=None)
    parser.add_argument("--max-import-operator", default=None)
    parser.add_argument("--max-filepath-arg", default="filepath")
    return parser.parse_args(argv)


def _resolve_operator(bpy_module, dotted_path: str):
    """Resolve a dotted bpy.ops path like 'import_scene.max' to the actual
    callable bpy.ops.import_scene.max, so the .max add-on's operator name
    can come from config.yml instead of being hardcoded here (different
    add-ons register different operator names/namespaces)."""
    parts = dotted_path.split(".")
    obj = bpy_module.ops
    for part in parts:
        obj = getattr(obj, part)
    return obj


def _import_max(bpy_module, source: Path, addon_module: str, operator_path: str, filepath_arg: str):
    """Import a .max file via a third-party add-on -- see module docstring
    '3ds Max (.max) source support'. Enables the add-on (works even under
    --factory-startup, which only skips auto-loading previously-enabled
    add-ons at startup, not their availability) then calls its import
    operator with the configured file-path kwarg."""
    try:
        bpy_module.ops.preferences.addon_enable(module=addon_module)
    except Exception as exc:
        raise RuntimeError(
            f"Could not enable Blender add-on '{addon_module}' (config.yml "
            f"ingest.max_import.addon_module) -- confirm it's installed on "
            f"this machine's Blender, not just this repo's config. Original "
            f"error: {exc}"
        ) from exc

    operator = _resolve_operator(bpy_module, operator_path)
    operator(**{filepath_arg: str(source)})


def _import_source(bpy_module, source: Path, max_addon_module=None, max_import_operator=None, max_filepath_arg="filepath"):
    ext = source.suffix.lower().lstrip(".")
    if ext == "fbx":
        bpy_module.ops.import_scene.fbx(filepath=str(source))
    elif ext == "obj":
        # Blender 4.x uses wm.obj_import; fall back to legacy import_scene.obj
        if hasattr(bpy_module.ops.wm, "obj_import"):
            bpy_module.ops.wm.obj_import(filepath=str(source))
        else:
            bpy_module.ops.import_scene.obj(filepath=str(source))
    elif ext == "abc":
        bpy_module.ops.wm.alembic_import(filepath=str(source))
    elif ext in ("gltf", "glb"):
        bpy_module.ops.import_scene.gltf(filepath=str(source))
    elif ext == "max":
        _import_max(bpy_module, source, max_addon_module, max_import_operator, max_filepath_arg)
    else:
        raise ValueError(f"No Blender importer wired up for .{ext}")


def _do_conversion_in_blender(input_path: str, output_path: str, max_addon_module=None, max_import_operator=None, max_filepath_arg="filepath"):
    import bpy

    source = Path(input_path)
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Start from a clean scene -- --factory-startup already avoids the
    # user's local startup.blend, this clears the default cube/light/camera.
    bpy.ops.wm.read_factory_settings(use_empty=True)

    _import_source(bpy, source, max_addon_module, max_import_operator, max_filepath_arg)

    bpy.ops.wm.usd_export(
        filepath=str(dest),
        export_materials=True,
        export_textures=True,
        export_uvmaps=True,
        export_normals=True,
        export_animation=False,
        evaluation_mode="RENDER",
    )
    print(f"[convert_to_usd] wrote {dest}")


if __name__ == "__main__" and RUNNING_IN_BLENDER:
    args = _parse_blender_args()
    _do_conversion_in_blender(
        args.input, args.output,
        max_addon_module=args.max_addon_module,
        max_import_operator=args.max_import_operator,
        max_filepath_arg=args.max_filepath_arg,
    )
