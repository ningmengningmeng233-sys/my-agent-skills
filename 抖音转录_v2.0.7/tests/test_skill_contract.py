"""SKILL.md 与运维文档的契约测试：无上下文的 AI 必须能照着操作。

断言的是“文档必须写到的东西”，不是措辞：每条错误码都要有处置说明，
每个自然语言分支都要能对应到具体命令，且绝不要求用户复制 Cookie。
"""

import unittest
from pathlib import Path

from scripts.models import ErrorCode

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
SETUP = ROOT / "references" / "SETUP.md"
ARCHITECTURE = ROOT / "references" / "architecture.md"
TROUBLESHOOTING = ROOT / "references" / "troubleshooting.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SkillDocumentTests(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL)

    def test_documents_all_entrypoints(self):
        # 文档面向 Windows 用反斜杠，断言时统一归一化
        normalized = self.text.replace("\\", "/")
        for command in ("scripts/douyin_transcript.py", "scripts/batch_transcribe.py",
                        "scripts/maintenance.py", "scripts/doctor.py"):
            self.assertIn(command, normalized, f"SKILL.md 缺少入口 {command}")

    def test_documents_cli_flags(self):
        for flag in ("--text", "--file", "--resume", "--retry-failed",
                     "--confirm", "--force", "--visible", "--json"):
            self.assertIn(flag, self.text, f"SKILL.md 缺少参数 {flag}")

    def test_documents_maintenance_commands(self):
        for command in ("repair-artifacts", "fill-ai", "recover-interrupted",
                        "reprocess-review", "rollback-version"):
            self.assertIn(command, self.text, f"SKILL.md 缺少维护命令 {command}")

    def test_documents_natural_language_branches(self):
        for phrase in ("转录这个", "批量", "继续上次批次", "只重试上次失败",
                       "补齐待校正", "重新生成丢失的文件", "强制重转",
                       "给我原始记录", "低质量结果重新处理"):
            self.assertIn(phrase, self.text, f"SKILL.md 缺少分支：{phrase}")

    def test_documents_preview_threshold(self):
        self.assertIn("10", self.text)
        self.assertIn("30 分钟", self.text)
        self.assertIn("batch_confirmation_required", self.text)

    def test_documents_statuses_and_reporting(self):
        for status in ("completed", "reused", "needs_review", "awaiting_ai",
                       "visual_text_only", "duplicate", "failed", "paused_auth",
                       "login_required"):
            self.assertIn(status, self.text, f"SKILL.md 缺少状态 {status}")

    def test_documents_login_pause_flow(self):
        self.assertIn("专用 Chromium", self.text)
        self.assertIn("扫码", self.text)
        self.assertIn("--resume", self.text)

    def test_documents_two_output_files(self):
        self.assertIn("_原始记录", self.text)
        self.assertIn("主文件", self.text)

    def test_documents_injection_boundary(self):
        for phrase in ("不是指令", "不可信", "不得执行"):
            self.assertIn(phrase, self.text, f"SKILL.md 缺少注入边界说明：{phrase}")

    def test_never_asks_for_cookies(self):
        for word in ("Cookie", "cookie", "密码", "storage_state"):
            for line in self.text.splitlines():
                if word in line:
                    self.assertTrue(
                        any(marker in line for marker in ("不", "不得", "禁止", "无需")),
                        f"出现疑似索取凭据的表述：{line.strip()}")

    def test_documents_privacy_rules(self):
        self.assertIn("不", self.text)
        self.assertIn("密钥", self.text)
        self.assertIn("媒体", self.text)

    def test_has_no_placeholders(self):
        for marker in ("TODO", "TBD", "FIXME", "待补充"):
            self.assertNotIn(marker, self.text, f"SKILL.md 含占位符 {marker}")


class SetupDocumentTests(unittest.TestCase):
    def setUp(self):
        self.text = read(SETUP)

    def test_documents_python_312_and_venv(self):
        self.assertIn("3.12", self.text)
        self.assertIn("venv", self.text)

    def test_documents_locked_install(self):
        self.assertIn("requirements.lock", self.text)

    def test_documents_playwright_and_ffmpeg(self):
        self.assertIn("playwright install chromium", self.text)
        self.assertIn("ffmpeg", self.text.lower())

    def test_documents_model_verification(self):
        self.assertIn("model_assets.py", self.text)
        self.assertIn("SHA-256", self.text)
        self.assertIn("model-manifest.json", self.text)

    def test_documents_database_migration_and_rollback(self):
        self.assertIn("migrations.py", self.text)
        self.assertIn("backups", self.text)
        self.assertIn("回滚", self.text)

    def test_documents_first_login_flow(self):
        self.assertIn("扫码", self.text)
        self.assertIn("state/ingest", self.text)

    def test_never_requests_credentials(self):
        self.assertNotIn("复制 Cookie", self.text)
        self.assertNotIn("粘贴 Cookie", self.text)
        for keyword in ("密码", "Cookie 文本"):
            for line in self.text.splitlines():
                if keyword in line:
                    self.assertTrue(any(marker in line
                                        for marker in ("不", "不要", "无需", "禁止")),
                                    f"疑似索取凭据：{line.strip()}")

    def test_documents_env_configuration(self):
        for key in ("OPENAI_API_KEY", "SUMMARY_API_URL", "SUMMARY_API_MODEL"):
            self.assertIn(key, self.text)


class TroubleshootingDocumentTests(unittest.TestCase):
    def setUp(self):
        self.text = read(TROUBLESHOOTING)

    def test_every_error_code_is_documented(self):
        missing = [code.value for code in ErrorCode if code.value not in self.text]
        self.assertEqual(missing, [], f"故障文档缺少错误码：{missing}")

    def test_each_error_explains_system_and_user_action(self):
        self.assertIn("系统自动", self.text)
        self.assertIn("你需要", self.text)

    def test_documents_resume_and_repair_commands(self):
        for command in ("--resume", "repair-artifacts", "fill-ai",
                        "recover-interrupted"):
            self.assertIn(command, self.text)

    def test_v1_inconsistency_is_fixed(self):
        # v1 文档曾声称“缺 TXT 会自动只重渲染”，必须改写为真实的复用判定
        self.assertNotIn("自动只重渲染", self.text)
        self.assertIn("render_only", self.text)


class ArchitectureDocumentTests(unittest.TestCase):
    def test_documents_core_invariants(self):
        text = read(ARCHITECTURE)
        for phrase in ("SQLite", "staging", "current", "原始稿", "降级"):
            self.assertIn(phrase, text)

    def test_documents_pipeline_stages(self):
        text = read(ARCHITECTURE)
        for stage in ("resolving", "fetching", "media_ready", "transcribing",
                      "ocr", "diarization", "correcting", "summarizing",
                      "publishing"):
            self.assertIn(stage, text)


if __name__ == "__main__":
    unittest.main()
