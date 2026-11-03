"""Alinhamento de uma legenda com a FALA do áudio do vídeo (opcional, só a pedido do utilizador).

Como funciona (a ideia do ffsubsync, sem modelos pesados):
1. Escolhem-se algumas janelas espalhadas pelo filme (por omissão 8 de 3 minutos) e o ffmpeg extrai só o áudio dessas janelas (mono, 8 kHz).
2. Um detetor de atividade de voz simples (energia por frames de 20 ms, limiar adaptativo, suavização) diz quando «há som de fala/ruído forte».
3. Para cada janela, compara-se esse sinal com os instantes em que a legenda mostra texto (correlação cruzada em ±150 s) e obtém-se o desvio daquela janela.
4. As janelas têm de CONCORDAR (offset constante ou deriva linear, ex.: 23,976 ↔ 25 fps). Se não concordarem, o resultado é «inconclusivo»: não se
   afirma nada. A confiança que se mostra é a fração de janelas que concordam, e não uma probabilidade calibrada.

Limites honestos: a energia não distingue fala de música ou efeitos; dobragens e vídeos com muita música podem enganar; legendas forçadas (só partes
estrangeiras) ou de outro corte do filme não alinham. Por isso nunca se aplica nada sem pré-visualização e confirmação.
"""
from __future__ import annotations

import subprocess
from typing import Callable

import numpy as np

SR = 8000                      # Hz do áudio extraído
FRAME = SR // 50               # frames de 20 ms → 50 por segundo
RES = 50
SEARCH_S = 150                 # ±150 s de procura de desvio por janela
WINDOW_S = 180                 # duração de cada janela
N_WINDOWS = 8
Z_MIN = 6.0                    # pico mínimo (em desvios-padrão da correlação) para uma janela contar
AGREE_TOL_S = 0.7              # tolerância para duas janelas «concordarem»


class AudioSyncError(Exception):
    pass


# ---- áudio → atividade ------------------------------------------------------------

