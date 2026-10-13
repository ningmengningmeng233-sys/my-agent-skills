import os
import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
import html
import posixpath
from dataclasses import dataclass
from typing import List, Union, Optional


@dataclass
class Chapter:
    index: int
    title: str
    content: str
    char_count: int


CHAPTER_REGEX = re.compile(
    r'(?m)^[ \t]*(第[0-9一二三四五六七八九十百千万]+[章卷回节]|Chapter\s*\d+)[ \t]*(.*)$'
)


class _HTMLTextExtractor(HTMLParser):
    """用于解析 EPUB/HTML 章节的高性能标签剥离器"""
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.title_parts = []
        self.in_title = False
        self.in_heading = False
        self.heading_parts = []

    def handle_starttag(self, tag, attrs):
        lower_tag = tag.lower()
        if lower_tag == "title":
            self.in_title = True
        elif lower_tag in ("h1", "h2", "h3"):
            self.in_heading = True
        elif lower_tag in ("p", "div", "br", "li", "tr"):
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        lower_tag = tag.lower()
        if lower_tag == "title":
            self.in_title = False
        elif lower_tag in ("h1", "h2", "h3"):
            self.in_heading = False
        elif lower_tag in ("p", "div", "li"):
            self.text_parts.append("\n")

    def handle_data(self, data):
        if not data:
            return
        if self.in_title:
            self.title_parts.append(data)
        if self.in_heading:
            self.heading_parts.append(data)
        self.text_parts.append(data)

    def get_clean_text(self) -> str:
        raw = "".join(self.text_parts)
        decoded = html.unescape(raw)
        # 规整连续空行
        lines = [line.strip() for line in decoded.split("\n")]
        return "\n".join(line for line in lines if line)

    def get_title(self) -> Optional[str]:
        if self.heading_parts:
            t = html.unescape("".join(self.heading_parts)).strip()
            if t:
                return t
        if self.title_parts:
            t = html.unescape("".join(self.title_parts)).strip()
            if t:
                return t
        return None


def _read_file_safe(file_path: str) -> str:
    """自适应多编码安全读取文件文本"""
    encodings = ["utf-8", "utf-8-sig", "gb18030", "gbk"]
    for enc in encodings:
        try:
            with open(file_path, "r", encoding=enc) as fp:
                return fp.read()
        except UnicodeDecodeError:
            continue
    with open(file_path, "r", encoding="utf-8", errors="replace") as fp:
        return fp.read()


def _read_docx(file_path: str) -> str:
    """零依赖原生解析 Word (.docx)，基于 zipfile + xml.etree 提取正文并过滤批注"""
    with zipfile.ZipFile(file_path, "r") as zf:
        if "word/document.xml" not in zf.namelist():
            return ""
        xml_content = zf.read("word/document.xml")

    root = ET.fromstring(xml_content)
    # OpenXML 命名空间
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []

    body = root.find("w:body", ns)
    if body is None:
        body = root

    for elem in body:
        elem_tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if elem_tag in ("comment", "commentRangeStart", "commentRangeEnd", "commentReference", "sectPr"):
            continue

        if elem_tag == "p":
            p_texts = []
            for child in elem:
                tag_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                # 过滤批注、页眉页脚与删除线等非正文节点
                if tag_name in ("comment", "commentReference", "headerReference", "footerReference", "del"):
                    continue
                if tag_name == "r":
                    for t_elem in child.iterfind(".//w:t", ns):
                        if t_elem.text:
                            p_texts.append(t_elem.text)
                elif tag_name in ("hyperlink", "smartTag"):
                    for r_elem in child.iterfind(".//w:r", ns):
                        for t_elem in r_elem.iterfind(".//w:t", ns):
                            if t_elem.text:
                                p_texts.append(t_elem.text)
            para_text = "".join(p_texts).strip()
            if para_text:
                paragraphs.append(para_text)

    return "\n\n".join(paragraphs)


