from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import resolve_prefix_binding
from .paths import pid_path, runtime_dir, save_path, socket_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="muxer")
    parser.add_argument("--prefix", help="client command prefix, e.g. c-a or c-b")
    subparsers = parser.add_subparsers(dest="command")

    new_parser = subparsers.add_parser("new", help="create a new session and attach")
    new_parser.add_argument("session", nargs="?", default="main")
    new_parser.add_argument("--restore")
    new_parser.add_argument("--prefix", help="client command prefix, e.g. c-a or c-b")

    attach_parser = subparsers.add_parser("attach", help="attach to an existing session")
    attach_parser.add_argument("session", nargs="?", default="main")
    attach_parser.add_argument("--prefix", help="client command prefix, e.g. c-a or c-b")

    save_parser = subparsers.add_parser("save", help="save a running session")
    save_parser.add_argument("session", nargs="?", default="main")
    save_parser.add_argument("path", nargs="?")

    restore_parser = subparsers.add_parser("restore", help="restore a saved session and attach")
    restore_parser.add_argument("path")
    restore_parser.add_argument("--name", default="restored")
    restore_parser.add_argument("--prefix", help="client command prefix, e.g. c-a or c-b")

    kill_parser = subparsers.add_parser("kill", help="stop a session daemon")
    kill_parser.add_argument("session", nargs="?", default="main")

    subparsers.add_parser("ls", help="list runtime sessions")

    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "new"
        args.session = "main"
        args.restore = None
    return args


def spawn_daemon(session_name: str, restore: str | None = None) -> None:
    cmd = [sys.executable, "-m", "muxer.daemon", "--session", session_name]
    if restore:
        cmd.extend(["--restore", restore])
    with open(runtime_dir() / f"{session_name}.daemon.log", "ab") as log:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    wait_for_socket(session_name)


def wait_for_socket(session_name: str, timeout: float = 5.0) -> None:
    sock = socket_path(session_name)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(sock))
                    return
            except OSError:
                time.sleep(0.05)
                continue
        time.sleep(0.05)
    raise SystemExit(f"timed out waiting for session socket {sock}")


def session_running(session_name: str) -> bool:
    sock = socket_path(session_name)
    if not sock.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(sock))
            return True
    except OSError:
        try:
            sock.unlink()
        except OSError:
            pass
        return False


async def send_command(session_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path(session_name)))
    writer.write(json.dumps(payload).encode("utf-8") + b"\n")
    await writer.drain()
    raw = await reader.readline()
    writer.close()
    await writer.wait_closed()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def list_sessions() -> int:
    runtime = socket_path("placeholder").parent
    sockets = sorted(runtime.glob("*.sock"))
    if not sockets:
        print("no running sessions")
        return 0
    for sock in sockets:
        print(sock.stem)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    command = args.command

    if command == "new":
        if not session_running(args.session):
            spawn_daemon(args.session, args.restore)
        return asyncio.run(_attach(args.session, resolve_prefix_binding(args.prefix)))

    if command == "attach":
        if not session_running(args.session):
            raise SystemExit(f"session {args.session!r} is not running")
        return asyncio.run(_attach(args.session, resolve_prefix_binding(args.prefix)))

    if command == "save":
        response = asyncio.run(
            send_command(
                args.session,
                {
                    "type": "save",
                    "path": args.path or str(save_path(args.session)),
                },
            )
        )
        if response.get("ok"):
            print(response["path"])
            return 0
        raise SystemExit(response.get("error") or "save failed")

    if command == "restore":
        if session_running(args.name):
            raise SystemExit(f"session {args.name!r} already exists")
        spawn_daemon(args.name, args.path)
        return asyncio.run(_attach(args.name, resolve_prefix_binding(args.prefix)))

    if command == "kill":
        response = asyncio.run(send_command(args.session, {"type": "kill"}))
        if response.get("ok"):
            sock = socket_path(args.session)
            for _ in range(50):
                if not sock.exists():
                    break
                time.sleep(0.05)
            pid = pid_path(args.session)
            if pid.exists():
                pid.unlink()
            return 0
        raise SystemExit(response.get("error") or "kill failed")

    if command == "ls":
        return list_sessions()

    raise SystemExit(f"unknown command {command}")


async def _attach(session_name: str, prefix_binding: str) -> int:
    from .client import run_client

    await run_client(session_name, prefix_binding)
    return 0
