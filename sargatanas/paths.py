"""Filesystem layout.

Everything the tool writes lives under a single data directory:

    SARGATANAS_HOME   overrides the default (~/Sargatanas)
    SARGATANAS_ISOS   extra ISO search paths, separated by os.pathsep

Keeping every path here means the CLI, the GTK app and the tests agree on
where things live. The previous layout had three different directories
depending on which entry point you ran.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIRNAME = "Sargatanas"
LEGACY_DIRNAMES = ("n01d-machine",)

# Bump when the on-disk config format changes in a way that needs migration.
CONFIG_SCHEMA = 1


def home() -> Path:
    override = os.environ.get("SARGATANAS_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / APP_DIRNAME


def config_file() -> Path:
    return home() / "config.json"


def vm_dir() -> Path:
    return home() / "vms"


def iso_dir() -> Path:
    return home() / "isos"


def log_dir() -> Path:
    return home() / "logs"


def extra_iso_search_paths() -> list[Path]:
    raw = os.environ.get("SARGATANAS_ISOS", "")
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def legacy_home() -> Path:
    return Path.home() / LEGACY_DIRNAMES[0]


def ensure_dirs() -> None:
    for d in (home(), vm_dir(), iso_dir(), log_dir()):
        d.mkdir(parents=True, exist_ok=True)
