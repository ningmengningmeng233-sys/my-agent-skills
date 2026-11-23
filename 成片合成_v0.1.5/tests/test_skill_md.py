"""行为 6：SKILL.md 前置元数据符合套件约定 + 边界/已知限制/一条命令 + 治理红线。"""
import re
from pathlib import Path

import yaml

SKILL_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = SKILL_ROOT / "SKILL.md"


def _frontmatter():
    text = SKILL_MD.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md 缺 YAML front matter"
    return yaml.safe_load(m.group(1)), text


def test_frontmatter_required_keys():
    fm, _ = _frontmatter()
    for key in ("name", "description"):
        assert fm.get(key), f"front matter 缺 {key}"
    assert fm["metadata"]["version"]
    assert fm.get("author")


def test_boundary_section_honest():
    _, text = _frontmatter()
    assert "边界" in text
    assert "NOT_RUN" in text
    assert "BLOCKED-CRED" in text
    assert "解锁条件" in text
    assert "桩" in text  # 本机仅桩渲染验证


def test_known_limits_and_quickstart():
    _, text = _frontmatter()
    assert "已知限制" in text
    assert "一条命令" in text
    assert "python -m renderplan" in text


def test_sdo_a_trigger_first_not_first_person():
    """SDO a) 以『当用户…』触发句开头，不得第一人称/抽象口号"""
    fm, _ = _frontmatter()
    d = (fm.get("description") or "").strip()
    assert re.match(r"^当用户", d), f"a) description 必须以触发句『当用户…』开头，实测开头：{d[:40]!r}"
    bad = re.findall(r"我可以|我会|我将|本人|帮你", d)
    assert not bad, f"a) description 含第一人称/服务口号，实测命中：{bad}"


def test_sdo_b_length_within_500():
    """SDO b) 长度 ≤ 500 字符（Unicode 字符计）"""
    fm, _ = _frontmatter()
    d = fm.get("description") or ""
    assert len(d) <= 500, f"b) description 实测 {len(d)} 字符，超过 500 上限"


def test_sdo_c_at_least_five_quoted_triggers():
    """SDO c) 「…」/『…』引号内具体触发词/症状 ≥ 5 个"""
    fm, _ = _frontmatter()
    d = fm.get("description") or ""
    quoted = re.findall(r"[「『][^」』]{1,30}[」』]", d)
    assert len(quoted) >= 5, f"c) 引号内具体触发词不足：实测 {len(quoted)} 个（要求 ≥5），实测列表：{quoted}"


def test_sdo_d_no_workflow_restate():
    """SDO d) 不复述工作流：无『流程为/步骤为/依次/三轨/一条指令/跑全链/跑通全链』式执行句。

    SDO 明文：description 只写「何时用」；写了流程会让代理照着描述干活而不读正文。
    """
    fm, _ = _frontmatter()
    d = fm.get("description") or ""
    ban = ("流程为", "步骤为", "依次", "三轨", "一条指令", "跑全链", "跑通全链", "第一步")
    hit = [p for p in ban if p in d]
    stage = ("先", "再", "然后", "接着", "最后", "首先", "继而", "逐步")
    sents = [s for s in d.split("。") if s.strip()]
    flow = [s for s in sents if any(w in s for w in stage)]
    assert not hit and len(flow) <= 1, (
        f"d) description 复述工作流：禁词命中={hit}；含阶段次序词的句子 {len(flow)} 个（要求 ≤1）：{flow}"
    )


def test_sdo_e_observable_input_precondition():
    """与上游分镜类技能及「小说转短剧」碰撞消歧：必须有可观察输入前置条件 + 排除路由。

    判据：①出现『输入已是分镜』式可观察前置条件（不攻击外部技能，只声明自己吃什麼）；
    ②排除句点名真实兄弟技能『小说转短剧』。
    """
    fm, _ = _frontmatter()
    d = fm.get("description") or ""
    assert re.search(r"(输入|手上?|已)经?是?(分镜|镜头组)|已有分镜|分镜/镜头组", d), (
        f"e) description 缺少『输入已是分镜/镜头组』可观察前置条件，实测：{d!r}"
    )
    m = re.search(r"不适用：.*", d, re.S)
    assert m, f"e) description 缺少『不适用：…』排除句，实测：{d!r}"
    assert "小说转短剧" in m.group(0), (
        f"e) 排除句未指向真实兄弟技能『小说转短剧』，实测排除句：{m.group(0)!r}"
    )


def test_no_unsupported_completion_claims():
    _, text = _frontmatter()
    # 「已完成」「正式版」若出现必须伴随日期与产物路径
    for kw in ("已完成", "正式版"):
        for line in text.splitlines():
            if kw in line:
                assert re.search(r"20\d\d-\d\d-\d\d", line) or "/" in line, \
                    f"『{kw}』缺日期或产物路径：{line}"
    # 不得把 SKIPPED/NOT_RUN 说成绿
    assert "SKIPPED 不是绿灯" in text or "NOT_RUN" in text
