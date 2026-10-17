"""Hash do OpenSubtitles (64 KiB inicial + final + tamanho)."""
from __future__ import annotations

import os
import struct

CHUNK = 65536


def moviehash(path: str) -> str | None:
    size = os.path.getsize(path)
    if size < CHUNK * 2:
        return None
    h = size
    with open(path, "rb") as f:
        for off in (0, size - CHUNK):
            f.seek(off)
            buf = f.read(CHUNK)
            for (v,) in struct.iter_unpack("<Q", buf):
                h = (h + v) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"
