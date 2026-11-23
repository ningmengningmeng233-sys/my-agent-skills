"""LLM 后端、改写后端与 CLI 子命令测试。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from v4.core import decisions, rewrite
from v4.core.audit import append_audit, now_iso
from v4.core.cli import main
from v4.core.hashing import sha256_bytes
from v4.core.jsonio import read_json, write_json_atomic
from v4.core.llm import LLMConfig, LLMError, call_llm, load_config
from v4.core.manifest import ManifestStore
from v4.core.paths import RULES_PATH
from v4.core.rewrite_backend import (
    Mapping,
    _collect_mappings,
    _needs_llm,
    _replace_all,
    rule_based_rewrite,
    rule_then_llm_backend,
)
from v4.core.workspace import create_workspace


def test_load_config_defaults(monkeypatch: object) -> None:
    """环境变量缺省时使用 DeepSeek 默认值。"""
    for key in (
        "NOVEL_LLM_PROVIDER",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "NOVEL_LLM_ENABLED",
        "NOVEL_LLM_DRY_RUN",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = load_config()
    assert cfg.provider == "deepseek"
    assert cfg.api_key == ""
    assert cfg.base_url == "https://api.deepseek.com/v1"
    assert cfg.model == "deepseek-chat"
    assert cfg.enabled is True
    assert cfg.dry_run is False
    assert cfg.timeout == 120.0
    assert cfg.max_retries == 2


def test_load_config_from_env(monkeypatch: object) -> None:
    """环境变量可覆盖全部配置项。"""
    monkeypatch.setenv("NOVEL_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEEPSEEK_MODEL", "test-model")
    monkeypatch.setenv("NOVEL_LLM_ENABLED", "0")
    monkeypatch.setenv("NOVEL_LLM_DRY_RUN", "1")
    monkeypatch.setenv("DEEPSEEK_TIMEOUT", "30")
    monkeypatch.setenv("DEEPSEEK_MAX_RETRIES", "0")

    cfg = load_config()
    assert cfg.provider == "deepseek"
    assert cfg.api_key == "sk-test"
    assert cfg.base_url == "https://example.com/v1"
    assert cfg.model == "test-model"
    assert cfg.enabled is False
    assert cfg.dry_run is True
    assert cfg.timeout == 30.0
    assert cfg.max_retries == 0


def test_call_llm_dry_run() -> None:
    cfg = LLMConfig(
        provider="deepseek",
        api_key="",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        timeout=120.0,
        enabled=True,
        dry_run=True,
        max_retries=2,
    )
    text = call_llm([{"role": "user", "content": "hello"}], config=cfg)
    assert text.startswith("[DRY-RUN]")
    assert "hello" in text


def test_call_llm_disabled_raises() -> None:
    cfg = LLMConfig(
        provider="deepseek",
        api_key="",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        timeout=120.0,
        enabled=False,
        dry_run=False,
        max_retries=2,
    )
    try:
        call_llm([{"role": "user", "content": "x"}], config=cfg)
    except LLMError as exc:
        assert "已禁用" in str(exc)
    else:
        raise AssertionError("禁用 LLM 应抛 LLMError")


def test_call_llm_missing_key_raises() -> None:
    cfg = LLMConfig(
        provider="deepseek",
        api_key="",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        timeout=120.0,
        enabled=True,
        dry_run=False,
        max_retries=2,
    )
    try:
        call_llm([{"role": "user", "content": "x"}], config=cfg)
    except LLMError as exc:
        assert "API key" in str(exc)
    else:
        raise AssertionError("缺少 API key 应抛 LLMError")


def _make_context(**overrides: object) -> rewrite.RewriteContext:
    defaults = {
        "chapter_id": "ch-001",
        "title": "第一章 相遇",
        "source_text": "森田推开了神社的大门。",
        "events": {"events": []},
        "project_decisions": {"entries": []},
        "chapter_decisions": {"entries": []},
        "rules_tiers": {
            "must_replace": [{"source": "森田", "target": "林田"}],
            "functional_adaptation": [{"source": "神社", "target": "祠堂"}],
            "default_keep": ["魔法"],
        },
        "rules_version": "v4-core-1",
        "workspace": None,
    }
    defaults.update(overrides)
    return rewrite.RewriteContext(**defaults)


def test_collect_mappings_extracts_source_target_pairs() -> None:
    tiers = {
        "must_replace": [{"source": "A", "target": "a"}, "ignored-token"],
        "functional_adaptation": [{"source": "B", "target": "b", "note": "x"}],
    }
    mappings = _collect_mappings(tiers)
    assert len(mappings) == 2
    assert mappings[0] == Mapping("A", "a", "must_replace")
    assert mappings[1] == Mapping("B", "b", "functional_adaptation", "x")


def test_rule_based_rewrite_replaces_mappings() -> None:
    ctx = _make_context()
    output = rule_based_rewrite(ctx)
    assert "林田" in output
    assert "森田" not in output
    assert "祠堂" in output
    assert "神社" not in output
    assert output.startswith("第一章 相遇")


def test_rule_based_rewrite_avoids_substring_collision() -> None:
    ctx = _make_context(
        source_text="神社祭与神社。",
        rules_tiers={
            "must_replace": [
                {"source": "神社祭", "target": "庙会"},
                {"source": "神社", "target": "祠堂"},
            ],
            "functional_adaptation": [],
            "default_keep": [],
        },
    )
    output = rule_based_rewrite(ctx)
    assert "庙会" in output
    assert "神社祭" not in output
    assert "祠堂" in output


def test_replace_all_skips_target_suffix_overlap() -> None:
    """目标后缀与后续原文重叠时，不应产生相邻重复词。"""
    assert _replace_all("心中ドキドキ直跳", "ドキドキ", "怦怦直跳") == "心中怦怦直跳"
    # 无重叠时行为与 str.replace 一致
    assert _replace_all("森田推门", "森田", "林田") == "林田推门"


def test_rule_based_rewrite_avoids_duplicate_from_overlap() -> None:
    ctx = _make_context(
        source_text="心中ドキドキ直跳。",
        rules_tiers={
            "must_replace": [{"source": "ドキドキ", "target": "怦怦直跳"}],
            "functional_adaptation": [],
            "default_keep": [],
        },
    )
    output = rule_based_rewrite(ctx)
    assert output == "第一章 相遇\n\n心中怦怦直跳。"
    assert "直跳直跳" not in output


def test_needs_llm_detects_functional_adaptation() -> None:
    mappings = [
        Mapping("神社", "祠堂", "functional_adaptation"),
        Mapping("森田", "林田", "must_replace"),
    ]
    assert _needs_llm("走进了神社。", mappings) is True
    assert _needs_llm("走进了林田的家。", mappings) is False


def test_rule_then_llm_backend_dry_run_uses_rules_only() -> None:
    cfg = LLMConfig(
        provider="deepseek",
        api_key="",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        timeout=120.0,
        enabled=True,
        dry_run=True,
        max_retries=2,
    )
    backend = rule_then_llm_backend(cfg)
    ctx = _make_context()
    output = backend(ctx)
    assert "林田" in output
    assert "祠堂" in output
    assert not output.startswith("[DRY-RUN]")


def _build_workspace_with_chapter(tmp: Path, *, state: str = "ready_for_rewrite") -> Path:
    ws = tmp / "ws-cli"
    create_workspace(ws, title="CLI 测试", author="test", input_language="zh-CN")
    source_text = "第一章 相遇\n\n森田推门进入大厅。\n\n森田向老板说明来意，老板答应借宿。\n"
    ch_dir = ws / "chapters" / "ch-0001"
    ch_dir.mkdir(parents=True, exist_ok=True)
    (ch_dir / "source.md").write_text(source_text, encoding="utf-8")

    from v4.core import events as events_mod
    from v4.core import fingerprints

    source_sha = sha256_bytes(source_text.encode("utf-8"))
    ev1 = events_mod.new_event(
        "e-0001", 1, participants=["森田"], action="推门进入大厅",
        result="进入大厅", world_function="无", confidence=0.9,
    )
    ev2 = events_mod.new_event(
        "e-0002", 2, participants=["森田", "老板"], prerequisites=["e-0001"],
        action="向老板说明来意", result="老板答应借宿", world_function="无",
        confidence=0.9,
    )
    events_doc = events_mod.build_event_graph("ch-0001", source_sha, [ev1, ev2])
    (ch_dir / "events.json").write_text(
        json.dumps(events_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    decisions_doc = {
        "schema_version": "v4-chapter-decisions-1",
        "chapter_id": "ch-0001",
        "revision": 1,
        "entries": [],
    }
    (ch_dir / "decisions.json").write_text(
        json.dumps(decisions_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ch_dir / "audit.jsonl").touch()

    fingerprints_rec = fingerprints.chapter_fingerprints(
        source=ch_dir / "source.md",
        events=ch_dir / "events.json",
        project_decisions=ws / "project-decisions.json",
        chapter_decisions=ch_dir / "decisions.json",
        rules=RULES_PATH,
        output=ch_dir / "output.md",
        checks=ch_dir / "checks.json",
    )

    chapter = {
        "id": "ch-0001",
        "ordinal": 1,
        "title": "第一章 相遇",
        "state": state,
        "paths": {
            "source": "chapters/ch-0001/source.md",
            "events": "chapters/ch-0001/events.json",
            "decisions": "chapters/ch-0001/decisions.json",
            "output": "chapters/ch-0001/output.md",
            "checks": "chapters/ch-0001/checks.json",
            "audit": "chapters/ch-0001/audit.jsonl",
        },
        "fingerprints": fingerprints_rec,
        "checks": {
            "hard_gate": "not_run",
            "event_comparison": "not_run",
            "last_run": None,
        },
        "blockers": [],
        "decisions": decisions.decision_summary(decisions_doc),
        "enhancements": {
            "reference_assets_used": False,
            "blind_review": {"enabled": False, "result": "not_enabled"},
        },
        "updated_at": now_iso(),
    }
    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = [chapter]
    write_json_atomic(ws / "manifest.json", manifest)
    append_audit(ws / "audit.jsonl", "state_change_detail", chapter_id="ch-0001", to=state)
    return ws


def test_cli_init_creates_workspace(monkeypatch: object) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "new-ws"
        monkeypatch.setattr(
            "sys.argv",
            ["novel", "init", str(ws), "--title", "测试小说", "--author", "tester"],
        )
        assert main() == 0
        assert (ws / "manifest.json").is_file()
        manifest = read_json(ws / "manifest.json")
        assert manifest["project"]["title"] == "测试小说"
        assert manifest["project"]["author"] == "tester"


def test_cli_full_pipeline_placeholder(monkeypatch: object) -> None:
    """CLI placeholder 后端完整流程：rewrite → check → merge。"""
    with tempfile.TemporaryDirectory() as tmp:
        ws = _build_workspace_with_chapter(Path(tmp), state="ready_for_rewrite")

        monkeypatch.setattr(
            "sys.argv",
            ["novel", "rewrite", str(ws), "--chapter", "ch-0001", "--backend", "placeholder"],
        )
        assert main() == 0

        monkeypatch.setattr(
            "sys.argv",
            ["novel", "check", str(ws), "--chapter", "ch-0001"],
        )
        assert main() == 0

        manifest = ManifestStore(ws).load()
        assert manifest["chapters"][0]["state"] == "passed"

        monkeypatch.setattr(
            "sys.argv",
            ["novel", "approve", str(ws), "--chapter", "ch-0001", "--version", "v001"],
        )
        assert main() == 0
        manifest = ManifestStore(ws).load()
        assert manifest["chapters"][0]["state"] == "approved"

        monkeypatch.setattr(
            "sys.argv",
            ["novel", "merge", str(ws)],
        )
        assert main() == 0
        assert (ws / "output" / "localized.md").is_file()


# ── 小段数分段的拆段容忍（真实日语源实测缺陷） ──────────────────────────


def test_paragraph_count_allows_split_for_small_expected() -> None:
    """expected ≤ 5 时必须允许「长段拆成多段」这一合法译文形态。

    真实日语源实测：整章仅 1 个长段时，模型输出 5~8 段（正常翻译拆段），
    旧逻辑对 expected ≤ 5 只接受完全相等，于是把合格译文判失败并**静默回退
    未翻译的规则稿**，章节永远无法通过 check。方向性约束：允许拆段，
    不允许吞段（actual < expected 视为可能丢内容）。
    """
    from v4.core.rewrite_backend import _paragraph_count_ok

    # 整章单段被合理拆成多段
    for actual in (1, 2, 5, 7, 8):
        assert _paragraph_count_ok(actual, 1, ratio=0.10, minimum=4) is True, actual
    # 吞段（内容丢失方向）必须拒绝
    assert _paragraph_count_ok(0, 1, ratio=0.10, minimum=4) is False
    # 无限膨胀必须拒绝（防止灌水/幻觉填充）
    assert _paragraph_count_ok(50, 1, ratio=0.10, minimum=4) is False
    # expected=4：允许拆段但拒绝吞段
    assert _paragraph_count_ok(4, 4, ratio=0.10, minimum=4) is True
    assert _paragraph_count_ok(10, 4, ratio=0.10, minimum=4) is True
    assert _paragraph_count_ok(2, 4, ratio=0.10, minimum=4) is False


def test_paragraph_count_large_expected_tolerance_unchanged() -> None:
    """大段数分段保持既有对称容忍语义（不在本次修复范围内放松）。"""
    from v4.core.rewrite_backend import _paragraph_count_ok

    assert _paragraph_count_ok(20, 20, ratio=0.10, minimum=4) is True
    assert _paragraph_count_ok(22, 20, ratio=0.10, minimum=4) is True   # 容差 4
    assert _paragraph_count_ok(18, 20, ratio=0.10, minimum=4) is True   # 容差 4
    assert _paragraph_count_ok(30, 20, ratio=0.10, minimum=4) is False
    assert _paragraph_count_ok(10, 20, ratio=0.10, minimum=4) is False


# ── 章节标题本地化（真实日语源实测缺陷：标题未译 → 残留门永久 rework） ──


def test_contains_japanese_detects_kana_only() -> None:
    """以假名判定「需本地化」：汉字与中文共享，不能作为判据。"""
    from v4.core.rewrite_backend import _contains_japanese

    assert _contains_japanese("第3章 学院の門") is True    # の 是平假名
    assert _contains_japanese("失われた御守り") is True
    assert _contains_japanese("出会い") is True
    assert _contains_japanese("第3章 学院之门") is False   # 纯汉字已是中文
    assert _contains_japanese("第三章 相遇") is False
    assert _contains_japanese("") is False


def test_replace_leading_title_rewrites_first_block() -> None:
    """标题本地化后必须同步替换规则稿首块，否则下游剥离/装配会错位。"""
    from v4.core.rewrite_backend import _replace_leading_title

    text = "第3章 学院の門\n\n学院の門は黒い鉄でできていた。"
    out = _replace_leading_title(text, "第3章 学院の門", "第三章 学院之门")
    assert out.startswith("第三章 学院之门")
    assert "学院の門" not in out.split("\n\n")[0]
    # 未匹配时原样返回（不做破坏性改动）
    assert _replace_leading_title(text, "别的标题", "新标题") == text


def test_localize_title_applies_rules_without_llm() -> None:
    """LLM 未启用时仍应用规则映射（确定性兜底，不引入额外调用）。"""
    from v4.core.llm import LLMConfig
    from v4.core.rewrite import RewriteContext
    from v4.core.rewrite_backend import _localize_title

    cfg = LLMConfig(provider="deepseek", api_key="", base_url="https://api.deepseek.com/v1",
                    model="deepseek-chat", enabled=False, dry_run=False,
                    timeout=5.0, max_retries=0)
    ctx = RewriteContext(
        chapter_id="ch-0003", title="第3章 学院の門", source_text="第3章 学院の門\n\n本文。",
        events={"events": []}, project_decisions={"entries": []},
        chapter_decisions={"entries": []},
        rules_tiers={"must_replace": [{"source": "門", "target": "门"}],
                     "functional_adaptation": [], "default_keep": []},
        rules_version="v4-core-2", workspace=None,
    )
    localized = _localize_title(ctx, cfg, ctx.title)
    assert "門" not in localized, f"规则层必须替换可映射词，实际={localized}"
    assert "门" in localized


# ── 返修不得毁掉源内容 / 不得注入新剧情（盲测批次实测缺陷） ─────────────


def test_repair_preserves_content_accepts_faithful_strengthening() -> None:
    """忠实强化（保留原段落内容、只加悬念落点）应通过内容守卫。"""
    from v4.core.rewrite_backend import _repair_preserves_content

    original = "月亮从云层的缝隙间探出头来。"
    strengthened = "云缝里，月亮探出头来——它似乎也在看着他们。"
    assert _repair_preserves_content(original, strengthened) is True


def test_repair_preserves_content_rejects_invented_hook() -> None:
    """凭空编造的钩子必须被拒（真实实测：把「三天后两人成了挚友」换成「穿黑斗篷的人盯着她」）。"""
    from v4.core.rewrite_backend import _repair_preserves_content

    original = "三天后，两人已经成了可以称为挚友的关系。"
    invented = "车门关上的那一瞬，凛瞥见站台尽头有个穿黑斗篷的人正盯着她。"
    assert _repair_preserves_content(original, invented) is False


def test_repair_preserves_content_ignores_non_chinese() -> None:
    """无汉字可比时不做判断（放行），避免误伤纯对白/纯符号段落。"""
    from v4.core.rewrite_backend import _repair_preserves_content

    assert _repair_preserves_content("……！", "——？") is True


def test_hook_repair_keeps_original_when_repair_invents(monkeypatch) -> None:
    """章末钩子返修若返回编造内容，必须保留原结尾而不是无条件替换。"""
    from v4.core import rewrite_backend as rb

    monkeypatch.setattr(rb.localization_tone, "hook_signal_check",
                        lambda text: type("R", (), {"result": "warning"})())
    monkeypatch.setattr(
        rb, "call_llm",
        lambda *a, **kw: "车门关上的那一瞬，凛瞥见站台尽头有个穿黑斗篷的人正盯着她。",
    )
    context = type("C", (), {"workspace": None, "chapter_id": "ch-0001"})()
    original = "第五章 试炼\n\n少女继续向南走去。\n\n三天后，两人已经成了可以称为挚友的关系。"
    out = rb._repair_chapter_hook(context, None, original, "第五章 试炼")
    assert "挚友" in out, "编造的钩子不得替换掉源内容"
    assert "黑斗篷" not in out


def test_hook_repair_applies_faithful_repair(monkeypatch) -> None:
    """忠实的钩子强化仍应生效（不能因为守卫而禁用该功能）。"""
    from v4.core import rewrite_backend as rb

    monkeypatch.setattr(rb.localization_tone, "hook_signal_check",
                        lambda text: type("R", (), {"result": "warning"})())
    monkeypatch.setattr(
        rb, "call_llm",
        lambda *a, **kw: "门在身后合上，走廊尽头传来脚步声。",
    )
    context = type("C", (), {"workspace": None, "chapter_id": "ch-0002"})()
    original = "第五章 试炼\n\n少女走进走廊。\n\n门在身后合上，她继续向前。"
    out = rb._repair_chapter_hook(context, None, original, "第五章 试炼")
    assert "走廊尽头传来脚步声" in out


# ── 分段失败不得直接回退成未翻译源文（跨语言实测：规则稿=源语言） ──────


def test_failed_chunk_is_retried_before_fallback(monkeypatch) -> None:
    """分段失败先重试一次；重试成功时不得回退到规则稿。

    实测背景：跨语言下「回退规则稿」等于回退**未翻译的源文**（如整句日文），
    该章必然被残留门拦住、无法通过。分段失败多为瞬时（段数校验/拒答），
    先重试一次能显著减少这种无谓失败。
    """
    from v4.core import rewrite_backend as rb
    from v4.core.llm import LLMConfig

    calls = {"n": 0}

    def fake_chunk(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None                     # 首次失败
        return ["第一章 相遇", "这是重试后的译文。"]   # 重试成功

    monkeypatch.setattr(rb, "_call_chunk", fake_chunk)
    cfg = LLMConfig(provider="deepseek", api_key="sk-x", base_url="https://api.deepseek.com/v1",
                    model="deepseek-chat", enabled=True, dry_run=False, timeout=5.0, max_retries=0)
    context = _context_for_chunk_retry()
    out = rb.llm_enhanced_rewrite(context, config=cfg)

    assert calls["n"] == 2, f"应恰好重试一次，实际调用 {calls['n']} 次"
    assert "这是重试后的译文" in out, f"重试成功的内容应被采用，实际输出={out!r}"


def test_chunk_falls_back_only_after_retry_fails(monkeypatch) -> None:
    """重试仍失败时**响亮失败**，不再静默回退规则稿。

    契约变更（2026-09-18 实测事故）：旧行为是「重试仍失败 → 回退规则稿并返回」，
    于是全部分段失败时成品读起来正常（就是规则稿本体）却报「改写完成」，
    退出码 0。真实全章实测：三个分段全回退后 185/301 段与源稿一字不差。
    新行为：一个分段都没成活就抛 LLMError，章节进入 error（可 novel recover）。
    """
    from v4.core import rewrite_backend as rb
    from v4.core.llm import LLMConfig, LLMError

    calls = {"n": 0}

    def always_fail(*args, **kwargs):
        calls["n"] += 1
        return None

    monkeypatch.setattr(rb, "_call_chunk", always_fail)
    cfg = LLMConfig(provider="deepseek", api_key="sk-x", base_url="https://api.deepseek.com/v1",
                    model="deepseek-chat", enabled=True, dry_run=False, timeout=5.0, max_retries=0)
    context = _context_for_chunk_retry()

    with pytest.raises(LLMError, match="拒绝提交未本地化的规则稿"):
        rb.llm_enhanced_rewrite(context, config=cfg)
    assert calls["n"] == 2, f"应尝试两次（1 次 + 重试 1 次），实际 {calls['n']} 次"


def _context_for_chunk_retry():
    from v4.core import checks
    from v4.core.rewrite import RewriteContext

    return RewriteContext(
        chapter_id="ch-0001",
        title="第一章 相遇",
        source_text="第一章 相遇\n\n森田推门进入大厅。\n\n森田向老板说明来意。",
        events={"events": []},
        project_decisions={"entries": []},
        chapter_decisions={"entries": []},
        rules_tiers=checks.load_rules_tiers(),
        rules_version="v4-core-2",
        workspace=None,
    )
