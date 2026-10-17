"""Normalização de idiomas e deteção heurística por conteúdo.

pt-PT, pt-BR e pt NÃO são equivalentes: `pt` significa "português sem variante
conhecida" e só satisfaz a preferência `pt`.
"""
from __future__ import annotations

import re
from collections import Counter

# código canónico -> (nome, aliases em minúsculas)
LANGS: dict[str, tuple[str, list[str]]] = {
    "pt-PT": ("Português (Portugal)", ["pt-pt", "pt_pt", "ptpt", "por-pt", "portugal", "european portuguese"]),
    "pt-BR": ("Português (Brasil)", ["pt-br", "pt_br", "ptbr", "pob", "br", "brazilian", "brasil", "por-br"]),
    "pt": ("Português", ["pt", "por", "portuguese", "português", "portugues"]),
    "en": ("Inglês", ["en", "eng", "english", "inglês", "en-us", "en-gb"]),
    "es": ("Espanhol", ["es", "spa", "spanish", "español", "espanol", "es-es", "es-la", "es-mx"]),
    "fr": ("Francês", ["fr", "fre", "fra", "french", "français", "francais"]),
    "de": ("Alemão", ["de", "ger", "deu", "german", "deutsch"]),
    "it": ("Italiano", ["it", "ita", "italian", "italiano"]),
    "nl": ("Neerlandês", ["nl", "dut", "nld", "dutch"]),
    "ru": ("Russo", ["ru", "rus", "russian"]),
    "ja": ("Japonês", ["ja", "jpn", "japanese", "jp"]),
    "ko": ("Coreano", ["ko", "kor", "korean"]),
    "zh": ("Chinês", ["zh", "chi", "zho", "chinese", "zh-cn", "zh-tw"]),
    "ar": ("Árabe", ["ar", "ara", "arabic"]),
    "pl": ("Polaco", ["pl", "pol", "polish"]),
    "tr": ("Turco", ["tr", "tur", "turkish"]),
    "sv": ("Sueco", ["sv", "swe", "swedish"]),
    "da": ("Dinamarquês", ["da", "dan", "danish"]),
    "no": ("Norueguês", ["no", "nor", "nob", "norwegian"]),
    "fi": ("Finlandês", ["fi", "fin", "finnish"]),
    "cs": ("Checo", ["cs", "cze", "ces", "czech"]),
    "el": ("Grego", ["el", "gre", "ell", "greek"]),
    "he": ("Hebraico", ["he", "heb", "hebrew"]),
    "ro": ("Romeno", ["ro", "rum", "ron", "romanian"]),
    "hu": ("Húngaro", ["hu", "hun", "hungarian"]),
}

_ALIAS: dict[str, str] = {}
for _code, (_n, _al) in LANGS.items():
    _ALIAS[_code.lower()] = _code
    for _a in _al:
        _ALIAS.setdefault(_a, _code)
# "br" sozinho é ambíguo mas é comum em nomes de ficheiro de legendas
_ALIAS["br"] = "pt-BR"


def normalize_lang(tag: str | None) -> str | None:
    if not tag:
        return None
    t = tag.strip().lower().replace(" ", "")
    if t in ("und", "unknown", "zxx", "mis", "mul"):
        return None
    return _ALIAS.get(t) or _ALIAS.get(t.split("-")[0] if "-" in t else t)


def lang_name(code: str | None) -> str:
    if not code:
        return "Desconhecido"
    return LANGS.get(code, (code, []))[0]


def lang_matches(have: str | None, want: str) -> bool:
    """`have` satisfaz `want`? Só iguais; pt genérico NÃO satisfaz pt-PT/pt-BR."""
    return have is not None and have == want


def provider_lang(code: str) -> str:
    """Código no formato dos fornecedores (OpenSubtitles usa pt-PT / pt-BR / en)."""
    return code


# --- deteção por conteúdo -------------------------------------------------

_STOP = {
    "en": "the and you that this with have for not are but what your was they his her there from can will would just know about like out one get all".split(),
    "pt": "que não uma para com por mais como mas foi são tem ele ela isso você está já muito tudo quando nos sua dos das ser ter".split(),
    "es": "que los las una por con para como pero más este esta está todo ella usted tengo hay muy cuando son sus del".split(),
    "fr": "que les des une pour avec pas vous nous est dans qui sur pas mais être tout cette très ils elle sont ces".split(),
    "de": "und der die das ist nicht ein eine ich sie mit den auf für zu von dem sich auch wir aber wie nur wenn".split(),
    "it": "che non per una con sono come più ma questo della degli gli del nel ho hai lo anche tutto quando molto".split(),
    "nl": "het een van dat niet zijn met voor maar dit ook wat ben heb jij wij ze naar als dan".split(),
}
_PT_PT = set("tu pois miúdo miúda autocarro telemóvel ecrã comboio casa-de-banho gajo gajos fixe bué estás fazes queres podes tens tenho vamos rapariga pequeno-almoço equipa facto".split())
_PT_BR = set("você vocês cara gente ônibus celular tá né legal garoto garota ótimo pra pro menina geladeira trem banheiro time".split())
_WORD = re.compile(r"[a-záàâãéêíóôõúüçñäöß']+", re.I)


def detect_language(text: str) -> tuple[str | None, float]:
    """Devolve (código, confiança 0..1). Heurística por stop-words; pt-PT/pt-BR só
    quando há marcadores claros, caso contrário `pt`."""
    words = [w.lower() for w in _WORD.findall(text[:60000])]
    if len(words) < 40:
        return None, 0.0
    cnt = Counter(words)
    scores = {lang: sum(cnt[w] for w in sw) / len(words) for lang, sw in _STOP.items()}
    best, bs = max(scores.items(), key=lambda kv: kv[1])
    second = sorted(scores.values())[-2]
    if bs < 0.04 or bs < second * 1.25:
        return None, 0.0
    conf = min(1.0, (bs - second) / max(bs, 1e-9) + 0.2)
    if best == "pt":
        pt = sum(cnt[w] for w in _PT_PT)
        br = sum(cnt[w] for w in _PT_BR)
        if pt >= 3 and pt > br * 1.5:
            return "pt-PT", conf * 0.8
        if br >= 3 and br > pt * 1.5:
            return "pt-BR", conf * 0.8
        return "pt", conf
    return best, conf
