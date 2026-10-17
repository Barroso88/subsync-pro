from __future__ import annotations

from .base import Provider
from .opensubtitles import OpenSubtitles
from .subdl import SubDL

PROVIDERS: dict[str, type[Provider]] = {OpenSubtitles.name: OpenSubtitles, SubDL.name: SubDL}
