"""Alinhamento de uma legenda com a FALA do áudio do vídeo (opcional, só a pedido do utilizador).

Como funciona (a ideia do ffsubsync, sem modelos pesados):
1. Escolhem-se janelas espalhadas pelo filme (10 de 4 minutos) e o ffmpeg extrai só o áudio dessas janelas: mono, 8 kHz, **canal central** quando o
   áudio é 5.1/7.1 (é onde está o diálogo) e filtrado para a banda da voz (150–4000 Hz).
2. Um detetor de atividade de voz simples converte a energia por frames de 20 ms num sinal contínuo 0–1 (limiar adaptativo ao próprio áudio, normalizado
   localmente para que cenas ruidosas não dominem).
3. Cada janela é comparada com os instantes em que a legenda mostra texto (correlação cruzada em ±150 s). As curvas de todas as janelas SOMAM-SE: um
   desvio verdadeiro reforça-se de janela em janela, ao passo que picos ao acaso não. Só se conclui se o pico somado for forte E várias janelas, cada
   uma por si, apoiarem esse desvio.
4. Resultado: offset constante, deriva linear (ex.: 23,976 ↔ 25 fps; testam-se as razões de frame rate habituais) ou «inconclusivo» (sem adivinhar). A
   confiança que se mostra é a fração de janelas que apoiam, e não uma probabilidade calibrada.

Limites honestos: a energia não distingue fala de música ou efeitos; dobragens e vídeos com muita música podem enganar; legendas forçadas (só partes
estrangeiras) ou de outro corte do filme não alinham. Por isso nunca se aplica nada sem pré-visualização e confirmação.
"""
from __future__ import annotations

import re
import subprocess
from typing import Callable

import numpy as np

SR = 8000                      # Hz do áudio extraído
FRAME = SR // 50               # frames de 20 ms → 50 por segundo
RES = 50
SEARCH_S = 150                 # ±150 s de procura de desvio
WINDOW_S = 240                 # duração de cada janela
N_WINDOWS = 10
ZT_MIN = 7.0                   # pico da soma das curvas (em desvios-padrão) para aceitar um desvio
SUPPORT_Z = 3.0                # uma janela «apoia» o desvio se a sua curva, nesse ponto, tiver pelo menos este z
LOCAL_S = 4.0                  # a janela só procura o seu pico a ±4 s do pico global (permite deriva pequena)
AGREE_TOL_S = 0.8
PROBE_BIN = "ffprobe"
_FILTER_CACHE: dict[str, str] = {}


class AudioSyncError(Exception):
    pass


# ---- áudio → atividade ------------------------------------------------------------

