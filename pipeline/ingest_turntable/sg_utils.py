"""
sg_utils.py -- shared ShotGrid / Toolkit helpers for the ingest + turntable
pipeline.

Authentication: bootstrapped via `sgtk.sgtk_from_path()` against the
Toolkit pipeline configuration itself (config.yml `shotgrid.pipeline_config_path`),
exactly the same call BUF_Mac_watcher/scripts/qt_watcher.py's `get_sgtk()`
already makes. This reads whatever credentials that Toolkit install's own
core config already has set up -- no separate ShotGrid API script or
SG_SCRIPT_NAME/SG_SCRIPT_KEY environment variables needed, and nothing
credential-shaped stored in this repo (satisfies CLAUDE_INSTRUCTIONS.md
rule "No credentials or real paths in commits" by having no credentials
here at all, rather than by moving them to env vars).

Call `get_sgtk(config)` once per process (watch_folder.py does this at
startup) and thread the returned `(tk, sg)` through rather than
re-bootstrapping per delivery.
"""
from __future__ import annotations

import platform
import logging
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger("ingest_turntable")

CONFIG_PATH = Path(__file__).parent / "config.yml"


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def platform_key() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "mac"
    if system == "windows":
        return "windows"
    return "linux"


def resolve_platform_path(entry: dict, key_prefix: str = "") -> str:
    """Resolve one of {linux,mac,windows}_path / {linux,mac,windows} keys
    for the current OS, matching the linux_path/mac_path/windows_path
    convention used throughout FlowTrackingConfig's roots.yml / paths.yml."""
    plat = platform_key()
    for candidate in (f"{key_prefix}{plat}_path", plat):
        if candidate in entry:
            return entry[candidate]
    raise KeyError(f"No path defined for platform '{plat}' in {entry!r}")


def get_executable(config: dict, name: str) -> str:
    plat = platform_key()
    return config["executables"][name][plat]


def get_watch_folder(config: dict) -> Path:
    return Path(resolve_platform_path(config["watch_folder"]))


def get_log_dir(config: dict) -> Path:
    plat = platform_key()
    return Path(config["logging"][f"log_dir_{plat}"])


def get_sgtk(config: Optional[dict] = None):
    """Bootstrap sgtk + a Shotgun connection from the pipeline configuration
    itself -- same call qt_watcher.py's get_sgtk() makes
    (`sgtk.sgtk_from_path(CONFIG_PATH)`), so it reuses whatever credentials
    that Toolkit install already has configured. Returns (tk, sg). Call once
    per process; both `tk` (for template resolution / folder creation) and
    `sg` (for direct API calls) get threaded through the rest of this
    pipeline from here rather than re-bootstrapped per delivery."""
    import sgtk

    config = config or load_config()
    config_path = resolve_platform_path(config["shotgrid"]["pipeline_config_path"])
    tk = sgtk.sgtk_from_path(config_path)
    sg = tk.shotgun
    return tk, sg


def find_or_create_asset(
    sg: Any,
    project_id: int,
    asset_code: str,
    asset_type: str,
) -> dict:
    existing = sg.find_one(
        "Asset",
        [["project", "is", {"type": "Project", "id": project_id}], ["code", "is", asset_code]],
        ["code", "sg_asset_type"],
    )
    if existing:
        log.info("Found existing Asset %s (id=%s)", asset_code, existing["id"])
        return existing

    log.info("Creating new Asset %s [%s]", asset_code, asset_type)
    return sg.create(
        "Asset",
        {
            "project": {"type": "Project", "id": project_id},
            "code": asset_code,
            "sg_asset_type": asset_type,
        },
    )


def find_or_create_task(
    sg: Any,
    entity: dict,
    project_id: int,
    step_code: str,
) -> Optional[dict]:
    """Find (or create) a Task on `entity` for the named Pipeline Step. Steps
    must already exist in ShotGrid -- per CLAUDE_INSTRUCTIONS.md rule "Step
    codes are sacred", this will raise rather than silently invent a step."""
    step = sg.find_one("Step", [["short_name", "is", step_code]], ["code", "short_name"])
    if not step:
        raise ValueError(
            f"Pipeline Step '{step_code}' does not exist in ShotGrid. "
            f"Create it (Shot/Asset step list) before running ingest -- "
            f"see README.md 'ShotGrid setup'."
        )

    task = sg.find_one(
        "Task",
        [["entity", "is", entity], ["step", "is", step]],
        ["content"],
    )
    if task:
        return task

    return sg.create(
        "Task",
        {
            "project": {"type": "Project", "id": project_id},
            "entity": entity,
            "step": step,
            "content": step["code"],
        },
    )


def valid_list_values(sg: Any, entity_type: str, field_name: str) -> Optional[set]:
    """Return the set of valid values for a ShotGrid list field, or None if
    it can't be determined. Same pattern BUF_Mac_watcher's qt_watcher.py uses
    (_valid_list_values) to avoid setting a list field to an unconfigured
    option -- used here at watcher startup to verify config.yml's
    ingest.asset_type_folders actually match ShotGrid's live sg_asset_type
    schema, so a typo'd folder name fails loudly instead of silently
    mis-typing every Asset dropped into it."""
    try:
        schema = sg.schema_field_read(entity_type, field_name)
        props = schema.get(field_name, {}).get("properties", {})
        valid = props.get("valid_values", {}).get("value")
        if valid:
            return set(valid)
    except Exception as exc:
        log.warning("Could not read schema for %s.%s: %s", entity_type, field_name, exc)
    return None


def find_or_create_published_file_type(sg: Any, code: str) -> dict:
    """Look up a PublishedFileType by its `code` field (PublishedFileType's
    display name is stored in `code`, not `name` -- passing {"type":
    "PublishedFileType", "name": ...} directly into sg.create()'s linked
    fields fails with 'invalid/missing entity hash integer id', since
    ShotGrid entity links require a real `id`, not a `name`/`code`). Creates
    the type if it doesn't already exist -- PublishedFileType entities are
    usually pre-seeded by ShotGrid/Toolkit setup, but this avoids a hard
    failure on a studio that hasn't added "Vendor Delivery" / "USD Asset" /
    "Turntable Render" yet."""
    existing = sg.find_one("PublishedFileType", [["code", "is", code]])
    if existing:
        return existing
    log.info("Creating new PublishedFileType '%s' (did not already exist in ShotGrid)", code)
    return sg.create("PublishedFileType", {"code": code})


def next_version_number(sg: Any, entity: dict, published_file_type: str) -> int:
    existing = sg.find(
        "PublishedFile",
        [["entity", "is", entity], ["published_file_type.PublishedFileType.code", "is", published_file_type]],
        ["version_number"],
        order=[{"field_name": "version_number", "direction": "desc"}],
    )
    return (existing[0]["version_number"] + 1) if existing else 1
