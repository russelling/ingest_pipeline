# Ingest + Turntable Pipeline

Automates: drop a vendor delivery / external download into a type folder in
the watched `incoming` directory → creates (or finds) the Asset in
Flow/ShotGrid → converts the source geometry to pipeline-standard USD →
publishes it → renders a 360° turntable in Blender → hands the frames off
to the studio's existing **qt_watcher** for baking and ShotGrid upload.

**Everything below is checked directly against the live
`russelling/FlowTrackingConfig` repo on `main` and the live
`russelling/BUF_Mac_watcher` repo on `main`** -- not assumed from
`CLAUDE_INSTRUCTIONS.md`, which turned out to describe some structure
(`config/paths.yml`, per-DCC `publish/{maya,nuke,blender}/` folders) that
doesn't actually exist in the repo yet. Where the two disagree, this
delivery follows the real repo and calls out the discrepancy.

## What's in this delivery

```
core/
  templates_ingest_turntable.yml   # paste into core/templates.yml (small -- see below)
  roots_additions.yml              # NOT NEEDED -- explains why, see file
  schema/project/assets/asset_type/asset/
    SCHEMA_ADDITIONS.md            # what folders to add, and why (also small)
    render/.gitkeep
    render/work/.gitkeep
    review/.gitkeep
    work/ingest/.gitkeep           # leftover, unused -- see SCHEMA_ADDITIONS.md
    work/turntable/.gitkeep        # leftover, unused
    publish/ingest/.gitkeep        # leftover, unused
    review/turntable/.gitkeep      # leftover, unused
config/
  paths_additions.yml              # NOT NEEDED -- explains why, see file
pipeline/ingest_turntable/
  config.yml                       # watch folder, ShotGrid, render, qt_watcher settings
  sg_utils.py                      # ShotGrid + Toolkit connection helpers
  watch_folder.py                  # long-running poller -- entry point
  ingest_asset.py                  # per-delivery ingest logic (also runnable standalone)
  convert_to_usd.py                # fbx/obj/abc/gltf -> usd, via headless Blender
  generate_turntable.py            # renders EXR frames + hands off to qt_watcher
  turntable_scene_template.py      # lighting/ground/camera rig, used inside Blender
  manifest.example.yml             # optional per-delivery metadata file
  requirements.txt
  launchd/
    com.buffalovfx.ingestturntable.plist   # LaunchAgent, mirrors qt_watcher's
    install_launchagent.sh                 # one-time setup, mirrors watcher_launch.txt
    LAUNCH_README.md                       # launch/stop/logs, mirrors QT_Watcher_README.md
CLAUDE_INSTRUCTIONS_ADDITIONS.md   # paste into CLAUDE_INSTRUCTIONS.md
```

## What actually needs to change in your repo

Turned out to be much smaller than the first draft of this pipeline
assumed, because the live repo already has most of the plumbing:

|Needed                                   |Why                                                                 |
|------------------------------------------|---------------------------------------------------------------------|
|2 new keys + 2 new path templates in `core/templates.yml`|For the ingest step only -- see `templates_ingest_turntable.yml`|
|3 new static folders in the asset schema (`render/`, `render/work/`, `review/`)|Pre-existing gap: `core/templates.yml` already references `unreal_asset_turntable_render`/`_flag`/`_movie` under these paths, but the schema folders backing them don't exist yet|
|`pipeline/ingest_turntable/` folder      |This pipeline's own code, not a Toolkit app                          |
|~~`config/paths.yml`~~                    |**Doesn't exist, don't need it** -- Blender's path already lives in `env/includes/software_paths.yml`|
|~~new `core/roots.yml` entry~~            |**Not needed** -- `incoming` is a plain filesystem path this pipeline's own watcher polls directly, never resolved through a Toolkit template|
|~~`work/ingest`, `publish/ingest` folders~~|**Not needed** -- reuses the asset schema's existing `source/` ("raw scans, purchased assets, original deliverables") and `publish/` ("single shared publish destination") folders, which already exist and already say exactly this|
|~~new turntable render/flag/movie templates~~|**Not needed** -- reuses `unreal_asset_turntable_render`/`_flag`/`_movie`, which already exist in `core/templates.yml`|

## Asset type: standing incoming/ folders

