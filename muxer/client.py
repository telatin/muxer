from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from .config import prefix_bytes, prefix_label
from .paths import socket_path
from .protocol import encode_bytes, read_messages, send_message


SPECIAL_KEYS: dict[Any, bytes] = {
    Keys.Enter: b"\r",
    Keys.Tab: b"\t",
    Keys.Backspace: b"\x7f",
    Keys.Delete: b"\x1b[3~",
    Keys.Up: b"\x1b[A",
    Keys.Down: b"\x1b[B",
    Keys.Right: b"\x1b[C",
    Keys.Left: b"\x1b[D",
    Keys.Home: b"\x1b[H",
    Keys.End: b"\x1b[F",
    Keys.Insert: b"\x1b[2~",
    Keys.ControlM: b"\r",
    Keys.ControlI: b"\t",
    Keys.ControlJ: b"\n",
}


def keypress_to_bytes(key_press: Any) -> bytes | None:
    key = key_press.key
    data = key_press.data

    if key in SPECIAL_KEYS:
        return SPECIAL_KEYS[key]

    if isinstance(data, str) and data:
        return data.encode("utf-8")

    if isinstance(key, Keys):
        name = key.value
        if name.startswith("c-") and len(name) == 3:
            return bytes([ord(name[-1].lower()) - 96])

    return None


@dataclass
class ClientState:
    session_name: str
    lines: list[str] = field(default_factory=list)
    terminals: list[dict[str, Any]] = field(default_factory=list)
    active_index: int | None = None
    message: str = ""


