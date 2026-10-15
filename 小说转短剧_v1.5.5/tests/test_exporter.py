import os
import json
import tempfile
import shutil
import pytest

from dramacraft.exporter import export_to_markdown, export_to_csv, export_to_html, export_all


@pytest.fixture
def sample_data():
    from dramacraft.ingestor import _read_file_safe
    script_data = json.loads(_read_file_safe(os.path.join("fixtures", "sample_script.json")))
    outline_data = json.loads(_read_file_safe(os.path.join("fixtures", "sample_outline.json")))
    return script_data, outline_data


def test_export_to_markdown_uses_character_names(sample_data):
    # 2026-09-21 分发事故回归：人类可读剧本的说话人必须是人名，不能是 C01 编号
    script_data, outline_data = sample_data
    md_text = export_to_markdown(script_data, outline_data)

    assert "第 01 集" in md_text
    assert "场 S01" in md_text
    assert "△ 顾浩端着热茶猛地泼在地上" in md_text
    assert "顾浩（踩着林晨手背，冷笑着晃动高脚酒杯）：" in md_text
    assert "让你倒杯茶都倒不好" in md_text
    assert "C01（" not in md_text
    assert "C02（" not in md_text
    assert "C03（" not in md_text
    # 头部人物表：编号→人名交叉核对
    assert "人物表" in md_text
    assert "C01 林晨" in md_text


def test_export_to_markdown_fallback_without_outline(sample_data):
    # 函数级允许无大纲降级：保留编号并在头部注明，绝不猜名字
    script_data, _ = sample_data
    md_text = export_to_markdown(script_data)

    assert "人物表缺失" in md_text
    assert "C03（踩着林晨手背，冷笑着晃动高脚酒杯）：" in md_text


def test_export_to_csv_uses_character_names(sample_data):
    script_data, outline_data = sample_data
    csv_text = export_to_csv(script_data, outline_data)

    lines = csv_text.strip().splitlines()
    assert len(lines) > 10
    header = lines[0]
    assert "集号" in header and "场号" in header and "类型" in header and "台词/动作" in header
    assert "E01" in lines[1]
    assert "顾浩" in csv_text
    assert ",C03," not in csv_text


def test_export_all_refuses_human_formats_without_outline(sample_data):
    # 拒跑而不是产出满屏编号的剧本给人看
    script_data, _ = sample_data
    out_dir = tempfile.mkdtemp()
    try:
        with pytest.raises(ValueError) as exc:
            export_all(
                script_data=script_data,
                outline_data=None,
                output_dir=out_dir,
                formats=["markdown"],
            )
        assert "outline" in str(exc.value)
    finally:
        shutil.rmtree(out_dir)


def test_export_all_formats(sample_data):
    script_data, outline_data = sample_data
    out_dir = tempfile.mkdtemp()
    try:
        results = export_all(
            script_data=script_data,
            outline_data=outline_data,
            output_dir=out_dir,
            formats=["markdown", "csv", "json", "html"]
        )

        assert os.path.exists(results["markdown"])
        assert os.path.exists(results["csv"])
        assert os.path.exists(results["json"])
        assert os.path.exists(results["html"])
        assert os.path.getsize(results["html"]) > 30000

        md_text = open(results["markdown"], encoding="utf-8").read()
        assert "顾浩（" in md_text and "C03（" not in md_text
    finally:
        shutil.rmtree(out_dir)
