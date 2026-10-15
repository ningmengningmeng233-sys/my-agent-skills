"""降级通道不得凭空造视频身份（T03 OC-1 的离线判定）。

`videos.aweme_id` 是主键、`transcript_versions` 按 (aweme_id, version_no) 唯一。
当 yt-dlp 下载成功但 `--print after_move:%(id)s` 拿不到 id 时，若落到常量 "ytdlp"，
所有取不到 id 的降级产物会塌进同一行，复用判定/补摘要/重建都会串片。
本合同要求：宁可白话失败，也不伪造身份。
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ingest_runner as ir  # noqa: E402


class _FakeCompleted:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _install_fake(monkeypatch, printed):
    """假 yt-dlp：只落地一个音轨文件并打印给定行，绝不联网。"""

    def fake_run(argv, **kwargs):
        out = pathlib.Path(str(argv[argv.index("-o") + 1]).replace("%(ext)s", "m4a"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake-audio")
        return _FakeCompleted(0, printed)

    monkeypatch.setattr(ir.subprocess, "run", fake_run)


def test_id_present_keeps_current_behaviour(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    _install_fake(monkeypatch, "7300000000000000000\t某视频\thttps://www.douyin.com/video/7300000000000000000\n")
    res = ir.run_yt_dlp("https://www.douyin.com/video/7300000000000000000", job,
                        executable="fake-yt-dlp")
    assert res.kind == "ok"
    assert res.payload["videos"][0]["aweme_id"] == "7300000000000000000"


def test_missing_id_must_not_fabricate_shared_identity(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    _install_fake(monkeypatch, "\t某视频\thttps://www.douyin.com/video/7300000000000000001\n")
    res = ir.run_yt_dlp("https://www.douyin.com/video/7300000000000000001", job,
                        executable="fake-yt-dlp")
    assert res.kind != "ok", "取不到 id 却返回成功，等于把这条视频塞进共享主键 " + repr(res.payload)
    if res.payload is not None:
        assert res.payload["videos"][0]["aweme_id"] != "ytdlp"
    assert res.message and "id" in res.message.lower() or "身份" in res.message, res.message


def test_missing_id_leaves_no_done_record(tmp_path, monkeypatch):
    """失败路径不得在库里留下可复用记录：只返回 platform_failed，不写库。"""
    job = tmp_path / "job"
    job.mkdir()
    _install_fake(monkeypatch, "\t某视频\thttps://example.invalid/x\n")
    res = ir.run_yt_dlp("https://example.invalid/x", job, executable="fake-yt-dlp")
    assert res.kind == "platform_failed", res.kind
    assert res.payload is None


def test_degrade_failure_reason_is_propagated(tmp_path, monkeypatch):
    """降级已经走了 yt-dlp，失败时就不能再回「上游不可用，请改用 --channel yt-dlp」。

    评审 I-1：`_degrade_to_yt_dlp` 丢掉了 run_yt_dlp 的真实原因，换成一句指错方向的建议。
    """
    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.setattr(ir, "yt_dlp_path", lambda: "fake-yt-dlp")
    monkeypatch.setattr(ir, "run_yt_dlp", lambda ui, jd, *, executable: ir.IngestRunResult(
        "platform_failed", None, "降级通道没能取得视频 id（身份缺失），已停止以免塌进同一条记录"))
    res = ir._degrade_to_yt_dlp("https://example.invalid/x", job, pin="9.9.9", installed="8.8.8")
    assert res.kind == "platform_failed"
    assert "身份" in res.message or "id" in res.message.lower(), res.message
    assert "请改用" not in res.message and "--channel yt-dlp" not in res.message, (
        "失败原因被换成指错方向的建议: " + res.message)
