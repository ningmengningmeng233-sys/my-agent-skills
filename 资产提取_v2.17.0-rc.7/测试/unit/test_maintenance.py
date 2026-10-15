# -*- coding: utf-8 -*-
"""maintenance.py 单元测试：status 统计 / backup-restore 往返 / 越界与非空保护（ADR-0079）/ 状态机与迁移（ADR-0084）/ 签发门（E1）/ 手动初始化（E2）。"""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from maintenance import (SKILL_DIR, _has_llm_config, cmd_status, collect_status, do_backup, do_init, do_migrate,
                         do_restore, do_verify)
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口


def make_lib(root: Path) -> Path:
    lib = root / "资产库"
    (lib / "原文段" / "测试小说-作者").mkdir(parents=True)
    (lib / "原文段" / "测试小说-作者" / "seg-001.md").write_text("段内容", encoding="utf-8")
    desc = lib / "描述"
    desc.mkdir()
    (desc / "人物描述.md").write_text(
        "# 人物描述\n\n## 001. 角落老者\n\n描述正文\n\n---\n\n## 002. 林姑娘\n\n描述正文\n", encoding="utf-8"
    )
    return lib


class TestStatus(unittest.TestCase):
    def test_status_counts_and_breakpoint(self):
        with tempfile.TemporaryDirectory() as td:
            lib = make_lib(Path(td))
            s = collect_status(lib)
        self.assertEqual(s["segments"], 1)
        self.assertEqual(s["desc"]["人物"], 2)
        self.assertIn("1 拆段", s["done"])
        self.assertIn("4 描述补全", s["done"])
        self.assertEqual(s["next_step"], "步骤 5 基础提示词")

    def test_empty_lib(self):
        with tempfile.TemporaryDirectory() as td:
            s = collect_status(Path(td) / "资产库")
        self.assertEqual(s["segments"], 0)
        self.assertEqual(s["done"], [])
        self.assertEqual(s["next_step"], "空库，将从步骤 1 开始")


class TestLlmConfigSource(unittest.TestCase):
    """F15 连带：has_config 也漏了 ASSET_ENV_PATH——同一种病在本单里已是第三处。"""

    def test_has_llm_config_honors_asset_env_path(self):
        """三件套只写在 ASSET_ENV_PATH 指的文件里时，has_config 必须为真。"""
        from engine import common
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / "relay.env"
            env_file.write_text("OPENAI_API_KEY=sk-file\n", encoding="utf-8")
            with mock.patch.object(common, "env_candidates", lambda *a, **k: [env_file]):
                with mock.patch.dict(os.environ, {"ASSET_ENV_PATH": str(env_file)}):
                    os.environ.pop("OPENAI_API_KEY", None)
                    self.assertTrue(_has_llm_config())

    def test_has_llm_config_false_when_unconfigured(self):
        """反例（判据不削弱）：两边都没有时必须为假，且不受本机 ~/.asset-extract/.env 影响。"""
        from engine import common
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(common, "env_candidates", lambda *a, **k: [Path(td) / "缺.env"]):
                with mock.patch.dict(os.environ, {}):
                    os.environ.pop("OPENAI_API_KEY", None)
                    self.assertFalse(_has_llm_config())


