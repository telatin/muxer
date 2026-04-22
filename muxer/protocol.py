from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any


JsonDict = dict[str, Any]


def encode_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_bytes(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


async def send_message(writer: asyncio.StreamWriter, payload: JsonDict) -> None:
    writer.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
    await writer.drain()


async def read_messages(reader: asyncio.StreamReader) -> AsyncIterator[JsonDict]:
    while not reader.at_eof():
        line = await reader.readline()
        if not line:
            break
        yield json.loads(line.decode("utf-8"))

