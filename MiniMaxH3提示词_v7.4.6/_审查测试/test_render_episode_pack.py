"""Task 6 guard：原子渲染四类整集交付物。

覆盖八层：
1. 工单 Step 1 三个冻结样例逐条保留：精确计划渲染四类交付物、`REVIEW_REQUIRED` 只出
   时长审查摘要与机器清单、失败 `--replace` 保留上一个完整输出；
2. 四类交付物内容：`episode-timeline.md` 人类可读台账（设计 §10.1 全列）、
   `prompts/GNNN.md` 独立提示词（至少三位数字、逐字节等于 `prompt_text`）、
   `prompt-pack.md` 按时序汇总（设计 §10.3）、`duration-manifest.json` 机器清单（设计 §10.4 全字段）；
3. 审计串联：每条 `prompt_text` 都过既有 `audit_cli`（`generate_output.run_real_audit_cli`，
   字节级 sha 绑定），任一单条失败整集不交换、staging 留证；
4. 原子性：复用 `generate_output` 的 realpath 边界与 staging 交换；非空目录无 `--replace`
   一律拒绝；`--replace` 先全部验证再交换；交换失败自动回滚；
5. 冻结决定 #22：动作原文传递契约——`ClipDraft` 只携带 `dialogue_text`，渲染方必须按
   `beat_ids` 反查 `Beat.text`（`action_beats_for_clip`），未知 ID 一律报错不静默丢弃；
6. 诚实清单：真实媒体字段如实透传（`NOT_RUN`/null 不被计划值代填），清单数字与
   `validate_episode_plan` 报告逐项一致；
7. CLI：`--plan/--out/--replace` 与退出码合同（0 渲染 / 1 拒绝 / 2 暂停审查 / 3 内部崩溃）；
8. `generate_output.py` 兼容回归：抽取的 `atomic_swap`/`create_staging_dir` 行为不变，
   `generate_case` 全链（审计 + staging 交换 + sha 绑定）照常工作。

断言一律用 `self.assertEqual` 等不可被 `python -O` 剥离的形式。

运行（目录名不是合法 Python 标识符，禁止 `-m unittest` 模块路径）：

    python "minimax-h3-craft/_审查测试/test_render_episode_pack.py" -v
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from duration_planner import KIND_ACTION, KIND_DIALOGUE, Beat, pack_beats  # noqa: E402
import generate_output  # noqa: E402
import render_episode_pack as render_module  # noqa: E402
from render_episode_pack import (  # noqa: E402
    EXIT_REFUSED,
    EXIT_RENDERED,
    EXIT_REVIEW_REQUIRED,
    UnknownBeatReferenceError,
    action_beats_for_clip,
    render_episode_pack,
)
from episode_contract import (  # noqa: E402
    EpisodeContractError,
    load_model_capabilities,
    validate_episode_plan,
)
from versioning import load_duration_policy  # noqa: E402

POLICY = load_duration_policy()

WORKFLOW = "native-drama-series"
START_STATE = "主角站在雪地边缘，面朝镜头，双手插兜。"
END_STATE = "主角向前迈出两步，视线压低，围巾被风吹起。"

DIALOGUE_TEXT = "一二三四五六七八九十甲乙"  # 12 字
ACTION_TEXT = "推门而入卷起风雪"  # 8 字
LONG_DIALOGUE_TEXT = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯"  # 24 字
LONG_ACTION_TEXT = "回头望向哨塔塔顶"  # 8 字


def drama_prompt(evolution: str) -> str:
    """探测验证过的 native-drama-series 六段式（audit_cli failed=0）。"""
    return "\n".join(
        [
            "这是一段连续的全画幅画面，遵守对话轴和 180 度原则。",
            "",
            "【环境光影】冷蓝雪夜，低照度侧光。",
            "",
            "【空间基准】雪原边缘的废弃哨塔外。",
            "",
            "【角色声音签名】",
            "主角（男，低沉沙哑，语速偏慢，音量压低）。",
            "",
            "【画面演进】",
            evolution,
            "",
            "【声音设计】风声持续，无配乐。",
            "",
            "【画面风格】写实真人摄影质感；真实皮肤质感；自然真实的人类面部特征，带有轻微的生理不完美。",
            "",
        ]
    )


PROMPT_G001 = drama_prompt("雪原边缘的废弃哨塔外，主角拉紧围巾向前走，脚步压进新雪。")
PROMPT_G002 = drama_prompt("哨塔门口，主角停下脚步，抬手拂去肩头的积雪。")
PROMPT_G003 = drama_prompt("主角侧身挤进塔门，风雪在身后卷起。")

AUDIT_FAIL_PROMPT = "subject_definitions:\n  - A: a man in a scarf\n【环境光影】冷蓝雪夜。"


def make_ref(source_id, span_start, span_end):
    return {"source_id": source_id, "span_start": span_start, "span_end": span_end}


def make_unit(unit_id, span_start=None, span_end=None, kind=KIND_DIALOGUE, **overrides):
    unit = {"id": unit_id, "kind": kind}
    if span_start is not None:
        unit["span_start"] = span_start
    if span_end is not None:
        unit["span_end"] = span_end
    unit.update(overrides)
    return unit


def make_clip(clip_id, start, duration, refs, prompt, **overrides):
    clip = {
        "id": clip_id,
        "start_seconds": start,
        "duration_seconds": duration,
        "source_refs": list(refs),
        "start_state": START_STATE,
        "end_state": END_STATE,
        "system_added_transitions": [],
        "trim_candidates": [],
        "prompt_text": prompt,
    }
    clip.update(overrides)
    return clip


def base_plan(plan_clips, plan_units, target, **overrides):
    total = sum(clip["duration_seconds"] for clip in plan_clips)
    document = {
        "episode_id": "EP01",
        "workflow": WORKFLOW,
        "model": "H3",
        "script_target_seconds": target,
        "source_units": list(plan_units),
        "clips": list(plan_clips),
        "planned_prompt_seconds": total,
        "real_model_render_status": "NOT_RUN",
        "actual_generated_seconds": None,
        "final_edit_seconds": None,
    }
    document.update(overrides)
    return document


def exact_plan(**overrides):
    """20 秒目标 / 20 秒计划 → PASS_EXACT；台词 12 字 + 动作 8 字，逐字覆盖。"""
    clips = [
        make_clip("G001", 0, 10, [make_ref("S001", 0, 10)], PROMPT_G001),
        make_clip(
            "G002",
            10,
            10,
            [make_ref("S001", 10, 12), make_ref("S002", 0, 8)],
            PROMPT_G002,
        ),
    ]
    units = [
        make_unit(
            "S001", 0, 12, text=DIALOGUE_TEXT, speaker_ids=["主角"], scene_id="SC01"
        ),
        make_unit("S002", 0, 8, kind=KIND_ACTION, text=ACTION_TEXT, scene_id="SC01"),
    ]
    return base_plan(clips, units, 20, **overrides)


def over_20_plan(**overrides):
    """20 秒目标 / 30 秒计划 → 超出 50% > 20% 上限 → REVIEW_REQUIRED。"""
    clips = [
        make_clip("G001", 0, 10, [make_ref("S001", 0, 10)], PROMPT_G001),
        make_clip("G002", 10, 10, [make_ref("S001", 10, 20)], PROMPT_G002),
        make_clip(
            "G003",
            20,
            10,
            [make_ref("S001", 20, 24), make_ref("S002", 0, 6)],
            PROMPT_G003,
        ),
    ]
    units = [
        make_unit(
            "S001", 0, 24, text=LONG_DIALOGUE_TEXT, speaker_ids=["主角"], scene_id="SC01"
        ),
        make_unit("S002", 0, 6, kind=KIND_ACTION, text=LONG_ACTION_TEXT, scene_id="SC01"),
    ]
    return base_plan(clips, units, 20, **overrides)


def invalid_plan(**overrides):
    """时间线 10—12 秒空档 + 首片 3 秒越界 → FAIL。"""
    clips = [
        make_clip("G001", 0, 3, [make_ref("S001", 0, 3)], PROMPT_G001),
        make_clip("G002", 3, 7, [make_ref("S001", 3, 10)], PROMPT_G002),
        make_clip("G003", 12, 8, [make_ref("S001", 10, 18)], PROMPT_G003),
    ]
    return base_plan(clips, [make_unit("S001", 0, 18, text="一" * 18)], 18, **overrides)


def audit_fail_plan(**overrides):
    """门禁 PASS_EXACT，但 G002 提示词触发审计通道冲突 → 整集不得交换。"""
    return exact_plan(
        clips=[
            make_clip("G001", 0, 10, [make_ref("S001", 0, 10)], PROMPT_G001),
            make_clip(
                "G002",
                10,
                10,
                [make_ref("S001", 10, 12), make_ref("S002", 0, 8)],
                AUDIT_FAIL_PROMPT,
            ),
        ],
        **overrides,
    )


EXACT_PLAN = exact_plan()
OVER_20_PLAN = over_20_plan()
INVALID_PLAN = invalid_plan()


def snapshot(root: Path) -> dict:
    """相对路径 → 内容 sha256；目录形态变化一律现形。"""
    state = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            state[rel + "/"] = "dir"
        else:
            state[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


class RenderTestCase(unittest.TestCase):
    def out_dir(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp, Path(tmp.name) / "out"

    def seed_previous_output(self, out: Path) -> dict:
        self.assertEqual(render_episode_pack(EXACT_PLAN, out), EXIT_RENDERED)
        previous = snapshot(out)
        self.assertTrue(previous, "种子输出为空，测试失去意义")
        return previous


class WorkOrderSpecTest(RenderTestCase):
    """工单 Task 6 Step 1 的三个冻结样例。"""

    def test_exact_plan_renders_four_artifact_classes(self):
        _, out = self.out_dir()
        rc = render_episode_pack(EXACT_PLAN, out)
        self.assertEqual(rc, 0)
        self.assertTrue((out / "episode-timeline.md").is_file())
        self.assertTrue((out / "prompt-pack.md").is_file())
        self.assertTrue((out / "prompts" / "G001.md").is_file())
        manifest = json.loads((out / "duration-manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["duration_status"], "PASS_EXACT")
        self.assertIsNone(manifest["actual_generated_seconds"])

    def test_review_required_writes_summary_but_not_full_pack(self):
        _, out = self.out_dir()
        rc = render_episode_pack(OVER_20_PLAN, out)
        self.assertEqual(rc, 2)
        self.assertTrue((out / "duration-manifest.json").is_file())
        self.assertFalse((out / "prompt-pack.md").exists())

    def test_review_required_writes_neither_timeline_nor_prompts(self):
        """冻结样例的补充口径：审查路径只出两件，任何生产包形态都不得出现。"""
        _, out = self.out_dir()
        self.assertEqual(render_episode_pack(OVER_20_PLAN, out), EXIT_REVIEW_REQUIRED)
        self.assertEqual(
            sorted(p.name for p in out.iterdir()),
            ["duration-manifest.json", "duration-review.md"],
        )
        self.assertFalse((out / "prompts").exists())

    def test_failed_replace_preserves_previous_output(self):
        _, out = self.out_dir()
        previous = self.seed_previous_output(out)
        rc = render_episode_pack(INVALID_PLAN, out, replace=True)
        self.assertNotEqual(rc, EXIT_RENDERED)
        self.assertEqual(snapshot(out), previous)


class FullPackContentTest(RenderTestCase):
    """四类交付物的内容合同（设计 §9 / §10.1—§10.4）。"""

    def render_exact(self, plan=None, **overrides):
        _, out = self.out_dir()
        document = exact_plan(**overrides) if plan is None else plan
        rc = render_episode_pack(document, out)
        self.assertEqual(rc, EXIT_RENDERED)
        return out

    def test_timeline_contains_every_design_10_1_column(self):
        out = self.render_exact()
        timeline = (out / "episode-timeline.md").read_text("utf-8")
        for column in (
            "片段ID",
            "时间码",
            "计划时长",
            "剧本原文范围",
            "在场角色与说话人",
            "台词字数及估算语速",
            "动作节拍",
            "起始状态",
            "结束状态",
            "系统补充过渡",
            "剪辑余量",
            "安全裁剪位置",
        ):
            self.assertIn(column, timeline, f"台账缺少设计 §10.1 列：{column}")
        for header_fact in ("EP01", WORKFLOW, "H3", "20", "100.0%"):
            self.assertIn(header_fact, timeline)

    def test_timeline_rows_carry_clip_facts(self):
        out = self.render_exact()
        timeline = (out / "episode-timeline.md").read_text("utf-8")
        self.assertIn("G001", timeline)
        self.assertIn("0—10秒", timeline)
        self.assertIn("10秒", timeline)
        self.assertIn("S001[0—10)", timeline)
        self.assertIn("主角", timeline)
        self.assertIn("10字", timeline)
        self.assertIn("3—4字/秒", timeline)
        g001_row = timeline[timeline.index("G001"): timeline.index("G002")]
        self.assertIn(START_STATE, g001_row)
        self.assertIn("|主角|10字", g001_row)
        self.assertNotIn("未申报", g001_row)
        g002_row = timeline[timeline.index("G002"):]
        self.assertIn(END_STATE, g002_row)

    def test_timeline_action_beats_come_from_declared_unit_text(self):
        out = self.render_exact()
        timeline = (out / "episode-timeline.md").read_text("utf-8")
        g001_row = timeline[timeline.index("G001"): timeline.index("G002")]
        self.assertIn("—", g001_row)
        g002_row = timeline[timeline.index("G002"):]
        self.assertIn(ACTION_TEXT, g002_row)

    def test_timeline_marks_dialogue_free_clips(self):
        """两片切分同一条 16 字动作线，台词列为零 → 「本段无对白」标记。"""
        plan = exact_plan(
            source_units=[
                make_unit(
                    "S002",
                    0,
                    16,
                    kind=KIND_ACTION,
                    text="推门而入卷起风雪。主角踩雪前行，回头看了一眼。",
                    scene_id="SC01",
                )
            ],
            clips=[
                make_clip("G001", 0, 10, [make_ref("S002", 0, 8)], PROMPT_G001),
                make_clip("G002", 10, 10, [make_ref("S002", 8, 16)], PROMPT_G002),
            ],
        )
        out = self.render_exact(plan)
        timeline = (out / "episode-timeline.md").read_text("utf-8")
        self.assertIn("本段无对白", timeline)

    def test_timeline_shows_transitions_and_trim_positions(self):
        plan = exact_plan(
            source_units=[
                make_unit(
                    "S001", 0, 12, text=DIALOGUE_TEXT, speaker_ids=["主角"], scene_id="SC01"
                ),
                make_unit("S002", 0, 8, kind=KIND_ACTION, text=ACTION_TEXT, scene_id="SC01"),
                make_unit("S003", kind="system_transition"),
            ],
            clips=[
                make_clip(
                    "G001",
                    0,
                    10,
                    [make_ref("S001", 0, 10)],
                    PROMPT_G001,
                    system_added_transitions=["S003"],
                    trim_candidates=[{"at_seconds": 8.0, "reason": "定格收尾"}],
                ),
                make_clip(
                    "G002",
                    10,
                    10,
                    [make_ref("S001", 10, 12), make_ref("S002", 0, 8)],
                    PROMPT_G002,
                ),
            ],
        )
        out = self.render_exact(plan)
        timeline = (out / "episode-timeline.md").read_text("utf-8")
        g001_row = timeline[timeline.index("G001"): timeline.index("G002")]
        self.assertIn("S003", g001_row)
        self.assertIn("8.0秒（定格收尾）", g001_row)
        self.assertIn("0.3—0.5秒", g001_row)
        self.assertIn("选择性", timeline)

    def test_action_unit_without_text_marks_unreported_beats(self):
        plan = exact_plan(
            source_units=[
                make_unit("S001", 0, 12, text=DIALOGUE_TEXT, speaker_ids=["主角"]),
                make_unit("S002", 0, 8, kind=KIND_ACTION),
            ]
        )
        out = self.render_exact(plan)
        timeline = (out / "episode-timeline.md").read_text("utf-8")
        self.assertIn("（未申报原文）", timeline)

    def test_speaker_id_edge_cases_render_with_honest_cells(self):
        """无 speaker_ids → 「未申报」；含非法项 → 可见标记；字符串 → 格式异常标记。"""
        plan = exact_plan(
            source_units=[
                make_unit("S001", 0, 12, text=DIALOGUE_TEXT, speaker_ids="主角"),
                make_unit("S002", 0, 8, kind=KIND_ACTION, text=ACTION_TEXT),
                make_unit("S003", 12, 20, text="八字台词正好八个", speaker_ids=["主角", 7]),
            ],
            clips=[
                make_clip("G001", 0, 10, [make_ref("S001", 0, 10)], PROMPT_G001),
                make_clip(
                    "G002",
                    10,
                    10,
                    [make_ref("S001", 10, 12), make_ref("S002", 0, 8), make_ref("S003", 12, 20)],
                    PROMPT_G002,
                ),
            ],
        )
        out = self.render_exact(plan)
        timeline = (out / "episode-timeline.md").read_text("utf-8")
        self.assertIn("（speaker_ids 格式异常）", timeline)
        self.assertIn("（speaker_ids 含非法项）", timeline)

    def test_dialogue_unit_without_speakers_marks_unreported(self):
        plan = exact_plan(
            source_units=[
                make_unit("S001", 0, 12, text=DIALOGUE_TEXT),
                make_unit("S002", 0, 8, kind=KIND_ACTION, text=ACTION_TEXT),
            ]
        )
        out = self.render_exact(plan)
        timeline = (out / "episode-timeline.md").read_text("utf-8")
        g001_row = timeline[timeline.index("G001"): timeline.index("G002")]
        self.assertIn("|未申报|", g001_row)

    def test_prompt_files_are_byte_exact_and_g_numbered(self):
        out = self.render_exact()
        prompts = sorted((out / "prompts").glob("G*.md"), key=lambda p: p.name)
        self.assertEqual([p.name for p in prompts], ["G001.md", "G002.md"])
        self.assertEqual((out / "prompts" / "G001.md").read_bytes(), PROMPT_G001.encode("utf-8"))
        self.assertEqual((out / "prompts" / "G002.md").read_bytes(), PROMPT_G002.encode("utf-8"))

    def test_prompt_numbering_grows_past_three_digits(self):
        clips = []
        units = [make_unit("S001", 0, 130, text="一" * 130, speaker_ids=["主角"])]
        start = 0
        for index in range(1, 14):  # 13 片 × 10 秒，编号必须到 G013
            clips.append(
                make_clip(
                    f"G{index:03d}",
                    start,
                    10,
                    [make_ref("S001", start, start + 10)],
                    drama_prompt(f"第{index}拍：主角在雪地里前行。"),
                )
            )
            start += 10
        plan = base_plan(clips, units, 130)
        out = self.render_exact(plan)
        self.assertTrue((out / "prompts" / "G001.md").is_file())
        self.assertTrue((out / "prompts" / "G013.md").is_file())
        manifest = json.loads((out / "duration-manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["clips"][-1]["prompt_file"], "prompts/G013.md")

    def test_prompt_pack_orders_clips_with_global_summary(self):
        out = self.render_exact()
        pack = (out / "prompt-pack.md").read_text("utf-8")
        self.assertIn("整集提示词包", pack)
        self.assertIn("EP01", pack)
        self.assertIn("目标 20 秒", pack)
        self.assertIn("覆盖率 100.0%", pack)
        self.assertIn("## G001", pack)
        self.assertIn("0—10秒", pack)
        self.assertIn(PROMPT_G001, pack)
        self.assertIn("## G002", pack)
        self.assertIn(PROMPT_G002, pack)
        self.assertLess(pack.index("## G001"), pack.index("## G002"))

    def test_manifest_carries_every_design_10_4_field(self):
        out = self.render_exact()
        manifest = json.loads((out / "duration-manifest.json").read_text("utf-8"))
        for key in (
            "script_target_seconds",
            "planned_prompt_seconds",
            "overage_seconds",
            "overage_ratio",
            "duration_status",
            "clip_range_seconds",
            "timeline_contiguous",
            "coverage_ratio",
            "missing_source_units",
            "duplicate_source_units",
            "system_added_transitions",
            "trim_candidates",
            "real_model_render_status",
            "actual_generated_seconds",
            "final_edit_seconds",
        ):
            self.assertIn(key, manifest, f"机器清单缺少设计 §10.4 字段：{key}")
        self.assertEqual(manifest["script_target_seconds"], 20)
        self.assertEqual(manifest["planned_prompt_seconds"], 20)
        self.assertEqual(manifest["clip_range_seconds"], [4, 10])
        self.assertIs(manifest["timeline_contiguous"], True)
        self.assertEqual(manifest["coverage_ratio"], 1.0)
        self.assertEqual(manifest["missing_source_units"], [])
        self.assertEqual(manifest["system_added_transitions"], [])
        self.assertEqual(manifest["real_model_render_status"], "NOT_RUN")

    def test_manifest_mirrors_the_gate_report(self):
        plan = exact_plan(
            clips=[
                make_clip(
                    "G001",
                    0,
                    10,
                    [make_ref("S001", 0, 10)],
                    PROMPT_G001,
                    trim_candidates=[{"at_seconds": 8.0, "reason": "定格收尾"}],
                ),
                make_clip(
                    "G002",
                    10,
                    10,
                    [make_ref("S001", 10, 12), make_ref("S002", 0, 8)],
                    PROMPT_G002,
                    system_added_transitions=["S003"],
                ),
            ],
            source_units=[
                make_unit(
                    "S001", 0, 12, text=DIALOGUE_TEXT, speaker_ids=["主角"], scene_id="SC01"
                ),
                make_unit("S002", 0, 8, kind=KIND_ACTION, text=ACTION_TEXT, scene_id="SC01"),
                make_unit("S003", kind="system_transition"),
            ],
        )
        report = validate_episode_plan(copy.deepcopy(plan))
        out = self.render_exact(plan)
        manifest = json.loads((out / "duration-manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["duration_status"], report.status)
        self.assertEqual(manifest["error_codes"], list(report.error_codes))
        self.assertEqual(manifest["warning_codes"], list(report.warning_codes))
        self.assertEqual(manifest["overage_seconds"], report.overage_seconds)
        self.assertEqual(manifest["overage_ratio"], report.overage_ratio)
        self.assertEqual(manifest["duplicate_source_units"], list(report.duplicate_source_units))
        self.assertEqual(
            manifest["system_added_transitions"], list(report.system_added_transitions)
        )
        self.assertEqual(
            manifest["trim_candidates"],
            [{"clip_id": t.clip_id, "at_seconds": t.at_seconds, "reason": t.reason}
             for t in report.trim_candidates],
        )
        self.assertEqual(manifest["clip_range_seconds"], list(report.clip_range_seconds))

    def test_manifest_binds_prompt_files_to_audit_sha(self):
        out = self.render_exact()
        manifest = json.loads((out / "duration-manifest.json").read_text("utf-8"))
        self.assertEqual(len(manifest["clips"]), 2)
        first = manifest["clips"][0]
        self.assertEqual(first["clip_id"], "G001")
        self.assertEqual(first["prompt_file"], "prompts/G001.md")
        self.assertEqual(first["duration_seconds"], 10)
        disk_sha = hashlib.sha256((out / "prompts" / "G001.md").read_bytes()).hexdigest()
        self.assertEqual(first["prompt_sha256"], disk_sha)
        self.assertEqual(first["audit_failed"], 0)

    def test_render_does_not_mutate_the_input_plan(self):
        plan = exact_plan()
        frozen = copy.deepcopy(plan)
        _, out = self.out_dir()
        self.assertEqual(render_episode_pack(plan, out), EXIT_RENDERED)
        self.assertEqual(plan, frozen)


class AtomicSwapTest(RenderTestCase):
    """复用 generate_output 的 staging 交换：先全部验证，再原子替换。"""

    def test_overwrite_without_replace_is_refused(self):
        _, out = self.out_dir()
        previous = self.seed_previous_output(out)
        rc = render_episode_pack(EXACT_PLAN, out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertEqual(snapshot(out), previous)

    def test_review_into_nonempty_dir_without_replace_is_refused(self):
        _, out = self.out_dir()
        previous = self.seed_previous_output(out)
        rc = render_episode_pack(OVER_20_PLAN, out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertEqual(snapshot(out), previous)

    def test_audit_failure_keeps_previous_output_and_leaves_refusal_doc(self):
        _, out = self.out_dir()
        previous = self.seed_previous_output(out)
        rc = render_episode_pack(audit_fail_plan(), out, replace=True)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertEqual(snapshot(out), previous)
        refusal = out.parent / ".staging" / out.name / "audit-refusal.json"
        self.assertTrue(refusal.is_file(), "审计失败必须留下 audit-refusal.json 取证")
        detail = json.loads(refusal.read_text("utf-8"))
        self.assertEqual(detail["reason"], "audit_rules_failed")
        self.assertTrue(detail["failed_rule_ids"])

    def test_audit_failure_on_fresh_dir_creates_nothing(self):
        _, out = self.out_dir()
        rc = render_episode_pack(audit_fail_plan(), out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_failed_validation_on_fresh_dir_creates_nothing(self):
        _, out = self.out_dir()
        rc = render_episode_pack(INVALID_PLAN, out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_replace_swaps_review_pair_over_previous_full_pack(self):
        _, out = self.out_dir()
        self.seed_previous_output(out)
        rc = render_episode_pack(OVER_20_PLAN, out, replace=True)
        self.assertEqual(rc, EXIT_REVIEW_REQUIRED)
        self.assertEqual(
            sorted(p.name for p in out.iterdir()),
            ["duration-manifest.json", "duration-review.md"],
        )
        self.assertFalse((out / "prompts").exists())

    def test_out_path_that_is_a_file_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "occupied"
        out.write_text("not a dir", encoding="utf-8")
        self.assertEqual(render_episode_pack(EXACT_PLAN, out), EXIT_REFUSED)
        self.assertEqual(out.read_text("utf-8"), "not a dir")

    def test_declared_workflow_pins_the_audit_channel(self):
        """声明 native-general 却写短剧结构 → 通道不符必须拒绝，不得靠 auto 检测放过。"""
        _, out = self.out_dir()
        rc = render_episode_pack(exact_plan(workflow="native-general"), out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_matching_native_general_plan_renders(self):
        _, out = self.out_dir()
        plan = exact_plan(
            workflow="native-general",
            clips=[
                make_clip("G001", 0, 10, [make_ref("S001", 0, 10)],
                          "主角在雪地里拉紧围巾，顶着风向前走了两步。"),
                make_clip("G002", 10, 10, [make_ref("S001", 10, 12), make_ref("S002", 0, 8)],
                          "主角走向哨塔门口，抬手拂去肩头的积雪，随后推门而入。"),
            ],
        )
        self.assertEqual(render_episode_pack(plan, out), EXIT_RENDERED)
        self.assertTrue((out / "prompts" / "G001.md").is_file())

    def test_replace_flag_is_harmless_on_fresh_dir(self):
        _, out = self.out_dir()
        self.assertEqual(render_episode_pack(EXACT_PLAN, out, replace=True), EXIT_RENDERED)
        self.assertTrue((out / "prompt-pack.md").is_file())


class Freeze22LookupTest(unittest.TestCase):
    """冻结决定 #22：动作原文必须按 beat_ids 反查 Beat.text，不得静默丢失。"""

    def beats(self):
        return [
            Beat(id="B001", scene_id="S1", estimated_seconds=5.0,
                 text="主角低声说出暗号。", kind=KIND_DIALOGUE, splittable=False,
                 speaker_ids=("主角",)),
            Beat(id="B002", scene_id="S1", estimated_seconds=5.0,
                 text="推门而入卷起风雪。", kind=KIND_ACTION),
            Beat(id="B003", scene_id="S1", estimated_seconds=5.0,
                 text="同伴回头望向塔顶。", kind=KIND_DIALOGUE, splittable=False,
                 speaker_ids=("同伴",)),
            Beat(id="B004", scene_id="S1", estimated_seconds=5.0,
                 text="两人踩雪走向哨塔。", kind=KIND_ACTION),
        ]

    def test_planner_round_trip_keeps_every_action_beat(self):
        """pack_beats 输出 → 逐片反查：动作节拍无丢失、无重复、与 beat_ids 保序。"""
        beats = self.beats()
        clips = pack_beats(beats, model_min=4, model_max=15, policy=POLICY)
        self.assertTrue(clips)
        seen: list[str] = []
        for clip in clips:
            looked_up = action_beats_for_clip(beats, clip.beat_ids)
            for beat in looked_up:
                self.assertNotEqual(beat.kind, KIND_DIALOGUE)
            seen.extend(beat.id for beat in looked_up)
        self.assertEqual(seen, ["B002", "B004"])

    def test_lookup_preserves_beat_ids_order(self):
        beats = self.beats()
        looked_up = action_beats_for_clip(beats, ["B004", "B002"])
        self.assertEqual([beat.id for beat in looked_up], ["B004", "B002"])
        self.assertEqual(looked_up[0].text, "两人踩雪走向哨塔。")

    def test_lookup_excludes_dialogue_beats(self):
        looked_up = action_beats_for_clip(self.beats(), ["B001", "B003"])
        self.assertEqual(looked_up, ())

    def test_lookup_on_empty_ids_is_empty(self):
        self.assertEqual(action_beats_for_clip(self.beats(), []), ())

    def test_duplicate_reference_to_one_beat_fails_closed(self):
        """评审 F9：同一 clip 的 beat_ids 重复引用同一节拍必须报错，不得二次渲染。"""
        with self.assertRaises(UnknownBeatReferenceError):
            action_beats_for_clip(self.beats(), ["B002", "B002"])

    def test_unknown_beat_id_fails_closed(self):
        with self.assertRaises(UnknownBeatReferenceError) as ctx:
            action_beats_for_clip(self.beats(), ["B002", "B999"])
        self.assertIn("B999", str(ctx.exception))

    def test_partially_unknown_ids_fail_closed_too(self):
        with self.assertRaises(UnknownBeatReferenceError):
            action_beats_for_clip(self.beats(), ["B00X"])

    def test_duplicate_beat_ids_in_the_input_fail_closed(self):
        impostor = Beat(id="B002", scene_id="S1", estimated_seconds=5.0,
                        text="冒名顶替的动作。", kind=KIND_ACTION)
        with self.assertRaises(UnknownBeatReferenceError):
            action_beats_for_clip(self.beats() + [impostor], ["B002"])


