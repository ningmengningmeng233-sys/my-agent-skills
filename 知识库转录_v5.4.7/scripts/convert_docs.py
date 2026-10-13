# -*- coding: utf-8 -*-
"""多格式文档转换器 (convert_docs.py)

功能：
1. 将各种外部文档（.docx, .pdf, .epub, .txt, .md）统一解析并清洗转为标准 Markdown/TXT 格式
2. 保持段落结构完整，过滤空白杂质
3. 支持单文件转换与目录批量转换
4. 输出规范结构化文件，供后续 split_segments.py 切段入库

使用示例：
    python convert_docs.py "资料.docx" -o "资料.txt"
    python convert_docs.py "论文.pdf" -o "论文.txt"
"""

import sys
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path


def clean_text(text: str) -> str:
    """文本基础清洗规整"""
    if not text:
        return ""
    # 替换 Windows CRLF 与特殊控制符
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 替换不可见字符与多余空格
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # 连续空行压缩为最多两行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_docx_text(file_path: Path) -> str:
    """提取 Word (.docx) 文件中的全部文本及表格内容"""
    try:
        import docx
    except ImportError:
        raise RuntimeError("未检测到 python-docx 模块，请先运行: pip install python-docx")

    doc = docx.Document(str(file_path))
    paragraphs = []

    # 提取正文段落，并保留 Word Heading 1/2/3 层级。
    for p in doc.paragraphs:
        txt = p.text.strip()
        if txt:
            style = (getattr(p.style, "name", "") or "").lower()
            level = next((n for n in (1, 2, 3, 4, 5, 6) if f"heading {n}" in style or f"标题 {n}" in style), None)
            paragraphs.append(("#" * level + " " + txt) if level else txt)

    # 提取表格内容
    for table in doc.tables:
        table_rows = []
        for row in table.rows:
            row_txt = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(row_txt):
                table_rows.append(" | ".join(row_txt))
        if table_rows:
            paragraphs.append("\n" + "\n".join(table_rows) + "\n")

    return "\n\n".join(paragraphs)


class _EpubParser(HTMLParser):
    """轻量 EPUB XHTML 转 Markdown 文本解析器，不依赖第三方包。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "nav"}:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "br", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "nav"} and self.skip:
            self.skip -= 1
        elif not self.skip and tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self):
        return "".join(self.parts)


def extract_epub_text(file_path: Path) -> str:
    """按 EPUB spine 顺序提取 XHTML 正文；复杂排版和扫描图片需人工复核。"""
    try:
        with zipfile.ZipFile(file_path) as book:
            names = set(book.namelist())
            container = book.read("META-INF/container.xml").decode("utf-8", "replace")
            root_match = re.search(r"full-path=[\"']([^\"']+)[\"']", container)
            if not root_match:
                raise RuntimeError("EPUB 缺少 OPF 根文件")
            opf_path = root_match.group(1)
            opf = book.read(opf_path).decode("utf-8", "replace")
            manifest = {
                m.group(1): m.group(2)
                for m in re.finditer(r"<item[^>]+id=[\"']([^\"']+)[\"'][^>]+href=[\"']([^\"']+)[\"']", opf, re.I)
            }
            spine = re.findall(r"<itemref[^>]+idref=[\"']([^\"']+)[\"']", opf, re.I)
            base = Path(opf_path).parent.as_posix()
            outputs = []
            for item_id in spine:
                href = manifest.get(item_id)
                if not href:
                    continue
                entry = (Path(base) / href).as_posix()
                entry = entry.replace("//", "/")
                if entry not in names:
                    continue
                parser = _EpubParser()
                parser.feed(book.read(entry).decode("utf-8", "replace"))
                outputs.append(parser.text())
            return "\n\n".join(outputs)
    except KeyError as exc:
        raise RuntimeError(f"EPUB 缺少必要文件：{exc}") from exc


def extract_pdf_text(file_path: Path) -> str:
    """提取 PDF (.pdf) 文件中的文本内容，带页码定位注记"""
    try:
        import pdfplumber
    except ImportError:
        # 回退尝试 pypdf
        try:
            import pypdf
            reader = pypdf.PdfReader(str(file_path))
            pages = []
            for i, page in enumerate(reader.pages):
                t = page.extract_text() or ""
                if t.strip():
                    pages.append(f"<!-- Page {i + 1} -->\n" + t.strip())
            return "\n\n".join(pages)
        except ImportError:
            raise RuntimeError("未检测到 pdfplumber 或 pypdf 模块，请运行: pip install pdfplumber")

    pages = []
    with pdfplumber.open(str(file_path)) as pdf:
        for i, page in enumerate(pdf.pages):
            txt = page.extract_text() or ""
            if txt.strip():
                pages.append(f"<!-- Page {i + 1} -->\n" + txt.strip())

    return "\n\n".join(pages)


def convert_file(input_path: Path, output_path: Path = None) -> Path:
    """转换单一文档"""
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"文件不存在: {input_path}")

    ext = input_path.suffix.lower()

    if ext in [".txt", ".md"]:
        with open(input_path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    elif ext == ".docx":
        raw = extract_docx_text(input_path)
    elif ext == ".pdf":
        raw = extract_pdf_text(input_path)
    elif ext == ".epub":
        raw = extract_epub_text(input_path)
    else:
        raise ValueError(f"不支持的输入文件格式: {ext} (支持 .txt, .md, .docx, .pdf, .epub)")

    cleaned = clean_text(raw)

    if output_path is None:
        output_path = input_path.with_suffix(".txt")
    else:
        output_path = Path(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(cleaned)

    return output_path


def main():
    import argparse
    parser = argparse.ArgumentParser(description="多格式文档转换为标准知识库文本")
    parser.add_argument("input", help="输入文件路径（.docx, .pdf, .epub, .txt, .md）")
    parser.add_argument("-o", "--output", help="输出文件路径（可选，默认为同名.txt）")
    args = parser.parse_args()

    try:
        out = convert_file(Path(args.input), Path(args.output) if args.output else None)
        print(f"[+] 转换成功: {args.input} -> {out}")
        print(f"[+] 输出大小: {out.stat().st_size} 字节")
    except Exception as e:
        print(f"[-] 转换失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
