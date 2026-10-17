"""Validação de caminhos: tudo o que toca no disco passa por aqui."""
from __future__ import annotations

import os
import re
from pathlib import Path


class PathSecurityError(ValueError):
    pass


def is_within(child: str | Path, parent: str | Path) -> bool:
    try:
        c = os.path.realpath(child)
        p = os.path.realpath(parent)
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False


def require_within(child: str | Path, roots: list[str | Path]) -> str:
    """Devolve o caminho real se estiver dentro de alguma raiz; senão levanta erro."""
    s = str(child)
    if "\x00" in s:
        raise PathSecurityError("Caminho inválido")
    real = os.path.realpath(s)
    for r in roots:
        if is_within(real, r):
            return real
    raise PathSecurityError("Caminho fora das pastas autorizadas")


_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, max_len: int = 200) -> str:
    """Nome de ficheiro válido em Linux/SMB/NTFS (Unraid partilha por SMB)."""
    name = _INVALID.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if len(name.encode("utf-8")) > max_len:
        stem, dot, ext = name.rpartition(".")
        keep = max_len - len(ext.encode()) - 1
        stem = stem.encode("utf-8")[:keep].decode("utf-8", "ignore")
        name = f"{stem}.{ext}" if dot else stem
    return name or "subtitle"
