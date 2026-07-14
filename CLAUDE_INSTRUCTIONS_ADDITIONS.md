# CLAUDE_INSTRUCTIONS.md additions -- qt_watcher / BUF_Mac_watcher / ingest_pipeline

Paste the section below into `CLAUDE_INSTRUCTIONS.md`. Suggested placement:
a new top-level section after "Pipeline Steps" and before "Template Key
Reference", since it's referenced by both.

## Repository Layout / Known Gaps updates

Add to **Repository Layout**: a note that `pipeline/ingest_turntable/` is
NOT part of `FlowTrackingConfig`'s own git history -- it's a checkout of the
separate [`russelling/ingest_pipeline`](https://github.com/russelling/ingest_pipeline)
repo, living at `buffalo_flow_config/config/pipeline/ingest_turntable/` on
the render machine (same pattern as `BUF_Mac_watcher`). See "Ingest +
Turntable Pipeline" below.

Add to **Work-in-Progress / Known Gaps**:
`- [ ] Ingest + turntable pipeline (pipeline/ingest_turntable/) -- color
pipeline (Blender linear -> ACEScg) not yet verified against the studio
OCIO config; test end-to-end before relying on it for a real delivery.`

-----

## QT Review Rendering (qt_watcher)

|Field       |Value                                                          |
|------------|----------------------------------------------------------------|
|Repository  |<https://github.com/russelling/BUF_Mac_watcher>                 |
|Purpose     |Render monitor + QT baking/upload for shot and asset turntable renders|
|Runs as     |macOS LaunchAgent `com.buffalovfx.qtwatcher`, on the Mac Studio |
|Entry point |`scripts/qt_watcher.py`, run by the Shotgun/Flow desktop app's bundled Python 3.11|

qt_watcher is a polling daemon, independent of this Toolkit config, that
turns raw EXR renders into reviewable ShotGrid Versions. It is the **single
place** color science, slates, and burn-ins are implemented for both shots
and assets -- nothing else in the pipeline should encode a QuickTime or
create a review Version directly; write a flag file and let qt_watcher do
it, so every review QT (shot or asset, Nuke or Blender or Unreal source) is
branded identically.

### What it does

1. Polls `SHOTS_ROOT` and `ASSETS_ROOT` (currently
   `/Volumes/atv-post-lucid3/atv-buffalo-s03/buffalo_vfx/{shots,assets}`)
   every `POLL_INTERVAL_SECONDS` (30s) for `.render_complete_*.json` flag
   files, found via recursive `os.walk()` -- flags can live anywhere under
   those trees.
2. Routes each flag to a bake tool based on `type`:
   - Shot renders (`type` absent or `"shot"`) → Nuke batch bake
     (`qt_bake_slate_burnin.py`) -- currently unused in favor of #2 below
     to avoid a Nuke license on the headless watcher machine.
   - **Both** shots and asset turntables currently route through the
     license-free OIIO + FFmpeg bake (`qt_bake_oiio.py`).
3. `qt_bake_oiio.py` applies the full color pipeline
   (**ACEScg → LogC4 → CDL (shots only) → Show LUT → Rec.709**), builds a
   slate (logo, three color-baked thumbnails, context metadata, grayscale +
   color-bar reference strip) and per-frame burn-ins, and encodes ProRes
   422 HQ to every output path the flag's context resolves to (shot: review
   folder + dated editorial drop; asset: review folder + dated editorial
   drop via the `unreal_asset_turntable_movie` template).
4. Uploads the resulting QT to ShotGrid as a `Version` linked to the
   Shot/Asset (`sg_uploaded_movie`), then renames the flag from
   `.render_complete_*.json` to `.processed_*.json` so it isn't reprocessed.

### Flag file contract

Any tool that wants a render baked and uploaded writes a
`.render_complete_<name>.json` file somewhere under `SHOTS_ROOT` or
`ASSETS_ROOT`. Minimum fields for an **asset turntable** flag (see
`qt_watcher.py` `resolve_asset_output_paths()` / `upload_version()` for the
authoritative list):

