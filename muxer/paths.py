from __future__ import annotations

import os
from pathlib import Path


APP_DIR_NAME = ".muxer"


def base_dir() -> Path:
    explicit = os.environ.get("MUXER_HOME")
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.home() / APP_DIR_NAME)
    candidates.append(Path(os.environ.get("TMPDIR", "/tmp")) / f"muxer-{os.getuid()}")

    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except PermissionError:
            continue
    raise PermissionError("unable to create a writable muxer state directory")


def runtime_dir() -> Path:
    path = base_dir() / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_dir() -> Path:
    path = base_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def socket_path(session_name: str) -> Path:
    return runtime_dir() / f"{session_name}.sock"


def pid_path(session_name: str) -> Path:
    return runtime_dir() / f"{session_name}.pid"


def log_path(session_name: str) -> Path:
    return runtime_dir() / f"{session_name}.log"


def hook_dir(session_name: str) -> Path:
    path = runtime_dir() / f"{session_name}-hooks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_path(session_name: str) -> Path:
    return session_dir() / f"{session_name}.yaml"
