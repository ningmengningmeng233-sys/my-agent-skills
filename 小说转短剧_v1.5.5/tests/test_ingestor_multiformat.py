import os
import zipfile
import pytest
from dramacraft.ingestor import load_novel, Chapter


def _create_mock_docx(file_path: str, paragraphs: list, comment_text: str = "这是废弃批注"):
    """使用纯标准库构造合法的 mock docx 文件"""
    doc_xml_lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">',
        '  <w:body>'
    ]
    for p in paragraphs:
        doc_xml_lines.append('    <w:p>')
        doc_xml_lines.append(f'      <w:r><w:t>{p}</w:t></w:r>')
        if comment_text:
            # 注入批注节点，验证解析器能正确过滤
            doc_xml_lines.append(f'      <w:comment w:id="1"><w:p><w:r><w:t>{comment_text}</w:t></w:r></w:p></w:comment>')
        doc_xml_lines.append('    </w:p>')
    doc_xml_lines.append('  </w:body>')
    doc_xml_lines.append('</w:document>')

    with zipfile.ZipFile(file_path, "w") as zf:
        zf.writestr("word/document.xml", "\n".join(doc_xml_lines).encode("utf-8"))


def _create_mock_epub(file_path: str, chapters: list):
    """使用纯标准库构造合法的 mock epub 文件"""
    with zipfile.ZipFile(file_path, "w") as zf:
        # 1. mimetype
        zf.writestr("mimetype", "application/epub+zip")

        # 2. container.xml
        container_xml = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
        zf.writestr("META-INF/container.xml", container_xml)

        # 3. content.opf
        manifest_items = []
        spine_items = []
        for i, (title, content) in enumerate(chapters, 1):
            fname = f"chapter_{i}.xhtml"
            manifest_items.append(f'<item id="c{i}" href="{fname}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="c{i}"/>')

            # 写入章节 xhtml
            xhtml_content = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title></head>
<body>
  <h1>{title}</h1>
  <p>{content}</p>
  <p>&ldquo;这是包含特殊实体引号的对话&rdquo;&nbsp;&mdash;&mdash;继续说。</p>
</body>
</html>"""
            zf.writestr(f"OEBPS/{fname}", xhtml_content.encode("utf-8"))

        opf_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <manifest>
    {"".join(manifest_items)}
  </manifest>
  <spine>
    {"".join(spine_items)}
  </spine>
</package>"""
        zf.writestr("OEBPS/content.opf", opf_xml.encode("utf-8"))


def test_docx_parsing_and_comment_filtering(tmp_path):
    docx_path = tmp_path / "novel.docx"
    paragraphs = [
        "第一章 风云再起",
        "北境长城之上，林战负手而立，狂风吹乱了他的战袍。",
        "“三年了，我终于回来了！”他冷声道。",
        "第二章 家族危机",
        "林家大厅内，族人们议论纷纷，气氛沉重到了极点。"
    ]
    _create_mock_docx(str(docx_path), paragraphs, comment_text="【编辑批注：这里语气建议更霸气一点】")

    chapters = load_novel(str(docx_path))
    assert len(chapters) == 2
    assert chapters[0].title.strip() == "第一章 风云再起"
    assert "北境长城之上" in chapters[0].content
    assert "三年了" in chapters[0].content
    # 验证批注已被过滤
    assert "编辑批注" not in chapters[0].content
    assert chapters[1].title.strip() == "第二章 家族危机"


def test_epub_parsing_and_spine_order(tmp_path):
    epub_path = tmp_path / "book.epub"
    ch_data = [
        ("第1章 初入江湖", "少年负剑下天山，江湖路远几多愁。"),
        ("第2章 客栈风波", "黑云压城城欲摧，悦来客栈杀气弥漫。")
    ]
    _create_mock_epub(str(epub_path), ch_data)

    chapters = load_novel(str(epub_path))
    assert len(chapters) == 2
    assert chapters[0].title == "第1章 初入江湖"
    assert "少年负剑下天山" in chapters[0].content
    # 验证 html 标签剥离和实体解码（“ ” ——）
    assert "<p>" not in chapters[0].content
    assert "“这是包含特殊实体引号的对话”" in chapters[0].content
    assert "——继续说。" in chapters[0].content
    assert chapters[1].title == "第2章 客栈风波"
    assert "黑云压城城欲摧" in chapters[1].content


def test_windows_quoted_path_handling(tmp_path):
    docx_path = tmp_path / "my novel.docx"
    _create_mock_docx(str(docx_path), ["第一回 梦幻开局", "故事由此展开。"], comment_text="")

    # 模拟 Windows 拖拽产生的带双引号路径
    quoted_input = f'"{str(docx_path)}"'
    chapters = load_novel(quoted_input)
    assert len(chapters) == 1
    assert "梦幻开局" in chapters[0].title
