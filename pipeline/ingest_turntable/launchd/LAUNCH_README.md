# Ingest + Turntable Watcher — Launch Instructions

Same pattern as `BUF_Mac_watcher/QT_Watcher_README.md` -- this watcher runs
as a **macOS LaunchAgent** (`com.buffalovfx.ingestturntable`) that executes
`watch_folder.py` using the Shotgun/Flow desktop app's bundled Python 3.

## Everyday launch (agent already installed)

**Option A — Terminal**

```bash
launchctl load ~/Library/LaunchAgents/com.buffalovfx.ingestturntable.plist
```

Because the plist has `RunAtLoad` and `KeepAlive` set to `true`, the watcher
also starts automatically on login/reboot and relaunches itself if it
crashes -- you generally only need to run the load command after it's been
unloaded (e.g. after `launchctl unload`, or a fresh machine setup).

**Option B — desktop launcher**

If you want a double-click launcher like `qt_watcher`'s
`start_qt_watcher.command`, copy that file's pattern:

```bash
cat << 'EOF' > ~/Desktop/start_ingest_turntable.command
#!/bin/bash
launchctl load ~/Library/LaunchAgents/com.buffalovfx.ingestturntable.plist
echo "Ingest + Turntable watcher started."
EOF
chmod +x ~/Desktop/start_ingest_turntable.command
```

## Checking it's running

```bash
launchctl list | grep ingestturntable
```

Logs:

- Output: `/Volumes/atv-post-lucid3/atv-buffalo-s03/buffalo_vfx/buffalo_flow_config/logs/ingest_turntable_watcher.log`
- Errors: `/Volumes/atv-post-lucid3/atv-buffalo-s03/buffalo_vfx/buffalo_flow_config/logs/ingest_turntable_watcher_error.log`
- Per-delivery ingest errors (separate from the above -- one file per failed
  delivery, written by `watch_folder.py` itself, not launchd):
  `<watch_folder>/_ingest_logs/` (per `config.yml` `logging.log_dir_mac`,
  currently `.../buffalo_vfx/assets/incoming/_ingest_logs/`)

## Stopping it

```bash
launchctl unload ~/Library/LaunchAgents/com.buffalovfx.ingestturntable.plist
```

## One-time setup (new machine, or reinstalling)

1. Confirm `pipeline/ingest_turntable/config.yml` is filled in --
   `shotgrid.pipeline_config_path`, `shotgrid.project_id`,
   `ingest.asset_type_folders`, `executables.blender` /
   `executables.oiiotool`, `turntable.blender_linear_colorspace` -- see the
   main `README.md` "Config you'll want to change immediately". The
   installer below reads some of these to sanity-check paths, so an
   unconfigured `config.yml` will show up as failed checks, not a mystery
   launchd crash loop.
2. In Terminal, run the installer script (it has no shebang, matching
   `watcher_launch.txt`'s convention, so invoke it with `bash`, not `./`):

   ```bash
   cd "/Volumes/atv-post-lucid3/atv-buffalo-s03/buffalo_vfx/buffalo_flow_config/config/pipeline/ingest_turntable/launchd"
   bash install_launchagent.sh
   ```

   This will:
   - verify the Shotgun-bundled Python, this pipeline's own files, and the
     `oiiotool`/`blender` paths `config.yml` points at all actually exist
   - `pip install PyYAML` into the Shotgun-bundled Python specifically (NOT
     your shell's plain `python3`/`pip3` -- see main `README.md`
     "Environment setup" for why that distinction matters on this machine)
   - create the shared logs folder
   - copy `com.buffalovfx.ingestturntable.plist` into
     `~/Library/LaunchAgents/`
   - `launchctl load` it
   - confirm with `launchctl list | grep ingestturntable`

## What the LaunchAgent runs

```
/Applications/Shotgun.app/Contents/Resources/Python3/bin/python3 \
  /Volumes/atv-post-lucid3/atv-buffalo-s03/buffalo_vfx/buffalo_flow_config/config/pipeline/ingest_turntable/watch_folder.py
```

Working directory: `.../ingest_turntable`. `PYTHONPATH` is set in the plist
to `.../buffalo_flow_config/install/core/python` so `import sgtk` resolves
-- same value `com.buffalovfx.qtwatcher.plist` uses.

No credentials are set in the plist's `EnvironmentVariables` -- unlike a
typical service, this watcher doesn't need a ShotGrid API script key at
all, since `sg_utils.get_sgtk()` bootstraps via
`sgtk.sgtk_from_path(shotgrid.pipeline_config_path)`, reading whatever
credentials that Toolkit pipeline configuration already has (the exact same
call `qt_watcher.py`'s `get_sgtk()` makes). If `qt_watcher` is already
authenticating fine on this machine, this watcher will too, with no
additional setup.

## Relationship to qt_watcher

This watcher and `com.buffalovfx.qtwatcher` are two independent
LaunchAgents that hand off to each other one-directionally: this one
renders turntable EXRs and writes a `.render_complete_*.json` flag;
`qt_watcher` (already installed, unrelated setup) polls for that flag and
does the actual QT baking/upload. Both need to be running for a vendor
delivery to end up as a reviewable Version in ShotGrid -- if turntables
never show up, check `qt_watcher`'s status too
(`launchctl list | grep qtwatcher`), not just this one.