def _read_epub(file_path: str, chunk_size: int = 3000) -> List[Chapter]:
    """零依赖原生解析电子书 (.epub)，按 spine 阅读顺序直接识别章节目录与正文"""
    with zipfile.ZipFile(file_path, "r") as zf:
        namelist = zf.namelist()
        if "META-INF/container.xml" not in namelist:
            # 非合规 epub，降级为尝试查找所有 html 文件
            html_files = [f for f in namelist if f.lower().endswith((".xhtml", ".html", ".htm"))]
            html_files.sort(key=_natural_sort_key)
            return _parse_html_files_to_chapters(zf, html_files)

        # 1. 定位 OPF 清单文件
        container_xml = zf.read("META-INF/container.xml")
        c_root = ET.fromstring(container_xml)
        opf_path = None
        for elem in c_root.iter():
            if elem.tag.endswith("rootfile") and elem.attrib.get("full-path"):
                opf_path = elem.attrib["full-path"]
                break

        if not opf_path or opf_path not in namelist:
            # 容错 fallback
            opfs = [f for f in namelist if f.lower().endswith(".opf")]
            if opfs:
                opf_path = opfs[0]
            else:
                html_files = [f for f in namelist if f.lower().endswith((".xhtml", ".html", ".htm"))]
                html_files.sort(key=_natural_sort_key)
                return _parse_html_files_to_chapters(zf, html_files)

        base_dir = posixpath.dirname(opf_path)
        opf_content = zf.read(opf_path)
        opf_root = ET.fromstring(opf_content)

        # 2. 读取 manifest 映射与 spine 顺序
        manifest = {}
        for elem in opf_root.iter():
            if elem.tag.endswith("item") and "id" in elem.attrib and "href" in elem.attrib:
                manifest[elem.attrib["id"]] = elem.attrib["href"]

        spine_order = []
        for elem in opf_root.iter():
            if elem.tag.endswith("itemref") and "idref" in elem.attrib:
                idref = elem.attrib["idref"]
                if idref in manifest:
                    href = manifest[idref]
                    full_href = posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else href
                    spine_order.append(full_href)

        # 若无 spine，则取 manifest 中所有 xhtml/html
        if not spine_order:
            for href in manifest.values():
                if href.lower().endswith((".xhtml", ".html", ".htm")):
                    full_href = posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else href
                    spine_order.append(full_href)
            spine_order.sort(key=_natural_sort_key)

        return _parse_html_files_to_chapters(zf, spine_order, chunk_size=chunk_size)


def _parse_html_files_to_chapters(zf: zipfile.ZipFile, html_files: List[str], chunk_size: int = 3000) -> List[Chapter]:
    """从已排序的 html 列表中抽取有效章节"""
    chapters = []
    idx = 1

    for fpath in html_files:
        if fpath not in zf.namelist():
            continue
        try:
            raw_bytes = zf.read(fpath)
            content_str = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            continue

        parser = _HTMLTextExtractor()
        try:
            parser.feed(content_str)
            clean_text = parser.get_clean_text()
            ch_title = parser.get_title()
        except Exception:
            clean_text = re.sub(r'<[^>]+>', '', content_str).strip()
            ch_title = None

        # 过滤过短的页面（如纯封面、版权页、空页等），少于 15 字且无特定章节特征跳过
        if len(clean_text) < 15 and not (ch_title and re.search(r'第.+章|Chapter', ch_title)):
            continue

        title = ch_title if ch_title else f"第 {idx} 章"
        # 兼容标题自带第一章前缀时
        chapters.append(
            Chapter(
                index=idx,
                title=title,
                content=clean_text,
                char_count=len(clean_text)
            )
        )
        idx += 1

    # 如果 epub 的 html 极其少（比如一本整书全部塞在单个 html 里），则按正则切分
    if len(chapters) == 1 and chapters[0].char_count > chunk_size * 2:
        return _parse_single_text(chapters[0].content, chunk_size=chunk_size)

    return chapters