class ManifestHonestyTest(RenderTestCase):
    """诚实清单：真实媒体字段如实透传，不被计划值代填。"""

    def test_not_run_keeps_media_fields_null(self):
        _, out = self.out_dir()
        self.assertEqual(render_episode_pack(EXACT_PLAN, out), EXIT_RENDERED)
        manifest = json.loads((out / "duration-manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["real_model_render_status"], "NOT_RUN")
        self.assertIsNone(manifest["actual_generated_seconds"])
        self.assertIsNone(manifest["final_edit_seconds"])

    def test_completed_declaration_is_passed_through_verbatim(self):
        plan = exact_plan(
            real_model_render_status="COMPLETED",
            actual_generated_seconds=20.0,
            final_edit_seconds=19.5,
        )
        _, out = self.out_dir()
        self.assertEqual(render_episode_pack(plan, out), EXIT_RENDERED)
        manifest = json.loads((out / "duration-manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["real_model_render_status"], "COMPLETED")
        self.assertEqual(manifest["actual_generated_seconds"], 20.0)
        self.assertEqual(manifest["final_edit_seconds"], 19.5)

    def test_review_manifest_stays_honest_and_complete(self):
        _, out = self.out_dir()
        self.assertEqual(render_episode_pack(OVER_20_PLAN, out), EXIT_REVIEW_REQUIRED)
        manifest = json.loads((out / "duration-manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["duration_status"], "REVIEW_REQUIRED")
        self.assertIs(manifest["may_render_full_pack"], False)
        self.assertEqual(manifest["planned_prompt_seconds"], 30)
        self.assertEqual(manifest["script_target_seconds"], 20)
        self.assertEqual(manifest["overage_seconds"], 10)
        self.assertEqual(manifest["overage_ratio"], 0.5)
        self.assertEqual(manifest["real_model_render_status"], "NOT_RUN")
        self.assertIsNone(manifest["actual_generated_seconds"])
        self.assertEqual(manifest["missing_source_units"], [])
        self.assertEqual(manifest["clip_range_seconds"], [4, 10])

    def test_review_summary_lists_trim_positions(self):
        plan = over_20_plan()
        plan["clips"][0]["trim_candidates"] = [{"at_seconds": 0.5, "reason": "开场留白"}]
        _, out = self.out_dir()
        self.assertEqual(render_episode_pack(plan, out), EXIT_REVIEW_REQUIRED)
        review = (out / "duration-review.md").read_text("utf-8")
        self.assertIn("G001 0.5秒（开场留白）", review)

    def test_review_path_policy_failure_refuses(self):
        from unittest import mock

        _, out = self.out_dir()
        with mock.patch("render_episode_pack.load_duration_policy",
                        side_effect=RuntimeError("policy broken")):
            rc = render_episode_pack(OVER_20_PLAN, out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_review_path_recheck_catches_target_populated_mid_review(self):
        from unittest import mock

        _, out = self.out_dir()
        real_create = render_module.create_staging_dir

        def sneaky_create(parent, name):
            slot = real_create(parent, name)
            out.mkdir(parents=True, exist_ok=True)
            (out / "midreview.txt").write_text("landed mid-review", encoding="utf-8")
            return slot

        with mock.patch("render_episode_pack.create_staging_dir", side_effect=sneaky_create):
            rc = render_episode_pack(OVER_20_PLAN, out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertEqual([p.name for p in out.iterdir()], ["midreview.txt"])

    def test_review_threshold_follows_policy_not_hardcoded(self):
        """评审 F7：审查摘要的阈值文案必须跟随政策 overage_review_ratio。"""
        from unittest import mock

        policy = dict(POLICY)
        policy["overage_review_ratio"] = 0.4
        _, out = self.out_dir()
        with mock.patch("render_episode_pack.load_duration_policy", return_value=policy):
            self.assertEqual(render_episode_pack(OVER_20_PLAN, out), EXIT_REVIEW_REQUIRED)
        review = (out / "duration-review.md").read_text("utf-8")
        self.assertIn("40%", review)
        self.assertIn("1.4 倍", review)

    def test_authoritative_target_mismatch_refuses(self):
        """评审 F5：传入剧本解析的权威目标后，改小申报目标必须被拦截。"""
        _, out = self.out_dir()
        rc = render_episode_pack(EXACT_PLAN, out, authoritative_target_seconds=68)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_authoritative_target_check_state_is_recorded(self):
        _, out = self.out_dir()
        self.assertEqual(
            render_episode_pack(EXACT_PLAN, out, authoritative_target_seconds=20),
            EXIT_RENDERED,
        )
        manifest = json.loads((out / "duration-manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["authoritative_target_check"], "performed")

        _, other = self.out_dir()
        self.assertEqual(render_episode_pack(EXACT_PLAN, other), EXIT_RENDERED)
        other_manifest = json.loads((other / "duration-manifest.json").read_text("utf-8"))
        self.assertEqual(other_manifest["authoritative_target_check"], "not_performed")

    def test_review_summary_names_the_decision_and_the_forbidden_shortcut(self):
        _, out = self.out_dir()
        self.assertEqual(render_episode_pack(OVER_20_PLAN, out), EXIT_REVIEW_REQUIRED)
        review = (out / "duration-review.md").read_text("utf-8")
        self.assertIn("REVIEW_REQUIRED", review)
        self.assertIn("may_render_full_pack", review)
        self.assertIn("OVERAGE_REVIEW_REQUIRED", review)
        self.assertIn("目标时长：20 秒", review)
        self.assertIn("计划时长：30 秒", review)
        self.assertIn("超出：10 秒", review)
        self.assertIn("50.0%", review)
        self.assertIn("不得为过门禁下调剧本声明时长", review)


class CliTest(RenderTestCase):
    """CLI 合同：--plan/--out/--replace 与退出码 0/1/2/3。"""

    def write_plan_file(self, plan) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "episode-plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return path

    def invoke(self, argv):
        from render_episode_pack import main as cli_main

        return cli_main(argv)

    def test_cli_renders_full_pack(self):
        plan_file = self.write_plan_file(EXACT_PLAN)
        _, out = self.out_dir()
        rc = self.invoke(["--plan", str(plan_file), "--out", str(out)])
        self.assertEqual(rc, EXIT_RENDERED)
        self.assertTrue((out / "episode-timeline.md").is_file())

    def test_cli_review_required_exit_code_two(self):
        plan_file = self.write_plan_file(OVER_20_PLAN)
        _, out = self.out_dir()
        rc = self.invoke(["--plan", str(plan_file), "--out", str(out)])
        self.assertEqual(rc, EXIT_REVIEW_REQUIRED)
        self.assertTrue((out / "duration-manifest.json").is_file())

    def test_cli_invalid_plan_exit_code_one(self):
        plan_file = self.write_plan_file(INVALID_PLAN)
        _, out = self.out_dir()
        rc = self.invoke(["--plan", str(plan_file), "--out", str(out)])
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_cli_missing_plan_file_exit_code_one(self):
        _, out = self.out_dir()
        rc = self.invoke(["--plan", "Z:/definitely/missing/plan.json", "--out", str(out)])
        self.assertEqual(rc, EXIT_REFUSED)

    def test_cli_malformed_json_exit_code_one(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        _, out = self.out_dir()
        rc = self.invoke(["--plan", str(path), "--out", str(out)])
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_cli_replace_flag_reaches_the_renderer(self):
        _, out = self.out_dir()
        self.seed_previous_output(out)
        plan_file = self.write_plan_file(EXACT_PLAN)
        rc = self.invoke(["--plan", str(plan_file), "--out", str(out), "--replace"])
        self.assertEqual(rc, EXIT_RENDERED)
        self.assertTrue((out / "prompt-pack.md").is_file())

    def test_script_entrypoint_renders_end_to_end(self):
        """真实子进程跑 scripts/render_episode_pack.py 本体（覆盖 __main__ 入口）。"""
        from conftest import run_py

        plan_file = self.write_plan_file(EXACT_PLAN)
        _, out = self.out_dir()
        script = SKILL_ROOT / "scripts" / "render_episode_pack.py"
        proc = run_py([str(script), "--plan", str(plan_file), "--out", str(out)])
        self.assertEqual(proc.returncode, EXIT_RENDERED, proc.stderr)
        self.assertTrue((out / "prompt-pack.md").is_file())

    def test_usage_error_exits_one_not_two(self):
        """评审 F8：argparse 用法错误不得占用 REVIEW_REQUIRED 的退出码 2。"""
        from render_episode_pack import main as cli_main

        _, out = self.out_dir()
        with self.assertRaises(SystemExit) as ctx:
            cli_main(["--out", str(out)])  # 缺 --plan
        self.assertEqual(ctx.exception.code, EXIT_REFUSED)

    def test_cli_internal_crash_exit_code_three(self):
        from unittest import mock

        plan_file = self.write_plan_file(EXACT_PLAN)
        _, out = self.out_dir()
        with mock.patch("render_episode_pack.validate_episode_plan",
                        side_effect=RuntimeError("boom")):
            rc = self.invoke(["--plan", str(plan_file), "--out", str(out)])
        self.assertEqual(rc, 3)


class DefensiveRefusalTest(RenderTestCase):
    """Fail-Closed 防线：审计崩溃 / 政策失效 / 配置异常 / 绑定断裂都不得交换。"""

    def test_audit_process_crash_refuses(self):
        from unittest import mock

        _, out = self.out_dir()
        with mock.patch("render_episode_pack.run_real_audit_cli",
                        side_effect=RuntimeError("audit subprocess died")):
            rc = render_episode_pack(EXACT_PLAN, out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_unusable_policy_refuses(self):
        from unittest import mock

        _, out = self.out_dir()
        with mock.patch("render_episode_pack.load_duration_policy",
                        side_effect=RuntimeError("policy broken")):
            rc = render_episode_pack(EXACT_PLAN, out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_unknown_workflow_refuses_instead_of_auto_detecting(self):
        """评审 F1（Block）：未知工作流必须拒绝，不得让 auto 按文本降级审计通道。"""
        _, out = self.out_dir()
        rc = render_episode_pack(exact_plan(workflow="vendor-x"), out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_audit_report_missing_failed_count_refuses(self):
        """评审 F6：审计报告缺 failed 计数时 fail-closed，不得默认放行。"""
        from unittest import mock

        _, out = self.out_dir()
        with mock.patch("render_episode_pack.run_real_audit_cli", return_value={}):
            rc = render_episode_pack(EXACT_PLAN, out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_mid_audit_landing_in_target_is_caught_by_swap_recheck(self):
        """评审 F2：审计窗口内落入目标目录的文件必须在交换前复检中被拒绝。"""
        from unittest import mock

        _, out = self.out_dir()

        def spy_audit(prompt_text, mode):
            out.mkdir(parents=True, exist_ok=True)
            (out / "victim.txt").write_text("landed mid-audit", encoding="utf-8")
            return {
                "failed": 0,
                "results": [],
                "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            }

        with mock.patch("render_episode_pack.run_real_audit_cli", side_effect=spy_audit):
            rc = render_episode_pack(EXACT_PLAN, out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertEqual([p.name for p in out.iterdir()], ["victim.txt"])

    def test_audit_report_none_refuses(self):
        """并发审计的防御行：报告为 None（非异常也非 JSON）必须拒绝。"""
        from unittest import mock

        _, out = self.out_dir()
        with mock.patch("render_episode_pack.run_real_audit_cli",
                        side_effect=lambda *args, **kwargs: None):
            rc = render_episode_pack(EXACT_PLAN, out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_gate_config_error_refuses(self):
        from unittest import mock

        _, out = self.out_dir()
        with mock.patch("render_episode_pack.validate_episode_plan",
                        side_effect=EpisodeContractError("config unavailable")):
            rc = render_episode_pack(EXACT_PLAN, out)
        self.assertEqual(rc, EXIT_REFUSED)
        self.assertFalse(out.exists())

    def test_audit_binding_mismatch_crashes_without_swap(self):
        from unittest import mock

        _, out = self.out_dir()

        def corrupt_write(path, text):
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")

        with mock.patch.object(render_module, "_write_text", side_effect=corrupt_write):
            with self.assertRaises(RuntimeError):
                render_episode_pack(EXACT_PLAN, out)
        self.assertFalse(out.exists())


class ConfigConsistencyTest(RenderTestCase):
    """优化轮：配置单次读盘，门禁与台账/审查文案共用同一份政策对象。"""

    def test_config_is_loaded_exactly_once_per_render(self):
        from unittest import mock

        calls = {"policy": 0, "caps": 0}
        real_policy, real_caps = load_duration_policy, load_model_capabilities

        def counting_policy():
            calls["policy"] += 1
            return real_policy()

        def counting_caps():
            calls["caps"] += 1
            return real_caps()

        _, out = self.out_dir()
        with mock.patch("render_episode_pack.load_duration_policy", side_effect=counting_policy), \
             mock.patch("render_episode_pack.load_model_capabilities", side_effect=counting_caps):
            self.assertEqual(render_episode_pack(EXACT_PLAN, out), EXIT_RENDERED)
        self.assertEqual(calls["policy"], 1)
        self.assertEqual(calls["caps"], 1)

    def test_concurrent_audits_keep_order_and_verdicts(self):
        """并发审计：逐条裁决结果与串行一致，且片段顺序不乱。"""
        from unittest import mock

        seen_order = []
        real_audit = render_module.run_real_audit_cli

        def recording_audit(prompt_text, mode):
            report = real_audit(prompt_text, mode)
            seen_order.append(prompt_text[:12])
            return report

        plan = base_plan(
            [
                make_clip("G001", 0, 10, [make_ref("S001", 0, 10)], PROMPT_G001),
                make_clip("G002", 10, 10, [make_ref("S001", 10, 12), make_ref("S002", 0, 8)], PROMPT_G002),
            ],
            [
                make_unit("S001", 0, 12, text=DIALOGUE_TEXT, speaker_ids=["主角"]),
                make_unit("S002", 0, 8, kind=KIND_ACTION, text=ACTION_TEXT),
            ],
            20,
        )
        _, out = self.out_dir()
        with mock.patch("render_episode_pack.run_real_audit_cli", side_effect=recording_audit):
            self.assertEqual(render_episode_pack(plan, out), EXIT_RENDERED)
        manifest = json.loads((out / "duration-manifest.json").read_text("utf-8"))
        self.assertEqual([c["clip_id"] for c in manifest["clips"]], ["G001", "G002"])
        self.assertEqual([c["audit_failed"] for c in manifest["clips"]], [0, 0])


class GenerateOutputCompatTest(unittest.TestCase):
    """generate_output.py 抽取的原子交换原语行为不变；全链 e2e 照常工作。"""

    def test_atomic_swap_replaces_target_content(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target"
            staging = Path(td) / "staging"
            target.mkdir()
            (target / "a.txt").write_text("old", encoding="utf-8")
            staging.mkdir()
            (staging / "b.txt").write_text("new", encoding="utf-8")
            generate_output.atomic_swap(str(staging), str(target))
            self.assertEqual([p.name for p in target.iterdir()], ["b.txt"])
            self.assertFalse(staging.exists())
            self.assertFalse(Path(str(target) + ".bak").exists())

    def test_atomic_swap_restores_target_when_staging_rename_fails(self):
        import os as _os
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target"
            staging = Path(td) / "staging"
            target.mkdir()
            (target / "a.txt").write_text("old", encoding="utf-8")
            staging.mkdir()
            (staging / "b.txt").write_text("new", encoding="utf-8")
            real_rename = _os.rename

            def failing_rename(src, dst):
                if str(src) == str(staging):
                    raise OSError("simulated swap failure")
                return real_rename(src, dst)

            with mock.patch("generate_output.os.rename", side_effect=failing_rename):
                with self.assertRaises(OSError):
                    generate_output.atomic_swap(str(staging), str(target))
            self.assertEqual([p.name for p in target.iterdir()], ["a.txt"])
            self.assertFalse(Path(str(target) + ".bak").exists())

    def test_atomic_swap_clears_a_stale_backup(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target"
            staging = Path(td) / "staging"
            target.mkdir()
            (target / "a.txt").write_text("old", encoding="utf-8")
            Path(str(target) + ".bak").mkdir()
            (Path(str(target) + ".bak") / "stale.txt").write_text("bak", encoding="utf-8")
            staging.mkdir()
            (staging / "b.txt").write_text("new", encoding="utf-8")
            generate_output.atomic_swap(str(staging), str(target))
            self.assertFalse(Path(str(target) + ".bak").exists())
            self.assertEqual([p.name for p in target.iterdir()], ["b.txt"])

    def test_swap_completes_even_if_backup_cleanup_fails(self):
        """评审 F3：交换成功后的 .bak 清理失败不得把成功说成崩溃（rc=3 谎报）。"""
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target"
            staging = Path(td) / "staging"
            target.mkdir()
            (target / "a.txt").write_text("old", encoding="utf-8")
            staging.mkdir()
            (staging / "b.txt").write_text("new", encoding="utf-8")
            with mock.patch("shutil.rmtree", side_effect=OSError("locked by scanner")):
                generate_output.atomic_swap(str(staging), str(target))
            self.assertEqual([p.name for p in target.iterdir()], ["b.txt"])
            self.assertTrue(Path(str(target) + ".bak").exists(),
                            "清理失败时应留下 .bak 供手工处理")

    def test_create_staging_dir_is_fresh_and_inside_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = Path(generate_output.create_staging_dir(str(root), "render-slot"))
            (first / "leftover.txt").write_text("x", encoding="utf-8")
            second = Path(generate_output.create_staging_dir(str(root), "render-slot"))
            self.assertEqual(first, second)
            self.assertEqual(list(second.iterdir()), [])
            self.assertTrue(str(second).startswith(str(root)))

    def test_generate_case_full_chain_still_works(self):
        case = {
            "case_id": "task6_compat_case",
            "model": "H3",
            "duration_s": 6,
            "scene": "雪原边缘的废弃哨塔外",
            "action": "主角拉紧围巾向前走，脚步压进新雪",
            "style": "冷蓝雪夜，低照度侧光",
            "audio": "风声持续，无配乐",
            "voice_signature": "主角（男，低沉沙哑，语速偏慢，音量压低）",
            "user_prompt": "雪夜哨塔外主角独自前行",
        }
        with tempfile.TemporaryDirectory() as td:
            case_path = Path(td) / "case.json"
            case_path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
            out_root = Path(td) / "results"
            summary = generate_output.generate_case(str(case_path), str(out_root))
            self.assertEqual(summary["exit_code"], 0)
            case_dir = Path(summary["case_dir"])
            self.assertEqual(
                sorted(p.name for p in case_dir.iterdir()),
                ["analysis.json", "audit-report.json", "prompt.txt"],
            )
            audit_doc = json.loads((case_dir / "audit-report.json").read_text("utf-8"))
            self.assertEqual(audit_doc["failed"], 0)
            disk_sha = hashlib.sha256((case_dir / "prompt.txt").read_bytes()).hexdigest()
            self.assertEqual(audit_doc["prompt_sha256"], disk_sha)
            replace_summary = generate_output.generate_case(
                str(case_path), str(out_root), run_id=summary["run_id"], replace=True
            )
            self.assertEqual(replace_summary["exit_code"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