def extract_window(path: str, start: float, dur: float, ffmpeg_bin: str = "ffmpeg", timeout: int = 180) -> np.ndarray | None:
    """Áudio mono 8 kHz s16 de [start, start+dur). Só leitura; sem shell; None se falhar."""
    cmd = [ffmpeg_bin, "-v", "error", "-nostdin", "-ss", f"{max(0.0, start):.2f}", "-t", f"{dur:.2f}", "-i", path, "-map", "0:a:0", "-vn",
           "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0 or len(cp.stdout) < SR * 5 * 2:
        return None
    return np.frombuffer(cp.stdout, dtype=np.int16)


def _smooth(act: np.ndarray, close: int = 8, open_: int = 3) -> np.ndarray:
    """Fecha buracos curtos (<160 ms) e remove picos curtos (<60 ms)."""
    k = np.ones(close, dtype=np.float32)
    dil = np.convolve(act.astype(np.float32), k, "same") > 0
    ero = np.convolve(dil.astype(np.float32), k, "same") >= close
    closed = ero | act
    k2 = np.ones(open_, dtype=np.float32)
    ero2 = np.convolve(closed.astype(np.float32), k2, "same") >= open_
    return np.convolve(ero2.astype(np.float32), k2, "same") > 0


def speech_activity(samples: np.ndarray) -> np.ndarray:
    """1 por frame de 20 ms com som forte (fala), 0 caso contrário. Limiar adaptativo ao próprio áudio; sem dinâmica suficiente → tudo 0."""
    n = len(samples) // FRAME
    if n < RES * 10:
        return np.zeros(0, dtype=np.float32)
    x = samples[: n * FRAME].reshape(n, FRAME).astype(np.float32)
    db = 10.0 * np.log10(np.mean(x * x, axis=1) + 1.0)
    floor, peak = np.percentile(db, 15), np.percentile(db, 92)
    if peak - floor < 6.0:
        return np.zeros(n, dtype=np.float32)
    act = db > floor + 0.4 * (peak - floor)
    return _smooth(act).astype(np.float32)


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

def window_offset(audio_act: np.ndarray, cues: list[tuple[int, int]], t0: float) -> tuple[float, float] | None:
    """(desvio em segundos a SOMAR à legenda, z) para uma janela que começa em t0. None se não houver sinal utilizável."""
    if len(audio_act) < RES * 20 or audio_act.sum() < RES * 5:
        return None
    L = len(audio_act) / RES
    s = subtitle_activity(cues, t0 - SEARCH_S, t0 + L + SEARCH_S)
    if s[SEARCH_S * RES: SEARCH_S * RES + len(audio_act)].sum() < RES * 3 and s.sum() < RES * 10:
        return None                                              # quase sem falas nesta zona
    a = audio_act - audio_act.mean()
    sm = s - s.mean()
    corr = np.correlate(sm, a, "valid")                          # corr[k] = Σ a[n]·s[n+k], k ∈ [0, 2·SEARCH]
    sd = corr.std()
    if sd < 1e-6:
        return None
    k = int(np.argmax(corr))
    z = float((corr[k] - corr.mean()) / sd)
    lag = (k - SEARCH_S * RES) / RES                             # legenda atrasada `lag` s face ao áudio
    return -lag, z


# ---- agregação robusta -------------------------------------------------------------

def _theil_sen(ts: list[float], ys: list[float]) -> tuple[float, float]:
    slopes = [(ys[j] - ys[i]) / (ts[j] - ts[i]) for i in range(len(ts)) for j in range(i + 1, len(ts)) if abs(ts[j] - ts[i]) > 60]
    b = float(np.median(slopes)) if slopes else 0.0
    a = float(np.median([y - b * t for t, y in zip(ts, ys)]))
    return a, b


def combine(wins: list[dict], duration: float) -> dict:
    """Decide entre: já alinhada, offset constante, deriva linear ou inconclusivo. `wins`: {t, offset, z}."""
    total = len(wins)
    good = [w for w in wins if w["offset"] is not None and w["z"] >= Z_MIN]
    out = {"windows_total": total, "windows_clear": len(good), "mode": "inconclusive", "agree": 0, "confidence": 0.0, "offset_s": 0.0, "scale": 1.0, "drift_s_per_hour": 0.0}
    for w in wins:
        w["used"] = False
    if len(good) < 3:
        out["reason"] = "Poucas janelas com fala clara (música, silêncio ou legenda sem falas nessas zonas): não é possível concluir."
        return out
    offs = sorted(w["offset"] for w in good)
    # offset constante: o maior grupo de janelas dentro de AGREE_TOL_S
    best_n, best_med = 0, 0.0
    for i, o in enumerate(offs):
        grp = [x for x in offs if abs(x - o) <= AGREE_TOL_S]
        if len(grp) > best_n:
            best_n, best_med = len(grp), float(np.median(grp))
    const_in = [w for w in good if abs(w["offset"] - best_med) <= AGREE_TOL_S]
    # deriva linear (Theil–Sen) sobre as janelas claras
    ts, ys = [w["t"] for w in good], [w["offset"] for w in good]
    a, b = _theil_sen(ts, ys)
    lin_in = [w for w in good if abs(w["offset"] - (a + b * w["t"])) <= AGREE_TOL_S]
    drift_total = b * duration
    need = max(3, int(np.ceil(0.6 * len(good))))
    use_linear = len(lin_in) >= max(4, need) and len(lin_in) >= len(const_in) + 2 and abs(drift_total) >= 1.5 and 0.9 <= 1 + b <= 1.1
    if use_linear:
        chosen = lin_in
        out.update(mode="linear", offset_s=round(a, 3), scale=round(1 + b, 8), drift_s_per_hour=round(b * 3600, 2))
    elif len(const_in) >= need:
        chosen = const_in
        out.update(mode="offset", offset_s=round(best_med, 3))
    else:
        out["reason"] = ("As janelas não concordam entre si (desvios diferentes ao longo do filme): pode ser outro corte/versão do filme, uma legenda forçada, "
                         "áudio dobrado ou muita música. Não se conclui nada.")
        return out
    for w in chosen:
        w["used"] = True
    out["agree"] = len(chosen)
    out["confidence"] = round(len(chosen) / total, 2)
    if out["confidence"] < 0.4:
        out.update(mode="inconclusive", reason="Menos de 40 % das janelas concordam: resultado pouco fiável, não se conclui nada.")
        for w in wins:
            w["used"] = False
        return out
    resid = [abs(w["offset"] - (out["offset_s"] + (out["scale"] - 1) * w["t"])) for w in chosen]
    out["residual_s"] = round(float(np.median(resid)), 2)
    if out["mode"] == "offset" and abs(out["offset_s"]) < 0.4:
        out["mode"] = "aligned"                                   # já está alinhada com a fala
    elif out["mode"] == "linear" and abs(out["offset_s"]) < 0.4 and abs(drift_total) < 1.0:
        out["mode"] = "aligned"
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
    """Corre as janelas e devolve o resultado de `combine` (+ janelas). Lança AudioSyncError se não for possível ler o áudio.
    Resultado final: t_novo = scale · t + offset_s (em segundos); `mode`: aligned | offset | linear | inconclusive."""
    extract = extract or extract_window
    if not cues:
        raise AudioSyncError("A legenda não tem falas.")
    starts = window_starts(duration)
    acts: list[tuple[float, np.ndarray, float]] = []
    failed = 0
    for i, t0 in enumerate(starts):
        if progress:
            progress(i, len(starts), f"A ler o áudio ({int(t0 // 60)} min)…")
        samples = extract(path, t0, min(WINDOW_S, max(30.0, duration - t0)), ffmpeg_bin)
        if samples is None:
            failed += 1
            continue
        acts.append((t0, speech_activity(samples), len(samples) / SR))
    if progress:
        progress(len(starts), len(starts), "A comparar com a legenda…")
    if not acts:
        raise AudioSyncError("Não foi possível extrair o áudio do vídeo (ffmpeg falhou, ficheiro inacessível ou sem faixa de áudio).")
    best = None
    for r in RATIOS:
        cr = [(int(s * r), int(e * r)) for s, e in cues] if r != 1.0 else cues
        wins = []
        for t0, act, length in acts:
            res = window_offset(act, cr, t0)
            wins.append({"t": round(t0 + length / 2, 1), "offset": None if res is None else round(res[0], 2), "z": 0.0 if res is None else round(res[1], 1)})
        out = combine(wins, duration)
        out["ratio"], out["windows"] = r, wins
        key = (out["mode"] != "inconclusive", out["agree"])
        if best is None or key > (best["mode"] != "inconclusive", best["agree"]):
            best = out
        if r == 1.0 and out["mode"] != "inconclusive" and out["confidence"] >= 0.6:
            break                                                  # 1.º teste já concordou bem: não se procuram deriva de frame rate
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