class TestCompilerStageDisplay(unittest.TestCase):
    """F16：同屏「资产资料交付: DELIVERED」与「签发[PENDING]」并存会读成自相矛盾。

    签发这一格属**视觉线**（pipeline_state.compiler_stages 的既定语义），把状态机改掉才是
    真错；这里只把本来就有的 detail 落屏，让两行各自带限定语。
    """

    def test_pending_and_blocked_stages_carry_their_detail(self):
        from maintenance import _stage_notes
        stages = {
            "事实库": {"state": "DONE", "detail": "9 条源事实"},
            "签发": {"state": "PENDING", "detail": "视觉线未签发（资料线签发见 data_delivery）"},
            "返工": {"state": "BLOCKED", "detail": "2 项待返工"},
        }
        notes = _stage_notes(stages)
        self.assertEqual(notes, ["签发: 视觉线未签发（资料线签发见 data_delivery）", "返工: 2 项待返工"])

    def test_done_and_na_stages_stay_silent(self):
        """反例（不许刷屏）：DONE / NOT_APPLICABLE 是合法状态，不逐条展开。"""
        from maintenance import _stage_notes
        self.assertEqual(_stage_notes({"事实库": {"state": "DONE", "detail": "x"},
                                       "生成": {"state": "NOT_APPLICABLE", "detail": "y"}}), [])
        self.assertEqual(_stage_notes({}), [])

    def test_status_screen_explains_pending_signing(self):
        """真库式样：资料线已交付时，签发格的 detail 必须落屏，不能只留一个 PENDING。"""
        with tempfile.TemporaryDirectory() as td:
            lib = make_lib(Path(td))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cmd_status(lib)
            out = buf.getvalue()
        self.assertIn("资产资料交付:", out)
        self.assertIn("编译链阶段: ", out)
        self.assertIn("↳ 签发: 视觉线未签发", out)

    def test_signing_stage_still_means_visual_line(self):
        """反例（判据不削弱）：签发格仍是视觉线的判据，不得为了让屏幕好看改成 DONE。"""
        from engine.pipeline_state import compiler_stages
        with tempfile.TemporaryDirectory() as td:
            stages = compiler_stages(Path(td), visual={"state": "NOT_ENABLED", "counts": {}, "total": 0})
        self.assertEqual(stages["签发"]["state"], "PENDING")
        self.assertIn("视觉线未签发", stages["签发"]["detail"])


