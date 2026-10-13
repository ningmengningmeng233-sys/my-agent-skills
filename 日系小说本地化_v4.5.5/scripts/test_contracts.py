#!/usr/bin/env python3
"""
日系小说本地化 — 契约自测脚本 v1.0

把"文档承诺 vs 脚本行为"的契约固化为可重复测试。**每次修改任何脚本后必跑**，
防止再出现"文档说一套、脚本做一套"（历史教训：拆批超长章不切、合并空书、
分类联动失真、质检误报——都是契约断裂）。

用法:
  python test_contracts.py              # 跑全部测试
  python test_contracts.py --quick      # 只跑核心管道（拆批→分类→合并）+ 质检
  python test_contracts.py --skip-pool  # 跳过参考池测试（无索引库环境）
  python -m pytest scripts/test_contracts.py -q   # 同一批用例经 pytest 收集（CI 走这条）

退出码: 0=全部通过或显式跳过, 1=存在失败

依赖: 仅标准库；参考池测试需要非空的爽文索引库（INDEX_LIB_DIR 或 skill 内 索引库/）。
缺库时相关用例**显式跳过并打印原因**，不计入通过数（历史上它们静默 return 被算成通过）。
"""
import argparse
import os
import re
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(SCRIPTS)
for _p in (SKILL, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from conftest import run_py  # noqa: E402  B1 范式：起 Python 子进程的唯一出口

try:  # Windows 控制台乱码防护
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS, FAIL, SKIP = [], [], []


class Skipped(Exception):
    """环境前提不满足：显式跳过并留下原因（绝不静默算通过）。"""


def require(cond, reason):
    if not cond:
        raise Skipped(reason)


def _user_index_lib():
    """需要完整用户索引库的用例只认显式配置，不回退到作者机器路径。

    skill 内置索引库只是最小兜底（单卡），撑不起「15 场景无缺口」这类断言，
    因此这里不把它当默认；未配置 INDEX_LIB_DIR 时 require() 显式跳过。
    """
    base = os.environ.get("INDEX_LIB_DIR", "").strip()
    return base if base else None


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  ✅ {name}")
    except Skipped as e:
        SKIP.append((name, str(e)))
        print(f"  ⏭️  {name}: 跳过（{e}）")
    except AssertionError as e:
        FAIL.append((name, str(e)))
        print(f"  ❌ {name}: {e}")
    except Exception as e:
        FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"  ❌ {name}: {type(e).__name__}: {e}")


def t_version_changelog_consistent():
    """SKILL.md 版本号与 CHANGELOG 首版号一致（防版本号忘更新）"""
    skill = open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8").read()
    m = re.search(r'version: "(\d+\.\d+\.\d+(?:[-+.]?[A-Za-z0-9.-]+)?)"', skill)
    assert m, "SKILL.md 缺 version 字段"
    ver = m.group(1)
    log = open(os.path.join(SKILL, "CHANGELOG.md"), encoding="utf-8").read()
    m2 = re.search(r"^## v(\d+\.\d+\.\d+(?:[-+.]?[A-Za-z0-9.-]+)?)", log, re.M)
    assert m2, "CHANGELOG.md 缺版本条目"
    assert ver == m2.group(1), f"版本不一致: SKILL.md={ver} CHANGELOG={m2.group(1)}"




def t_docs_integrity():
    """文档-资产一致性（lint）通过"""
    import check_docs_integrity as di
    assert di.main() == 0, "lint 发现问题"


