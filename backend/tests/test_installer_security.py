import os
import zipfile
import io

import pytest

from app.installer import (InstallConflict, InvalidDownload, build_dest_name, install, plan_install, safe_zip_pick, stage,
                           validate_download)
from app.safepaths import PathSecurityError, require_within, sanitize_filename
from tests.conftest import make_srt, make_zip


def test_require_within_blocks_traversal_and_symlink(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (root / "link").symlink_to(tmp_path / "outside")
    assert require_within(root / "a" / ".." / "b", [root])
    with pytest.raises(PathSecurityError):
        require_within(root / ".." / "outside", [root])
    with pytest.raises(PathSecurityError):
        require_within(root / "link" / "x.srt", [root])
    with pytest.raises(PathSecurityError):
        require_within("/etc/passwd", [root])
    with pytest.raises(PathSecurityError):
        require_within(str(root) + "\x00/x", [root])
    # prefixo parecido não conta ("/r" vs "/rx")
    (tmp_path / "rx").mkdir()
    with pytest.raises(PathSecurityError):
        require_within(tmp_path / "rx", [root])


def test_sanitize_and_dest_names():
    assert sanitize_filename('a<b>:c?.srt') == "a_b__c_.srt"
    assert build_dest_name("/m/Filme (2024)/Filme (2024).mkv", "pt-PT") == "Filme (2024).pt-PT.srt"
    assert build_dest_name("/m/S/Serie.S01E01.mkv", "en", forced=True) == "Serie.S01E01.en.forced.srt"
    assert build_dest_name("/m/S/Serie.S01E01.mkv", "pt-BR", hi=True) == "Serie.S01E01.pt-BR.sdh.srt"


def test_validate_rejects_bad_downloads():
    for blob, msg in [(b"", "vazio"), (b"<!DOCTYPE html><html><body>Rate limit</body></html>", "HTML"),
                      (b"hello world no subtitle", "reconhecido"), (b"\x00\x01\x02" * 100, "binário")]:
        with pytest.raises(InvalidDownload, match=msg):
            validate_download("x.srt", blob)
    v = validate_download("ok.srt", make_srt(40).encode())
    assert v.ext == ".srt" and len(v.parsed.cues) == 40


def test_zip_picks_matching_episode_and_ignores_paths():
    z = make_zip({"../../evil.srt": make_srt(40).encode(), "Show.S01E02.srt": make_srt(50).encode(), "readme.txt": b"hi"})
    name, data = safe_zip_pick(z, {"kind": "episode", "season": 1, "episode": 2})
    assert name == "Show.S01E02.srt"           # só basename; nada é escrito com nomes do arquivo
    name, _ = safe_zip_pick(make_zip({"../../evil.srt": make_srt(40).encode()}))
    assert name == "evil.srt" and "/" not in name


def test_zip_bomb_and_garbage_rejected():
    with pytest.raises(InvalidDownload):
        safe_zip_pick(make_zip({"a.srt": b"0" * (7 * 1024 * 1024)}))      # rácio altíssimo
    with pytest.raises(InvalidDownload):
        safe_zip_pick(make_zip({"a.exe": b"MZ"}))
    with pytest.raises(InvalidDownload):
        safe_zip_pick(b"PK\x03\x04garbage")


def _setup(tmp_path):
    lib = tmp_path / "lib"
    d = lib / "Filme (2024)"
    d.mkdir(parents=True)
    video = d / "Filme (2024).mkv"
    video.write_bytes(b"v" * 100)
    staging = tmp_path / "st"
    staged = stage(staging, validate_download("x.srt", make_srt(40).encode()))
    return lib, d, video, staged


def test_install_creates_file_and_never_overwrites(tmp_path):
    lib, d, video, staged = _setup(tmp_path)
    dest = str(d / "Filme (2024).pt-PT.srt")
    out = install(staged, dest, [str(lib)], tmp_path / "bk")
    assert out == dest and os.path.exists(dest)
    assert not [p for p in d.iterdir() if p.name.endswith(".subsync-tmp")]
    assert video.read_bytes() == b"v" * 100
    staged2 = stage(tmp_path / "st", validate_download("y.srt", make_srt(41).encode()))
    before = open(dest, "rb").read()
    with pytest.raises(InstallConflict):
        install(staged2, dest, [str(lib)], tmp_path / "bk", "fail")
    assert open(dest, "rb").read() == before                       # intacto
    kept = install(staged2, dest, [str(lib)], tmp_path / "bk", "keep_both")
    assert kept.endswith(".2.srt") and open(dest, "rb").read() == before
    rep = install(staged2, dest, [str(lib)], tmp_path / "bk", "replace")
    assert rep == dest and open(dest, "rb").read() != before
    assert len(list((tmp_path / "bk").iterdir())) == 1               # cópia de segurança


def test_install_outside_authorized_roots_refused(tmp_path):
    lib, d, video, staged = _setup(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(PathSecurityError):
        install(staged, str(other / "x.srt"), [str(lib)], tmp_path / "bk")
    with pytest.raises(PathSecurityError):
        install(staged, str(lib / ".." / "other" / "x.srt"), [str(lib)], tmp_path / "bk")


def test_install_readonly_dir_clear_error(tmp_path):
    lib, d, video, staged = _setup(tmp_path)
    d.chmod(0o555)
    try:
        if os.access(d, os.W_OK):
            pytest.skip("a correr como root")
        with pytest.raises(PermissionError):
            install(staged, str(d / "x.srt"), [str(lib)], tmp_path / "bk")
    finally:
        d.chmod(0o755)


def test_plan_conflicts(tmp_path):
    lib, d, video, staged = _setup(tmp_path)
    media = {"path": str(video), "dir": str(d)}
    subs = [{"kind": "external", "present": 1, "lang": "pt-PT", "forced": 0, "hi": 0, "path": str(d / "Outro.pt.srt")}]
    p = plan_install(media, "pt-PT", [str(lib)], subs)
    assert p.conflict == "same_language"
    assert plan_install(media, "en", [str(lib)], subs).conflict is None
    (d / "Filme (2024).en.srt").write_text("x")
    assert plan_install(media, "en", [str(lib)], subs).conflict == "exists"
    with pytest.raises(PathSecurityError):
        plan_install(media, "en", [str(tmp_path / "nowhere")], subs)