class MuxClient:
    def __init__(self, session_name: str, prefix_binding: str) -> None:
        self.session_name = session_name
        self.prefix_binding = prefix_binding
        self.prefix_label = prefix_label(prefix_binding)
        self.prefix_key_bytes = prefix_bytes(prefix_binding)
        self.state = ClientState(session_name=session_name)
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.prefix_pending = False
        self.rename_buffer: str | None = None
        self.local_message = ""
        self.app = Application(
            layout=Layout(
                HSplit(
                    [
                        Window(
                            content=FormattedTextControl(self.render_body),
                            always_hide_cursor=True,
                        ),
                        Window(
                            content=FormattedTextControl(self.render_status),
                            height=1,
                            style="reverse",
                        ),
                    ]
                )
            ),
            full_screen=True,
            key_bindings=self.bindings(),
            refresh_interval=0.2,
        )
        self.last_size: tuple[int, int] | None = None

    def bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add(self.prefix_binding)
        def _(event: Any) -> None:
            if self.rename_buffer is not None:
                return
            if self.prefix_pending:
                self.prefix_pending = False
                self.set_local_message("")
                event.app.create_background_task(self.send_input_bytes(self.prefix_key_bytes))
                return
            self.prefix_pending = True
            self.set_local_message(f"{self.prefix_label} command")
            event.app.invalidate()

        @kb.add(Keys.Any)
        def _(event: Any) -> None:
            key_press = event.key_sequence[-1]

            if self.rename_buffer is not None:
                self.handle_rename_key(key_press, event)
                return

            if self.prefix_pending:
                self.prefix_pending = False
                if self.handle_prefix_command(key_press, event):
                    return
                self.set_local_message(f"unknown {self.prefix_label} command")
                event.app.invalidate()
                return

            payload = keypress_to_bytes(key_press)
            if payload is None:
                return
            event.app.create_background_task(self.send_input_bytes(payload))

        return kb

    def render_body(self) -> str:
        rows = self.state.lines or ["(empty terminal)"]
        return "\n".join(rows)

    def render_status(self) -> list[tuple[str, str]]:
        parts: list[tuple[str, str]] = [("reverse", f" {self.session_name} ")]
        for tab in self.state.terminals:
            index = tab.get("index", tab.get("id", "?"))
            active = index == self.state.active_index
            label = f"[{index}:{tab['name']}]" if active else f" {index}:{tab['name']} "
            style = "reverse bold" if active else "reverse"
            parts.append((style, label))
        hint = (
            f"  {self.prefix_label} c new  p prev  n next  0-9 jump  a rename"
            f"  d detach  k kill  s save  PgUp/PgDn scroll"
        )
        parts.append(("reverse", hint))
        message = self.local_message or self.state.message
        if message:
            parts.append(("reverse", f" | {message}"))
        return parts

    def set_local_message(self, message: str) -> None:
        self.local_message = message

    async def send_input_bytes(self, payload: bytes) -> None:
        await self.send({"type": "input", "data": encode_bytes(payload)})

    def handle_prefix_command(self, key_press: Any, event: Any) -> bool:
        data = key_press.data.lower() if isinstance(key_press.data, str) else ""

        if data == "c":
            self.set_local_message("")
            event.app.create_background_task(self.send({"type": "new_tab"}))
            return True
        if data == "d":
            self.set_local_message("")
            event.app.create_background_task(self.detach(event))
            return True
        if data == "k":
            self.set_local_message("")
            event.app.create_background_task(self.send({"type": "kill_tab"}))
            return True
        if data == "a":
            self.begin_rename()
            event.app.invalidate()
            return True
        if data == "p":
            self.set_local_message("")
            event.app.create_background_task(self.send({"type": "switch", "direction": "prev"}))
            return True
        if data == "n":
            self.set_local_message("")
            event.app.create_background_task(self.send({"type": "switch", "direction": "next"}))
            return True
        if data == "s":
            self.set_local_message("")
            event.app.create_background_task(self.send({"type": "save"}))
            return True
        if data.isdigit():
            self.set_local_message("")
            event.app.create_background_task(
                self.send({"type": "select_tab", "index": int(data)})
            )
            return True
        if key_press.key == Keys.PageUp:
            self.set_local_message("")
            event.app.create_background_task(self.send({"type": "scroll", "direction": "up"}))
            return True
        if key_press.key == Keys.PageDown:
            self.set_local_message("")
            event.app.create_background_task(self.send({"type": "scroll", "direction": "down"}))
            return True
        return False

    def begin_rename(self) -> None:
        current_name = self.active_tab_name()
        self.rename_buffer = current_name
        self.set_local_message(f"rename tab: {self.rename_buffer}")

    def active_tab_name(self) -> str:
        for tab in self.state.terminals:
            if tab.get("index") == self.state.active_index:
                return str(tab.get("name") or "")
        return ""

    def handle_rename_key(self, key_press: Any, event: Any) -> None:
        assert self.rename_buffer is not None

        if key_press.key == Keys.Escape:
            self.rename_buffer = None
            self.set_local_message("rename cancelled")
            event.app.invalidate()
            return

        if key_press.key in (Keys.Enter, Keys.ControlM, Keys.ControlJ):
            name = self.rename_buffer.strip()
            self.rename_buffer = None
            if not name:
                self.set_local_message("rename cancelled")
                event.app.invalidate()
                return
            self.set_local_message("")
            event.app.create_background_task(self.send({"type": "rename_tab", "name": name}))
            return

        if key_press.key == Keys.Backspace:
            self.rename_buffer = self.rename_buffer[:-1]
            self.set_local_message(f"rename tab: {self.rename_buffer}")
            event.app.invalidate()
            return

        if isinstance(key_press.data, str) and key_press.data and key_press.data.isprintable():
            self.rename_buffer += key_press.data
            self.set_local_message(f"rename tab: {self.rename_buffer}")
            event.app.invalidate()

    async def detach(self, event: Any) -> None:
        await self.send({"type": "detach"})
        event.app.exit()

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(str(socket_path(self.session_name)))
        await self.send_hello()

    async def send_hello(self) -> None:
        rows, cols = self.current_size()
        await self.send({"type": "hello", "rows": rows - 1, "cols": cols})

    async def send(self, payload: dict[str, Any]) -> None:
        if self.writer is None:
            return
        await send_message(self.writer, payload)

    def current_size(self) -> tuple[int, int]:
        size = self.app.output.get_size()
        return size.rows, size.columns

    async def receive(self) -> None:
        assert self.reader is not None
        async for message in read_messages(self.reader):
            if message.get("type") != "snapshot":
                continue
            self.state.lines = [str(line) for line in message.get("lines", [])]
            self.state.terminals = list(message.get("terminals", []))
            self.state.active_index = message.get("active_index")
            self.state.message = str(message.get("message") or "")
            self.app.invalidate()

    async def watch_resize(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            size = self.current_size()
            if size != self.last_size:
                self.last_size = size
                await self.send({"type": "resize", "rows": size[0] - 1, "cols": size[1]})

    async def run(self) -> None:
        await self.connect()
        receive_task = asyncio.create_task(self.receive())
        resize_task = asyncio.create_task(self.watch_resize())
        try:
            await self.app.run_async()
        finally:
            receive_task.cancel()
            resize_task.cancel()
            if self.writer is not None:
                self.writer.close()
                await self.writer.wait_closed()


async def run_client(session_name: str, prefix_binding: str) -> None:
    client = MuxClient(session_name, prefix_binding)
    await client.run()
