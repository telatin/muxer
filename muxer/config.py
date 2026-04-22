from __future__ import annotations

import os
import re


DEFAULT_PREFIX = "c-a"
PREFIX_RE = re.compile(r"c-([a-z])$")


def resolve_prefix_binding(value: str | None = None) -> str:
    raw = (value or os.environ.get("MUXER_PREFIX") or DEFAULT_PREFIX).strip().lower()
    if not raw:
        return DEFAULT_PREFIX
    if raw.startswith("^") and len(raw) == 2 and raw[1].isalpha():
        return f"c-{raw[1]}"
    raw = raw.replace("control-", "c-").replace("ctrl-", "c-")
    if PREFIX_RE.fullmatch(raw):
        return raw
    raise ValueError(
        f"unsupported prefix {value!r}; use Ctrl+<letter> such as 'c-a', 'ctrl-b', or '^a'"
    )


def prefix_label(binding: str) -> str:
    match = PREFIX_RE.fullmatch(binding)
    if not match:
        raise ValueError(f"invalid normalized prefix binding {binding!r}")
    return f"Ctrl+{match.group(1).upper()}"


def prefix_bytes(binding: str) -> bytes:
    match = PREFIX_RE.fullmatch(binding)
    if not match:
        raise ValueError(f"invalid normalized prefix binding {binding!r}")
    return bytes([ord(match.group(1)) - 96])
