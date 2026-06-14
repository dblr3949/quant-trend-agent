from __future__ import annotations

import os
from pathlib import Path


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: str | Path, *, override: bool = False) -> list[str]:
    """Load KEY=VALUE pairs from a local env file.

    Returns the keys that were applied. Values are intentionally not returned so
    callers can log the result without leaking secrets.
    """
    env_path = Path(path)
    if not env_path.exists():
        return []

    loaded: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if not key:
            continue
        if key in os.environ and not override:
            continue
        os.environ[key] = _unquote(value.strip())
        loaded.append(key)
    return loaded