def audio_filter(path: str) -> str:
    """Filtro do ffmpeg: canal central se existir (5.1/7.1), senão mistura mono; e banda da voz."""
    if path in _FILTER_CACHE:
        return _FILTER_CACHE[path]
    band = "highpass=f=150,lowpass=f=4000"
    pan = ""
    try:
        cp = subprocess.run([PROBE_BIN, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels,channel_layout", "-of", "csv=p=0", path],
                            capture_output=True, timeout=30, check=False)
        layout = cp.stdout.decode("utf-8", "ignore").strip().split(",") if cp.returncode == 0 else []
        chans = int(layout[0]) if layout and layout[0].strip().isdigit() else 0
        if chans >= 5 or (len(layout) >= 2 and re.match(r"^(3\.|5\.|6\.|7\.|8\.)", layout[1].strip())):
            pan = "pan=mono|c0=FC,"                           # o diálogo vive no canal central (5.1/7.1; sem layout declarado assume-se o habitual)
    except (OSError, subprocess.SubprocessError):
        pass
    _FILTER_CACHE[path] = pan + band
    return _FILTER_CACHE[path]


def extract_window(path: str, start: float, dur: float, ffmpeg_bin: str = "ffmpeg", timeout: int = 180) -> np.ndarray | None:
    """Áudio mono 8 kHz s16 de [start, start+dur). Só leitura; sem shell; None se falhar."""
    cmd = [ffmpeg_bin, "-v", "error", "-nostdin", "-ss", f"{max(0.0, start):.2f}", "-t", f"{dur:.2f}", "-i", path, "-map", "0:a:0", "-vn",
           "-af", audio_filter(path), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0 or len(cp.stdout) < SR * 5 * 2:
        return None
    return np.frombuffer(cp.stdout, dtype=np.int16)


def _movavg(x: np.ndarray, k: int) -> np.ndarray:
    k = max(1, k)
    c = np.cumsum(np.insert(x.astype(np.float64), 0, 0.0))
    half = k // 2
    idx = np.arange(len(x))
    lo, hi = np.clip(idx - half, 0, len(x)), np.clip(idx + half + 1, 0, len(x))
    return ((c[hi] - c[lo]) / np.maximum(1, hi - lo)).astype(np.float32)


def speech_envelope(samples: np.ndarray) -> tuple[np.ndarray, float]:
    """(envolvente de «fala» por frame de 20 ms, % de frames com som forte). Contínua em 0–1 e normalizada localmente (subtrai a média de ±5 s)."""
    n = len(samples) // FRAME
    if n < RES * 10:
        return np.zeros(0, dtype=np.float32), 0.0
    x = samples[: n * FRAME].reshape(n, FRAME).astype(np.float32)
    db = 10.0 * np.log10(np.mean(x * x, axis=1) + 1.0)
    floor, peak = np.percentile(db, 15), np.percentile(db, 92)
    if peak - floor < 2.5:
        return np.zeros(n, dtype=np.float32), 0.0
    env = np.clip((db - floor) / (peak - floor), 0.0, 1.2)
    env = _movavg(env, 5)
    pct = float(np.mean(db > floor + 0.4 * (peak - floor)))
    return (env - _movavg(env, 5 * RES)).astype(np.float32), pct


def subtitle_activity(cues: list[tuple[int, int]], t0: float, t1: float) -> np.ndarray:
    """1 nos frames em que a legenda mostra texto, entre t0 e t1 (segundos)."""
    n = int(round((t1 - t0) * RES))
    out = np.zeros(max(0, n), dtype=np.float32)
    for s, e in cues:
        a = int((s / 1000 - t0) * RES)
        b = int((e / 1000 - t0) * RES) + 1
        if b <= 0 or a >= n:
            continue
        out[max(0, a): min(n, b)] = 1.0
    return out


# ---- correlação por janela ---------------------------------------------------------

def window_curve(env: np.ndarray, cues: list[tuple[int, int]], t0: float) -> np.ndarray | None:
    """Curva de correlação normalizada (z) para os desvios k ∈ [−SEARCH, +SEARCH] (em frames; índice k+SEARCH·RES). None se a janela não tiver sinal útil."""
    if len(env) < RES * 20 or float(np.abs(env).sum()) < 1.0:
        return None
    L = len(env) / RES
    s = subtitle_activity(cues, t0 - SEARCH_S, t0 + L + SEARCH_S)
    if s[SEARCH_S * RES: SEARCH_S * RES + len(env)].sum() < RES * 3:
        return None                                              # quase sem falas nesta zona
    corr = np.correlate(s - s.mean(), env, "valid")              # corr[i] = Σ env[n]·s[n+i], i ∈ [0, 2·SEARCH·RES]
    sd = corr.std()
    if sd < 1e-9:
        return None
    return ((corr - corr.mean()) / sd).astype(np.float32)


def _theil_sen(ts: list[float], ys: list[float]) -> tuple[float, float]:
    slopes = [(ys[j] - ys[i]) / (ts[j] - ts[i]) for i in range(len(ts)) for j in range(i + 1, len(ts)) if abs(ts[j] - ts[i]) > 60]
    b = float(np.median(slopes)) if slopes else 0.0
    a = float(np.median([y - b * t for t, y in zip(ts, ys)]))
    return a, b


def decide(curves: list[np.ndarray | None], centers: list[float], duration: float, speech: list[float]) -> dict:
    """Junta as curvas das janelas e decide: aligned | offset | linear | inconclusive (com justificação)."""
    total = len(curves)
    wins = [{"t": t, "offset": None, "z": 0.0, "used": False, "speech_pct": round(sp * 100)} for t, sp in zip(centers, speech)]
    out = {"windows_total": total, "windows_clear": 0, "mode": "inconclusive", "agree": 0, "confidence": 0.0, "offset_s": 0.0, "scale": 1.0,
           "drift_s_per_hour": 0.0, "z_total": 0.0, "windows": wins}
    valid = [(i, c) for i, c in enumerate(curves) if c is not None]
    out["windows_clear"] = len(valid)
    if len(valid) < 3:
        out["reason"] = ("Poucas janelas com sinal utilizável (silêncio, áudio sem dinâmica ou legenda sem falas nessas zonas). "
                         "Veja a coluna «Fala» da tabela: valores muito baixos indicam que o áudio lido não tem diálogo detetável.")
        return out
    C = np.sum([c for _, c in valid], axis=0)
    zt = float((C.max() - C.mean()) / (C.std() + 1e-9))
    k = int(np.argmax(C))
    out["z_total"] = round(zt, 1)
    loc = int(LOCAL_S * RES)
    lo, hi = max(0, k - loc), min(len(C), k + loc + 1)
    support = []
    for i, c in valid:
        seg = c[lo:hi]
        kw = lo + int(np.argmax(seg))
        wins[i]["offset"] = round(-((kw - SEARCH_S * RES) / RES), 2)
        wins[i]["z"] = round(float(c[kw]), 1)
        if c[kw] >= SUPPORT_Z:
            support.append(i)
    need = max(3, int(np.ceil(0.4 * total)))
    if zt < ZT_MIN:
        out["reason"] = (f"Nenhum desvio se destaca (pico somado {zt:.1f}, são precisos {ZT_MIN:.0f}): o áudio pode ter pouco diálogo, ou a legenda é de outro corte/versão, "
                         "forçada, de outra língua falada ou muito diferente do filme.")
        return out
    if len(support) < need:
        out["reason"] = f"O desvio mais forte só é apoiado por {len(support)} de {total} janelas (mínimo {need}): não se conclui nada."
        return out
    offs = [wins[i]["offset"] for i in support]
    ts = [wins[i]["t"] for i in support]
    const = float(np.median(offs))
    mode, a, b = "offset", const, 0.0
    if len(support) >= 4:
        a2, b2 = _theil_sen(ts, offs)
        inl = [i for i in support if abs(wins[i]["offset"] - (a2 + b2 * wins[i]["t"])) <= AGREE_TOL_S]
        const_in = [i for i in support if abs(wins[i]["offset"] - const) <= AGREE_TOL_S]
        if len(inl) >= max(4, int(np.ceil(0.6 * len(support)))) and len(inl) >= len(const_in) + 2 and abs(b2 * duration) >= 1.5 and 0.9 <= 1 + b2 <= 1.1:
            mode, a, b, support = "linear", a2, b2, inl
    for i in support:
        wins[i]["used"] = True
    out.update(mode=mode, agree=len(support), confidence=round(len(support) / total, 2), offset_s=round(a, 3), scale=round(1 + b, 8), drift_s_per_hour=round(b * 3600, 2))
    resid = [abs(wins[i]["offset"] - (a + b * wins[i]["t"])) for i in support]
    out["residual_s"] = round(float(np.median(resid)), 2)
    if mode == "offset" and abs(a) < 0.4:
        out["mode"] = "aligned"
    elif mode == "linear" and abs(a) < 0.4 and abs(b * duration) < 1.0:
        out["mode"] = "aligned"
    return out


def spoken_span(cues: list[tuple[int, int, int]]) -> list[tuple[int, int]]:
    """[(início, fim, nº de caracteres)] → [(início, fim)] com o fim alongado até ao tempo que a fala demora a dizer (≈14 car/s, 1,2–7 s).
    Falas que desaparecem depressa demais (fim muito cedo) deixariam a correlação alinhar o início do som com o meio da fala e dar um desvio sistemático."""
    out = []
    for s, e, n in cues:
        spoken = int(min(7000, max(1200, n * 1000 / 14)))
        out.append((s, max(e, s + spoken)))
    return out


# ---- orquestração ---------------------------------------------------------------------

def window_starts(duration: float, n: int = N_WINDOWS, length: int = WINDOW_S) -> list[float]:
    """Janelas espalhadas entre 5 % e 92 % do filme (evita genéricos), sem sair do ficheiro."""
    if duration < length * 2:
        return [0.0]
    lo, hi = duration * 0.05, max(duration * 0.92 - length, duration * 0.05)
    return [round(lo + (hi - lo) * i / max(1, n - 1), 1) for i in range(n)]


RATIOS = (1.0, 25 / 23.976, 23.976 / 25, 25 / 24, 24 / 25)     # diferenças de frame rate habituais (a deriva pode passar os ±150 s de procura)


def analyze(path: str, cues: list[tuple[int, int]], duration: float, ffmpeg_bin: str = "ffmpeg",
            progress: Callable[[int, int, str], None] | None = None, extract: Callable | None = None) -> dict:
    """Corre as janelas e devolve o resultado de `decide`. Lança AudioSyncError se não for possível ler o áudio.
    Resultado final: t_novo = scale · t + offset_s (em segundos); `mode`: aligned | offset | linear | inconclusive."""
    extract = extract or extract_window
    if not cues:
        raise AudioSyncError("A legenda não tem falas.")
    starts = window_starts(duration)
    envs: list[tuple[float, np.ndarray, float, float]] = []
    failed = 0
    for i, t0 in enumerate(starts):
        if progress:
            progress(i, len(starts), f"A ler o áudio ({int(t0 // 60)} min)…")
        samples = extract(path, t0, min(WINDOW_S, max(30.0, duration - t0)), ffmpeg_bin)
        if samples is None:
            failed += 1
            continue
        env, pct = speech_envelope(samples)
        envs.append((t0, env, len(samples) / SR, pct))
    if progress:
        progress(len(starts), len(starts), "A comparar com a legenda…")
    if not envs:
        raise AudioSyncError("Não foi possível extrair o áudio do vídeo (ffmpeg falhou, ficheiro inacessível ou sem faixa de áudio).")
    centers = [round(t0 + length / 2, 1) for t0, _, length, _ in envs]
    speech = [pct for _, _, _, pct in envs]
    best = None
    for r in RATIOS:
        cr = [(int(s * r), int(e * r)) for s, e in cues] if r != 1.0 else cues
        out = decide([window_curve(env, cr, t0) for t0, env, _, _ in envs], centers, duration, speech)
        out["ratio"] = r
        if best is None or (out["mode"] != "inconclusive", out["z_total"]) > (best["mode"] != "inconclusive", best["z_total"]):
            best = out
        if r == 1.0 and out["mode"] != "inconclusive" and out["confidence"] >= 0.6:
            break                                                  # o 1.º teste já concordou bem: não se procura deriva de frame rate
    out = best
    # mapeamento final sobre os tempos ORIGINAIS: t_audio = (r·t + a) / (1 − b)
    r = out["ratio"]
    b = out["scale"] - 1 if out["mode"] == "linear" else 0.0
    if out["mode"] in ("offset", "linear", "aligned"):
        out["scale"] = round(r / (1 - b), 8)
        out["offset_s"] = round(out["offset_s"] / (1 - b), 3)
        if r != 1.0 and out["mode"] in ("aligned", "offset"):
            out["mode"] = "linear"                                  # só alinha depois de corrigir a escala (frame rate)
        out["drift_s_per_hour"] = round((out["scale"] - 1) * 3600, 2)
    out["windows_failed"] = failed
    out["duration_s"] = round(duration, 1)
    return out
