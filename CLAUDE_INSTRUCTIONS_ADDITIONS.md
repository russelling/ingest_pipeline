# CLAUDE_INSTRUCTIONS.md additions -- qt_watcher / BUF_Mac_watcher

Paste the section below into `CLAUDE_INSTRUCTIONS.md`. Suggested placement:
a new top-level section after "Pipeline Steps" and before "Template Key
Reference", since it's referenced by both.

## Also fix: documented repo layout doesn't match the live repo

While building the ingest + turntable pipeline, I fetched the actual
`russelling/FlowTrackingConfig` repo on `main` directly to verify paths
before writing merge instructions, rather than trusting this file's
existing "Repository Layout" section. Two things there are aspirational,
not real yet:

- **`config/paths.yml` does not exist.** DCC executable paths are actually
  resolved through `env/includes/software_paths.yml` (`path.<os>.<dcc>`
  values) + `env/includes/app_locations.yml`, consumed by
  `env/includes/settings/tk-multi-launchapp.yml`. Blender's path is already
  there as a placeholder. Either update the "Repository Layout" table to
  drop `config/paths.yml` and point at the real files, or -- if
  `config/paths.yml` is something you actually want to introduce -- treat
  it as new work, not documentation of something that already exists.
- **Assets do not have per-DCC `publish/{maya,nuke,blender}/` subfolders.**
  The real schema (`core/schema/project/assets/asset_type/asset/publish.yml`)
  is a single shared `publish/` folder for all DCCs ("single shared publish
  destination for all DCCs"), with `maya_asset_publish` /
  `blender_asset_publish` / etc. all writing into it with DCC-specific
  filenames instead. Fix the "Repository Layout" asset schema tree to match.

Also update these two existing spots while you're in there:

- **Repository Layout** table: add a note that `pipeline/ingest_turntable/`
  (this delivery) exists at the repo root alongside `core/` and `env/`.
- **Work-in-Progress / Known Gaps**: add
  `- [ ] Ingest + turntable pipeline (pipeline/ingest_turntable/) -- color
  pipeline (Blender linear -> ACEScg) not yet verified against the studio
  OCIO config; test end-to-end before relying on it for a real delivery.`
  and
  `- [ ] core/templates.yml already defines unreal_asset_turntable_render/
  _flag/_movie, but the asset schema folders they point at (render/,
  render/work/, review/) don't exist yet -- templates were added before
  schema (violates this file's own rule 3). Unclear if the Unreal turntable
  path (publish_turntable_unreal.py) has actually been tested end-to-end.`

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