```json
{
  "type": "asset_turntable",
  "entity_name": "HeroChair",
  "asset_type": "Prop",
  "step": "turntable",
  "version": 1,
  "project_id": 123,
  "entity_id": 456,
  "entity_type": "Asset",
  "task_id": 789,
  "frame_first": 1,
  "frame_last": 120,
  "exr_path_pattern": "/Volumes/atv-post-lucid3/atv-buffalo-s03/buffalo_vfx/assets/Prop/HeroChair/render/work/HeroChair_turntable_v001/HeroChair_turntable_v001.%04d.exr",
  "start_timecode": null,
  "artist": "Ingest Pipeline",
  "date": "2026-07-13",
  "submitted_for": null,
  "description": "Automated turntable render generated on ingest."
}
```

EXRs referenced by `exr_path_pattern` **must be scene-linear ACEScg** --
the bake pipeline does not detect or convert source color space, it assumes
ACEScg going in.

Known asset-turntable flag producers today:
- `publish_turntable_unreal.py` (Unreal MRQ turntables -- original producer
  this flag schema was designed for)
- `pipeline/ingest_turntable/generate_turntable.py` (this repo, added for
  vendor/external asset ingest -- see that folder's README.md
  "qt_watcher integration" for the Blender-specific color-conversion step
  it adds before writing the flag)

### Dependencies (installed via `watcher_launch.txt` in that repo)

- `/opt/homebrew/bin/oiiotool` (Homebrew `openimageio`)
- `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` (Homebrew `ffmpeg-full` --
  **not** the core `ffmpeg` formula, which lacks `drawtext`/freetype needed
  for slate + burn-ins)
- Shotgun/Flow desktop app's bundled Python 3.11 (`sgtk`, `shotgun_api3`)
- Nuke 17.0 (only if the Nuke bake path is re-enabled)

### Operating it

See that repo's `QT_Watcher_README.md` for full launch/stop/logs
instructions. Quick reference:

```bash
# start (after one-time install, see that repo)
launchctl load ~/Library/LaunchAgents/com.buffalovfx.qtwatcher.plist

# check it's running
launchctl list | grep qtwatcher

# logs
tail -f /Volumes/atv-post-lucid3/atv-buffalo-s03/buffalo_vfx/buffalo_flow_config/logs/qt_watcher.log
```

### Known launchd gotcha: StandardOutPath/StandardErrorPath on a network volume

Both `com.buffalovfx.qtwatcher` and `com.buffalovfx.ingestturntable` originally
pointed their `StandardOutPath`/`StandardErrorPath` at
`buffalo_flow_config/logs/*.log` on the `atv-post-lucid3` SMB share. On the
Mac Studio this made `ingestturntable` fail to spawn entirely: `launchctl
print` showed `runs = 1`, `state = not running`,
`last exit code = 78: EX_CONFIG`, and the log files themselves never got
created -- meaning launchd couldn't even open the redirect targets, so
nothing from the Python process (or its traceback) ever landed anywhere.

Root cause: launchd's daemon-spawn context doesn't carry the same
session-level SMB authentication as an interactive login, so `open()` for a
*new* file on that share fails when launchd does it on the job's behalf --
even though the same user, once the process is actually running, can read
and write that same share without issue (confirmed: `config.yml` reads, the
ShotGrid API calls, and the script's own `logging.log_dir_mac` writes all
work fine once the process is up). It's specifically the launchd-level
stdio redirect that breaks on this share, not general network volume access.

Fix applied to `ingestturntable`: point `StandardOutPath`/`StandardErrorPath`
at a local path instead --
`~/Library/Logs/buffalovfx/ingest_turntable_watcher{,_error}.log` -- then
`launchctl bootout` + `bootstrap` + `kickstart` (plain `unload`/`load` did
NOT clear the bad state from earlier failed attempts; `bootout` was
required). Confirmed stable afterward (`state = running`,
`last exit code = (never exited)`).

`qt_watcher.plist` still points its own stdout/stderr at the same network
share as of this writing -- check whether it's hitting the same failure
(`launchctl print gui/<uid>/com.buffalovfx.qtwatcher | grep -E
"runs|state|last exit"`) before assuming a qt_watcher problem is a
script/credentials issue. Apply the same local-log fix if it shows
`EX_CONFIG` with `runs` not climbing / `state = not running`.

