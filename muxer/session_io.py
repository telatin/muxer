from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only when dependency is absent.
    yaml = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump_session(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is not None:
        content = yaml.safe_dump(payload, sort_keys=False)
    else:
        content = json.dumps(payload, indent=2)
    path.write_text(content, encoding="utf-8")


def load_session(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) if yaml is not None else json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"session file {path} is not a YAML mapping")
    return data
