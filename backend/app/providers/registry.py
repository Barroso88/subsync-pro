from __future__ import annotations

from .base import Provider
from .opensubtitles import OpenSubtitles
from .subdl import SubDL
from .subtitlecat import Subtitlecat

PROVIDERS: dict[str, type[Provider]] = {OpenSubtitles.name: OpenSubtitles, SubDL.name: SubDL, Subtitlecat.name: Subtitlecat}
