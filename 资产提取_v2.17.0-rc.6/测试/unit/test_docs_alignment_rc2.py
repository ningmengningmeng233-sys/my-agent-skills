#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 P1-3：文档与依赖对齐的机械回归。

红测目标（修复前必须失败）：
- rc.1 的文档里有 5 处 `scripts\\visual_production.py` 被转义序列破坏成
  `scriptsisual_production.py`（`\\v` 被解释为垂直制表符），另 1 处 FAQ 路径同理；
- 文档缺少「两条独立交付线」「Gemini 配置与 .env 优先级」「安装私有风格包」
  「TXT/DOCX/EPUB 依赖与 PDF 不支持」这些用户必需信息。

这些是「照抄命令会失败」的真实缺陷，必须由机械测试长期钉死。
"""
import re
import sys
from pathlib import Path

import pytest

import skill_meta
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DOCS = ("README.md", "QUICKSTART.md", "SKILL.md", "FAQ.md", "CONTEXT.md")
# 阶段 8 工单 8.1：主入口瘦身后，细则下沉到 references/；文档口径检查覆盖这批契约文档，
# 否则「必需说明」会随着内容下沉而悄悄消失。
CONTRACT_DOCS = (
    "references/核心反推契约.md",
    "references/视觉生产契约.md",
    "references/Omni质量审核契约.md",
    "references/角色参考入库契约.md",
    "references/导演构图与站位契约.md",
    "references/并发与失败处理契约.md",
)
# 文档里引用的脚本路径必须真实存在：只认「紧跟在 scripts/ 或 scripts\ 之后的 .py」，
# 且必须确认 scripts 前不是别的目录段（避免把 服饰设计\scripts\x.py 误判为 scripts\x.py）
SCRIPT_REF_RE = re.compile(r"(?<![\w\\/])scripts[/\\]([A-Za-z_][A-Za-z0-9_]*\.py)")


def _docs():
    for name in (*DOCS, *CONTRACT_DOCS):
        path = REPO_ROOT / name
        if path.is_file():
            yield path, path.read_text(encoding="utf-8")


# --- 控制字符与转义破坏 -----------------------------------------------------------

def test_no_control_characters_in_docs():
    """文档不得含控制字符：`\\v`/`\\a`/`\\b` 等被解释后会让命令无法照抄执行。"""
    bad: list[str] = []
    for path, text in _docs():
        for i, ch in enumerate(text):
            if ord(ch) < 32 and ch not in "\r\n\t":
                bad.append(f"{path.name}@{i}: U+{ord(ch):04X} {text[max(0, i - 30):i + 30]!r}")
    assert not bad, "文档含控制字符（转义被解释）：\n" + "\n".join(bad[:5])


def test_no_scripts_path_glued_to_next_letter():
    """`scripts` 后必须紧跟路径分隔符：`scriptsisual_...` 说明反斜杠被吞。"""
    bad: list[str] = []
    for path, text in _docs():
        for m in re.finditer(r"scripts(?=[a-z_])[A-Za-z0-9_]*", text):
            bad.append(f"{path.name}: {m.group(0)}")
    assert not bad, "发现被破坏的脚本路径：\n" + "\n".join(bad[:5])


def test_referenced_scripts_exist():
    """文档引用的脚本必须真实存在（按其**完整相对路径**解析）。

    注意 `服饰设计\\scripts\\check_costumes.py` 这类路径也要按整段核对，
    不能只截取 `scripts/...` 后半段，否则会误报。
    """
    missing: list[str] = []
    # 取「可选的一层中文/英文目录 + scripts + 脚本名」整段路径
    pattern = re.compile(
        r"((?:[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*[/\\])?scripts[/\\]"
        r"[A-Za-z_][A-Za-z0-9_]*\.py)")
    for path, text in _docs():
        for m in pattern.finditer(text):
            rel = m.group(1).replace("\\", "/")
            if not (REPO_ROOT / rel).is_file():
                missing.append(f"{path.name}: {rel}")
    assert not missing, "文档引用了不存在的脚本：\n" + "\n".join(sorted(set(missing)))


def test_key_commands_present_and_runnable():
    """rc.2 的关键入口必须在文档中可查，且脚本文件存在。"""
    blob = "\n".join(text for _p, text in _docs())
    for token in ("visual_production.py", "visual-doctor", "install_stylepack.py",
                  "verify-visual", "visual-plan"):
        assert token in blob, f"文档缺少关键入口：{token}"


# --- 必备说明段落 ----------------------------------------------------------------

def test_docs_declare_primary_purpose():
    """入口文档开头必须说明主用途是脚本/小说资产反推，视觉是可选扩展。

    FAQ/CONTEXT 是查错与上下文文档，不要求开头宣言，但必须给出主用途指针。
    """
    for name in ("README.md", "QUICKSTART.md", "SKILL.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        head = text[:1500]
        assert "反推" in head or "提取" in head, f"{name} 开头未说明主用途"
    for name in ("FAQ.md", "CONTEXT.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "反推" in text or "提取" in text, f"{name} 未提及主用途"
    skill = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
    core = (REPO_ROOT / "references" / "核心反推契约.md").read_text(encoding="utf-8")
    assert "核心与可选视觉的边界" in core, "缺少核心/视觉边界章节（主入口或核心契约）"
    assert "核心与可选视觉的边界" in skill or "references/核心反推契约.md" in skill, (
        "主入口必须能路由到边界章节")


def test_docs_declare_two_independent_delivery_lines():
    blob = "\n".join(text for _p, text in _docs())
    assert "data_delivery" in blob and "visual_delivery" in blob, \
        "文档必须说明资料交付与视觉交付是两条独立状态线"


def test_docs_declare_gemini_config_and_env_precedence():
    blob = "\n".join(text for _p, text in _docs())
    for token in ("GEMINI_API_KEY", "GEMINI_IMAGE_MODEL", "GEMINI_QC_MODEL", "ASSET_ENV_PATH"):
        assert token in blob, f"配置段缺少变量：{token}"
    assert "google-genai" in blob or "google.genai" in blob, "缺少 Google SDK 安装说明"


def test_docs_declare_input_formats_and_pdf_unsupported():
    blob = "\n".join(text for _p, text in _docs())
    for token in ("DOCX", "EPUB", "TXT"):
        assert token.upper() in blob.upper(), f"缺少输入格式说明：{token}"
    assert "PDF" in blob, "必须明确说明 PDF 是否支持"
    assert "不支持" in blob or "不支持 PDF" in blob, "PDF 必须明确标注不支持"


def test_docs_declare_visual_gate_exit_codes():
    blob = "\n".join(text for _p, text in _docs())
    assert "VISUAL_NOT_ENABLED" in blob, "必须说明视觉未启用的原因码"
    assert "DELIVERED" in blob, "必须说明只有 DELIVERED 算通过"


def test_docs_declare_stylepack_install():
    blob = "\n".join(text for _p, text in _docs())
    assert "install_stylepack" in blob, "缺少私有风格包安装说明"
    assert "仙侠国风电影CG" in blob, "必须给出权威风格的中文正式名"


def test_docs_do_not_claim_unverified_passes():
    """不得声称真实 API / Linux / 人工画质已通过（无物理证据）。"""
    for path, text in _docs():
        for claim in ("真实 API 已通过", "Linux 已验收", "人工画质已通过", "正式版已完成"):
            assert claim not in text, f"{path.name} 出现无证据的完成声明：{claim}"


def test_version_aligned_across_docs_and_pyproject():
    from engine.common import toml_module

    version = toml_module().loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert version == "2.17.0rc5", version
    skill = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert skill_meta.skill_version(skill) == "2.17.0-rc.5"
    assert "2.17.0-rc.5" in (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def test_requirements_declare_source_format_deps():
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for pkg in ("python-docx", "ebooklib", "beautifulsoup4", "python-dotenv"):
        assert pkg in req, f"requirements 缺少依赖：{pkg}"


def test_google_sdk_is_declared_optional_not_core_dependency():
    """Google SDK 只能作为**可选视觉依赖**声明，不得混进核心依赖段。

    核心链路（反推/拆段/提取/聚合/签发）不得因缺它而失败。
    """
    lines = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    idx = next((i for i, l in enumerate(lines) if l.strip().lower().startswith("google-genai")),
               None)
    assert idx is not None, "requirements 未声明视觉可选依赖 google-genai"
    # 之前 6 行内必须有「可选」说明，把它和核心依赖区分开
    context = "\n".join(lines[max(0, idx - 6):idx])
    assert "可选" in context, f"google-genai 未标注为可选视觉依赖：{context!r}"


def test_core_gate_runs_without_google_sdk():
    """核心静态检查不得 import google（视觉是可选扩展）。"""

    src = (REPO_ROOT / "scripts" / "engine" / "pipeline_state.py").read_text(encoding="utf-8")
    assert "import google" not in src, "核心状态机不得静态导入 Google SDK"
    r = run_py(
            ["-c", "import sys; sys.path.insert(0, r'%s'); " "import scripts.engine.pipeline_state as m; " "print('google' in sys.modules)" % REPO_ROOT],
    )
    assert r.returncode == 0, r.stderr[-300:]
    assert "False" in r.stdout, "导入核心状态机不得连带加载 Google SDK"


if __name__ == "__main__":
    sys.exit(0)


# --- 交付报告与实物一致性 ---------------------------------------------------------

def test_rc2_report_records_actual_package_hashes(tmp_path):
    """交付报告 §4 记录的 zip 哈希必须与发布目录里的实物一致。

    这是防止「报告写 A、实物是 B」这类交付物不一致的机械闸门——
    rc.2 首轮正是因为包早于文档构建而出现该问题。
    """
    import hashlib

    report = REPO_ROOT / "docs" / "验收" / "v2.15.0-rc.2交付报告.md"
    if not report.is_file():
        pytest.skip("当前形态不含 rc.2 交付报告（如包内树）")
    text = report.read_text(encoding="utf-8")

    publish = Path(r"D:\Desktop_Migrated\转移\v2.15.0-rc.2发布")
    if not publish.is_dir():
        pytest.skip(f"发布目录不存在：{publish}")
    for name in ("资产提取-v2.15.0-rc.2-private.zip",
                 "资产提取-v2.15.0-rc.2-shareable.zip"):
        path = publish / name
        if not path.is_file():
            pytest.skip(f"发布包不存在：{name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest in text, f"报告未记录 {name} 的真实哈希 {digest}"


def test_rc2_report_file_counts_match_packages():
    """报告记录的文件数必须与 zip 实际条目数一致。"""
    import zipfile

    report = REPO_ROOT / "docs" / "验收" / "v2.15.0-rc.2交付报告.md"
    if not report.is_file():
        pytest.skip("当前形态不含 rc.2 交付报告")
    text = report.read_text(encoding="utf-8")
    publish = Path(r"D:\Desktop_Migrated\转移\v2.15.0-rc.2发布")
    for name, expected in (("资产提取-v2.15.0-rc.2-private.zip", 643),
                           ("资产提取-v2.15.0-rc.2-shareable.zip", 631)):
        path = publish / name
        if not path.is_file():
            pytest.skip(f"发布包不存在：{name}")
        with zipfile.ZipFile(path) as zf:
            entries = [n for n in zf.namelist()
                       if not n.endswith(("SHA256SUMS.txt", "发布清单.json"))]
        assert len(entries) == expected, f"{name} 实际 {len(entries)} 个文件，期望 {expected}"
        assert str(expected) in text, f"报告未记录 {name} 的文件数 {expected}"


def test_packaged_manual_does_not_self_reference_package_hash():
    """包内不得出现描述「本包哈希」的报告，否则哈希永远无法定稿。"""
    import zipfile

    publish = Path(r"D:\Desktop_Migrated\转移\v2.15.0-rc.2发布")
    if not publish.is_dir():
        pytest.skip("发布目录不存在")
    for name in ("资产提取-v2.15.0-rc.2-private.zip",
                 "资产提取-v2.15.0-rc.2-shareable.zip"):
        path = publish / name
        if not path.is_file():
            pytest.skip(f"发布包不存在：{name}")
        with zipfile.ZipFile(path) as zf:
            self_ref = [n for n in zf.namelist() if "rc.2交付报告" in n]
        assert not self_ref, f"{name} 不应包含描述自身哈希的报告：{self_ref}"


class TestVerdictTemplateContracts:
    """裁决模板的两条硬契约（工单 T4）。

    都来自一次真实 verify 的未过项，且**都不是内容质量问题**：
    - 场景「衙门」未在资产表建档：裁决已把「衙门」并入 SC_守备前厅，但模板只要求
      「合并」列填组ID，旧名无处落地 → 章节表按旧名引用时认不出来；
    - `SC_密道 CONNECTS_TO→屏风后` 阻断：模板的标签示例是 ID 写法，却没说"必须是已收录场景"，
      裁决遂把方位短语写进了连接标签，而 CONNECTS_TO 只对场景解析 → 判为"目标在索引中不存在"。
    """

    TEMPLATE = REPO_ROOT / "templates" / "聚合裁决模板.md"

    def _text(self) -> str:
        return self.TEMPLATE.read_text(encoding="utf-8")

    def test_merged_old_names_must_land_in_alias_column(self):
        t = self._text()
        assert "被合并" in t, "须写明被合并组的名字要有落点"
        assert "别名" in t
        assert "旧名" in t or "旧称呼" in t, "须点明落的是「旧名」，否则读的人不会想到"
        assert "落地" in t or "写入" in t, "须是明确动作，不能只是描述"

    def test_connects_to_target_must_be_a_scene(self):
        t = self._text()
        assert "CONNECTS_TO" in t
        assert "屏风后" in t, "必须保留方位短语反例——实测就是它把链路卡住的"
        i = t.find("屏风后")
        window = t[max(0, i - 400): i + 400]
        assert "场景" in window, "反例附近必须讲清「目标只能是场景」"

    def test_connects_to_is_documented_as_bidirectional(self):
        """关系本身是**双向**的（RELATION_MATRIX 与 continuity 都按双向校验）。

        不写清这一点，读的人有两种误读，实测各出一次问题：
        以为「要两端各写一行」（重复表达），或以为「单向合法」（索引里落半条边被 verify 判单向）。
        """
        t = self._text()
        assert "双向" in t, "须写明该关系是双向的"
        i = t.find("双向")
        window = t[max(0, i - 300): i + 300]
        assert "自动补全" in window, "须说清反向边由聚合补全，不靠人写"
        assert "一次" in window, "须说清任一端声明一次即可"


class TestSegmentTemplateStateTag:
    """`[状态:…]` 全仓只有一处文档（段级提取模板），而它教的是**反的**。

    实测（2026-09-21 全链）：模板示例写 `[状态:受损]`，意思像是"这件资产自己坏了"；
    而消费侧 `aggregate.py` 把它读作「本资产是 *X* 的**状态变体**」，要求 *X* 是**已收录的母资产**
    （`RELATION_MATRIX["STATE_OF"] = {source: 变体, target: 母资产}`）。
    示例词永远解析不到 → 必然进 `unresolved_relations` → `continuity` 无条件阻断交付。
    真实报错：`PR_残破木门: 未解析关系 STATE_OF→受损（状态母资产命中非唯一或不存在，需人工裁决）`。
    """

    TEMPLATE = REPO_ROOT / "templates" / "段级提取模板.md"

    def _text(self) -> str:
        return self.TEMPLATE.read_text(encoding="utf-8")

    def test_state_tag_means_parent_asset(self):
        t = self._text()
        i = t.find("[状态")
        assert i >= 0, "模板必须仍然说明这个标签"
        window = t[max(0, i - 200): i + 900]
        assert "母资产" in window, "须写明括号里写的是「母资产名」，不是状态形容词"
        assert "受损" in window, "必须保留实测反例——它就是那条把交付卡住的写法"
        assert "不要" in window or "不得" in window, "必须给出明确禁止写法"
        assert "阻断" in window, "要说清代价：写错不是少个字段，是把整条交付卡住"

    def test_descriptive_state_has_a_legitimate_channel(self):
        """光说"别写"不够：描述性状态必须有落点，否则模型只能二选一（乱写或丢信息）。

        模板第 4 条已有这条通道（道具需求：原文明确出现的状态变化 → 登记为道具需求）。
        """
        t = self._text()
        i = t.find("[状态")
        window = t[max(0, i - 200): i + 900]
        assert "需求" in window, "须把「自身状态变化」指向需求/衍生需求通道"
