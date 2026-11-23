#!/usr/bin/env python3
"""H7 渲染锁定/内容语言检查的集成回归（真实调用 check_schedule）。"""
import sys
import tempfile
import unittest
from pathlib import Path
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "scripts" / "engine"))

from engine import style as style_lib


def _final_text(genre="玄幻"):
    return f"""# 测试
## 项目基础信息
| 项目名称 | 测试小说 |
| 题材类型 | {genre} |

## 人物资产提取表
| 资产ID | 人物姓名 | 别名/称呼 | 出现位置 | 重要等级 | 备注 |
| p1 | 林姑娘 | | 第1章 | 主角 | |
"""


def _lock_lines(card):
    return style_lib.render_lock_sentences(card)


def _content_line(card):
    return style_lib.content_lock_sentence(card)


class TestH7LockIntegration(unittest.TestCase):
    def setUp(self):
        self.card = style_lib.parse_style_card("H7")
        self.layers = _lock_lines(self.card)
        self.content = _content_line(self.card)
        self.assertGreaterEqual(len(self.layers), 4)
        self.assertTrue(self.content)

    def _run(self, base_content, genre="玄幻"):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / "提示词" / "基础").mkdir(parents=True)
            final_path = lib / "反推_最终.md"
            final_path.write_text(_final_text(genre), encoding="utf-8")
            (lib / "提示词" / "基础" / "人物提示词.md").write_text(
                "# 人物基础提示词\n\n## 001. 林姑娘\n\n" + base_content + "\n\n---\n", encoding="utf-8"
            )
            r = run_py(
                    [str(SKILL / "scripts" / "check_schedule.py"), str(final_path), "--lib", str(lib), "--style", "H7", "--json"],
                    cwd=SKILL,
            )
            return r.returncode, r.stdout + r.stderr

    def test_missing_layers_and_content_reported(self):
        content = "渲染锁定：\n" + "\n".join(self.layers[:2]) + "\n整体三格图板：左面人物正面特写，右面正视图与后视图。素灰背景#B8B8B8。国漫三维，三维渲染，物理渲染材质。图中不要有任何文字。负面提示词：后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。"
        rc, out = self._run(content)
        self.assertNotEqual(rc, 0)
        self.assertIn("画风锁定段-场景/背景", out)
        self.assertIn("画风锁定段-镜头", out)
        self.assertIn("画风锁定段-内容语言", out)

    def test_all_layers_but_missing_content_reported(self):
        content = "渲染锁定：\n" + "\n".join(self.layers) + "\n整体三格图板：左面人物正面特写，右面正视图与后视图。素灰背景#B8B8B8。国漫三维，三维渲染，物理渲染材质。图中不要有任何文字。负面提示词：后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。"
        rc, out = self._run(content)
        self.assertNotEqual(rc, 0)
        self.assertIn("画风锁定段-内容语言", out)

    def test_mixed_genre_does_not_require_content_language(self):
        content = "渲染锁定：\n" + "\n".join(self.layers) + "\n整体三格图板：左面人物正面特写，右面正视图与后视图。素灰背景#B8B8B8。国漫三维，三维渲染，物理渲染材质。图中不要有任何文字。负面提示词：后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。"
        rc, out = self._run(content, genre="都市玄幻")
        self.assertNotIn("画风锁定段-内容语言", out)

    def test_full_compliant_passes(self):
        view_body = style_lib.build_view_block(style_lib.load_stylepack("H7"), "人物").split("\n", 1)[1]
        content = "渲染锁定：\n" + "\n".join(self.layers) + "\n" + self.content + "\n视图基调：" + view_body + "\n身份锁：脸型骨相清秀，肤色白皙，眉形细长，眼型杏眼，瞳孔色黑棕，鼻型挺直，唇形适中，发型长发黑发，无疤痕痣胎记。\n整体三格图板：左面人物正面特写，右面正视图与后视图。素灰背景#B8B8B8。国漫三维，三维渲染，物理渲染材质。图中不要有任何文字。负面提示词：无真人感光元件写实、无蜡质皮肤、后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。"
        rc, out = self._run(content)
        self.assertEqual(rc, 0, out[-500:])


if __name__ == "__main__":
    unittest.main()
