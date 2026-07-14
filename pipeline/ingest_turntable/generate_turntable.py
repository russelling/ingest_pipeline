"""
generate_turntable.py -- renders a 360 degree turntable of a published USD
asset to scene-linear ACEScg EXR frames, then hands off to the studio's
existing qt_watcher (https://github.com/russelling/BUF_Mac_watcher) for
baking (slate, burn-ins, color pipeline) and ShotGrid Version upload --
exactly the same handoff a Nuke shot render or Unreal MRQ turntable already
uses. See README.md "qt_watcher integration" for the full rationale.

Output paths are resolved from templates that ALREADY EXIST in the live
core/templates.yml (verified directly against russelling/FlowTrackingConfig
@ main, not assumed):

    unreal_asset_turntable_render  -- EXR frame sequence
    unreal_asset_turntable_flag    -- .render_complete_*.json flag qt_watcher polls for
    unreal_asset_turntable_movie   -- final QT (written by qt_watcher, not here)

They're named "unreal_" because Unreal MRQ turntables were their first
producer, but nothing about them is Unreal-specific -- they're just Toolkit
templates pointing at a folder path and a filename pattern. This module
reuses them as-is rather than adding parallel "blender_" versions, since
qt_watcher's own code only cares about the flag's JSON content, never which
template or engine produced it. See README.md if you'd rather rename them.

This module does NOT encode a QuickTime or touch ShotGrid Versions itself.
Its job ends at writing the flag file that qt_watcher.py's poll loop will
pick up on its own schedule (default every 30s).

Like convert_to_usd.py, this module has two lives:

1. Imported by ingest_asset.py (plain Python) -- call
   `render_and_flag(usd_path, asset, project_id, task, artist, config, tk)`.
   Renders the frame sequence via a Blender subprocess, color-converts each
   frame from Blender's linear working space to ACEScg with oiiotool, and
   writes the render-complete flag.

2. Executed *by* Blender:
       blender --background --factory-startup --python generate_turntable.py -- \
           --usd <path> --frame-pattern <printf-style path with %04d> \
           --frame-start N --frame-end N --fps N --turns N \
           --res-x N --res-y N --engine EEVEE_NEXT
   In this mode it imports the USD, builds the turntable rig via
   turntable_scene_template.py, and renders one scene-linear EXR per frame,
   named exactly per `frame_pattern % frame_number` -- matching Toolkit's
   template naming exactly rather than relying on Blender's own frame-suffix
   behavior.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("ingest_turntable.generate_turntable")

try:
    import bpy  # noqa: F401
    RUNNING_IN_BLENDER = True
except ImportError:
    RUNNING_IN_BLENDER = False


# ---------------------------------------------------------------------------
# Driver side (plain Python, called from ingest_asset.py)
# ---------------------------------------------------------------------------
def _render_fields(asset: dict, version: int) -> dict:
    return {
        "Asset": asset["code"],
        "sg_asset_type": asset["sg_asset_type"],
        "version": version,
    }


def _resolve_frame_pattern(tk, fields: dict) -> str:
    """Resolve unreal_asset_turntable_render to a printf-style path with a
    %04d frame token, using sgtk's documented "FORMAT: %d" sentinel for
    sequence keys -- the SEQ key's own format_spec ("04" in templates.yml)
    supplies the padding, so this comes back as e.g.
    '.../HeroChair_turntable_v001.%04d.exr'."""
    template = tk.templates["unreal_asset_turntable_render"]
    pattern_fields = dict(fields, SEQ="FORMAT: %d")
    return template.apply_fields(pattern_fields)


def _render_frames(usd_path: Path, frame_pattern: str, blender_exe: str, tt_cfg: dict):
    Path(frame_pattern % tt_cfg["frame_start"]).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        blender_exe,
        "--background",
        "--factory-startup",
        "--python",
        str(Path(__file__).resolve()),
        "--",
        "--usd", str(usd_path),
        "--frame-pattern", frame_pattern,
        "--frame-start", str(tt_cfg["frame_start"]),
        "--frame-end", str(tt_cfg["frame_end"]),
        "--fps", str(tt_cfg["fps"]),
        "--turns", str(tt_cfg["turns"]),
        "--res-x", str(tt_cfg["resolution"][0]),
        "--res-y", str(tt_cfg["resolution"][1]),
        "--engine", tt_cfg["render_engine"],
    ]
    log.info("Rendering turntable frames: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("Blender turntable render failed:\nSTDOUT:\n%s\nSTDERR:\n%s", result.stdout, result.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    log.info("Turntable frames rendered per pattern %s", frame_pattern)


def _verify_frames_written(frame_pattern: str, first: int, last: int):
    missing = [n for n in range(first, last + 1) if not Path(frame_pattern % n).exists()]
    if missing:
        raise RuntimeError(
            f"Blender reported success but {len(missing)} frame(s) are missing, "
            f"e.g. {frame_pattern % missing[0]}"
        )


def _convert_to_acescg(frame_pattern: str, first: int, last: int, oiiotool_exe: str, tt_cfg: dict):
    """Color-convert every rendered frame in place, from Blender's linear
    working space to ACEScg, using the same OCIO config qt_bake_oiio.py uses
    so the two stay in sync. VERIFY tt_cfg['blender_linear_colorspace'] is
    the correct alias for your OCIO config before trusting the result --
    see config.yml's comment on that key."""
    ocio_config = "ocio://studio-config-latest"
    src_space = tt_cfg["blender_linear_colorspace"]
    dst_space = tt_cfg["target_colorspace"]

    for n in range(first, last + 1):
        frame_path = Path(frame_pattern % n)
        tmp_path = frame_path.with_suffix(".exr.tmp")
        cmd = [
            oiiotool_exe,
            "--colorconfig", ocio_config,
            str(frame_path),
            "--colorconvert", src_space, dst_space,
            "-o", str(tmp_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("oiiotool color convert failed on %s:\n%s", frame_path, result.stderr)
            raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
        tmp_path.replace(frame_path)
    log.info("Color-converted frames %d-%d: %s -> %s", first, last, src_space, dst_space)


def _write_flag(
    tk,
    fields: dict,
    frame_pattern: str,
    first: int,
    last: int,
    asset: dict,
    project_id: int,
    task: dict | None,
    artist: str,
    config: dict,
) -> Path:
    """Write the .render_complete_*.json flag that BUF_Mac_watcher's
    qt_watcher.py polls for, at the path unreal_asset_turntable_flag already
    defines. Field names/values must match what qt_watcher.py's
    resolve_asset_output_paths() and upload_version() read -- see that
    repo's scripts/qt_watcher.py for the authoritative schema."""
    import sg_utils

    flag_path = Path(tk.templates["unreal_asset_turntable_flag"].apply_fields(fields))

    assets_root = Path(sg_utils.resolve_platform_path(config["qt_watcher"]["assets_root"]))
    try:
        flag_path.relative_to(assets_root)
    except ValueError:
        log.warning(
            "Turntable flag path %s is not under config.yml qt_watcher.assets_root "
            "(%s) -- qt_watcher.py's os.walk(ASSETS_ROOT) on the watcher machine "
            "may never find this flag. Check that core/roots.yml's primary root "
            "matches BUF_Mac_watcher/scripts/qt_watcher.py's ASSETS_ROOT.",
            flag_path, assets_root,
        )

    flag_data = {
        "type": "asset_turntable",
        "entity_name": asset["code"],
        "asset_type": asset["sg_asset_type"],
        "step": config["turntable"]["step_code"],
        "version": fields["version"],
        "project_id": project_id,
        "entity_id": asset["id"],
        "entity_type": "Asset",
        "task_id": task["id"] if task else None,
        "frame_first": first,
        "frame_last": last,
        "exr_path_pattern": frame_pattern,
        # Turntables have no meaningful source timecode (no plate, no edit) --
        # left null so qt_bake_oiio.py falls back to a frame-derived TC.
        "start_timecode": None,
        "artist": artist,
        "date": datetime.date.today().isoformat(),
        "submitted_for": None,
        "description": "Automated turntable render generated on ingest.",
    }

    flag_path.parent.mkdir(parents=True, exist_ok=True)
    with open(flag_path, "w") as f:
        json.dump(flag_data, f, indent=2)
    log.info("Wrote qt_watcher flag: %s", flag_path)
    return flag_path


def render_and_flag(usd_path: Path, asset: dict, project_id: int, task: dict | None, artist: str, config: dict, tk, sg) -> Path:
    """Full turntable pipeline up to the qt_watcher handoff: render EXR
    frames, color-convert to ACEScg, register a PublishedFile for the raw
    render, and write the render-complete flag. Returns the flag path.
    Does NOT create a ShotGrid Version or encode a movie -- qt_watcher does
    that asynchronously once it polls and finds the flag. `tk`/`sg` are the
    pair from sg_utils.get_sgtk(), passed through from ingest_asset.py
    rather than re-bootstrapped here."""
    import sg_utils

    version = sg_utils.next_version_number(sg, asset, "Turntable Render")
    fields = _render_fields(asset, version)

    frame_pattern = _resolve_frame_pattern(tk, fields)
    blender_exe = sg_utils.get_executable(config, "blender")
    oiiotool_exe = sg_utils.get_executable(config, "oiiotool")
    tt_cfg = config["turntable"]

    _render_frames(usd_path, frame_pattern, blender_exe, tt_cfg)
    _verify_frames_written(frame_pattern, tt_cfg["frame_start"], tt_cfg["frame_end"])
    _convert_to_acescg(frame_pattern, tt_cfg["frame_start"], tt_cfg["frame_end"], oiiotool_exe, tt_cfg)

    turntable_render_type = sg_utils.find_or_create_published_file_type(sg, "Turntable Render")
    sg.create(
        "PublishedFile",
        {
            "project": {"type": "Project", "id": project_id},
            "code": Path(frame_pattern).name,
            "entity": {"type": "Asset", "id": asset["id"]},
            "task": {"type": "Task", "id": task["id"]} if task else None,
            "version_number": version,
            "path": {"local_path": frame_pattern},
            "published_file_type": {"type": "PublishedFileType", "id": turntable_render_type["id"]},
        },
    )

    flag_path = _write_flag(
        tk, fields, frame_pattern, tt_cfg["frame_start"], tt_cfg["frame_end"],
        asset, project_id, task, artist, config,
    )
    return flag_path


# ---------------------------------------------------------------------------
# Blender side (executed inside `blender --background --python`)
# ---------------------------------------------------------------------------
def _parse_blender_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    parser = argparse.ArgumentParser()
    parser.add_argument("--usd", required=True)
    parser.add_argument("--frame-pattern", required=True, help="printf-style path with a %04d-equivalent frame token")
    parser.add_argument("--frame-start", type=int, required=True)
    parser.add_argument("--frame-end", type=int, required=True)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--turns", type=int, default=1)
    parser.add_argument("--res-x", type=int, required=True)
    parser.add_argument("--res-y", type=int, required=True)
    parser.add_argument("--engine", default="EEVEE_NEXT")
    return parser.parse_args(argv)


def _do_render_in_blender(args):
    import bpy
    import turntable_scene_template as tts

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.usd_import(filepath=args.usd)

    imported = [o for o in bpy.context.selected_objects] or list(bpy.context.scene.objects)
    if not imported:
        raise RuntimeError(f"No objects found after importing {args.usd}")

    # Parent everything imported to an empty so the whole asset spins as one.
    root = bpy.data.objects.new("TT_AssetRoot", None)
    bpy.context.collection.objects.link(root)
    for obj in imported:
        if obj.parent is None:
            obj.parent = root

    tts.build_turntable(
        asset_root_obj=root,
        frame_start=args.frame_start,
        frame_end=args.frame_end,
        turns=args.turns,
        resolution=(args.res_x, args.res_y),
        render_engine=args.engine,
    )

    scene = bpy.context.scene
    scene.render.fps = args.fps

    # Write scene-linear EXR, not a display-referred format: qt_watcher's
    # bake (qt_bake_oiio.py) expects raw ACEScg values and does its own
    # display transform. "Standard" view transform + "None" look is meant to
    # ensure Blender doesn't bake AgX/Filmic tonemapping into the EXR pixel
    # data -- VERIFY this holds on your Blender version/build before
    # trusting the output; if the render still looks tonemapped, the
    # generate_turntable.py driver's oiiotool color-convert step
    # (lin_rec709 -> ACEScg) will be operating on the wrong input and the
    # result will not match shot-render color.
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_depth = '32'
    scene.render.image_settings.color_mode = 'RGBA'
    if hasattr(scene.render.image_settings, "exr_codec"):
        scene.render.image_settings.exr_codec = 'ZIP'
    scene.render.use_file_extension = False

    # Render frame-by-frame (rather than bpy.ops.render.render(animation=True)
    # with a filepath prefix) so each frame's filename matches the Toolkit
    # template's naming exactly, instead of relying on Blender's own
    # frame-suffix convention lining up with it.
    for frame in range(args.frame_start, args.frame_end + 1):
        scene.frame_set(frame)
        scene.render.filepath = args.frame_pattern % frame
        bpy.ops.render.render(write_still=True)

    print(f"[generate_turntable] rendered frames {args.frame_start}-{args.frame_end} per pattern {args.frame_pattern}")


if __name__ == "__main__" and RUNNING_IN_BLENDER:
    # See convert_to_usd.py's matching comment: Blender doesn't propagate an
    # unhandled exception in a --python script into its own exit code, so
    # this must catch and sys.exit(1) explicitly or the driver side will see
    # a false "success" and a confusing downstream "frames missing" error
    # instead of the real traceback.
    try:
        _do_render_in_blender(_parse_blender_args())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
