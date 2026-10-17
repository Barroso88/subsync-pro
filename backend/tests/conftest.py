from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest

from app import ffprobe as fp
from app.config import AppConfig
from app.services import App

EN = ["I don't know what you are talking about, but this is not the time.", "You have to understand that we can't wait for them.",
      "What are you doing here with all of that? It was just a question.", "They said that he would come back for you and your friend.",
      "This is the one thing that I know about the world, and you know it too.", "Can you tell me where she went? I will not do this without her."]
PT_PT = ["Não sei do que estás a falar, mas este não é o momento para isso.", "Tu tens de perceber que não podemos esperar por eles, pois não?",
         "O que é que estás a fazer aqui com tudo isso? Era só uma pergunta, miúdo.", "Disseram que ele ia voltar para ti e para o teu amigo, que está no autocarro.",
         "É a única coisa que eu sei sobre o mundo, e tu também sabes isso muito bem.", "Podes dizer-me para onde ela foi? Não vou fazer isto sem ela, gajo."]


def make_srt(n: int = 120, step_ms: int = 30_000, start_ms: int = 10_000, lang: str = "en", offset_ms: int = 0) -> str:
    pool = {"en": EN, "pt-PT": PT_PT}[lang]
    def ts(ms):
        s, ms = divmod(ms, 1000)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d},{ms:03d}"
    out = []
    for i in range(n):
        s = start_ms + i * step_ms + offset_ms + (i * 137) % 400   # pequenas variações
        out.append(f"{i + 1}\n{ts(s)} --> {ts(s + 2500)}\n{pool[i % len(pool)]}\n")
    return "\n".join(out)


def make_zip(files: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


@pytest.fixture
def media_root(tmp_path):
    root = tmp_path / "media"
    mv = root / "Filmes" / "Filme Teste (2024)"
    mv.mkdir(parents=True)
    (mv / "Filme Teste (2024).mkv").write_bytes(b"\0" * 200_000)
    (mv / "Filme Teste (2024).en.srt").write_text(make_srt(120, 30_000, lang="en"), "utf-8")
    ep = root / "Séries" / "Minha Série" / "Season 01"
    ep.mkdir(parents=True)
    (ep / "Minha Série - S01E01.mkv").write_bytes(b"\1" * 200_000)
    (ep / "Minha Série - S01E02.mkv").write_bytes(b"\2" * 200_000)
    (ep / "Minha Série - S01E01.pt-PT.srt").write_text(make_srt(120, 30_000, lang="pt-PT"), "utf-8")
    return root


FAKE_DURATION = {"Filme Teste (2024).mkv": 3700.0}


@pytest.fixture
def fake_probe(monkeypatch):
    calls = []

    def probe(path, ffprobe_bin="ffprobe", timeout=60):
        calls.append(path)
        name = Path(path).name
        subs = []
        if "S01E02" in name:
            subs = [fp.EmbeddedSub(2, "subrip", "en", "eng", None, False, False, True)]
        return fp.ProbeResult(True, FAKE_DURATION.get(name, 3600.0), "h264", 1920, 1080, 23.976, subs)

    monkeypatch.setattr(fp, "probe", probe)
    return calls


@pytest.fixture
def cfg(tmp_path, media_root):
    return AppConfig(data_dir=tmp_path / "data", allowed_roots=[str(media_root)], start_background=False, max_jobs=2)


@pytest.fixture
def core(cfg, fake_probe):
    app = App(cfg)
    yield app
    app.stop()


def wait_job(core, jid, timeout=15):
    t = time.time()
    while time.time() - t < timeout:
        r = core.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if r["status"] not in ("queued", "running"):
            return dict(r)
        time.sleep(0.05)
    raise AssertionError("job timeout")