def _natural_sort_key(s: str):
    """支持中文与数字的自然排序键"""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def _split_plain_text(text: str, chunk_size: int = 3000) -> List[Chapter]:
    """对无章节标题的纯文本按段落与字数边界做自适应滑窗分块"""
    raw_paragraphs = text.split("\n")
    # 如果单个段落本身超过 chunk_size，做强制长度切分
    paragraphs = []
    for p in raw_paragraphs:
        if len(p) <= chunk_size:
            paragraphs.append(p)
        else:
            for i in range(0, len(p), chunk_size):
                paragraphs.append(p[i:i + chunk_size])

    chapters = []
    current_chunk = []
    current_len = 0
    chunk_idx = 1

    for p in paragraphs:
        p_len = len(p)
        if current_len + p_len > chunk_size and current_chunk:
            content = "\n".join(current_chunk).strip()
            if content:
                chapters.append(
                    Chapter(
                        index=chunk_idx,
                        title=f"分段 {chunk_idx:03d}",
                        content=content,
                        char_count=len(content),
                    )
                )
                chunk_idx += 1
            current_chunk = [p]
            current_len = p_len
        else:
            current_chunk.append(p)
            current_len += p_len + 1

    if current_chunk:
        content = "\n".join(current_chunk).strip()
        if content:
            chapters.append(
                Chapter(
                    index=chunk_idx,
                    title=f"分段 {chunk_idx:03d}",
                    content=content,
                    char_count=len(content),
                )
            )

    return chapters


def _parse_single_text(text: str, chunk_size: int = 3000) -> List[Chapter]:
    """解析单一大文本，优先按章节正则切分，未匹配则滑窗"""
    matches = list(CHAPTER_REGEX.finditer(text))
    if not matches:
        return _split_plain_text(text, chunk_size=chunk_size)

    chapters = []
    first_start = matches[0].start()
    prelude = text[:first_start].strip()
    idx = 1
    if prelude and len(prelude) > 50:
        chapters.append(
            Chapter(
                index=idx,
                title="序章 / 楔子",
                content=prelude,
                char_count=len(prelude),
            )
        )
        idx += 1

    for i, m in enumerate(matches):
        prefix = m.group(1).strip()
        name = m.group(2).strip()
        title = f"{prefix} {name}".strip()

        start_pos = m.end()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start_pos:end_pos].strip()

        chapters.append(
            Chapter(
                index=idx,
                title=title,
                content=content,
                char_count=len(content),
            )
        )
        idx += 1

    return chapters


def load_novel(source: Union[str, os.PathLike], chunk_size: int = 3000) -> List[Chapter]:
    """万能小说输入加载器（零依赖支持 txt/md、docx、epub、目录或原始文本，自动清洗拖拽引号）"""
    # 清洗终端输入路径，剥离 Windows 拖拽产生的首尾引号与空格
    clean_src = str(source).strip(' "\'\t\r\n')

    if os.path.isdir(clean_src):
        valid_exts = {".txt", ".md", ".text"}
        files = [
            f for f in os.listdir(clean_src)
            if os.path.isfile(os.path.join(clean_src, f)) and os.path.splitext(f)[1].lower() in valid_exts
        ]
        files.sort(key=_natural_sort_key)

        chapters = []
        for i, fname in enumerate(files, start=1):
            fpath = os.path.join(clean_src, fname)
            content = _read_file_safe(fpath).strip()
            title = os.path.splitext(fname)[0]
            chapters.append(
                Chapter(
                    index=i,
                    title=title,
                    content=content,
                    char_count=len(content),
                )
            )
        return chapters

    elif os.path.isfile(clean_src):
        ext = os.path.splitext(clean_src)[1].lower()
        if ext == ".docx":
            docx_text = _read_docx(clean_src)
            return _parse_single_text(docx_text, chunk_size=chunk_size)
        elif ext == ".epub":
            return _read_epub(clean_src, chunk_size=chunk_size)
        elif ext == ".pdf":
            raise ValueError(
                "检测到 PDF 格式小说。因 PDF 格式版面复杂且网文极少使用，为了保证剧本质量，建议将其另存为 Word (.docx) 或 记事本 (.txt) 后重试。"
            )
        else:
            raw_text = _read_file_safe(clean_src)
            return _parse_single_text(raw_text, chunk_size=chunk_size)

    else:
        # 非文件路径，直接作为纯文本内容解析
        return _parse_single_text(clean_src, chunk_size=chunk_size)