`incoming` has one standing subfolder per Asset Type (config.yml
`ingest.asset_type_folders`, default `[Character, Prop, Environment,
Vehicle, FX]`, matching CLAUDE_INSTRUCTIONS.md's asset type conventions).
Whichever type folder a delivery is dropped into **is** its `sg_asset_type`
-- nothing is guessed from filenames, geometry, or content:

```
incoming/
  Prop/
    HeroChair.fbx                  -> ingested as sg_asset_type=Prop
    HeroLamp_delivery/             -> folder delivery, optional manifest.yml
  Character/
    Villain.fbx                    -> ingested as sg_asset_type=Character
```

At startup, `watch_folder.py` reads ShotGrid's live `Asset.sg_asset_type`
schema and refuses to start if any configured folder name doesn't match a
real option there -- a typo'd folder can't silently mis-type every asset
dropped into it. A `manifest.yml` inside a delivery folder can still
override the type explicitly for the rare one-off delivery that needs to
differ from its folder; doing so is logged.

Anything dropped loose at the `incoming/` root (not inside a recognized
type folder) is left alone and logged as a warning on every poll -- it is
never ingested with a guessed type.

`incoming` itself is a plain filesystem path (`pipeline/ingest_turntable/config.yml`
`watch_folder.*_path`), not a Toolkit root -- see `core/roots_additions.yml`
for why that's deliberate.

## qt_watcher integration

You already have a QT-baking watcher running in production:
[`russelling/BUF_Mac_watcher`](https://github.com/russelling/BUF_Mac_watcher)
("Render monitor and qt renderer for exr files") -- a macOS LaunchAgent
(`com.buffalovfx.qtwatcher`) that polls `shots/` and `assets/` for
`.render_complete_*.json` flag files, bakes the EXR sequence through your
full color pipeline (ACEScg → LogC4 → CDL → Show LUT → Rec.709), builds a
branded slate + burn-ins, encodes ProRes 422 HQ, and uploads the result to
ShotGrid as a Version. It already has a dedicated code path for asset
turntables (`type: "asset_turntable"` in the flag) -- so far fed by
`publish_turntable_unreal.py` for Unreal-rendered turntables, resolving
output paths through templates **that already exist** in
`core/templates.yml`:

```yaml
# Turntable EXR render output (Unreal -> qt_watcher -> Nuke bake)
unreal_asset_turntable_render:
    definition: '@asset_root/render/work/{Asset}_turntable_v{version}/{Asset}_turntable_v{version}.{SEQ}.exr'
# Render-complete flag JSON picked up by qt_watcher
unreal_asset_turntable_flag:
    definition: '@asset_root/render/work/{Asset}_turntable_v{version}/.render_complete_{Asset}_turntable_v{version}.json'
# Final baked QT (written by the bake tool)
unreal_asset_turntable_movie:
    definition: '@asset_root/review/{Asset}_turntable_v{version}.mov'
```

**This pipeline does not duplicate any of that, and does not add parallel
templates.** `generate_turntable.py` resolves `unreal_asset_turntable_render`
and `unreal_asset_turntable_flag` directly via `tk.templates[...]` and
reuses them as-is. It stops after rendering EXR frames: it renders the
turntable in Blender, color-converts the frames from Blender's linear
working space to ACEScg with `oiiotool` (same tool qt_watcher's
`qt_bake_oiio.py` already requires), and writes the flag. qt_watcher picks
it up on its own poll cycle (default 30s) and does everything from there:
slate, burn-ins, color, encode, and the ShotGrid Version. This means:

- Vendor-ingested turntables get the exact same branding/slate/burn-in
  treatment as shot renders and Unreal-rendered turntables -- one bake
  pipeline, not two.
- The turntable Version does **not** appear in ShotGrid immediately when
  ingest finishes -- allow up to a poll cycle (~30-60s) for qt_watcher to
  bake and upload it.
- This pipeline registers its own `Turntable Render` PublishedFile for the
  raw EXR sequence (for provenance / re-bake later), but does not create
  the Version or upload the movie -- that stays entirely qt_watcher's job.
- The templates are named `unreal_*` because Unreal MRQ turntables were
  their first producer, but nothing about them is Unreal-specific --
  they're just a folder path and filename pattern. qt_watcher's own code
  only reads the flag's JSON content, never which template or engine wrote
  it. If you'd rather have separately-named `blender_asset_turntable_*`
  templates instead of sharing these, that's a small change (new templates
  here + point `generate_turntable.py` at them); not done by default since
  it'd suggest engine-exclusivity that isn't actually enforced anywhere.

**Flag file placement**: `unreal_asset_turntable_flag` resolves under
`@asset_root`, i.e. under `core/roots.yml`'s `primary` root
(`/Volumes/atv-post-lucid3/atv-buffalo-s03`) + `assets/{sg_asset_type}/{Asset}/...`
-- which matches `ASSETS_ROOT` in `BUF_Mac_watcher/scripts/qt_watcher.py`
(`/Volumes/atv-post-lucid3/atv-buffalo-s03/buffalo_vfx/assets`), so
`os.walk()` on the watcher machine will find it. `generate_turntable.py`
still sanity-checks this at runtime (`config.yml`'s `qt_watcher.assets_root`)
and warns loudly if it ever doesn't line up, e.g. after a `core/roots.yml`
change.

### Color pipeline verification needed

qt_bake_oiio.py's bake assumes every EXR it's handed is scene-linear
**ACEScg**. Blender's default working space is linear Rec.709 primaries,
not ACEScg, so `generate_turntable.py` runs an `oiiotool --colorconvert`
pass (`config.yml` `turntable.blender_linear_colorspace` →
`turntable.target_colorspace`) against the same
`ocio://studio-config-latest` config qt_bake_oiio.py uses. Two things here
are marked for verification in the code (matching qt_bake_oiio.py's own
"UNTESTED" comment style, since I can't confirm your OCIO config's exact
alias names from here):

- `blender_linear_colorspace: lin_rec709` in `config.yml` -- confirm this
  alias actually exists in `ocio://studio-config-latest` and really is
  Blender's default working space alias.
- `generate_turntable.py`'s Blender-side render sets `view_transform =
  'Standard'` / `look = 'None'` on the scene before writing EXR, intended
  to stop Blender's default AgX/Filmic tonemap from being baked into the
  linear pixel data. Verify a rendered frame really is untonemapped linear
  before trusting the color convert step downstream.

If either assumption is wrong, turntables will bake through qt_watcher
without erroring but look subtly off (wrong primaries or double-tonemapped)
compared to shot renders -- test on one asset before relying on this in
production.

## 3ds Max (.max) source support

Blender has no built-in `.max` importer, so this uses the
["Import Autodesk MAX (.max)"](https://extensions.blender.org/add-ons/io-scene-max/)
extension ([github.com/nrgsille76/io_scene_max](https://github.com/nrgsille76/io_scene_max)).
`convert_to_usd.py` drives it generically (enable the add-on, call its
import operator) through three values in `config.yml`'s `ingest.max_import`,
rather than hardcoding this one add-on's API directly, so switching add-ons
later is a config change, not a code change:

```yaml
max_import:
  addon_module: "PLACEHOLDER_SET_ME"   # see below -- the one value that needs your confirmation
  operator: "import_scene.max"
  filepath_arg: "filepath"
```

`operator` and `filepath_arg` are confirmed directly from the add-on's
source (`source/__init__.py`): its `ImportMax` operator's `bl_idname` is
literally `"import_scene.max"`, and it uses Blender's standard
`ImportHelper` mixin, whose file-path property is always `filepath`. Both
are filled in already -- nothing to do there.

`addon_module` is the one value still a placeholder, and it's genuinely
not something I can give you with full confidence from here. This add-on
ships through Blender's newer Extensions system
(`blender_manifest.toml`, `id = "io_scene_max"`) rather than the older
zip-and-enable style -- installed extensions register under a
`bl_ext.<repository_id>.<extension_id>` module name, and the
`repository_id` segment depends on which extensions repository it was
installed from and your Blender version, which I can't see from here. To
get the exact string for the machine `executables.blender` points at:

1. Enable the add-on once interactively in that Blender (Edit →
   Preferences → Add-ons → search "MAX" → enable), then
2. In Blender's Python console, run:
   ```python
   [m.__name__ for m in __import__("addon_utils").modules() if "max" in m.__name__.lower()]
   ```
   and use the string it prints as `addon_module`.

Until that's filled in, ingesting a `.max` delivery raises a clear
`config.yml ingest.max_import.addon_module is not set` error rather than a
confusing failure partway through Blender.

Also worth confirming once `addon_module` is set: whether the extension's
`--background --factory-startup` headless enable actually works the same
as its interactive one on this add-on -- `bpy.ops.preferences.addon_enable()`
works for the general case (factory-startup only skips auto-loading
*previously enabled* add-ons at launch, the extension's files are still on
disk either way), but this specific add-on hasn't been tested end-to-end
in this pipeline. Worth a one-off manual run
(`python3 convert_to_usd.py` can be exercised standalone for this, or just
watch the first real `.max` delivery closely) before trusting it in the
unattended watch-folder loop.

## How a delivery flows through the system

1. Someone drops a downloaded/vendor asset into the right
   `incoming/<Type>/` folder (a file, or a folder optionally containing
   `manifest.yml`).
2. `watch_folder.py` polls the folder, waits until the delivery's size is
   unchanged for `stability_window_seconds` (default 15s) so partial
   copies/downloads aren't ingested mid-transfer.
3. `ingest_asset.py` parses the delivery, finds-or-creates the Asset in
   ShotGrid with `sg_asset_type` from the type folder (or a manifest
   override), and calls `tk.create_filesystem_structure()` so the folder
   schema is created exactly the way Toolkit creates it everywhere else in
   this repo.
4. The raw delivery is copied into the asset's existing `source/` area
   (`asset_source_area` -- "raw scans, purchased assets, original
   deliverables", already in the schema) and registered as a
   `Vendor Delivery` PublishedFile, so the original vendor file is always
   retrievable.
5. `convert_to_usd.py` launches Blender headless to import the fbx/obj/abc/
   gltf and export USD (files already in USD/USDA/USDC/USDZ are passed
   through). The result is published to the asset's existing shared
   `publish/` area as `{Asset}_ingest_v{version}.usd` and registered as a
   `USD Asset` PublishedFile.
6. `generate_turntable.py` launches Blender headless again: imports the
   published USD, builds a three-point-lit turntable rig
   (`turntable_scene_template.py`), spins the asset 360° over the
   configured frame range, and renders a scene-linear EXR sequence to the
   path `unreal_asset_turntable_render` already defines
   (`render/work/{Asset}_turntable_v{version}/`).
7. Each frame is color-converted to ACEScg with `oiiotool`, the sequence is
   registered as a `Turntable Render` PublishedFile, and the
   `unreal_asset_turntable_flag` JSON is written next to the frames.
8. **qt_watcher** (already running, unmodified) finds the flag on its next
   poll, bakes the QT (slate, burn-ins, color, ProRes), and uploads it to
   ShotGrid as a Version linked to the Asset, at the
   `unreal_asset_turntable_movie` path.
9. The original delivery is archived under `incoming/_processed/<Type>/`
   (or `incoming/_failed/<Type>/` with an error log alongside, if any step
   raised).

## Merging into FlowTrackingConfig

1. `core/templates_ingest_turntable.yml` → copy its `keys:` entries into
   `core/templates.yml`'s `keys:` block, and its two `paths:` entries into
   the `paths:` block. That's the entire diff to this file -- everything
   else it needs already exists there.
2. `core/schema/project/assets/asset_type/asset/SCHEMA_ADDITIONS.md` →
   add `render/`, `render/work/`, and `review/` (each `type: "static"`,
   matching the pattern in the existing `source.yml`/`publish.yml`). This
   must happen before step 1 takes effect (rule: "Templates before
   schema"). Ignore the leftover `work/ingest`, `publish/ingest`,
   `work/turntable`, `review/turntable` folders also included in this
   delivery -- unused, explained in that file.
3. Copy `pipeline/ingest_turntable/` into the repo as-is (e.g. at the repo
   root, alongside `core/` and `env/`) -- it's a standalone automation tool,
   not a Toolkit engine/app, so it doesn't need an `env/*.yml` entry.
4. `CLAUDE_INSTRUCTIONS_ADDITIONS.md` → paste into `CLAUDE_INSTRUCTIONS.md`
   so future sessions know qt_watcher/BUF_Mac_watcher exists and that this
   pipeline depends on it. Also worth fixing there while you're in it: the
   documented repo layout (`config/paths.yml`, per-DCC `publish/{maya,nuke,blender}/`
   folders) doesn't match the live repo (`env/includes/software_paths.yml`
   for DCC paths, a single shared `publish/` folder for assets) -- see that
   file's notes.
5. `config/paths_additions.yml` and `core/roots_additions.yml` -- **skip
   both**, they're explanatory-only in this delivery (why no change is
   needed there), not things to merge.

## ShotGrid setup required before first run

Per rule "Step codes are sacred" this pipeline does **not** auto-create
Pipeline Steps -- it will raise an error instead if a step is missing so
you can decide deliberately:

- Add two new Asset Pipeline Steps: `ingest` and `turntable` (short codes
  must match `ingest.step_code` / `turntable.step_code` in `config.yml`).
- Add PublishedFileTypes: `Vendor Delivery`, `USD Asset`, and
  `Turntable Render` (Admin → PublishedFileType). qt_watcher's own upload
  path doesn't need a PublishedFileType -- it just creates a Version.
- Confirm your Asset Type list (`sg_asset_type`) includes every value in
  `config.yml`'s `ingest.asset_type_folders`
  (`Character, Prop, Environment, Vehicle, FX` by default). The watcher
  checks this at startup and refuses to run on a mismatch.

## Environment setup

- **Credentials**: nothing to set up. `sg_utils.get_sgtk()` bootstraps via
  `sgtk.sgtk_from_path(shotgrid.pipeline_config_path)` -- the same call
  `qt_watcher.py`'s `get_sgtk()` makes -- which reads whatever credentials
  the Toolkit pipeline configuration itself already has configured. No
  ShotGrid API script key, no `SG_SERVER`/`SG_SCRIPT_NAME`/`SG_SCRIPT_KEY`
  environment variables, nothing credential-shaped in this repo at all.
- **Interpreter**: use the same Python `qt_watcher.py` runs with (its
  LaunchAgent uses `/Applications/Shotgun.app/Contents/Resources/Python3/bin/python3`),
  not your shell's plain `python3` -- a generic system/Homebrew Python won't
  have `sgtk` on its path, and installing packages into the wrong
  interpreter is a common source of `ModuleNotFoundError` here. See
  `pipeline/ingest_turntable/launchd/` for a LaunchAgent that hardcodes the
  right one so this is a non-issue once it's running as a service.
- **Python deps**: install into that same interpreter:
  `/Applications/Shotgun.app/Contents/Resources/Python3/bin/python3 -m pip install -r pipeline/ingest_turntable/requirements.txt --break-system-packages`
  (see that file for the `shotgun_api3` note -- it isn't on PyPI, and may
  already be vendored by your `tk-core` install, in which case you can skip it).
- **oiiotool**: `brew install openimageio` (macOS) -- same dependency
  `BUF_Mac_watcher/watcher_launch.txt` already installs, so if qt_watcher is
  set up on this machine you likely have it.
- **Blender**: confirm `env/includes/software_paths.yml`'s `path.mac.blender`
  points at a real install (it's currently a placeholder), and match
  `pipeline/ingest_turntable/config.yml`'s `executables.blender` to it.
- **Running it**: see `pipeline/ingest_turntable/launchd/LAUNCH_README.md`
  for the LaunchAgent (recommended -- survives logout/reboot, matches how
  qt_watcher itself runs). For a one-off manual ingest without the watcher,
  use `python3 pipeline/ingest_turntable/ingest_asset.py --path
  <file_or_folder> --project-id <id>` (asset type is inferred from the
  parent folder name, or pass `--asset-type` explicitly) with the same
  interpreter/PYTHONPATH as above.

## Config you'll want to change immediately

In `pipeline/ingest_turntable/config.yml`:
- `shotgrid.pipeline_config_path` and `shotgrid.project_id`
- `ingest.asset_type_folders` if your Asset Type list differs from the
  CLAUDE_INSTRUCTIONS.md default
- `executables.blender` / `executables.oiiotool` -- confirm against
  `env/includes/software_paths.yml` and your actual `oiiotool` install
- `turntable.blender_linear_colorspace` -- see "Color pipeline verification
  needed" above, don't skip this
- `ingest.max_import.addon_module` -- the only remaining placeholder for
  `.max` support (`operator`/`filepath_arg` are already filled in), see
  "3ds Max (.max) source support" above for the one-time lookup. Ingesting
  a `.max` file fails with a clear error until it's set; every other
  extension is unaffected.
- `watch_folder.linux_path` / `windows_path` and `qt_watcher.assets_root`
  linux/windows values are unconfirmed placeholders (only the mac_path
  values were verified against the live repo) -- fix if you run on those
  platforms

## Known limitations / follow-ups

- Color pipeline (Blender linear → ACEScg) is implemented but flagged for
  verification against your actual OCIO config -- see above.
- Large vendor deliveries (many GB) will make the size-polling stability
  check slow to converge; consider raising `poll_interval_seconds` and
  `stability_window_seconds` for those.
- No retry/backoff on ShotGrid API or subprocess failures -- a failed
  delivery goes straight to `incoming/_failed/<Type>/` with an error log
  for a human to look at and re-drop.
- qt_watcher's asset turntable path was written for Unreal MRQ output
  (`publish_turntable_unreal.py`); this pipeline is the first non-Unreal
  producer of that flag schema. Test end-to-end on one asset before
  trusting it for a real delivery batch.
- `render/`, `render/work/`, and `review/` not existing yet in the asset
  schema despite `unreal_asset_turntable_*` already referencing them
  suggests the Unreal turntable path may not be fully wired up/tested
  either -- worth checking with whoever built `publish_turntable_unreal.py`.
- Add this to the WIP/Known Gaps list in `CLAUDE_INSTRUCTIONS.md` once
  merged, and remove it once you're comfortable it's stable in production.
