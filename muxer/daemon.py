from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import json
import os
import pty
import re
import signal
import struct
import termios
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pyte

from .paths import hook_dir, pid_path, save_path, socket_path
from .protocol import decode_bytes, send_message
from .session_io import dump_session, load_session, utc_now


OSC7_RE = re.compile(rb"\x1b]7;([^\x07\x1b]+)(?:\x07|\x1b\\)")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\))")
TRANSIENT_ENV_PREFIXES = ("MUXER_",)
TRANSIENT_ENV_KEYS = {"PROMPT_COMMAND", "ZDOTDIR"}


def set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def best_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    return "/bin/sh"


def visible_text(chunk: bytes) -> str:
    text = chunk.decode("utf-8", errors="ignore")
    text = ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return text


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    raw = path.read_bytes()
    result: dict[str, str] = {}
    for part in raw.split(b"\0"):
        if not part or b"=" not in part:
            continue
        key, value = part.split(b"=", 1)
        result[key.decode("utf-8", errors="ignore")] = value.decode("utf-8", errors="ignore")
    return result


def restorable_env(env: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in env.items()
        if key not in TRANSIENT_ENV_KEYS and not key.startswith(TRANSIENT_ENV_PREFIXES)
    }


def shell_bootstrap(
    session_name: str,
    tab_id: int,
    shell_path: str,
    state_file: Path,
) -> tuple[list[str], dict[str, str]]:
    shell_name = Path(shell_path).name
    env = dict(os.environ)
    env["MUXER_SESSION"] = session_name
    env["MUXER_TAB_ID"] = str(tab_id)
    env["MUXER_STATE_FILE"] = str(state_file)
    hooks = hook_dir(session_name)

    if shell_name == "bash":
        rc_path = hooks / f"bash-{tab_id}.rc"
        rc_path.write_text(
            "\n".join(
                [
                    '[ -f ~/.bashrc ] && . ~/.bashrc',
                    '_muxer_precmd() {',
                    '  printf "\\033]7;file://%s%s\\007" "${HOSTNAME:-localhost}" "$PWD"',
                    '  env -0 > "$MUXER_STATE_FILE"',
                    '}',
                    'case ";$PROMPT_COMMAND;" in',
                    '  *";_muxer_precmd;"*) ;;',
                    '  *) PROMPT_COMMAND="_muxer_precmd${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;',
                    'esac',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return [shell_path, "--rcfile", str(rc_path), "-i"], env

    if shell_name == "zsh":
        zdotdir = hooks / f"zsh-{tab_id}"
        zdotdir.mkdir(parents=True, exist_ok=True)
        (zdotdir / ".zshrc").write_text(
            "\n".join(
                [
                    '[ -f ~/.zshrc ] && source ~/.zshrc',
                    'autoload -Uz add-zsh-hook',
                    '_muxer_precmd() {',
                    '  printf "\\033]7;file://%s%s\\007" "${HOST:-localhost}" "$PWD"',
                    '  env -0 > "$MUXER_STATE_FILE"',
                    '}',
                    'add-zsh-hook precmd _muxer_precmd',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        env["ZDOTDIR"] = str(zdotdir)
        return [shell_path, "-i"], env

    return [shell_path, "-i"], env


@dataclass
class TerminalTab:
    session_name: str
    tab_id: int
    name: str
    cwd: str
    launch_env: dict[str, str]
    shell: str
    rows: int
    cols: int
    history_limit: int = 5000
    pid: int | None = None
    fd: int | None = None
    exit_status: int | None = None
    cwd_hint: str | None = None
    transcript: deque[str] = field(default_factory=lambda: deque(maxlen=5000))
    partial_line: str = ""
    osc_buffer: bytes = b""

    def __post_init__(self) -> None:
        self.screen = pyte.HistoryScreen(self.cols, self.rows, history=self.history_limit)
        self.stream = pyte.ByteStream(self.screen)
        self.state_file = hook_dir(self.session_name) / f"tab-{self.tab_id}.env"

    @property
    def alive(self) -> bool:
        return self.pid is not None and self.fd is not None and self.exit_status is None

    def spawn(self) -> None:
        argv, env = shell_bootstrap(self.session_name, self.tab_id, self.shell, self.state_file)
        child_env = dict(env)
        child_env.update(self.launch_env)
        child_env.setdefault("TERM", "xterm-256color")
        self.launch_env = dict(child_env)

        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.cwd)
            except OSError:
                os.chdir(Path.home())
            set_winsize(0, self.rows, self.cols)
            os.execvpe(argv[0], argv, child_env)

        self.pid = pid
        self.fd = fd
        set_nonblocking(fd)
        self.cwd_hint = self.cwd

    def feed_output(self, chunk: bytes) -> None:
        self.osc_buffer = (self.osc_buffer + chunk)[-4096:]
        for match in OSC7_RE.finditer(self.osc_buffer):
            payload = match.group(1).decode("utf-8", errors="ignore")
            if payload.startswith("file://"):
                parsed = urlparse(payload)
                if parsed.path:
                    self.cwd_hint = unquote(parsed.path)

        self.stream.feed(chunk)

        text = visible_text(chunk)
        if not text:
            return
        combined = self.partial_line + text
        parts = combined.split("\n")
        self.partial_line = parts.pop() if parts else ""
        for line in parts:
            self.transcript.append(line.rstrip())

    def write_input(self, data: bytes) -> None:
        if self.fd is None or self.exit_status is not None:
            return
        os.write(self.fd, data)

    def resize(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self.screen.resize(lines=rows, columns=cols)
        if self.fd is not None:
            set_winsize(self.fd, rows, cols)

    def scroll_up(self) -> None:
        self.screen.prev_page()

    def scroll_down(self) -> None:
        self.screen.next_page()

    def state_env(self) -> dict[str, str]:
        env = parse_env_file(self.state_file)
        if env:
            return env
        return dict(self.launch_env)

    def snapshot(self) -> dict[str, Any]:
        current_env = self.state_env()
        current_cwd = current_env.get("PWD") or self.cwd_hint or self.cwd
        title = self.name
        if self.exit_status is not None:
            title = f"{self.name} [dead]"
        return {
            "id": self.tab_id,
            "name": title,
            "alive": self.exit_status is None,
            "cwd": current_cwd,
        }

    def serialize(self) -> dict[str, Any]:
        current_env = restorable_env(self.state_env())
        return {
            "id": self.tab_id,
            "name": self.name,
            "cwd": current_env.get("PWD") or self.cwd_hint or self.cwd,
            "env": current_env,
            "shell": self.shell,
            "history_tail": list(self.transcript)[-50:],
        }


class SessionDaemon:
    def __init__(
        self,
        session_name: str,
        rows: int = 24,
        cols: int = 120,
        restore_file: Path | None = None,
    ) -> None:
        self.session_name = session_name
        self.rows = rows
        self.cols = cols
        self.restore_file = restore_file
        self.tabs: list[TerminalTab] = []
        self.active_index = 0
        self.next_tab_id = 0
        self.message = ""
        self.clients: set[asyncio.StreamWriter] = set()
        self.server: asyncio.base_events.Server | None = None
        self.loop = asyncio.get_running_loop()
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        sock = socket_path(self.session_name)
        if sock.exists():
            sock.unlink()

        self._write_pid()
        if self.restore_file:
            self.restore_from_file(self.restore_file)
        else:
            self.create_tab()

        self.server = await asyncio.start_unix_server(self._handle_client, path=str(sock))

        for sig in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(sig, self.stopping.set)

        await self.stopping.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

        for writer in list(self.clients):
            writer.close()
            await writer.wait_closed()

        for tab in self.tabs:
            if tab.fd is not None:
                self.loop.remove_reader(tab.fd)
                try:
                    os.close(tab.fd)
                except OSError:
                    pass
            if tab.pid is not None and tab.exit_status is None:
                try:
                    os.kill(tab.pid, signal.SIGHUP)
                except OSError:
                    pass

        sock = socket_path(self.session_name)
        if sock.exists():
            sock.unlink()
        pid = pid_path(self.session_name)
        if pid.exists():
            pid.unlink()

    def _write_pid(self) -> None:
        pid_path(self.session_name).write_text(str(os.getpid()), encoding="utf-8")

    def create_tab(
        self,
        *,
        name: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        shell: str | None = None,
    ) -> TerminalTab:
        tab = TerminalTab(
            session_name=self.session_name,
            tab_id=self.next_tab_id,
            name=name or f"term-{len(self.tabs) + 1}",
            cwd=cwd or os.getcwd(),
            launch_env=env or {},
            shell=shell or best_shell(),
            rows=self.rows,
            cols=self.cols,
        )
        self.next_tab_id += 1
        tab.spawn()
        self.tabs.append(tab)
        self.active_index = len(self.tabs) - 1
        self.loop.add_reader(tab.fd, self._drain_tab, tab)
        return tab

    def restore_from_file(self, path: Path) -> None:
        payload = load_session(path)
        terminals = payload.get("terminals") or payload.get("panes") or []
        if not isinstance(terminals, list):
            raise ValueError("session file does not contain a terminal list")
        if not terminals:
            self.create_tab()
            return
        for entry in terminals:
            if not isinstance(entry, dict):
                continue
            self.create_tab(
                name=str(entry.get("name") or f"term-{len(self.tabs) + 1}"),
                cwd=str(entry.get("cwd") or os.getcwd()),
                env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                shell=str(entry.get("shell") or best_shell()),
            )
        self.active_index = min(self.active_index, len(self.tabs) - 1)
        self.message = f"restored {path}"

    def _drain_tab(self, tab: TerminalTab) -> None:
        if tab.fd is None:
            return
        try:
            while True:
                chunk = os.read(tab.fd, 65536)
                if not chunk:
                    raise OSError(errno.EIO, "pty closed")
                tab.feed_output(chunk)
        except BlockingIOError:
            pass
        except OSError:
            self._mark_tab_dead(tab)
        self._schedule_broadcast()

    def _mark_tab_dead(self, tab: TerminalTab) -> None:
        if tab.fd is not None:
            self.loop.remove_reader(tab.fd)
            try:
                os.close(tab.fd)
            except OSError:
                pass
            tab.fd = None
        if tab.pid is not None and tab.exit_status is None:
            try:
                _, status = os.waitpid(tab.pid, os.WNOHANG)
            except ChildProcessError:
                status = 0
            tab.exit_status = status

    def switch_tab(self, direction: int) -> None:
        if not self.tabs:
            return
        self.active_index = (self.active_index + direction) % len(self.tabs)

    def select_tab(self, index: int) -> bool:
        if 0 <= index < len(self.tabs):
            self.active_index = index
            return True
        return False

    def active_tab(self) -> TerminalTab | None:
        if not self.tabs:
            return None
        return self.tabs[self.active_index]

    def rename_active_tab(self, name: str) -> bool:
        tab = self.active_tab()
        cleaned = name.strip()
        if tab is None or not cleaned:
            return False
        tab.name = cleaned
        self.message = f"renamed to {cleaned}"
        return True

    def _close_tab(self, tab: TerminalTab) -> None:
        if tab.fd is not None:
            self.loop.remove_reader(tab.fd)
            try:
                os.close(tab.fd)
            except OSError:
                pass
            tab.fd = None
        if tab.pid is not None and tab.exit_status is None:
            try:
                os.killpg(tab.pid, signal.SIGHUP)
            except OSError:
                try:
                    os.kill(tab.pid, signal.SIGHUP)
                except OSError:
                    pass
            try:
                _, status = os.waitpid(tab.pid, os.WNOHANG)
            except ChildProcessError:
                status = 0
            tab.exit_status = status

    def kill_active_tab(self) -> bool:
        tab = self.active_tab()
        if tab is None:
            return False

        old_name = tab.name
        current_cwd = tab.state_env().get("PWD") or tab.cwd_hint or tab.cwd
        current_env = restorable_env(tab.state_env())
        current_shell = tab.shell

        self.tabs.pop(self.active_index)
        self._close_tab(tab)

        if self.tabs:
            self.active_index = min(self.active_index, len(self.tabs) - 1)
            self.message = f"killed {old_name}"
            return True

        self.create_tab(cwd=current_cwd, env=current_env, shell=current_shell)
        self.active_index = 0
        self.message = f"killed {old_name}; opened replacement terminal"
        return True

    def resize(self, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        self.rows = rows
        self.cols = cols
        for tab in self.tabs:
            tab.resize(rows, cols)

    def save_session(self, target: Path | None = None) -> Path:
        path = target or save_path(self.session_name)
        payload = {
            "name": self.session_name,
            "saved_at": utc_now(),
            "terminals": [tab.serialize() for tab in self.tabs],
        }
        dump_session(path, payload)
        self.message = f"saved {path}"
        return path

    def snapshot(self) -> dict[str, Any]:
        active = self.active_tab()
        lines = active.screen.display if active else []
        return {
            "type": "snapshot",
            "session": self.session_name,
            "terminals": [
                {
                    **tab.snapshot(),
                    "index": index,
                }
                for index, tab in enumerate(self.tabs)
            ],
            "active_index": self.active_index if active else None,
            "active_tab": active.tab_id if active else None,
            "cursor_x": active.screen.cursor.x if active else 0,
            "cursor_y": active.screen.cursor.y if active else 0,
            "lines": lines,
            "message": self.message,
        }

    async def broadcast(self) -> None:
        payload = self.snapshot()
        dead_clients: list[asyncio.StreamWriter] = []
        for writer in list(self.clients):
            try:
                await send_message(writer, payload)
            except (BrokenPipeError, ConnectionError):
                dead_clients.append(writer)
        for writer in dead_clients:
            self.clients.discard(writer)
        self.message = ""

    def _schedule_broadcast(self) -> None:
        self.loop.create_task(self.broadcast())

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        subscribed = False
        try:
            while not reader.at_eof():
                raw = await reader.readline()
                if not raw:
                    break
                message = json.loads(raw.decode("utf-8"))
                response = await self._handle_message(message, writer)
                if message.get("type") == "hello":
                    subscribed = True
                if response is not None:
                    await send_message(writer, response)
        finally:
            if subscribed:
                self.clients.discard(writer)
            writer.close()
            await writer.wait_closed()

    async def _handle_message(
        self,
        message: dict[str, Any],
        writer: asyncio.StreamWriter,
    ) -> dict[str, Any] | None:
        kind = message.get("type")

        if kind == "hello":
            rows = int(message.get("rows") or self.rows)
            cols = int(message.get("cols") or self.cols)
            self.resize(rows, cols)
            self.clients.add(writer)
            return self.snapshot()

        if kind == "input":
            tab = self.active_tab()
            if tab is not None:
                tab.write_input(decode_bytes(str(message["data"])))
            return {"type": "ack", "ok": True}

        if kind == "resize":
            self.resize(int(message.get("rows") or self.rows), int(message.get("cols") or self.cols))
            self._schedule_broadcast()
            return {"type": "ack", "ok": True}

        if kind == "new_tab":
            self.create_tab()
            self._schedule_broadcast()
            return {"type": "ack", "ok": True}

        if kind == "switch":
            direction = str(message.get("direction") or "next")
            self.switch_tab(-1 if direction == "prev" else 1)
            self._schedule_broadcast()
            return {"type": "ack", "ok": True}

        if kind == "select_tab":
            ok = self.select_tab(int(message.get("index", -1)))
            self._schedule_broadcast()
            return {"type": "ack", "ok": ok}

        if kind == "rename_tab":
            ok = self.rename_active_tab(str(message.get("name") or ""))
            self._schedule_broadcast()
            return {"type": "ack", "ok": ok}

        if kind == "kill_tab":
            ok = self.kill_active_tab()
            self._schedule_broadcast()
            return {"type": "ack", "ok": ok}

        if kind == "scroll":
            tab = self.active_tab()
            if tab is not None:
                if message.get("direction") == "up":
                    tab.scroll_up()
                else:
                    tab.scroll_down()
            self._schedule_broadcast()
            return {"type": "ack", "ok": True}

        if kind == "save":
            target = Path(message["path"]) if message.get("path") else None
            path = self.save_session(target)
            self._schedule_broadcast()
            return {"type": "ack", "ok": True, "path": str(path)}

        if kind == "kill":
            self.stopping.set()
            return {"type": "ack", "ok": True}

        if kind == "detach":
            if writer in self.clients:
                self.clients.discard(writer)
            return {"type": "ack", "ok": True}

        return {"type": "ack", "ok": False, "error": f"unknown message type {kind}"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m muxer.daemon")
    parser.add_argument("--session", required=True)
    parser.add_argument("--rows", type=int, default=24)
    parser.add_argument("--cols", type=int, default=120)
    parser.add_argument("--restore")
    return parser.parse_args(argv)


async def main_async(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    daemon = SessionDaemon(
        session_name=args.session,
        rows=args.rows,
        cols=args.cols,
        restore_file=Path(args.restore) if args.restore else None,
    )
    await daemon.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    raise SystemExit(main())
