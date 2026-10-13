"""输入规范化与文件导入测试：四种链接统一、相似域拒绝、文件上限与编码错误。"""

import tempfile
import unittest
from pathlib import Path

from scripts.input_normalizer import (MAX_FILE_BYTES, MAX_URL_CANDIDATES,
                                      InputError, load_input_file,
                                      normalize_inputs)

AWEME = "7410000000000001234"
SHARE_TEXT = (
    "8.88 复制打开抖音，看看【张三的作品】端午就看我奶龙 "
    "https://v.douyin.com/aaa/ 复制此链接，打开Dou音搜索"
)


class ChatInputTests(unittest.TestCase):
    def test_standard_video_link(self):
        items = normalize_inputs(f"https://www.douyin.com/video/{AWEME}")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, f"https://www.douyin.com/video/{AWEME}")
        self.assertEqual(items[0].candidate_aweme_id, AWEME)
        self.assertEqual(items[0].kind, "video")

    def test_short_link_is_kept_for_upstream_expansion(self):
        items = normalize_inputs("https://v.douyin.com/iRabcdef/")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://v.douyin.com/iRabcdef/")
        self.assertEqual(items[0].candidate_aweme_id, "")
        self.assertEqual(items[0].kind, "short_link")

    def test_full_share_text_with_chinese_punctuation(self):
        items = normalize_inputs(SHARE_TEXT)
        self.assertEqual([item.url for item in items],
                         ["https://v.douyin.com/aaa/"])
        self.assertEqual(items[0].raw_input, SHARE_TEXT)

    def test_modal_id_on_profile_url_becomes_video_url(self):
        raw = ("https://www.douyin.com/user/self?from_tab_name=main"
               f"&modal_id={AWEME}&showSubTab=favorite_folder"
               "&showTab=favorite_collection")
        items = normalize_inputs(raw)
        self.assertEqual([item.url for item in items],
                         [f"https://www.douyin.com/video/{AWEME}"])
        self.assertNotIn("user/self", items[0].url)
        self.assertNotIn("modal_id", items[0].url)
        self.assertEqual(items[0].candidate_aweme_id, AWEME)

    def test_modal_id_wins_over_other_query_parameters(self):
        items = normalize_inputs(
            f"https://www.douyin.com/?modal_id={AWEME}&previous_page=home")
        self.assertEqual(items[0].url, f"https://www.douyin.com/video/{AWEME}")

    def test_multiple_links_preserve_order_without_dedup(self):
        text = (f"https://www.douyin.com/video/7410000000000000001 "
                f"https://v.douyin.com/bbb/ "
                f"https://www.douyin.com/video/7410000000000000002 "
                f"https://v.douyin.com/bbb/")
        items = normalize_inputs(text)
        self.assertEqual([item.url for item in items], [
            "https://www.douyin.com/video/7410000000000000001",
            "https://v.douyin.com/bbb/",
            "https://www.douyin.com/video/7410000000000000002",
            "https://v.douyin.com/bbb/",
        ])

    def test_chinese_trailing_punctuation_is_stripped(self):
        text = (f"看这个 https://www.douyin.com/video/{AWEME}。"
                f"还有（https://www.douyin.com/video/7410000000000000002）")
        items = normalize_inputs(text)
        self.assertEqual([item.url for item in items], [
            f"https://www.douyin.com/video/{AWEME}",
            "https://www.douyin.com/video/7410000000000000002",
        ])

    def test_tracking_parameters_are_removed_without_touching_id(self):
        items = normalize_inputs(
            f"https://www.douyin.com/video/{AWEME}?previous_page=web_code_link"
            "&enter_from=search_result")
        self.assertEqual(items[0].url, f"https://www.douyin.com/video/{AWEME}")
        self.assertEqual(items[0].candidate_aweme_id, AWEME)

    def test_lookalike_domains_are_rejected(self):
        for text in (
            "https://douyin.com.evil.test/video/7410000000000001234",
            "https://www.douyin.com.evil.test/video/7410000000000001234",
            "https://notdouyin.com/video/7410000000000001234",
            "https://douyin.com@evil.test/video/7410000000000001234",
            "https://v.douyin.com.evil.test/aaa/",
        ):
            with self.subTest(text=text):
                self.assertEqual(normalize_inputs(text), [])

    def test_non_douyin_hosts_are_ignored(self):
        items = normalize_inputs(
            f"https://example.com/video/123 https://www.douyin.com/video/{AWEME}")
        self.assertEqual([item.url for item in items],
                         [f"https://www.douyin.com/video/{AWEME}"])

    def test_empty_and_linkless_input_yields_nothing(self):
        self.assertEqual(normalize_inputs(""), [])
        self.assertEqual(normalize_inputs("今天天气不错"), [])

    def test_uppercase_host_is_accepted(self):
        items = normalize_inputs(f"https://WWW.DOUYIN.COM/video/{AWEME}")
        self.assertEqual(items[0].candidate_aweme_id, AWEME)


class TextFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_utf8_txt(self):
        path = self.root / "list.txt"
        path.write_text(f"第一条 https://www.douyin.com/video/{AWEME}\n"
                        "不是链接的一行\n", encoding="utf-8")
        items = load_input_file(path)
        self.assertEqual([item.url for item in items],
                         [f"https://www.douyin.com/video/{AWEME}"])
        self.assertIn("第一条", items[0].raw_input)

    def test_utf8_bom_txt(self):
        path = self.root / "bom.txt"
        path.write_text("https://v.douyin.com/ccc/\n", encoding="utf-8-sig")
        items = load_input_file(path)
        self.assertEqual([item.url for item in items], ["https://v.douyin.com/ccc/"])
        self.assertEqual(items[0].kind, "short_link")

    def test_markdown_file(self):
        path = self.root / "list.md"
        path.write_text(f"# 待转录\n\n- [链接](https://www.douyin.com/video/{AWEME})\n",
                        encoding="utf-8")
        items = load_input_file(path)
        self.assertEqual([item.url for item in items],
                         [f"https://www.douyin.com/video/{AWEME}"])

    def test_undecodable_file_reports_encoding_error(self):
        path = self.root / "gbk.txt"
        path.write_bytes("抖音链接 https://v.douyin.com/ddd/".encode("gbk"))
        with self.assertRaises(InputError) as ctx:
            load_input_file(path)
        self.assertEqual(ctx.exception.code, "input_encoding_error")

    def test_oversized_file_is_rejected(self):
        path = self.root / "huge.txt"
        path.write_bytes(b"a" * (MAX_FILE_BYTES + 1))
        with self.assertRaises(InputError) as ctx:
            load_input_file(path)
        self.assertEqual(ctx.exception.code, "invalid_input")
        self.assertIn("10", ctx.exception.message)

    def test_too_many_candidates_is_rejected(self):
        path = self.root / "many.txt"
        path.write_text("https://v.douyin.com/x/\n" * (MAX_URL_CANDIDATES + 1),
                        encoding="utf-8")
        with self.assertRaises(InputError) as ctx:
            load_input_file(path)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_missing_file_raises_invalid_input(self):
        with self.assertRaises(InputError) as ctx:
            load_input_file(self.root / "nope.txt")
        self.assertEqual(ctx.exception.code, "invalid_input")


class CsvFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8-sig")
        return path

    def test_named_url_column(self):
        path = self._write("a.csv", (
            f"标题,url,备注\n作品一,https://www.douyin.com/video/{AWEME},x\n"
            "作品二,https://v.douyin.com/eee/,y\n"))
        items = load_input_file(path)
        self.assertEqual([item.url for item in items], [
            f"https://www.douyin.com/video/{AWEME}", "https://v.douyin.com/eee/"])

    def test_chinese_column_name(self):
        path = self._write("b.csv", (
            f"序号,分享链接\n1,https://www.douyin.com/video/{AWEME}\n"))
        items = load_input_file(path)
        self.assertEqual([item.url for item in items],
                         [f"https://www.douyin.com/video/{AWEME}"])

    def test_cell_scan_when_no_url_column(self):
        path = self._write("c.csv", (
            f"a,b\n文案 https://www.douyin.com/video/{AWEME},无关\n"))
        items = load_input_file(path)
        self.assertEqual([item.url for item in items],
                         [f"https://www.douyin.com/video/{AWEME}"])

    def test_link_column_wins_over_other_columns(self):
        path = self._write("d.csv", (
            f"link,note\nhttps://v.douyin.com/fff/,"
            "https://www.douyin.com/video/7410000000000009999\n"))
        items = load_input_file(path)
        self.assertEqual([item.url for item in items], ["https://v.douyin.com/fff/"])


class ChainedNormalizerTests(unittest.TestCase):
    def test_file_rows_keep_their_own_raw_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.txt"
            path.write_text("第一行 https://v.douyin.com/aaa/\n"
                            "第二行 https://v.douyin.com/bbb/\n", encoding="utf-8")
            items = load_input_file(path)
        self.assertIn("第一行", items[0].raw_input)
        self.assertNotIn("第二行", items[0].raw_input)
        self.assertIn("第二行", items[1].raw_input)


if __name__ == "__main__":
    unittest.main()
