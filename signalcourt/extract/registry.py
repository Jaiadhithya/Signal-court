"""Loads the extraction registries. ``REGISTRY_DIR`` env var overrides the packaged location."""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Any

import yaml


def registry_dir() -> Path:
    env = os.environ.get("SIGNAL_REGISTRY_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "registry").is_dir():
            return p / "registry"
    raise FileNotFoundError("registry/ directory not found")


@cache
def load_yaml(name: str) -> Any:
    with open(registry_dir() / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def metrics() -> list[dict[str, Any]]:
    return load_yaml("metrics.yaml")


def metric_by_id() -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in metrics()}
