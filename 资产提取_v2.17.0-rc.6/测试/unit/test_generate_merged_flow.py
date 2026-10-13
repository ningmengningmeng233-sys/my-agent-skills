#!/usr/bin/env python3
"""Integration tests for merged character prompt documents (LLM mocked)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate_prompts as gp


PERSON_PROMPT = (
    "国漫三维角色三格图板设定图。左面人物正面特写，右面正视图与后视图，"
    "素灰背景#B8B8B8，均匀柔光，物理渲染材质，国漫三维。全身立像。图中不要有任何文字。"
    "身份锁：鹅蛋脸清秀，杏眼温和，鼻梁挺直，黑发高束。"
    "负面提示词：后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。"
)
SCENE_PROMPT = (
    "国漫三维场景主视图概念图，物理渲染材质，自然光。景别：全景。角度：平视。布光：晨光。"
    "画面中无任何人物；单画面；图中不要有任何文字。"
    "负面提示词：无人物、无低精度/塑料感、无光照均匀平坦。"
)
DERIV_PERSON = ("国漫三维人物衍生提示词，三格图板：左面人物正面特写，右面正视图与后视图，"
                "素灰背景#B8B8B8，图中不要有任何文字。"
                "身份锁：鹅蛋脸清秀，杏眼温和，鼻梁挺直，黑发高束。"
                "负面提示词：后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。")
DERIV_SCENE = ("国漫三维场景衍生提示词，单画面，无人物，图中不要有任何文字。"
               "景别：全景。角度：平视。布光：晨光。"
               "负面提示词：无人物、无低精度/塑料感、无光照均匀平坦。")


def _rows():
    return [
        ("人物", ["p1", "林姑娘", "林小姐", "第1章", "S", ""]),
        ("人物", ["p2", "船夫", "", "第2章", "A", ""]),
        ("场景", ["s1", "茶楼", "第1章", "A", ""]),
    ]


def _type_aware_stub(*args, **kwargs):
    """合规桩：按任务类型返回合规内容（人物→人物桩，场景→场景桩）。

    T11 合规闸后，桩输出必须各自满足其类型的硬要求，且不得触发纠正重试。
    """
    user = args[1] if len(args) > 1 else ""
    if "场景:" in user or "类型: 场景" in user or "类型:场景" in user:
        return (SCENE_PROMPT, {})
    return (PERSON_PROMPT, {})


# 缺负面锁的场景产出：其余硬要求都在，只差「无低精度塑料感」那一条等价项。
SCENE_PROMPT_NO_LOCK = (
    "国漫三维场景主视图概念图，物理渲染材质，自然光。景别：全景。角度：平视。布光：晨光。"
    "画面中无任何人物；单画面；图中不要有任何文字。"
    "负面提示词：无人物、无光照均匀平坦。"
)


def _scene_rows():
    return [("场景", ["s1", "茶楼", "第1章", "A", ""])]


class TestComplianceRepairWiring(unittest.TestCase):
    """合规闸与机械化修复的接线（实测：闸门丢弃产出、autofix 无对象可修 → 链路卡死）。"""

    def test_incompliant_output_is_repaired_not_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm",
                                   side_effect=lambda *a, **k: (SCENE_PROMPT_NO_LOCK, {})):
                rc = gp.generate_base(_scene_rows(), "玄幻", lib, False)
            merged = lib / "提示词" / "基础" / "场景提示词.md"
            self.assertTrue(merged.exists(),
                            "缺负面锁的产出一旦被丢弃，这个文件就不会出现")
            text = merged.read_text(encoding="utf-8")
            self.assertIn("茶楼", text)
            from engine import prompt_rules
            ok, missing = prompt_rules.check_compliance(text, "场景")
            self.assertTrue(ok, f"修复后仍不合规：{missing}")
            self.assertEqual(rc, 0)

    def test_unrepairable_still_fails_loudly(self):
        """修不好时仍计失败——闸门不许被这次改动削弱。

        用**真实内容**构造不可修复的输入：缺「身份锁」不在 autofix 的五条规则覆盖内
        （它只管顿号/镜头体系/三视图/负面锁/层次雷同），所以修复后必然仍不合格。
        不 mock 修复函数，否则测的是桩而不是行为。
        """
        person_no_identity = (
            "国漫三维角色三格图板设定图。左面人物正面特写，右面正视图与后视图，"
            "素灰背景#B8B8B8，均匀柔光，物理渲染材质，国漫三维。全身立像。图中不要有任何文字。"
            "负面提示词：后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。"
        )
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm",
                                   side_effect=lambda *a, **k: (person_no_identity, {})):
                rc = gp.generate_base([("人物", ["p1", "林姑娘", "林小姐", "第1章", "S", ""])],
                                      "玄幻", lib, False)
            self.assertGreater(rc, 0, "修不好的产出必须仍计失败，闸门不得被放宽")
            merged = lib / "提示词" / "基础" / "人物提示词.md"
            self.assertFalse(merged.exists(), "不合格内容不得进入合并文档")

    def test_repair_keeps_gate_teeth(self):
        """修复只补机械性缺口，不放行真正的缺失（同一份规则判定）。"""
        from engine import autofix, prompt_rules
        scene_missing_all = (
            "国漫三维场景主视图概念图。景别：全景。角度：平视。布光：晨光。"
            "画面中无任何人物；单画面；图中不要有任何文字。"
        )   # 连负面段都没有
        fixed, n = autofix.repair_text(scene_missing_all, "场景")
        ok, missing = prompt_rules.check_compliance(fixed, "场景")
        self.assertTrue(ok, f"机械性缺口应被补齐，实际仍缺 {missing}")
        self.assertGreater(n, 0)

    def test_repair_converges_when_one_fix_breaks_another(self):
        """补一条规则不能把另一条规则弄成不合格（对抗评审 B2）。

        追加三视图会把同一行的顿号推过上限，而顿号规则排在它之前；
        单轮遍历会停在"既不合规、又比原文更差"的中间态。
        """
        from engine import autofix, prompt_rules
        text = ("素灰、材质、纹理、光泽、厚度、缝线、纹样、包边、伞骨\n"
                "负面提示词：无人物/肢体、无复杂场景背景。\n")   # 缺三视图；该行 8 个顿号未超限
        fixed, n = autofix.repair_text(text, "道具")
        ok, missing = prompt_rules.check_compliance(fixed, "道具")
        self.assertTrue(ok, f"修复应收敛到合规，实际仍缺 {missing}")
        self.assertGreater(n, 0)


class TestRejectedOutputHandling(unittest.TestCase):
    """修复后仍不合格的产出如何留档（对抗评审 B3/B4）。

    用「人物缺身份锁」构造真实不可修复输入：身份锁不在 autofix 的五条规则覆盖内。
    """

    PERSON_NO_IDENTITY = (
        "国漫三维角色三格图板设定图。左面人物正面特写，右面正视图与后视图，"
        "素灰背景#B8B8B8，均匀柔光，物理渲染材质，国漫三维。全身立像。图中不要有任何文字。"
        "负面提示词：后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。"
    )

    def _two_person_rows(self):
        return [("人物", ["p1", "林姑娘", "林小姐", "第1章", "S", ""]),
                ("人物", ["p2", "船夫", "", "第2章", "A", ""])]

    def _run(self, lib, stub):
        with mock.patch.object(gp, "call_llm", side_effect=stub):
            return gp.generate_base(self._two_person_rows(), "玄幻", lib, False)

    @staticmethod
    def _mixed_stub(*args, **kwargs):
        """同一次运行里：林姑娘不合格（修不好），船夫正常。"""
        user = args[1] if len(args) > 1 else ""
        if "林姑娘" in user:
            return (TestRejectedOutputHandling.PERSON_NO_IDENTITY, {})
        return (PERSON_PROMPT, {})

    def test_rejected_output_survives_sibling_success(self):
        """同类型里有兄弟成功时，失败产出也不得被连带删除。"""
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            rc = self._run(lib, self._mixed_stub)
            self.assertGreater(rc, 0)
            merged = lib / "提示词" / "基础" / "人物提示词.md"
            text = merged.read_text(encoding="utf-8")
            self.assertIn("船夫", text)
            self.assertNotIn("林姑娘", text)
            rejected = list((lib / "提示词" / "基础" / gp.REJECT_DIR_NAME).glob("*.md"))
            self.assertEqual(len(rejected), 1, "失败产出必须留档，不能被连带删掉")
            self.assertIn("林姑娘", rejected[0].read_text(encoding="utf-8"))

    def test_rejected_output_not_promoted_on_rerun(self):
        """失败产出不得靠"再跑一次"绕过闸门进入合并文档。"""
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            self._run(lib, self._mixed_stub)
            rc2 = self._run(lib, self._mixed_stub)          # 第二次运行
            merged = lib / "提示词" / "基础" / "人物提示词.md"
            text = merged.read_text(encoding="utf-8")
            self.assertNotIn("林姑娘", text, "不合格内容不得被事后提升")
            self.assertGreater(rc2, 0, "第二次运行仍须把不合格项计为失败")


class TestMergedBaseFlow(unittest.TestCase):
    def test_character_prompts_merge_into_one_numbered_document(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub):
                self.assertEqual(gp.generate_base(_rows(), "玄幻", lib, False), 0)
            merged = lib / "提示词" / "基础" / "人物提示词.md"
            self.assertTrue(merged.exists())
            text = merged.read_text(encoding="utf-8")
            self.assertIn("## 001. 林姑娘", text)
            self.assertIn("## 002. 船夫", text)
            self.assertLess(text.index("001"), text.index("002"))
            self.assertFalse((lib / "提示词" / "基础" / "人物_林姑娘.md").exists())
            self.assertFalse((lib / "提示词" / "基础" / "场景_茶楼.md").exists())
            scene_doc = lib / "提示词" / "基础" / "场景提示词.md"
            self.assertTrue(scene_doc.exists())
            self.assertIn("## 001. 茶楼", scene_doc.read_text(encoding="utf-8"))

    def test_second_run_skips_merged_characters(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub) as m:
                gp.generate_base(_rows(), "玄幻", lib, False)
                first_calls = m.call_count
                gp.generate_base(_rows(), "玄幻", lib, False)
                self.assertEqual(m.call_count, first_calls)  # all jobs skipped

    def test_force_removes_old_section_when_regeneration_fails(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub):
                self.assertEqual(gp.generate_base(_rows(), "玄幻", lib, False), 0)
            merged = lib / "提示词" / "基础" / "人物提示词.md"
            self.assertIn("## 001. 林姑娘", merged.read_text(encoding="utf-8"))
            with mock.patch.object(gp, "call_llm", side_effect=RuntimeError("boom")):
                self.assertGreater(gp.generate_base(_rows(), "玄幻", lib, True), 0)
            text = merged.read_text(encoding="utf-8")
            self.assertNotIn("## 001. 林姑娘", text)  # 失败不允许旧内容伪装成已覆盖

    def test_crop_mode_writes_crop_person_document(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub):
                self.assertEqual(gp.generate_base(_rows(), "玄幻", lib, False, view_mode="crop"), 0)
            crop_doc = lib / "提示词" / "基础" / "人物提示词_裁头版.md"
            full_doc = lib / "提示词" / "基础" / "人物提示词.md"
            self.assertTrue(crop_doc.exists())
            self.assertFalse(full_doc.exists())  # 裁头版是独立可选产物，不混入默认版
            text = crop_doc.read_text(encoding="utf-8")
            self.assertIn("## 001. 林姑娘", text)
            self.assertIn("## 002. 船夫", text)


class TestMergedEnrichFlow(unittest.TestCase):
    def test_descriptions_merge_by_type(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            rows = [
                {"type": "人物", "name": "林姑娘", "level": "S", "note": ""},
                {"type": "场景", "name": "茶楼", "level": "A", "note": ""},
                {"type": "道具", "name": "琴", "level": "B", "note": ""},
            ]
            long_desc = "视觉描述内容足够长，材质与光影清晰。" * 6
            with mock.patch.object(gp, "call_llm", return_value=(long_desc, {})):
                self.assertEqual(gp.generate_enrich(rows, "古风", lib, False), 0)
            for fname, title in (("人物描述.md", "林姑娘"), ("场景描述.md", "茶楼"), ("道具描述.md", "琴")):
                doc = lib / "描述" / fname
                self.assertTrue(doc.exists(), fname)
                text = doc.read_text(encoding="utf-8")
                self.assertIn(f"## 001. {title}", text)


class TestLegacyMigration(unittest.TestCase):
    def test_legacy_character_file_migrates_and_is_removed(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            base_dir = lib / "提示词" / "基础"
            base_dir.mkdir(parents=True)
            legacy = base_dir / "人物_林姑娘.md"
            legacy.write_text("旧版人物提示词内容，长度足够跳过检查。" * 4 + "图中不要有任何文字", encoding="utf-8")
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub) as m:
                gp.generate_base(_rows(), "玄幻", lib, False)
                self.assertEqual(m.call_count, 2)  # 林姑娘迁移跳过，船夫+场景生成
            merged = base_dir / "人物提示词.md"
            self.assertTrue(merged.exists())
            self.assertIn("旧版人物提示词内容", merged.read_text(encoding="utf-8"))
            self.assertFalse(legacy.exists())


class TestMergedDerivFlow(unittest.TestCase):
    def test_character_variants_merge_by_schedule_order(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub):
                gp.generate_base(_rows(), "玄幻", lib, False)
            items = [
                {"类型": "人物", "资产": "林姑娘", "变体": "淡蓝长裙", "备注": "服装造型衍生"},
                {"类型": "人物", "资产": "船夫", "变体": "蓑衣", "备注": "服装造型衍生"},
                {"类型": "场景", "资产": "茶楼", "变体": "黄昏", "备注": "剧本驱动"},
            ]
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub) as m:
                gp.generate_deriv(items, "玄幻", lib, False)
                self.assertEqual(m.call_count, 3)
            merged = lib / "提示词" / "衍生" / "人物提示词.md"
            self.assertTrue(merged.exists())
            text = merged.read_text(encoding="utf-8")
            self.assertIn("## 001. 林姑娘（淡蓝长裙）", text)
            self.assertIn("## 002. 船夫（蓑衣）", text)
            self.assertFalse((lib / "提示词" / "衍生" / "场景_茶楼_黄昏.md").exists())
            scene_doc = lib / "提示词" / "衍生" / "场景提示词.md"
            self.assertTrue(scene_doc.exists())
            self.assertIn("## 001. 茶楼（黄昏）", scene_doc.read_text(encoding="utf-8"))
            # second run skips both character variants (schedule unchanged)
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub) as m2:
                gp.generate_deriv(items, "玄幻", lib, False)
                self.assertEqual(m2.call_count, 0)

    def test_scene_variant_injects_base_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub):
                gp.generate_base(_rows(), "玄幻", lib, False)
            items = [
                {"类型": "场景", "资产": "茶楼", "变体": "黄昏", "备注": "剧本驱动"},
            ]
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub) as m:
                self.assertEqual(gp.generate_deriv(items, "玄幻", lib, False), 0)
            user_prompt = m.call_args[0][1]
            self.assertIn(SCENE_PROMPT, user_prompt)

    def test_person_variant_fails_without_base_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            items = [
                {"类型": "人物", "资产": "林姑娘", "变体": "淡蓝长裙", "备注": "服装造型衍生"},
            ]
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub) as m:
                self.assertGreater(gp.generate_deriv(items, "玄幻", lib, False), 0)
                m.assert_not_called()

    def test_scene_variant_fails_without_base_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            items = [
                {"类型": "场景", "资产": "茶楼", "变体": "黄昏", "备注": "剧本驱动"},
            ]
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub) as m:
                self.assertGreater(gp.generate_deriv(items, "玄幻", lib, False), 0)
                m.assert_not_called()

    def test_crop_variants_write_crop_person_document(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub):
                gp.generate_base(_rows(), "玄幻", lib, False)
            items = [
                {"类型": "人物", "资产": "林姑娘", "变体": "淡蓝长裙", "备注": "服装造型衍生"},
                {"类型": "人物", "资产": "船夫", "变体": "蓑衣", "备注": "服装造型衍生"},
            ]
            with mock.patch.object(gp, "call_llm", side_effect=_type_aware_stub):
                self.assertEqual(gp.generate_deriv(items, "玄幻", lib, False, view_mode="crop"), 0)
            crop_doc = lib / "提示词" / "衍生" / "人物提示词_裁头版.md"
            full_doc = lib / "提示词" / "衍生" / "人物提示词.md"
            self.assertTrue(crop_doc.exists())
            self.assertFalse(full_doc.exists())
            text = crop_doc.read_text(encoding="utf-8")
            self.assertIn("## 001. 林姑娘（淡蓝长裙）", text)
            self.assertIn("## 002. 船夫（蓑衣）", text)


DESC_OK = ("面部骨相：方圆清秀，脸型：鹅蛋，肤色：冷白。"
           "负面限制：无网红脸、无随机换脸、无夸张表情。\n")
DESC_NO_LIMIT = "面部骨相：方圆清秀，脸型：鹅蛋，肤色：冷白。\n"


class TestDescComplianceWiring(unittest.TestCase):
    """描述补全的生成侧自检（工单 T3）。

    实测：描述模板**要求**输出「负面限制」段、校验器（check_schedule）**也查**它，
    唯独生成时不检查（guard_on 只含基础/衍生提示词）——35 项漏 1 项，
    要白跑一整趟到 verify 才发现。
    """

    @staticmethod
    def _rows():
        return [{"name": "林姑娘", "type": "人物", "level": "S", "note": ""}]

    def test_missing_negative_limit_is_retried(self):
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            return (DESC_NO_LIMIT if len(calls) == 1 else DESC_OK, {})

        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                rc = gp.generate_enrich(self._rows(), "古风", lib, False)
            self.assertEqual(len(calls), 2, "缺负面限制时应带纠正提示重试一次")
            self.assertIn("描述负面限制", calls[1], "纠正提示要点名缺的那一项")
            text = (lib / "描述" / "人物描述.md").read_text(encoding="utf-8")
            self.assertIn("负面限制", text)
            self.assertEqual(rc, 0)

    def test_compliant_desc_is_not_retried(self):
        """防多余重试：合规描述只发一次请求（重试是要花钱的）。"""
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            return (DESC_OK, {})

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                rc = gp.generate_enrich(self._rows(), "古风", Path(td), False)
            self.assertEqual(len(calls), 1, "合规产出不得触发重试")
            self.assertEqual(rc, 0)

    def test_unrepairable_desc_keeps_content(self):
        """重试仍不合格时**保留内容**——描述整个丢掉比缺一个标记更糟。

        缺项由 verify 记名（check_schedule 会报"缺描述负面限制"），不是静默放过。
        """
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm",
                                   side_effect=lambda *a, **k: (DESC_NO_LIMIT, {})):
                rc = gp.generate_enrich(self._rows(), "古风", lib, False)
            text = (lib / "描述" / "人物描述.md").read_text(encoding="utf-8")
            self.assertIn("面部骨相", text, "内容不得因不合规而丢失")
            self.assertNotIn("负面限制", text)
            self.assertEqual(rc, 0, "保留内容即算产出成功；缺项留给 verify 记名")


# 含抽象空洞词的产出：除用词外与 PERSON_PROMPT 完全一致（硬判据全部满足）。
PERSON_PROMPT_TASTY = PERSON_PROMPT.replace("全身立像。", "全身立像，气质极致华丽。")


class TestTasteWiring(unittest.TestCase):
    """用词审查的生成侧接线（工单 F6②）。

    实测：模型写出「极致/唯美」，而生成请求没提抽象空洞词、生成闸也不查，
    一路带到 verify 才被 check_schedule 拦下——一次全链白跑。
    """

    @staticmethod
    def _rows():
        return [("人物", ["p1", "林姑娘", "林小姐", "第1章", "S", ""])]

    def test_request_announces_the_word_list(self):
        """生成请求必须把词表原样告知模型（不告知等于让它猜哪几个词会被拦）。"""
        seen: list[str] = []

        def stub(system, user, *a, **k):
            seen.append(" ".join(k.get("extra_requirements") or ()))
            return (PERSON_PROMPT, {})

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                gp.generate_base(self._rows(), "玄幻", Path(td), False)
        self.assertTrue(seen)
        self.assertIn("抽象空洞词", seen[0])
        self.assertIn("极致", seen[0])

    def test_taste_hit_is_retried_once_and_clean_version_kept(self):
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            return (PERSON_PROMPT_TASTY if len(calls) == 1 else PERSON_PROMPT, {})

        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                rc = gp.generate_base(self._rows(), "玄幻", lib, False)
            self.assertEqual(len(calls), 2, "命中抽象空洞词应点名重试一次")
            self.assertIn("用词纠正", calls[1])
            self.assertIn("极致", calls[1], "纠正提示要点名命中的词")
            text = (lib / "提示词" / "基础" / "人物提示词.md").read_text(encoding="utf-8")
            self.assertNotIn("极致", text)
            self.assertEqual(rc, 0)

    def test_stubborn_taste_hit_keeps_content_without_failing(self):
        """重试仍命中时**保留产出且不判失败**。

        一个填充词若升级成「整条资产丢弃」，verify 的覆盖率会跟着崩——
        实测第八步的死锁就是这么来的。命中项由 verify 记名（报告写「命中抽象空洞词」）。
        """
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            return (PERSON_PROMPT_TASTY, {})

        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                rc = gp.generate_base(self._rows(), "玄幻", lib, False)
            self.assertEqual(len(calls), 2, "只重试一次，不做无界重试")
            text = (lib / "提示词" / "基础" / "人物提示词.md").read_text(encoding="utf-8")
            self.assertIn("林姑娘", text, "内容不得因一个填充词而丢失")
            self.assertIn("极致", text)
            self.assertEqual(rc, 0, "保留内容即算产出成功")

    def test_compliant_person_prompt_is_not_retried(self):
        """防多余重试：干净产出只发一次请求。"""
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            return (PERSON_PROMPT, {})

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                rc = gp.generate_base(self._rows(), "玄幻", Path(td), False)
            self.assertEqual(len(calls), 1)
            self.assertEqual(rc, 0)

    def test_taste_retry_that_breaks_hard_rules_is_rejected(self):
        """重试结果必须**硬判据也过关**才采纳：否则宁可留上一版。"""
        person_no_identity = PERSON_PROMPT_TASTY.replace("身份锁：鹅蛋脸清秀，杏眼温和，鼻梁挺直，黑发高束。", "")
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            return (PERSON_PROMPT_TASTY if len(calls) == 1 else person_no_identity, {})

        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                rc = gp.generate_base(self._rows(), "玄幻", lib, False)
            text = (lib / "提示词" / "基础" / "人物提示词.md").read_text(encoding="utf-8")
            self.assertEqual(rc, 0)
            self.assertIn("身份锁", text, "不能采纳一个把硬要求写坏的重试结果")


class TestStyleLockLayerWiring(unittest.TestCase):
    """画风锁定分层的生成侧自检（工单 F11）。

    实测：`人物提示词.md#卖药少年: 缺画风锁定段-镜头`——锁定段由代码逐字注入，
    模型却漏抄了一层，而生成侧不比对这条判据，只能白跑到 verify 才拦得住。
    """

    CARD = {"id": "A2", "name": "仙侠奇幻风", "pack": "", "layers": ["镜头层：低机位广角，景深压缩"]}
    # 缺「镜头层」那句的产出（其余硬要求全满足）
    PERSON_NO_LOCK = PERSON_PROMPT.replace("国漫三维角色三格图板设定图。", "国漫三维角色三格图板设定图。")

    def _rows(self):
        return [("人物", ["p1", "林姑娘", "林小姐", "第1章", "S", ""])]

    def test_missing_lock_layer_is_retried_with_the_layer_named(self):
        from engine import prompt_rules
        checks = {"人物": prompt_rules.style_lock_layer_checks(self.CARD, "人物")}
        content = PERSON_PROMPT + "\n镜头层：低机位广角，景深压缩\n"
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            return (PERSON_PROMPT if len(calls) == 1 else content, {})

        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                rc = gp.generate_base(self._rows(), "古风", lib, False, style_lock_checks=checks)
            self.assertEqual(len(calls), 2, "缺锁定分层应点名重试一次")
            self.assertIn("锁定段纠正", calls[1])
            self.assertIn("镜头", calls[1])
            text = (lib / "提示词" / "基础" / "人物提示词.md").read_text(encoding="utf-8")
            self.assertIn("镜头层：低机位广角，景深压缩", text)
            self.assertEqual(rc, 0)

    def test_stubborn_missing_layer_keeps_content(self):
        """重试仍缺层时**保留产出**：一个锁定层不该让整条资产消失（verify 会如实记名）。"""
        from engine import prompt_rules
        checks = {"人物": prompt_rules.style_lock_layer_checks(self.CARD, "人物")}
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            return (PERSON_PROMPT, {})

        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                rc = gp.generate_base(self._rows(), "古风", lib, False, style_lock_checks=checks)
            self.assertEqual(len(calls), 2, "只重试一次")
            text = (lib / "提示词" / "基础" / "人物提示词.md").read_text(encoding="utf-8")
            self.assertIn("林姑娘", text, "内容不得因缺一层锁定段而丢失")
            self.assertEqual(rc, 0)

    def test_complete_lock_block_is_not_retried(self):
        """防多余重试：锁定段齐全时只发一次请求。"""
        from engine import prompt_rules
        checks = {"人物": prompt_rules.style_lock_layer_checks(self.CARD, "人物")}
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            return (PERSON_PROMPT + "\n镜头层：低机位广角，景深压缩\n", {})

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                rc = gp.generate_base(self._rows(), "古风", Path(td), False, style_lock_checks=checks)
            self.assertEqual(len(calls), 1)
            self.assertEqual(rc, 0)


class TestRetryAcceptanceKeepsHardCriteria(unittest.TestCase):
    """W3/W4（评审发现）：三个"点名重试"块的接受口径必须一致，且修复成功不得跳过后续检查。

    - W3：锁定分层/结构标记的重试只查软标记就采纳 → 重试补齐软标记却丢了「身份锁」也会被采纳。
    - W4：机械修复成功后只赋 `ok2` 不回写 `ok` → 后面 `if ok and …` 恒假，抽象空洞词检查整段被跳过。
    """

    ROWS = [("人物", ["p1", "林姑娘", "林小姐", "第1章", "S", ""])]
    CARD = {"zh": "仙侠国风电影CG\n建模层：高精度建模\n渲染层：物理渲染材质\n",
            "pack": "", "layers": {"建模层": "高精度建模", "渲染层": "物理渲染材质"}}
    HARD_OK = ("国漫三维角色三格图板设定图。左面人物正面特写，右面正视图与后视图，"
               "素灰背景#B8B8B8，均匀柔光，物理渲染材质，国漫三维。全身立像。图中不要有任何文字。"
               "身份锁：鹅蛋脸清秀，杏眼温和，鼻梁挺直，黑发高束。"
               "负面提示词：后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。")
    # 锁定层齐全但**丢了身份锁与负面段**（硬判据不合规）
    SOFT_ONLY = "建模层：高精度建模\n渲染层：物理渲染材质\n三格图板：左面正面特写、右面正视图与后视图。"
    # 硬判据合规但命中抽象空洞词
    TASTY = HARD_OK.replace("全身立像。", "全身立像，气质极致华丽。")

    def test_retry_that_loses_hard_markers_is_not_accepted(self):
        from engine import prompt_rules
        checks = {"人物": prompt_rules.style_lock_layer_checks(self.CARD, "人物")}
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            return (self.HARD_OK if len(calls) == 1 else self.SOFT_ONLY, {})

        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                gp.generate_base(self.ROWS, "古风", lib, False, style_lock_checks=checks)
            text = (lib / "提示词" / "基础" / "人物提示词.md").read_text(encoding="utf-8")
        self.assertIn("身份锁", text, "重试丢了硬标记时，必须保留上一版而不是采纳它")

    # 缺负面锁（硬判据不合规，但 autofix 能机械补上）+ 命中抽象空洞词
    REPAIRABLE_TASTY = ("国漫三维角色三格图板设定图。左面人物正面特写，右面正视图与后视图，"
                        "素灰背景#B8B8B8，均匀柔光，物理渲染材质，国漫三维。全身立像，气质极致华丽。"
                        "图中不要有任何文字。身份锁：鹅蛋脸清秀，杏眼温和，鼻梁挺直，黑发高束。")

    def test_taste_check_runs_after_mechanical_repair(self):
        """机械修复修好硬判据后，抽象空洞词仍必须被查到并重试。

        修法前 `ok2` 只写进局部变量、没回写 `ok`，于是后面 `if ok and …` 恒假，
        这条路径上抽象空洞词检查整段被跳过（坏词直接进产物）。
        """
        from engine import prompt_rules
        checks = {"人物": prompt_rules.style_lock_layer_checks(self.CARD, "人物")}
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            # 前两次都缺负面锁（首版 + 硬判据重试），逼出「机械修复」这条路径；
            # 修复后的内容仍带「极致」，只有把 `ok` 回写才会继续走用词检查
            return (self.REPAIRABLE_TASTY, {}) if len(calls) <= 2 else (self.HARD_OK, {})

        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.object(gp, "call_llm", side_effect=stub):
                gp.generate_base(self.ROWS, "古风", lib, False, style_lock_checks=checks)
            text = (lib / "提示词" / "基础" / "人物提示词.md").read_text(encoding="utf-8")
        self.assertNotIn("极致", text, "机械修复后必须继续查抽象空洞词")
        self.assertTrue(any("用词纠正" in c for c in calls), f"抽象空洞词检查被跳过：{calls}")


class TestGenerationAuditTrail(unittest.TestCase):
    """F23：生成过程留痕不得只认环境变量。

    实测：`_event` 只从 `ASSET_LIB`/`ASSET_LIB_DIR` 取库根，而官方步骤表（SKILL.md）用的是
    `--lib <库>`、并不导出这两个变量 → 重试与「保留产出」事件一条都不落，留痕形同不存在。
    """

    PERSON_CLEAN = ("国漫三维角色三格图板设定图。左面人物正面特写，右面正视图与后视图，"
                    "素灰背景#B8B8B8，均匀柔光，物理渲染材质，国漫三维。全身立像。图中不要有任何文字。"
                    "身份锁：鹅蛋脸清秀，杏眼温和，鼻梁挺直，黑发高束。"
                    "负面提示词：后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。")

    def test_events_land_when_lib_comes_from_argument(self):
        import json
        import os
        calls: list[str] = []

        def stub(system, user, *a, **k):
            calls.append(user)
            return (PERSON_PROMPT_TASTY if len(calls) == 1 else self.PERSON_CLEAN, {})

        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ASSET_LIB", None)
                os.environ.pop("ASSET_LIB_DIR", None)
                with mock.patch.object(gp, "call_llm", side_effect=stub):
                    rc = gp.generate_base([("人物", ["p1", "林姑娘", "林小姐", "第1章", "S", ""])],
                                          "玄幻", lib, False)
            self.assertEqual(rc, 0)
            events = lib / ".process" / "调用事件.jsonl"
            self.assertTrue(events.is_file(), "库根由参数给出时也必须留痕")
            rows = [json.loads(x) for x in events.read_text(encoding="utf-8").splitlines()]
            self.assertIn("generation_taste", [r["event"] for r in rows])


if __name__ == "__main__":
    unittest.main()
