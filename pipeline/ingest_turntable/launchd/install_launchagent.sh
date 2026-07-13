# Ingest + Turntable watcher installer -- run with: bash install_launchagent.sh
#
# Mirrors BUF_Mac_watcher/watcher_launch.txt's structure and checks, for the
# same reason that file does them: verify every binary the watcher needs
# actually exists BEFORE wiring up a LaunchAgent that will otherwise fail
# silently on every launch attempt (KeepAlive=true means launchd just keeps
# relaunching a script that immediately crashes, burning CPU and filling
# the error log instead of telling you what's actually wrong up front).

REPO_DIR="/Volumes/atv-post-lucid3/atv-buffalo-s03/buffalo_vfx/buffalo_flow_config/config/pipeline/ingest_turntable"
SG_PYTHON="/Applications/Shotgun.app/Contents/Resources/Python3/bin/python3"
PLIST_SRC="$REPO_DIR/launchd/com.buffalovfx.ingestturntable.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.buffalovfx.ingestturntable.plist"
LOG_DIR="/Volumes/atv-post-lucid3/atv-buffalo-s03/buffalo_vfx/buffalo_flow_config/logs"

# ---------------------------------------------------------------------------
# 1. Verify every binary/path the watcher references actually exists
# ---------------------------------------------------------------------------
FAIL=0

check() {
    if [ -e "$1" ]; then
        echo "OK: $1"
    else
        echo "MISSING: $1   ($2)"
        FAIL=1
    fi
}

check "$SG_PYTHON"                          "Shotgun/Flow desktop app - launch it once if missing"
check "$REPO_DIR/watch_folder.py"           "this pipeline's own code - confirm REPO_DIR above matches where you merged it"
check "$REPO_DIR/config.yml"                "pipeline config -- confirm shotgrid.pipeline_config_path, project_id, etc. are filled in before continuing"

# oiiotool / blender paths come from config.yml itself (per-OS), not
# hardcoded here -- read them out with the same python so this check can't
# drift from what watch_folder.py will actually use.
OIIOTOOL_PATH=$("$SG_PYTHON" -c "import sys; sys.path.insert(0, '$REPO_DIR'); import sg_utils; print(sg_utils.get_executable(sg_utils.load_config(), 'oiiotool'))" 2>/dev/null)
BLENDER_PATH=$("$SG_PYTHON" -c "import sys; sys.path.insert(0, '$REPO_DIR'); import sg_utils; print(sg_utils.get_executable(sg_utils.load_config(), 'blender'))" 2>/dev/null)

if [ -n "$OIIOTOOL_PATH" ]; then
    check "$OIIOTOOL_PATH" "config.yml executables.oiiotool -- brew install openimageio"
else
    echo "WARNING: could not read executables.oiiotool from config.yml (is PyYAML installed into \$SG_PYTHON yet? see step 2)"
fi
if [ -n "$BLENDER_PATH" ]; then
    check "$BLENDER_PATH" "config.yml executables.blender"
else
    echo "WARNING: could not read executables.blender from config.yml"
fi

# ---------------------------------------------------------------------------
# 2. Python deps into the Shotgun-bundled interpreter (NOT system/Homebrew python3)
# ---------------------------------------------------------------------------
echo "Installing Python dependencies into $SG_PYTHON ..."
"$SG_PYTHON" -m pip install PyYAML --break-system-packages || FAIL=1

if [ "$FAIL" -ne 0 ]; then
    echo ""
    echo "One or more dependencies/paths are missing (see above). Fix them, then re-run:"
    echo "    bash install_launchagent.sh"
    exit 1
fi

# ---------------------------------------------------------------------------
# 3. Logs folder + LaunchAgent
# ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

cp "$PLIST_SRC" "$PLIST_DEST"
plutil -lint "$PLIST_DEST" || exit 1

launchctl unload "$PLIST_DEST" 2>/dev/null

launchctl load "$PLIST_DEST"

launchctl list | grep ingestturntable