class TestBackupRestore(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = make_lib(root)
            zip_path = do_backup(lib, root / "backups")
            self.assertTrue(zip_path.is_file())
            target = root / "restore目标" / "资产库"
            rc = do_restore(zip_path, target)
            self.assertEqual(rc, 0)
            self.assertEqual(
                (target / "描述" / "人物描述.md").read_text(encoding="utf-8"),
                (lib / "描述" / "人物描述.md").read_text(encoding="utf-8"),
            )

    def test_restore_refuses_nonempty_without_force(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = make_lib(root)
            zip_path = do_backup(lib, root / "backups")
            rc = do_restore(zip_path, lib, force=False)
            self.assertEqual(rc, 1)
            rc = do_restore(zip_path, lib, force=True)
            self.assertEqual(rc, 0)

    def test_restore_rejects_zip_slip(self):
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = root / "资产库"
            lib.mkdir()
            evil = root / "evil.zip"
            with zipfile.ZipFile(evil, "w") as zf:
                zf.writestr("../evil.txt", "boom")
            rc = do_restore(evil, lib, force=True)
            self.assertEqual(rc, 1)
            self.assertFalse((root / "evil.txt").exists())


class TestMigrate(unittest.TestCase):
    """ADR-0084 老库迁移：先备份、旧表不动、索引与母版生成、缺证据标制作提案。"""

    def test_migrate_old_lib(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = make_lib(root)
            (lib / "反推_最终.md").write_text(
                "# 反推\n\n## 人物资产提取表\n| 资产ID | 人物姓名 | 别名/称呼 | 出现位置 | 重要等级 | 备注 |\n"
                "|---|---|---|---|---|---|\n| CH_张三 | 张三 | | 第1章 | B | 备注 |\n\n"
                "## 场景资产提取表\n| 资产ID | 场景名称 | 出现位置 | 重要等级 | 备注 |\n"
                "|---|---|---|---|---|\n| SC_茶楼 | 茶楼 | 第1章 | B | 备注 |\n\n"
                "## 道具资产提取表\n| 资产ID | 道具名称 | 出现位置 | 重要等级 | 备注 |\n"
                "|---|---|---|---|---|\n| PR_琴 | 琴 | 第1章 | B | 备注 |\n\n"
                "## 服装妆造资产提取表\n| 资产ID | 造型名称 | 绑定人物 | 出现位置 | 重要等级 | 备注 |\n"
                "|---|---|---|---|---|---|\n| CO_长裙 | 长裙 | 张三 | 第1章 | B | 备注 |\n\n"
                "## 六、分集/章节资产使用表\n| 章节 | 剧情概要 | 出场人物 | 主要场景 | 关键道具 | 服装造型 |\n"
                "|---|---|---|---|---|---|\n| 第1章 | 概要 | 张三 | 茶楼 | 琴 | 长裙 |\n",
                encoding="utf-8",
            )
            rc = do_migrate(lib)
            self.assertEqual(rc, 0)
            # 旧表未被覆盖
            self.assertIn("CH_张三", (lib / "反推_最终.md").read_text(encoding="utf-8"))
            # 索引与母版已生成
            index = json.loads((lib / ".process" / "资产索引.json").read_text(encoding="utf-8"))
            self.assertEqual(len(index["assets"]), 4)
            for a in index["assets"]:
                # 缺证据旧字段不得假定为剧本事实
                self.assertNotEqual(a["fact_level"], "剧本事实")
            self.assertTrue((lib / "连续性" / "场景母版.md").is_file())


class TestVerifyGate(unittest.TestCase):
    """E1 签发门：启动证据不可核验时 verify 必须非零退出且绝不产出交付清单。"""

    def _lib_with_final(self, root: Path) -> Path:
        lib = make_lib(root)
        (lib / "反推_最终.md").write_text(
            "# 反推\n\n## 人物资产提取表\n| 资产ID | 人物姓名 | 别名/称呼 | 出现位置 | 重要等级 | 备注 |\n"
            "|---|---|---|---|---|---|\n| CH_张三 | 张三 | | 第1章 | B | 备注 |\n",
            encoding="utf-8",
        )
        return lib

    def _state(self, lib: Path, style: str, input_source: str) -> None:
        (lib / ".start-state.json").write_text(
            json.dumps({"style": style, "input_source": input_source,
                        "genre": "古风", "emphasis_hash": "h", "output_root": str(lib),
                        "last_confirmed_at": "t"}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_verify_refuses_without_start_state(self):
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib_with_final(Path(td))
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = do_verify(lib, "H12")
            out = buf.getvalue()
            self.assertNotEqual(rc, 0)
            self.assertFalse((lib / "交付清单.json").exists())
            self.assertIn("SOURCE_UNVERIFIABLE", out)
            # 签发门在跑校验前短路：不靠"检查失败"凑非零
            self.assertNotIn("[check_", out)

    def test_verify_refuses_when_input_source_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib_with_final(Path(td))
            gone = lib / "原文段" / "测试小说-作者" / "已删除.txt"
            self._state(lib, "H12", str(gone))
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = do_verify(lib, "H12")
            self.assertNotEqual(rc, 0)
            self.assertFalse((lib / "交付清单.json").exists())
            self.assertIn("SOURCE_UNVERIFIABLE", buf.getvalue())

    def test_verify_refuses_style_conflict_with_start_state(self):
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib_with_final(Path(td))
            seg = lib / "原文段" / "测试小说-作者" / "seg-001.md"
            self._state(lib, "H12", str(seg))
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = do_verify(lib, "A2")
            out = buf.getvalue()
            self.assertNotEqual(rc, 0)
            self.assertFalse((lib / "交付清单.json").exists())
            self.assertIn("STYLE_MISMATCH", out)
            self.assertTrue((lib / ".process" / "验证报告.md").is_file())

    def test_verify_does_not_refuse_card_alias(self):
        # A2 与「仙侠奇幻风」同一张卡：不得按画风冲突拒签（其余检查失败属正常）
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib_with_final(Path(td))
            seg = lib / "原文段" / "测试小说-作者" / "seg-001.md"
            self._state(lib, "仙侠奇幻风", str(seg))
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                do_verify(lib, "A2")
            self.assertNotIn("STYLE_MISMATCH", buf.getvalue())
            self.assertNotIn("SOURCE_UNVERIFIABLE", buf.getvalue())


class TestVerifyReportFreshness(unittest.TestCase):
    """F17：验证报告不带时间戳，上一次失败留下的「状态判定: BLOCKED」会被后来成功的签发反衬成矛盾。

    2026-09-21 实测（两座已签发库都中招）：报告 02:22:53 写「BLOCKED」，交付清单 03:00:57 签「DELIVERED」——
    只看那份报告会把已签发库判成阻断。文档口径是「`.process/验证报告.md` 存在 ⇒ 最近一次 verify 失败」，
    所以修法是让成功路径**清掉**它，而不是改成绿色报告（那会推翻 README/FAQ/错误处理矩阵的读法）。
    """

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def _lib_with_final(self, root: Path) -> Path:
        lib = make_lib(root)
        (lib / "反推_最终.md").write_text("# 反推\n", encoding="utf-8")
        return lib

    def _drive_success(self, lib: Path) -> int:
        """外部依赖换成全绿桩，只走「全绿 → 签发 → 报告收尾」这条主路径。"""
        from engine import delivery_checks, pipeline_state
        with mock.patch.object(pipeline_state, "validate_start_evidence",
                               lambda _lib, style: (True, "EVIDENCE_OK", str(_lib), style)), \
             mock.patch.object(pipeline_state, "build_delivery_manifest",
                               lambda *_a: {"source_fingerprint": "sha256:stub", "status": "DELIVERED"}), \
             mock.patch.object(pipeline_state, "archive_delivery_manifest", lambda _lib: None), \
             mock.patch.object(delivery_checks, "run_all", lambda _lib: [("check_asset_index", 0, "ok")]), \
             mock.patch.object(subprocess, "run", lambda *_a, **_k: self._Ok()), \
             contextlib.redirect_stdout(io.StringIO()):
            return do_verify(lib, "H12")

    def test_success_clears_stale_failure_report(self):
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib_with_final(Path(td))
            (lib / ".process").mkdir(parents=True, exist_ok=True)
            report = lib / ".process" / "验证报告.md"
            report.write_text("# 验证报告\n\n- 状态判定: BLOCKED\n", encoding="utf-8")
            self.assertEqual(self._drive_success(lib), 0)
            self.assertTrue((lib / "交付清单.json").is_file())
            self.assertFalse(report.is_file(), "成功签发后仍留着上一次的 BLOCKED 报告")

    def test_failure_still_writes_blocked_and_keeps_manifest(self):
        """反例（ADR-0084 不削弱）：失败路径仍写 BLOCKED，且绝不覆盖最后有效的交付清单。"""
        with tempfile.TemporaryDirectory() as td:
            lib = self._lib_with_final(Path(td))          # 故意不写 .start-state.json
            (lib / "交付清单.json").write_text('{"status": "DELIVERED"}', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = do_verify(lib, "H12")
            self.assertNotEqual(rc, 0)
            self.assertIn("BLOCKED", (lib / ".process" / "验证报告.md").read_text(encoding="utf-8"))
            self.assertEqual((lib / "交付清单.json").read_text(encoding="utf-8"), '{"status": "DELIVERED"}')


class TestInit(unittest.TestCase):
    """E2 可移植手动初始化：原子写 .start-state.json，且写出的证据能过签发门。"""

    def _src(self, root: Path) -> Path:
        src = root / "原始输入.txt"
        src.write_text("第1章 测试\n张三进茶楼\n", encoding="utf-8")
        return src

    def _init(self, lib: Path, src: Path, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = do_init(lib, src, kw.pop("genre", "古风"), kw.pop("style", "仙侠奇幻风"), **kw)
        return rc, buf.getvalue()

    def test_init_writes_atomic_start_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib, src = root / "资产库", self._src(root)
            rc, _out = self._init(lib, src)
            self.assertEqual(rc, 0)
            state = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
            for key in ("input_source", "genre", "style", "emphasis_hash", "output_root", "last_confirmed_at"):
                self.assertIn(key, state)
            self.assertTrue(Path(state["input_source"]).is_absolute())
            self.assertTrue(Path(state["input_source"]).is_file())
            self.assertEqual(state["output_root"], str(lib))
            # 无临时残留（原子替换而非半截写入）
            self.assertEqual([p.name for p in lib.glob(".start-state.json*")], [".start-state.json"])

    def test_init_style_evidence_passes_signing_gate(self):
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
        from scripts.engine.pipeline_state import validate_start_evidence
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib, src = root / "资产库", self._src(root)
            self._init(lib, src, style="A2")
            ok, reason, _p, _s = validate_start_evidence(lib, "仙侠奇幻风")
            self.assertTrue(ok, reason)

    def test_init_refuses_missing_input_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = root / "资产库"
            rc, out = self._init(lib, root / "不存在.txt")
            self.assertNotEqual(rc, 0)
            self.assertFalse((lib / ".start-state.json").exists())
            self.assertIn("不存在", out)

    def test_init_refuses_empty_style(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib, src = root / "资产库", self._src(root)
            rc, _out = self._init(lib, src, style="   ")
            self.assertNotEqual(rc, 0)
            self.assertFalse((lib / ".start-state.json").exists())

    def test_init_requires_force_to_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib, src = root / "资产库", self._src(root)
            self.assertEqual(self._init(lib, src, style="H12")[0], 0)
            before = (lib / ".start-state.json").read_bytes()
            manifest = lib / "交付清单.json"
            manifest.write_text('{"status": "DELIVERED"}', encoding="utf-8")
            rc, out = self._init(lib, src, style="A2")
            self.assertNotEqual(rc, 0)
            self.assertIn("--force", out)
            self.assertEqual((lib / ".start-state.json").read_bytes(), before)
            # 拒签不改配置时，已有有效清单必须原样不动
            self.assertEqual(manifest.read_text(encoding="utf-8"), '{"status": "DELIVERED"}')
            self.assertEqual(self._init(lib, src, style="A2", force=True)[0], 0)
            state = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["style"], "A2")

    def test_init_same_inputs_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib, src = root / "资产库", self._src(root)
            self.assertEqual(self._init(lib, src)[0], 0)
            rc, out = self._init(lib, src)
            self.assertEqual(rc, 0)
            self.assertIn("一致", out)

    def test_init_records_resolved_card_and_warns_on_free_style(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib, src = root / "资产库", self._src(root)
            self._init(lib, src, style="仙侠奇幻风")
            state = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["style_card"], "A2")
            self.assertEqual(state["style_mode"], "card")
            rc, out = self._init(lib, src, style="我自创的画风", force=True)
            self.assertEqual(rc, 0)
            self.assertIn("自由描述", out)
            state = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["style_mode"], "free")

    def test_init_cli_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib, src = root / "资产库", self._src(root)
            r = run_py(
                    [str(SKILL_DIR / "scripts" / "maintenance.py"), "init", "--lib", str(lib), "--input-source", str(src), "--genre", "古风", "--style", "A2"],
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((lib / ".start-state.json").is_file())


class TestInitMultiSource(unittest.TestCase):
    """工单01-Task4：init 支持可重复 --source 与 --source-order-file，兼容单文件 --input-source。"""

    def _sources(self, root: Path) -> tuple[Path, Path, Path]:
        a = root / "卷1.txt"
        a.write_text("第一章 甲\n甲出现。\n", encoding="utf-8")
        b = root / "卷2.txt"
        b.write_text("第二章 乙\n乙出现。\n", encoding="utf-8")
        order = root / "顺序清单.txt"
        order.write_text("卷2.txt\n卷1.txt\n", encoding="utf-8")
        return a, b, order

    def test_multi_source_init_writes_normalized_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = root / "资产库"
            a, b, order = self._sources(root)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = do_init(lib, None, "古风", "仙侠奇幻风",
                             sources=[a, b], source_order_file=order)
            self.assertEqual(rc, 0, buf.getvalue())
            state = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
            for key in ("input_source", "source_manifest_path", "source_manifest_hash",
                        "source_order_file"):
                self.assertIn(key, state)
            self.assertTrue(Path(state["source_manifest_path"]).is_file())
            self.assertEqual(len(state["source_manifest_hash"]), 64)
            self.assertTrue(Path(state["input_source"]).is_file())
            self.assertIn("规范化原文", state["input_source"])
            text = Path(state["input_source"]).read_text(encoding="utf-8")
            self.assertLess(text.index("乙出现。"), text.index("甲出现。"))

    def test_single_docx_input_source_triggers_normalization(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = root / "资产库"
            docx = SKILL_DIR / "测试" / "fixtures" / "sources" / "最小剧本.docx"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = do_init(lib, docx, "古风", "仙侠奇幻风")
            self.assertEqual(rc, 0, buf.getvalue())
            state = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
            self.assertIn("规范化原文", state["input_source"])
            self.assertIn("source_manifest_path", state)

    def test_single_txt_input_source_keeps_legacy_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib, src = root / "资产库", root / "原始输入.txt"
            src.write_text("第1章 测试\n张三进茶楼\n", encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = do_init(lib, src, "古风", "仙侠奇幻风")
            self.assertEqual(rc, 0, buf.getvalue())
            state = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["input_source"], str(src.resolve()))
            self.assertNotIn("source_manifest_path", state)
            self.assertFalse((lib / ".process" / "normalized_sources").exists())

    def test_cli_repeatable_source_flags(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = root / "资产库"
            a, b, order = self._sources(root)
            r = run_py(
                    [str(SKILL_DIR / "scripts" / "maintenance.py"), "init", "--lib", str(lib), "--genre", "古风", "--style", "A2", "--source", str(a), "--source", str(b), "--source-order-file", str(order)],
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            state = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
            self.assertIn("source_manifest_path", state)

    def test_cli_requires_some_source(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "资产库"
            r = run_py(
                    [str(SKILL_DIR / "scripts" / "maintenance.py"), "init", "--lib", str(lib), "--genre", "古风", "--style", "A2"],
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertFalse((lib / ".start-state.json").exists())


class TestNormalizedSourceFingerprint(unittest.TestCase):
    """工单01-Task4 Step4：源指纹含全部原文件、顺序清单与规范化文件哈希。"""

    def _project(self, root: Path) -> Path:
        import sys as _sys
        _sys.path.insert(0, str(SKILL_DIR))
        import conftest
        from scripts.engine.pipeline_state import build_delivery_manifest
        from scripts.engine.source_normalizer import normalize_sources, normalized_dir, write_normalized

        lib = conftest.make_delivered_lib(root)
        a = root / "卷1.txt"
        a.write_text("第一章 甲\n甲出现。\n", encoding="utf-8")
        b = root / "卷2.txt"
        b.write_text("第二章 乙\n乙出现。\n", encoding="utf-8")
        order = root / "顺序清单.txt"
        order.write_text("卷1.txt\n卷2.txt\n", encoding="utf-8")
        out = write_normalized(normalize_sources([a, b], order), normalized_dir(lib))
        state_path = lib / ".start-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["input_source"] = out["markdown"]
        state["source_manifest_path"] = out["manifest"]
        state["source_manifest_hash"] = hashlib.sha256(
            Path(out["manifest"]).read_bytes()).hexdigest()
        state["source_order_file"] = str(order.resolve())
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        from scripts.engine.pipeline_state import REQUIRED_DELIVERY_CHECKS
        checks = [(n, 0) for n in REQUIRED_DELIVERY_CHECKS]
        (lib / "交付清单.json").write_text(
            json.dumps(build_delivery_manifest(lib, "H7", checks), ensure_ascii=False),
            encoding="utf-8")
        return lib

    def test_changing_one_original_source_leaves_delivered(self):
        import sys as _sys
        _sys.path.insert(0, str(SKILL_DIR))
        from scripts.engine.pipeline_state import derive_pipeline_state

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = self._project(root)
            self.assertEqual(derive_pipeline_state(lib, style="H7")["state"], "DELIVERED")
            (root / "卷1.txt").write_text("第一章 甲\n甲出现（改了）。\n", encoding="utf-8")
            state = derive_pipeline_state(lib, style="H7")
            self.assertNotEqual(state["state"], "DELIVERED")
            self.assertEqual(state["reason_code"], "SOURCE_CHANGED")

    def test_missing_original_source_blocks_signing_evidence(self):
        import sys as _sys
        _sys.path.insert(0, str(SKILL_DIR))
        from scripts.engine.pipeline_state import derive_pipeline_state, validate_start_evidence

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = self._project(root)
            (root / "卷2.txt").unlink()
            ok, reason, _src, _style = validate_start_evidence(lib, "H7")
            self.assertFalse(ok)
            self.assertEqual(reason, "SOURCE_UNVERIFIABLE")
            self.assertNotEqual(derive_pipeline_state(lib, style="H7")["state"], "DELIVERED")

    def test_order_file_change_leaves_delivered(self):
        import sys as _sys
        _sys.path.insert(0, str(SKILL_DIR))
        from scripts.engine.pipeline_state import derive_pipeline_state

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = self._project(root)
            (root / "顺序清单.txt").write_text("卷2.txt\n卷1.txt\n", encoding="utf-8")
            self.assertNotEqual(derive_pipeline_state(lib, style="H7")["state"], "DELIVERED")


if __name__ == "__main__":
    unittest.main()
