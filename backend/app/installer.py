"""Validação, extração segura e instalação atómica de legendas.

Nunca toca em ficheiros de vídeo. Nunca substitui legendas sem política explícita.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .naming import TEXT_SUB_EXTS, parse_episode_tag
from .safepaths import PathSecurityError, require_within, sanitize_filename
from .subtitles.parser import MAX_SUB_BYTES, ParsedSub, decode_bytes, looks_like_html_error, parse_text, sniff_format

MAX_ZIP_MEMBERS = 200
MAX_ZIP_TOTAL = 40 * 1024 * 1024
MAX_RATIO = 200


class InvalidDownload(Exception):
    pass


class InstallConflict(Exception):
    def __init__(self, dest: str):
        super().__init__(f"Já existe: {dest}")
        self.dest = dest


def safe_zip_pick(data: bytes, media: dict | None = None) -> tuple[str, bytes]:
    """Lê (em memória) o ficheiro de legenda mais adequado de um ZIP.

    Nunca escreve com nomes vindos do arquivo: usa só o basename para decidir/extensão,
    pelo que `../../etc/x.srt` não consegue sair de lado nenhum. Rejeita bombas ZIP.
    """
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise InvalidDownload("Arquivo ZIP corrompido.") from e
    infos = [i for i in z.infolist() if not i.is_dir()]
    if len(infos) > MAX_ZIP_MEMBERS:
        raise InvalidDownload("ZIP com demasiados ficheiros.")
    if sum(i.file_size for i in infos) > MAX_ZIP_TOTAL:
        raise InvalidDownload("ZIP demasiado grande depois de descomprimido.")
    cands = []
    for i in infos:
        base = PurePosixPath(i.filename.replace("\\", "/")).name
        if not base or base.startswith("."):
            continue
        if Path(base).suffix.lower() not in TEXT_SUB_EXTS:
            continue
        if i.flag_bits & 0x1:
            raise InvalidDownload("ZIP encriptado.")
        if i.file_size > MAX_SUB_BYTES or (i.compress_size and i.file_size / max(i.compress_size, 1) > MAX_RATIO):
            raise InvalidDownload("Entrada de ZIP suspeita (tamanho/rácio).")
        cands.append((i, base))
    if not cands:
        raise InvalidDownload("O ZIP não contém ficheiros de legenda de texto.")

    def rank(c):
        i, base = c
        r = 0
        if media and media.get("kind") == "episode":
            t = parse_episode_tag(base)
            if t and (t[0], t[1]) == (media.get("season"), media.get("episode")):
                r -= 10
        if base.lower().endswith(".srt"):
            r -= 1
        return r

    i, base = sorted(cands, key=rank)[0]
    with z.open(i) as fh:
        blob = fh.read(MAX_SUB_BYTES + 1)
    if len(blob) > MAX_SUB_BYTES:
        raise InvalidDownload("Legenda demasiado grande.")
    return base, blob


@dataclass
class Validated:
    filename: str
    text: str
    parsed: ParsedSub
    ext: str
    encoding: str


def validate_download(name: str, data: bytes, media: dict | None = None) -> Validated:
    if not data:
        raise InvalidDownload("Ficheiro vazio.")
    if data[:4] == b"PK\x03\x04":
        name, data = safe_zip_pick(data, media)
    if looks_like_html_error(data):
        raise InvalidDownload("O fornecedor devolveu uma página HTML/erro em vez de uma legenda.")
    if len(data) > MAX_SUB_BYTES:
        raise InvalidDownload("Legenda demasiado grande.")
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise InvalidDownload("Conteúdo binário; não é uma legenda de texto.")
    text, enc = decode_bytes(data)
    ext = Path(name).suffix.lower() or ".srt"
    fmt = sniff_format(text, ext)
    if fmt is None:
        raise InvalidDownload("Formato de legenda não reconhecido (SRT/ASS/SSA/VTT).")
    parsed = parse_text(text, ext)
    if parsed is None or not parsed.cues:
        raise InvalidDownload("A legenda não contém eventos.")
    if parsed.errors > max(5, len(parsed.cues) * 0.25):
        raise InvalidDownload("Legenda com demasiados blocos inválidos (corrompida).")
    out_ext = {"srt": ".srt", "vtt": ".vtt", "ass": ".ass", "ssa": ".ssa"}[fmt]
    return Validated(name, text, parsed, out_ext, enc)


def build_dest_name(video_path: str, lang: str | None, *, forced: bool = False, hi: bool = False, ext: str = ".srt") -> str:
    """`Filme (2024).pt-PT.srt` / `Serie.S01E01.pt-PT.forced.srt` (compatível com Plex/Radarr/Sonarr)."""
    stem = Path(video_path).stem
    parts = [stem]
    if lang:
        parts.append(lang)
    if forced:
        parts.append("forced")
    if hi:
        parts.append("sdh")
    return sanitize_filename(".".join(parts) + ext)


@dataclass
class Plan:
    dest: str
    conflict: str | None            # None | "exists" | "same_language"
    existing: list[str]


def plan_install(media: dict, lang: str | None, writable_roots: list[str], existing_subs: list[dict], *,
                 forced: bool = False, hi: bool = False, ext: str = ".srt") -> Plan:
    name = build_dest_name(media["path"], lang, forced=forced, hi=hi, ext=ext)
    dest = os.path.join(media["dir"], name)
    try:
        dest = require_within(dest, writable_roots)
    except PathSecurityError:
        raise PathSecurityError("A pasta de destino não está numa biblioteca com escrita autorizada.")
    conflict = None
    existing = []
    if os.path.lexists(dest):
        conflict = "exists"
        existing.append(dest)
    else:
        for s in existing_subs:
            if (s["kind"] == "external" and s["present"] and s["lang"] == lang and bool(s["forced"]) == forced
                    and bool(s["hi"]) == hi):
                conflict = "same_language"
                existing.append(s["path"])
    return Plan(dest, conflict, existing)


def stage(staging_dir: Path, v: Validated) -> str:
    """Grava o texto normalizado em UTF-8 (sem BOM) na pasta de trabalho."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    p = staging_dir / f"{uuid.uuid4().hex}{v.ext}"
    p.write_bytes(v.text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿").encode("utf-8"))
    return str(p)


def install(staged: str, dest: str, writable_roots: list[str], backup_dir: Path, policy: str = "fail") -> str:
    """Instala `staged` em `dest` atomicamente.

    policy: fail (erro se existir) | replace (arquivo antigo vai para backup) | keep_both (sufixo .2, .3...)
    Devolve o caminho final.
    """
    dest = require_within(dest, writable_roots)
    if not os.path.isfile(staged):
        raise InvalidDownload("Ficheiro em preparação desapareceu.")
    d = os.path.dirname(dest)
    if not os.path.isdir(d):
        raise PathSecurityError("Pasta de destino inexistente.")
    if not os.access(d, os.W_OK):
        raise PermissionError(f"Sem permissão de escrita em {d} (verifique UID/GID e o modo da montagem).")
    if os.path.lexists(dest):
        if policy == "fail":
            raise InstallConflict(dest)
        if policy == "keep_both":
            base, ext = os.path.splitext(dest)
            n = 2
            while os.path.lexists(f"{base}.{n}{ext}"):
                n += 1
            dest = f"{base}.{n}{ext}"
        elif policy == "replace":
            if os.path.isdir(dest):
                raise InstallConflict(dest)
        else:
            raise ValueError(policy)
    tmp = os.path.join(d, f".{os.path.basename(dest)}.{uuid.uuid4().hex[:8]}.subsync-tmp")
    try:
        with open(staged, "rb") as src, open(tmp, "xb") as out:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.chmod(tmp, 0o664)
        except OSError:
            pass
        if os.path.lexists(dest) and policy == "replace":
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, backup_dir / f"{int(time.time())}-{sanitize_filename(os.path.basename(dest))}")
            os.replace(tmp, dest)
        else:
            try:
                os.link(tmp, dest)       # não sobrescreve: falha se aparecer entretanto
                os.unlink(tmp)
            except FileExistsError:
                raise InstallConflict(dest)
            except OSError:
                # sistemas de ficheiros sem hardlinks (alguns FUSE/SMB): melhor esforço
                if os.path.lexists(dest):
                    raise InstallConflict(dest)
                os.rename(tmp, dest)
    finally:
        if os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return dest