General launchd debugging lesson: `launchctl print gui/<uid>/<label>` is far
more informative than `launchctl list` -- it shows `runs`, `last exit code`,
and full ProgramArguments/paths/environment. When a job has been through
multiple failed `load`/`unload` cycles, prefer `launchctl bootout` +
`bootstrap` over `unload`/`load` to guarantee a clean state rather than
resuming a possibly-corrupted one.

### Rules for Claude across sessions (qt_watcher-specific)

1. **Never re-implement QT baking.** If a new pipeline needs a reviewable
   render in ShotGrid, write a `.render_complete_*.json` flag matching the
   contract above and let qt_watcher bake/upload it. Don't add a second
   ffmpeg-encode-and-`sg.create("Version")` path elsewhere in the config.
2. **EXRs must be ACEScg before the flag is written.** Any renderer whose
   native output isn't already ACEScg (e.g. Blender's default linear
   Rec.709) needs an explicit color-convert step first -- see
   `pipeline/ingest_turntable/generate_turntable.py` for the pattern
   (`oiiotool --colorconvert` against the same `ocio://studio-config-latest`
   config qt_bake_oiio.py uses).
3. **BUF_Mac_watcher is a separate repo.** Changes to `qt_watcher.py` /
   `qt_bake_oiio.py` / `qt_bake_slate_burnin.py` happen in that repo, not
   here. If a change here (e.g. a new template name) requires a
   corresponding change there, call it out explicitly rather than assuming
   it's already been made.
4. **`entity_type` defaults to `"Shot"`** in `qt_watcher.py`'s
   `upload_version()` if omitted from the flag -- any asset-producing flag
   writer MUST set `"entity_type": "Asset"` explicitly, or the Version will
   be linked to the wrong entity type.

-----

## Ingest + Turntable Pipeline (ingest_pipeline)

|Field       |Value                                                          |
|------------|----------------------------------------------------------------|
|Repository  |<https://github.com/russelling/ingest_pipeline>                 |
|Purpose     |Vendor/external asset ingest -> USD publish -> turntable render, feeding into qt_watcher|
|Runs as     |macOS LaunchAgent `com.buffalovfx.ingestturntable`, on the Mac Studio|
|Entry point |`watch_folder.py`, run by the Shotgun/Flow desktop app's bundled Python 3|
|Deploy path |`buffalo_flow_config/config/pipeline/ingest_turntable/` -- a checkout of `ingest_pipeline`, same pattern as `BUF_Mac_watcher`'s deploy path|

Watches `incoming/<AssetType>/` folders (standing subfolders per
`sg_asset_type`) inside the asset storage tree for new vendor deliveries or
external downloads. For each one: creates/finds the Asset in ShotGrid,
converts the source geometry (fbx/obj/abc/gltf/max/usd) to USD via headless
Blender, publishes it, then renders a 360° turntable and hands off to
qt_watcher (see "QT Review Rendering" above) for baking/upload rather than
encoding a QuickTime itself.

Like `BUF_Mac_watcher`, this is tracked in its own repo rather than inside
`FlowTrackingConfig` -- only two small pieces actually belong in
`FlowTrackingConfig` itself: a handful of `core/templates.yml` entries and
three new asset schema folders (`render/`, `render/work/`, `review/`). See
that repo's own `README.md` "Merging into FlowTrackingConfig" for the
exact diff -- don't duplicate those instructions here, they'll drift.

Authentication matches `qt_watcher.py`'s pattern exactly
(`sgtk.sgtk_from_path()` against the pipeline configuration itself, no
separate ShotGrid API script/credentials) -- see that repo's `sg_utils.py`.

### Rules for Claude across sessions (ingest_pipeline-specific)

1. **`ingest_pipeline` is a separate repo.** Changes to `watch_folder.py` /
   `ingest_asset.py` / `convert_to_usd.py` / `generate_turntable.py` happen
   in that repo, not here. If a change here (e.g. a template rename) needs
   a corresponding change there, call it out explicitly.