def _write_demo_lib_fixture(lib):
    """按 templates/章节索引模板.md 的形状造最小索引库：
    frontmatter「包含元素」组名 + 源文件 + 带行号的爽点条目 + 段文件原文。"""
    idx_dir = lib / "章节索引"
    src_dir = lib / "小说原文" / "夹具书-作者"
    idx_dir.mkdir(parents=True, exist_ok=True)
    src_dir.mkdir(parents=True, exist_ok=True)
    para = ["第1章 开局。", "这里是一句无关的过渡叙述。", "这里又是一句无关的过渡叙述。",
            "还是无关行。"]
    fight = ("少年提着断刀冲进雨里，对面是三步外才肯落地的对手，"
             "他没有后退，反而把最后半口真气压进刀脊，"
             "他知道这一刀若躲开就再也没有资格站在山门下，"
             "于是刀光落处，两个人的影子同时被雨水撕开。")
    lines = para + [fight] + ["后续无关行。"] * 3
    (src_dir / "夹具书-作者-第1章~第5章-001.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    idx = """---
小说名称: 《夹具书》
作者: 作者
索引类型: 章节索引
索引序号: '001'
章节范围: 第1章~第5章
包含元素:
  - 战斗对决 | 越级杀敌 | 反杀
  - 金句 | 悬念
源文件: 小说原文/夹具书-作者/夹具书-作者-第1章~第5章-001.md
状态: 完成
---

# 《夹具书》章节索引 001（第1章 ~ 第5章）

## 第1章 开局（行号：1 ~ 8）

### 爽点/看点

- **战斗对决**（行5-5）：少年以断刀硬接三步之外的对手，一刀定生死。

### 金句/名场面

- "于是刀光落处，两个人的影子同时被雨水撕开。"（行5，简述）
"""
    (idx_dir / "夹具书-001-idx.md").write_text(idx, encoding="utf-8", newline="\n")


def t_demo_lib_build():
    """示范库骨架生成：夹具索引库跑通「标签→组名→行号→逐字摘录」全链，
    产出文件且含摘录与出处（P30：原用例写死真实索引库，无库机器上必红）"""
    import build_demo_libs as bdl
    from pathlib import Path
    lib = Path(tempfile.mkdtemp())
    _write_demo_lib_fixture(lib)
    out = tempfile.mkdtemp()
    rc = bdl.build_one("动作戏", lib, Path(out), force=True)
    assert rc == 0, "骨架生成失败"
    f = Path(out) / "动作戏示范库-候选.md"
    assert f.exists(), "骨架文件未生成"
    content = f.read_text(encoding="utf-8")
    assert "出处: " in content and "夹具书-作者" in content, "骨架缺出处"
    assert "【拆解位】" in content, "骨架格式不完整"
    assert "行号 5-5" in content, f"未按索引行号定位: {content[:400]}"
    assert "少年提着断刀冲进雨里" in content, "骨架无正文摘录"
    assert "这里是一句无关的过渡叙述" not in content, "越界摘录了行号范围外的段落"


def t_translation_quality():
    """翻译质量体检：正常文本 0 误报"""
    import check_translation_quality as ctq
    from pathlib import Path
    d = tempfile.mkdtemp()
    p = Path(d) / "正常-001.md"
    p.write_text("他推门而出，已是华灯初上。\n她转身离开，没有回头。\n", encoding="utf-8")
    r = ctq.check_file(str(p))
    assert sum(len(v) for v in r.values()) == 0, f"误报 {r}"


def t_scene_card_anchors():
    """场景专卡锚点验证：查无=0（防编造锚点回归）"""
    _lib = _user_index_lib()
    CARD = os.path.join(_lib, "文风蒸馏卡") if _lib else "<未配置 INDEX_LIB_DIR>"
    ORIG = os.path.join(_lib, "小说原文") if _lib else "<未配置 INDEX_LIB_DIR>"
    require(os.path.isdir(CARD), f"无索引库环境：目录不存在 {CARD}")
    import verify_anchors as va
    total = 0
    for fn in os.listdir(CARD):
        if not fn.endswith("-场景卡.md"):
            continue
        text = open(os.path.join(CARD, fn), encoding="utf-8").read()
        sample = va._sample_books(text)
        for a, src in va.extract_anchors_with_sources(text):
            if src:
                bdir = va.find_book_dir(ORIG, src)
                r = va.verify_in_dir(a, bdir) if bdir else "查无"
            else:
                r = "查无"
                for b in sample:
                    bdir = va.find_book_dir(ORIG, b)
                    if bdir and va.verify_in_dir(a, bdir) == "通过":
                        r = "通过"
                        break
            assert r != "查无", f"{fn} 锚点查无: {a[:30]}"
            total += 1
    assert total > 0, "无场景卡参与验证"


def t_card_index_gaps():
    """卡库索引：15 场景候选卡全部 ≥1（无严重缺口）"""
    _lib = _user_index_lib()
    CARD = os.path.join(_lib, "文风蒸馏卡") if _lib else "<未配置 INDEX_LIB_DIR>"
    require(os.path.isdir(CARD), f"无索引库环境：目录不存在 {CARD}")
    with tempfile.TemporaryDirectory() as out:
        run_py([os.path.join(SCRIPTS, "build_card_index.py"),
                        CARD, out], check=True, capture_output=True)
        gaps = open(os.path.join(out, "缺口清单.md"), encoding="utf-8").read()
        assert "候选卡 0 张" not in gaps, "存在严重缺口（候选卡 0 张）"

# ── 工具 ──────────────────────────────────────────────
def make_batch_file(body: str, name: str, frontmatter: str = "") -> str:
    d = tempfile.mkdtemp()
    fm = frontmatter or "---\n批号: 001\n章节范围: 第1话~第2话\n映射表版本: v1\n参考池: 遮天/打脸x2段\n状态: 已完成\n---\n"
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(fm + body)
    return p


# ── 1. 拆批契约 ─────────────────────────────────────
def t_split_long_chapter():
    """超长章必须子切，每批 ≤ max_chars（允许 1.3 倍容差）"""
    import split_ja_segments as sjs
    src = tempfile.mktemp(suffix=".md")
    with open(src, "w", encoding="utf-8") as f:
        f.write("第1话 短章\n" + "他冷冷开口。" * 100 + "\n")
        f.write("第2话 超长章\n" + "狂风呼啸，他站在城头，目光如电。" * 400 + "\n")
        f.write("第3话 收尾\n" + "她转身离开。" * 100 + "\n")
    out = tempfile.mkdtemp()
    old_argv = sys.argv
    sys.argv = ["x", src, "--novel", "测试书", "--author", "测试", "--out", out, "--max-chars", "2000"]
    try:
        sjs.main()
    finally:
        sys.argv = old_argv
    files = sorted(os.listdir(out))
    assert len(files) >= 4, f"超长章应切成多批，实际 {len(files)} 批"
    for f in files:
        text = open(os.path.join(out, f), encoding="utf-8").read()
        body = text.split("---", 2)[2] if text.startswith("---") else text
        n = len(body.replace("\n", "").replace(" ", ""))
        assert n <= 2600, f"{f} 超长: {n} 字（上限 2000×1.3）"


def t_split_no_chapters():
    """无章节文本 → 按字数切分回退"""
    import split_ja_segments as sjs
    src = tempfile.mktemp(suffix=".md")
    with open(src, "w", encoding="utf-8") as f:
        f.write("没有章节标题的纯文本。" * 500)
    out = tempfile.mkdtemp()
    old_argv = sys.argv
    sys.argv = ["x", src, "--novel", "无章书", "--author", "测试", "--out", out, "--max-chars", "1000"]
    try:
        sjs.main()
    finally:
        sys.argv = old_argv
    files = sorted(os.listdir(out))
    assert files, "无章节回退未产出批文件"
    fm = open(os.path.join(out, files[0]), encoding="utf-8").read()
    assert "按字数切分" in fm


def t_split_mixed_titles():
    """简体第X话 + 繁体第X話 + 第X章 混排识别"""
    import split_ja_segments as sjs
    src = tempfile.mktemp(suffix=".md")
    with open(src, "w", encoding="utf-8") as f:
        f.write("第1话 初见\n" + "他冷冷开口。" * 60 + "\n")
        f.write("第2話 转折\n" + "她转身离开。" * 60 + "\n")
        f.write("第3章 决战\n" + "轰然爆裂。" * 60 + "\n")
    out = tempfile.mkdtemp()
    old_argv = sys.argv
    sys.argv = ["x", src, "--novel", "测试书", "--author", "测试", "--out", out, "--max-chars", "500"]
    try:
        sjs.main()
    finally:
        sys.argv = old_argv
    files = sorted(os.listdir(out))
    assert len(files) == 3, f"混排应 3 批，实际 {len(files)}"


# ── 2. 分类契约 ─────────────────────────────────────
def t_classify_no_blank():
    """无空行批文件（拆批产物常见）→ 自动按行分段，不得判 1 段氛围 100%"""
    import classify_paragraphs as cp
    p = make_batch_file(
        "他猛然拍案而起，怒喝道：\"放肆！\"\n"
        "夜色如墨，风掠过窗棂，带着细碎的凉意。\n"
        "她快步上前，一剑斩落，血光迸溅。\n"
        "远处传来马蹄声，越来越近。\n", "无空行-001.md")
    out = p.replace(".md", ".classified.md")
    cp.process_file(p, out)
    marks = re.findall(r"分类:(\S+)", open(out, encoding="utf-8").read())
    assert len(marks) >= 3, f"应多段分类，实际 {len(marks)} 段"


def t_classify_with_blank():
    """有空行段落 → 正常空行分段不受影响"""
    import classify_paragraphs as cp
    p = make_batch_file("他猛然拍案而起。\n\n夜色如墨。\n\n她一剑斩落。\n", "有空行-001.md")
    out = p.replace(".md", ".classified.md")
    cp.process_file(p, out)
    marks = re.findall(r"分类:(\S+)", open(out, encoding="utf-8").read())
    assert len(marks) == 3, f"应有 3 段，实际 {len(marks)}"


# ── 3. 合并契约 ─────────────────────────────────────
def t_merge_both_names():
    """两种命名（-NNN.md / -NNN-改写.md）都能合并，正文完整、元数据剥离"""
    d = tempfile.mkdtemp()
    open(os.path.join(d, "书-作者-第1话~第2话-001.md"), "w", encoding="utf-8").write(
        "---\n批号: 001\n---\n第1话 初见\n他冷冷开口。\n\n---\n## 本批说明\n- 独立盲审记录: 盲审结论: 通过\n")
    open(os.path.join(d, "书-作者-第3话-002-改写.md"), "w", encoding="utf-8").write(
        "---\n批号: 002\n---\n第3话 决战\n轰然爆裂。\n\n---\n## 本批说明\n- 独立盲审记录: 盲审结论: 通过\n")
    out = tempfile.mkdtemp()
    r = run_py([os.path.join(SCRIPTS, "merge_batches.py"),
                        d, "--novel", "书", "--author", "作者", "--out", out],
                       capture_output=True)
    clean = open(os.path.join(out, "书-作者-中国化版.md"), encoding="utf-8").read()
    assert "第1话 初见" in clean and "第3话 决战" in clean, "正文缺失"
    assert "本批说明" not in clean and "盲审结论" not in clean, "元数据未剥离"
    assert r.returncode == 0


def t_merge_empty_dir():
    """空目录 → 报错退出码 2（不得静默产出空书）"""
    d = tempfile.mkdtemp()
    r = run_py([os.path.join(SCRIPTS, "merge_batches.py"),
                        d, "--novel", "书", "--author", "作者", "--out", tempfile.mkdtemp()],
                       capture_output=True)
    assert r.returncode == 2, f"应退出 2，实际 {r.returncode}"
    assert "无任何批稿" in r.stdout


def t_merge_blind_review():
    """待盲审判断：结论通过（含'曾标记待盲审'回顾）→ 判已盲审；纯待盲审 → 未盲审"""
    d = tempfile.mkdtemp()
    open(os.path.join(d, "书-作者-第1话-001-改写.md"), "w", encoding="utf-8").write(
        "---\n批号: 001\n---\n第1话\n正文。\n\n---\n## 本批说明\n- 独立盲审记录: 盲审结论: 通过（该批曾标记待盲审，补审后通过）\n")
    open(os.path.join(d, "书-作者-第2话-002-改写.md"), "w", encoding="utf-8").write(
        "---\n批号: 002\n---\n第2话\n正文。\n\n---\n## 本批说明\n- 独立盲审记录: 盲审结论: 待盲审\n")
    out = tempfile.mkdtemp()
    r = run_py([os.path.join(SCRIPTS, "merge_batches.py"),
                        d, "--novel", "书", "--author", "作者", "--out", out],
                       capture_output=True)
    assert "待盲审批次 1" in r.stdout, "应标出 1 个待盲审批次"


# ── 4. 质检契约 ─────────────────────────────────────
def t_check_localization_no_false_positive():
    """大陆正常网文 → 0 误报"""
    import check_localization as cl
    p = make_batch_file(
        "他冷笑一声，看着田野说：“你确实很强，但其实我更强。”\n"
        "他微微点头，慢慢站起来。夫君大人，君子一言驷马难追。\n"
        "他坐上电车，在拉面店吃了碗兰州拉面，又带了个便当回家。\n", "正常-001.md")
    r = cl.check_file(p)
    total = sum(len(v) for v in r.values())
    assert total == 0, f"误报 {total} 处: {r}"


def t_check_localization_detect_jp():
    """日式残留 → 必须检出"""
    import check_localization as cl
    p = make_batch_file("佐藤君，您前辈桑，今天神社有祭典。\n她笑着说：\"好痛的感觉的样子……\"\n", "日式-001.md")
    r = cl.check_file(p)
    total = sum(len(v) for v in r.values())
    assert total > 0, "日式残留未检出"


def t_check_localization_sentence_end():
    """句尾检测（$ 锚定 + re.M）：句中'……的样子'必须命中"""
    import check_localization as cl
    p = make_batch_file("第一行\n她有种说不出的感觉的样子\n第三行\n", "句尾-001.md")
    r = cl.check_file(p)
    assert sum(len(v) for v in r["翻译腔"]) >= 1, "句尾翻译腔漏报"


def t_check_localization_punct_end():
    """翻译腔句尾带标点（。！？…）收尾也必须命中（第三轮报告 #2 回归）"""
    import check_localization as cl
    p = make_batch_file(
        "她很生气的样子。\n他看起来有种被冒犯的样子。\n好像有点疼的样子……\n"
        "你这样说话的样子，其实挺可爱的。\n", "句尾标点-001.md")
    r = cl.check_file(p)
    hits = len(r["翻译腔"])
    assert hits == 3, f"带标点句尾应命中 3 处，实际 {hits}: {r['翻译腔']}"


def t_split_per_seg():
    """--chapters-per-seg N 必须生效（第三轮报告 #3 死参数回归）"""
    import split_ja_segments as sjs
    src = tempfile.mktemp(suffix=".md")
    with open(src, "w", encoding="utf-8") as f:
        for i in range(1, 5):
            f.write(f"第{i}话 短章\n" + "他冷冷开口。" * 30 + "\n")
    out = tempfile.mkdtemp()
    old_argv = sys.argv
    sys.argv = ["x", src, "--novel", "测试书", "--author", "测试", "--out", out,
                "--max-chars", "5000", "--chapters-per-seg", "1"]
    try:
        sjs.main()
    finally:
        sys.argv = old_argv
    files = sorted(os.listdir(out))
    assert len(files) == 4, f"--chapters-per-seg 1 应 4 批，实际 {len(files)}"


def t_check_ratio_bounds():
    """篇幅守恒边界：95% 过 / 150% 超限 / 79% 不足"""
    import check_ratio as cr
    src = tempfile.mktemp(suffix=".md")
    open(src, "w", encoding="utf-8").write("原文内容。" * 100)
    for ratio, expect_ok in [(0.95, True), (1.50, False), (0.79, False)]:
        out = tempfile.mktemp(suffix=".md")
        body = open(src, encoding="utf-8").read()
        n = int(len(body) * ratio)
        # 精确补足目标长度（单字符填充，避免多字节词导致比率偏差）
        open(out, "w", encoding="utf-8").write(body[:n] if n < len(body) else body + "字" * (n - len(body)))
        old_argv = sys.argv
        sys.argv = ["x", src, out]
        try:
            cr.main()
            passed = True
        except SystemExit as e:
            passed = (e.code == 0)
        finally:
            sys.argv = old_argv
        assert passed == expect_ok, f"ratio={ratio}: 期望{'过' if expect_ok else '拦'}，实际{'过' if passed else '拦'}"


def t_check_source_health_detect():
    """体检：乱码/空批/批号缺失/重复章节全检出"""
    import check_source_health as csh
    d = tempfile.mkdtemp()
    open(os.path.join(d, "书-作者-第1话~第2话-001.md"), "w", encoding="utf-8").write(
        "第1话 初见\n他冷冷开口。\n第2话 转折\n她离开。\n")
    open(os.path.join(d, "书-作者-第3话~第4话-002.md"), "w", encoding="utf-8").write(
        "第1话 初见\n她笑着说。�乱码\n")
    open(os.path.join(d, "书-作者-第5话~第6话-004.md"), "w", encoding="utf-8").write("")
    _, _, issues = csh.scan_dir(d)
    joined = "\n".join(issues)
    assert "[乱码]" in joined and "[空批]" in joined and "[批号缺失]" in joined and "重复章节" in joined


# ── 5. 断点库契约 ───────────────────────────────────
def t_index_db_semantics():
    """断点库：pending→done→should_skip / hash 失效 / 输出缺失 / failed"""
    sys.path.insert(0, os.path.join(SCRIPTS, "v333"))
    import index_db
    lib = tempfile.mkdtemp()
    seg = os.path.join(lib, "批001.md")
    out = os.path.join(lib, "批001-改写.md")
    open(seg, "w", encoding="utf-8").write("原文")
    open(out, "w", encoding="utf-8").write("改写")
    ib = index_db.IndexDB(lib)
    ib.mark_pending(seg, out)
    ib.mark_done(seg, out)
    assert ib.should_skip(seg, out) is True
    open(seg, "w", encoding="utf-8").write("原文改了")
    assert ib.should_skip(seg, out) is False, "hash 失效应重跑"
    os.remove(out)
    assert ib.should_skip(seg, out) is False, "输出缺失应重跑"


# ── 6. v333 路径契约 ────────────────────────────────
def t_v333_skill_dir():
    """v333 脚本 SKILL_DIR 必须解析到 skill 根（.env/索引库路径正确性）"""
    sys.path.insert(0, os.path.join(SCRIPTS, "v333"))
    import build_novel_index
    assert build_novel_index.SKILL_DIR == SKILL, f"SKILL_DIR={build_novel_index.SKILL_DIR}"


# ── 7. 参考池契约（需要真实索引库，缺库显式跳过）──────
def index_lib_seg() -> str:
    """章节索引目录：INDEX_LIB_DIR 或 skill 内 索引库/"""
    lib = os.environ.get("INDEX_LIB_DIR", "") or os.path.join(SKILL, "索引库")
    return os.path.join(lib, "章节索引")


def has_index_lib() -> bool:
    """索引库可用性：章节索引目录存在且**非空**"""
    seg = index_lib_seg()
    if not os.path.isdir(seg):
        return False
    return any(f.endswith(".md") for f in os.listdir(seg))


def _pool_missing_reason() -> str:
    return (f"索引库为空或不存在（章节索引目录：{index_lib_seg()}）；"
            "在无索引库的机器上此项不计入通过")


def t_ref_pool_basic():
    """参考池：检索/选书/轮换避开（需要非空的索引库，缺库显式跳过）"""
    require(has_index_lib(), _pool_missing_reason())
    rot = os.path.join(tempfile.mkdtemp(), "轮换.md")
    r = run_py([os.path.join(SCRIPTS, "build_ref_pool.py"),
                        "--scene", "打脸碾压", "--tags", "装逼打脸", "--rotation", rot],
                       capture_output=True, env=dict(os.environ))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "参考池:" in r.stdout


def t_ref_pool_zero_hit():
    """零命中标签 → 退出码 1（不得硬检索）"""
    require(has_index_lib(), _pool_missing_reason())
    rot = os.path.join(tempfile.mkdtemp(), "轮换.md")
    r = run_py([os.path.join(SCRIPTS, "build_ref_pool.py"),
                        "--scene", "测试", "--tags", "不存在的标签xyz", "--rotation", rot],
                       capture_output=True, env=dict(os.environ))
    assert r.returncode == 1, f"零命中应退出 1，实际 {r.returncode}"


# ── 7b. 回归保护（曾修复的 bug 固化）──────────────
def t_v333_line_bracket():
    """verify_and_fix_lines 修正行号后必须保留右括号）（B1 回归）"""
    # 脚本按 INDEX_LIB_DIR/小说原文/ 定位段文件 → 构造假索引库
    fake_lib = tempfile.mkdtemp()
    book_dir = os.path.join(fake_lib, "小说原文", "书-作者")
    os.makedirs(book_dir)
    open(os.path.join(book_dir, "书-作者-第1章~第5章-001.md"), "w", encoding="utf-8").write(
        "第1章 开头\n其他内容\n\"金句就藏在这里，厉害非常。\"\n更多内容\n")
    idx = os.path.join(tempfile.mkdtemp(), "书-作者-第1章~第5章-001-idx.md")
    open(idx, "w", encoding="utf-8").write(
        "### 金句/名场面\n\n- \"金句就藏在这里，厉害非常。\"（行5，高手风范）\n")
    old_env = os.environ.get("INDEX_LIB_DIR")
    os.environ["INDEX_LIB_DIR"] = fake_lib
    try:
        sys.path.insert(0, os.path.join(SCRIPTS, "v333"))
        import verify_and_fix_lines as vfl
        vfl.process_file(idx, None)
    finally:
        if old_env:
            os.environ["INDEX_LIB_DIR"] = old_env
        else:
            os.environ.pop("INDEX_LIB_DIR", None)
    out = open(idx, encoding="utf-8").read()
    assert "（行3，高手风范）" in out, f"修正后应保留右括号: {out}"
    assert out.count("）") >= 1, "右括号丢失"


def t_fanfuaji_1mb_limit():
    """fanfuaji 超 1MB 必须报错且提示 --max-input-bytes（C2 回归）"""
    sys.path.insert(0, os.path.join(SCRIPTS, "fanfuaji"))
    import fanfuaji
    big = tempfile.mktemp(suffix=".txt")
    with open(big, "w", encoding="utf-8") as f:
        f.write("大" * (1024 * 1024 + 100))  # 略超 1MB
    try:
        fanfuaji.validate_input_path(big)
        raise AssertionError("超 1MB 应报错")
    except Exception as e:
        msg = str(e)
        assert "--max-input-bytes" in msg, f"提示应含 --max-input-bytes: {msg}"


def t_extract_samples_sampling():
    """extract_samples：广告段过滤 / 超长段句子截断 / 场景归属去重（H 系列回归）"""
    fake_lib = tempfile.mkdtemp()
    book_dir = os.path.join(fake_lib, "小说原文", "测试书")
    os.makedirs(book_dir)
    # 正常段（含场景词"冷笑"）—— ≥60 字
    normal = "他冷哼一声，嘴角挂着不屑的冷笑，缓缓开口：\"蝼蚁一般的东西，也配与我为敌？\"足足三百字。"
    normal = normal + "他继续说着话。" * 5
    # 广告段
    ad = "百度云搜索,搜小说就是方便 http://www.pan58.com 他冷笑一声继续说。"
    # 超长段（>400 字，无句号结尾处截断）
    long_p = "他冷笑一声，目光如电。" * 80
    with open(os.path.join(book_dir, "第1章~第5章-001.md"), "w", encoding="utf-8") as f:
        f.write(f"{normal}\n\n{ad}\n\n{long_p}\n")
    old_env = os.environ.get("INDEX_LIB_DIR")
    os.environ["INDEX_LIB_DIR"] = fake_lib
    try:
        import extract_samples as es
        samples = es.extract_book_samples("测试书", max_files=5, per_scene=2)
    finally:
        if old_env:
            os.environ["INDEX_LIB_DIR"] = old_env
        else:
            os.environ.pop("INDEX_LIB_DIR", None)
    all_text = "\n".join(p for ps in samples.values() for p in ps)
    assert "http" not in all_text and "百度" not in all_text, "广告段未过滤"
    assert all(len(p) <= 420 for p in (x for ps in samples.values() for x in ps)), "超长段未截断"
    assert any("冷笑" in p for p in samples.get("打脸碾压", [])), "正常段未入场景"


# ── 8. 全链路联动 ───────────────────────────────────
def t_full_pipeline():
    """全链路：拆批（超长章）→ 分类 → 改名 → 合并，无断裂"""
    import split_ja_segments as sjs
    import classify_paragraphs as cp
    src = tempfile.mktemp(suffix=".md")
    with open(src, "w", encoding="utf-8") as f:
        f.write("第1话 短章\n" + "他冷冷开口。" * 100 + "\n")
        f.write("第2话 超长章\n" + "狂风呼啸，他站在城头，目光如电。" * 360 + "\n")
        f.write("第3话 收尾\n" + "她转身离开。" * 100 + "\n")
    batch_dir = tempfile.mkdtemp()
    old_argv = sys.argv
    sys.argv = ["x", src, "--novel", "联动书", "--author", "测试", "--out", batch_dir, "--max-chars", "2000"]
    try:
        sjs.main()
    finally:
        sys.argv = old_argv
    batches = sorted(os.listdir(batch_dir))
    for f in batches:
        cp.process_file(os.path.join(batch_dir, f),
                        os.path.join(batch_dir, f.replace(".md", ".classified.md")))
    rewrite_dir = tempfile.mkdtemp()
    for f in batches:
        text = open(os.path.join(batch_dir, f), encoding="utf-8").read()
        fm, body = text.split("---")[1], text.split("---", 2)[2]
        with open(os.path.join(rewrite_dir, f.replace(".md", "-改写.md")), "w", encoding="utf-8") as fh:
            fh.write("---\n" + fm.strip() + "\n参考池: 遮天/打脸x2段\n状态: 已完成\n---\n" + body
                     + "\n---\n## 本批说明\n- 独立盲审记录: 盲审结论: 通过\n")
    out = tempfile.mkdtemp()
    r = run_py([os.path.join(SCRIPTS, "merge_batches.py"),
                        rewrite_dir, "--novel", "联动书", "--author", "测试", "--out", out],
                       capture_output=True)
    clean = open(os.path.join(out, "联动书-测试-中国化版.md"), encoding="utf-8").read()
    assert len(clean) > 1000, f"干净版过短: {len(clean)}"
    assert "第1话 短章" in clean and "第2话 超长章" in clean and "第3话 收尾" in clean
    assert "本批说明" not in clean and "盲审结论" not in clean
    assert r.returncode == 0, f"合并退出码 {r.returncode}"


# ── 9. 文档-脚本字段一致性（防模板/判定断裂复发）──
def t_doc_script_field_consistency():
    """批稿模板字段 = 脚本判定字段：'盲审结论'在流程指南模板 / SKILL.md / merge_batches 三者一致。
    B2 回归：模板曾写'复核结论'而脚本找'盲审结论'，导致全部批稿被误判待盲审、交付永远不完整。"""
    guide = open(os.path.join(SKILL, "references", "流程指南.md"), encoding="utf-8").read()
    skill = open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8").read()
    merge_src = open(os.path.join(SCRIPTS, "merge_batches.py"), encoding="utf-8").read()
    for fname, text in [("流程指南.md", guide), ("SKILL.md", skill)]:
        assert "盲审结论" in text, f"{fname} 缺少批稿必填字段'盲审结论'（merge 脚本靠它判定已盲审）"
    assert re.search(r"盲审结论[:：]", merge_src), \
        "merge_batches.py 判定缺少'盲审结论:'字段（与文档模板字段不一致）"
    # 红线'确认来源'字段三处一致（无确认来源 = 红线未确认 = 该批不得完成）
    redline = open(os.path.join(SKILL, "references", "审核红线清单.md"), encoding="utf-8").read()
    assert "确认来源" in redline and "确认来源" in guide and "确认来源" in skill, \
        "红线'确认来源'字段在审核红线清单/流程指南/SKILL.md 中缺失或不全"


ALL_TESTS = [
    ("拆批-超长章子切", t_split_long_chapter),
    ("拆批-无章节回退", t_split_no_chapters),
    ("拆批-简繁混排识别", t_split_mixed_titles),
    ("分类-无空行降级分段", t_classify_no_blank),
    ("分类-有空行正常", t_classify_with_blank),
    ("合并-两种命名兼容", t_merge_both_names),
    ("合并-空目录报错退出2", t_merge_empty_dir),
    ("合并-待盲审结构判断", t_merge_blind_review),
    ("质检-正常文本0误报", t_check_localization_no_false_positive),
    ("版本-CHANGELOG一致", t_version_changelog_consistent),
    ("质检-日式残留检出", t_check_localization_detect_jp),
    ("质检-句尾翻译腔检出", t_check_localization_sentence_end),
    ("质检-句尾带标点检出", t_check_localization_punct_end),
    ("拆批-chapters-per-seg生效", t_split_per_seg),
    ("质检-篇幅边界80-120", t_check_ratio_bounds),
    ("体检-乱码/空批/缺号/重复", t_check_source_health_detect),
    ("断点库-pending/done/hash", t_index_db_semantics),
    ("v333-SKILL_DIR路径", t_v333_skill_dir),
    ("参考池-检索与轮换", t_ref_pool_basic),
    ("参考池-零命中退出1", t_ref_pool_zero_hit),
    ("回归-行号修正括号", t_v333_line_bracket),
    ("回归-fanfuaji 1MB上限", t_fanfuaji_1mb_limit),
    ("回归-抽样广告过滤/截断", t_extract_samples_sampling),
    ("全链路-拆批→分类→合并", t_full_pipeline),
    ("回归-文档模板字段=脚本判定", t_doc_script_field_consistency),

    ("文档-资产一致性", t_docs_integrity),
    ("示范库骨架生成", t_demo_lib_build),
    ("翻译体检-正常文本", t_translation_quality),
    ("场景卡锚点-查无清零", t_scene_card_anchors),
    ("卡库索引-无严重缺口", t_card_index_gaps),]

QUICK_TESTS = [
    t_split_long_chapter, t_classify_no_blank, t_merge_both_names,
    t_merge_empty_dir, t_merge_blind_review,
    t_check_localization_no_false_positive, t_check_localization_sentence_end,
    t_full_pipeline,
]


# ── pytest 适配器（P30 回归：t_* 命名的契约用例曾被 CI 收集为 0 条却全绿）──
try:
    import pytest
except ImportError:  # 无 pytest 时 CLI 仍可用
    pytest = None

if pytest is not None:

    @pytest.mark.parametrize("name,fn", ALL_TESTS, ids=[n for n, _ in ALL_TESTS])
    def test_contract(name, fn):
        """每条契约一个 pytest 用例；环境前提不满足显式 skip（带原因），不算通过。"""
        try:
            fn()
        except Skipped as e:
            pytest.skip(str(e))


def main():
    ap = argparse.ArgumentParser(description="契约自测（文档承诺 vs 脚本行为）")
    ap.add_argument("--quick", action="store_true", help="只跑核心管道+质检")
    ap.add_argument("--skip-pool", action="store_true", help="跳过参考池测试（无索引库环境）")
    args = ap.parse_args()

    print(f"契约自测（{'快速' if args.quick else '全部'}"
          f"{'，跳过参考池' if args.skip_pool else ''}）— {SKILL}\n")
    if args.quick:
        for fn in QUICK_TESTS:
            name = [n for n, f in ALL_TESTS if f is fn][0]
            check(name, fn)
    else:
        for name, fn in ALL_TESTS:
            if args.skip_pool and fn in (t_ref_pool_basic, t_ref_pool_zero_hit):
                SKIP.append((name, "--skip-pool"))
                print(f"  ⏭️  {name}（--skip-pool 跳过）")
                continue
            check(name, fn)

    print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败 / {len(SKIP)} 跳过")
    if SKIP:
        print("\n跳过项（不计入通过）:")
        for name, why in SKIP:
            print(f"  ⏭️  {name}: {why}")
    if FAIL:
        print("\n失败项:")
        for name, err in FAIL:
            print(f"  ❌ {name}: {err}")
        sys.exit(1)
    print("全部通过 ✅（跳过项见上，需在有索引库的机器上补跑）")


if __name__ == "__main__":
    main()
