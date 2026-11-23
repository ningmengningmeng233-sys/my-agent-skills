import pytest


def test_bad_chapter_column_fails_at_segment_level():
    from engine.segment_format import parse_summary_rows, SegmentFormatError
    bad = "| 人物 | 老罗 |\n"          # 缺章节列
    with pytest.raises(SegmentFormatError) as e:
        parse_summary_rows(bad)
    assert e.value.code == "CHAPTER_COLUMN_INVALID"


def test_single_parser_source():
    """段级与聚合必须引用同一个函数对象（禁止两套正则）。"""
    import scripts.aggregate as agg      # 按实际导入路径调整
    from engine import segment_format
    assert agg.parse_summary_rows is segment_format.parse_summary_rows
