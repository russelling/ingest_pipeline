"""
watch_folder.py -- polls the `incoming` drop folder for new external
downloads / vendor deliveries and runs each one through ingest_asset.py.

Asset typing: `incoming` is expected to contain one standing subfolder per
Asset Type (config.yml ingest.asset_type_folders, e.g. Prop/, Character/,
Environment/, Vehicle/, FX/). Whatever type folder a delivery is dropped
into IS its sg_asset_type -- there's no guessing from filenames or content.
At startup this script verifies those folder names against ShotGrid's live
sg_asset_type schema and refuses to start on a mismatch, rather than
silently mis-typing every asset dropped into a typo'd folder.

Polling (rather than an OS filesystem-event watcher) is deliberate: the
watch folder typically lives on a network share (SMB/NFS), where
inotify/FSEvents-style watchers are unreliable or unavailable. Polling
works the same everywhere.

Run as a long-lived process (systemd unit / Windows service / supervisord):
    python watch_folder.py

Layout:
    incoming/
      Prop/
        HeroChair.fbx                  <- ingested as sg_asset_type=Prop
        HeroLamp_delivery/             <- folder delivery, optional manifest.yml
      Character/
        Villain.fbx                    <- ingested as sg_asset_type=Character
      _processed/<Type>/<timestamp>_<name>   <- archived after success
      _failed/<Type>/<timestamp>_<name>      <- archived after failure, + .error.log
      _ingest_logs/

Anything dropped directly in `incoming/` (not inside a recognized type
folder) is left alone and logged as a warning every poll -- it is NOT
ingested with a guessed type.
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

import sg_utils
from ingest_asset import ingest_delivery, IngestError

log = logging.getLogger("ingest_turntable.watch_folder")

PROCESSED_DIRNAME = "_processed"
FAILED_DIRNAME = "_failed"
RESERVED_TOP_LEVEL = {PROCESSED_DIRNAME, FAILED_DIRNAME, "_ingest_logs"}


def _entry_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _is_stable(path: Path, seen: dict, stability_window: int) -> bool:
    now = time.time()
    size = _entry_size(path)
    key = str(path)
    prev = seen.get(key)

    if prev is None or prev["size"] != size:
        seen[key] = {"size": size, "first_seen_stable": now}
        return False

    return (now - prev["first_seen_stable"]) >= stability_window


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _move_to(entry: Path, dest_root: Path, asset_type: str) -> Path:
    dest_dir = dest_root / asset_type
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{_timestamp()}_{entry.name}"
    shutil.move(str(entry), str(dest))
    return dest


def verify_asset_type_folders(sg, asset_type_folders: list[str]) -> None:
    """Refuse to start if config.yml's asset_type_folders don't match
    ShotGrid's live sg_asset_type list -- a typo here would silently mis-type
    every asset dropped into that folder, so this fails loudly instead."""
    valid = sg_utils.valid_list_values(sg, "Asset", "sg_asset_type")
    if valid is None:
        log.warning(
            "Could not read Asset.sg_asset_type schema from ShotGrid -- "
            "skipping asset_type_folders verification. Typos in config.yml "
            "will silently mis-type assets until this is checked manually."
        )
        return

    unknown = [t for t in asset_type_folders if t not in valid]
    if unknown:
        raise SystemExit(
            f"config.yml ingest.asset_type_folders contains {unknown}, which "
            f"{'is' if len(unknown) == 1 else 'are'} not in ShotGrid's "
            f"Asset.sg_asset_type options {sorted(valid)}. Fix config.yml or "
            f"add the type(s) in ShotGrid before starting the watcher."
        )
    log.info("Verified asset_type_folders against ShotGrid: %s", asset_type_folders)


def _iter_deliveries(watch_dir: Path, asset_type_folders: list[str]):
    """Yield (entry_path, asset_type) for every top-level entry inside a
    recognized type folder. Warns (once per poll) about anything sitting
    loose at the watch-folder root."""
    for child in sorted(watch_dir.iterdir()):
        if child.name in RESERVED_TOP_LEVEL or child.name.startswith("."):
            continue

        if child.is_dir() and child.name in asset_type_folders:
            for entry in sorted(child.iterdir()):
                if entry.name.startswith("."):
                    continue
                yield entry, child.name
        elif child.is_dir():
            log.warning(
                "Ignoring top-level folder '%s' in %s -- not a recognized "
                "asset type folder (expected one of %s)",
                child.name, watch_dir, asset_type_folders,
            )
        else:
            log.warning(
                "Ignoring loose file '%s' directly in %s -- deliveries must "
                "be dropped inside a type folder, e.g. %s/%s/%s",
                child.name, watch_dir, watch_dir, asset_type_folders[0], child.name,
            )


def process_once(watch_dir: Path, config: dict, seen_state: dict, tk, sg) -> None:
    processed_dir = watch_dir / PROCESSED_DIRNAME
    failed_dir = watch_dir / FAILED_DIRNAME
    log_dir = sg_utils.get_log_dir(config)
    log_dir.mkdir(parents=True, exist_ok=True)

    asset_type_folders = config["ingest"]["asset_type_folders"]
    live_entries = set()

    for entry, asset_type in _iter_deliveries(watch_dir, asset_type_folders):
        live_entries.add(str(entry))

        if not _is_stable(entry, seen_state, config["watch_folder"]["stability_window_seconds"]):
            log.debug("Waiting for %s to finish copying", entry.name)
            continue

        log.info("Ingesting delivery: %s (asset_type=%s)", entry, asset_type)
        try:
            result = ingest_delivery(entry, asset_type, config, tk=tk, sg=sg)
            moved = _move_to(entry, processed_dir, asset_type)
            log.info(
                "Ingest OK: Asset=%s USD=%s turntable flag=%s (delivery archived at %s)",
                result["asset"]["code"], result["usd_path"], result["flag_path"], moved,
            )
        except (IngestError, Exception) as exc:  # noqa: BLE001 -- top-level loop must not die
            log.exception("Ingest FAILED for %s", entry)
            moved = _move_to(entry, failed_dir, asset_type)
            error_log = log_dir / f"{moved.name}.error.log"
            error_log.write_text(f"{type(exc).__name__}: {exc}\n")
            log.error("Delivery moved to %s, error written to %s", moved, error_log)
        finally:
            seen_state.pop(str(entry), None)

    # Drop stale stability-tracking entries for anything no longer present
    # (e.g. it was moved/deleted out from under us).
    for key in list(seen_state):
        if key not in live_entries:
            seen_state.pop(key, None)


def main():
    config = sg_utils.load_config()
    logging.basicConfig(
        level=getattr(logging, config["logging"]["level"]),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    watch_dir = sg_utils.get_watch_folder(config)
    watch_dir.mkdir(parents=True, exist_ok=True)
    for asset_type in config["ingest"]["asset_type_folders"]:
        (watch_dir / asset_type).mkdir(parents=True, exist_ok=True)

    tk, sg = sg_utils.get_sgtk(config)
    verify_asset_type_folders(sg, config["ingest"]["asset_type_folders"])

    log.info("Watching %s (poll every %ss)", watch_dir, config["watch_folder"]["poll_interval_seconds"])
    seen_state: dict = {}

    while True:
        try:
            process_once(watch_dir, config, seen_state, tk, sg)
        except Exception:
            log.exception("Unexpected error in watch loop -- continuing")
        time.sleep(config["watch_folder"]["poll_interval_seconds"])


if __name__ == "__main__":
    main()