2. **Asset type comes from the standing `incoming/<Type>/` folder it was
   dropped into, never guessed.** Don't add filename/content-based type
   inference -- that was deliberately replaced with folder-based typing.
3. **`.max` source support depends on the `io_scene_max` Blender extension**
   (<https://github.com/nrgsille76/io_scene_max>) being installed on
   whichever Blender `executables.blender` points at, plus
   `config.yml ingest.max_import.addon_module` being set correctly for
   that install (Blender Extensions module names aren't fully predictable
   across versions/repositories -- see that repo's README "3ds Max (.max)
   source support").
4. **YAML edits to `core/templates.yml` must not contain literal tabs.**
   A single tab character accidentally introduced when merging the `.max`
   choice entry into `keys.ingest_extension.choices` broke `sgtk_from_path()`
   for every consumer of this pipeline config, not just this pipeline --
   `tank.errors.TankError: ... found character '\t' that cannot start any
   token`. Confirmed and fixed directly on the live Mac Studio file. **Not
   yet confirmed pushed to the `russelling/FlowTrackingConfig` GitHub repo**
   -- check before assuming it's there. When editing this file, verify with
   `python3 -c "import yaml; yaml.safe_load(open('core/templates.yml'))"`
   before committing/deploying.
5. **`shotgrid.project_id` is now set** (fixed during first live test).
   `ingest.max_import.addon_module` is still a placeholder -- `.max` files
   specifically will fail until it's confirmed (see item 3).
6. **`sg.create()` PublishedFile links must use `id`, not `name`.**
   `{"type": "PublishedFileType", "name": "Vendor Delivery"}` passed
   directly into a linked field fails: `API create() invalid/missing entity
   hash integer 'id'`. Fixed via `sg_utils.find_or_create_published_file_type()`
   (looks up by `code`, the actual field PublishedFileType stores its label
   in) -- used in `ingest_asset.py` (Vendor Delivery, USD Asset) and
   `generate_turntable.py` (Turntable Render). Confirmed fixed on the live
   Mac Studio file; **not yet confirmed pushed to GitHub**.
7. **Blender does not propagate a `--python` script's unhandled exception
   into its own process exit code** -- it prints the traceback and exits 0
   anyway. This made `convert_to_usd.py`/`generate_turntable.py`'s
   `subprocess.run()` calls see false "success" while nothing was actually
   written. Both Blender-side `__main__` blocks now wrap their entry call in
   `try/except` + explicit `sys.exit(1)` so the driver side's existing
   `if result.returncode != 0` error path actually fires with the real
   traceback. Confirmed fixed on the live Mac Studio file; **not yet
   confirmed pushed to GitHub**.
8. **`bpy.ops.wm.usd_export`'s accepted kwargs vary by Blender version.**
   `export_textures` is not a valid kwarg on Blender 5.1.2 (`TypeError:
   Converting py args to operator properties:: keyword "export_textures"
   unrecognized`), though it exists on older Blender releases.
   `convert_to_usd.py`'s `_usd_export_kwargs()` now introspects
   `bpy.ops.wm.usd_export.get_rna_type().properties` at runtime and only
   passes kwargs the running Blender build actually supports, logging
   anything dropped -- confirmed working on Blender 5.1.2 (real `.usd` file
   produced from a vendor FBX). Not yet confirmed pushed to GitHub.
9. **Status as of the last session:** USD conversion confirmed working
   end-to-end via a manual Blender invocation (FBX -> USD, real file
   written). The turntable render step itself (EEVEE frames, `oiiotool`
   ACEScg color convert, `qt_watcher` flag handoff) has NOT yet been run
   end-to-end -- that's the next thing to test. A real test delivery
   (`phonebooth`, a vendor FBX of a graffiti phone booth prop) is sitting in
   `incoming/_failed/Prop/` on the Mac Studio pending re-test; move it back
   to `incoming/Prop/` to resume. Also still open: confirm whether
   `com.buffalovfx.qtwatcher`'s LaunchAgent has the same network-volume
   `StandardOutPath` spawn issue `ingestturntable` had (never got a
   confirmed answer either way), and confirm items 4/6/7/8 above are
   actually committed to GitHub and not just fixed locally on the render
   machine.
