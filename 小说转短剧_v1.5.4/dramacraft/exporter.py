import csv
import io
import json
import os
import subprocess
import tempfile
from typing import Dict, List, Optional, Any

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RENDER_SCRIPT = os.path.join(ROOT_DIR, "skills_runtime", "novel-script", "scripts", "novel-script.mjs")


HUMAN_READABLE_FORMATS = ("markdown", "csv", "html")

# humanizer 24 类中的 markdown 排版三件（14 粗体滥用 / 15 行内标题竖排清单 / 16 标题首字母大写）：
# 它们出在交付排版而非剧本内容上，故不设剧本闸，交付前人工照此清单扫一眼。
EXPORT_STYLE_CHECKLIST = """【交付物排版自检（三条，人工扫一眼即可）】
1. 粗体滥用：正文别用 **加粗** 强调情绪或关键词——强调靠台词与动作，不靠排版。
2. 行内标题竖排清单：别写成「- 动机：xxx / - 手段：yyy」这类加粗小标题列表，剧本是叙事不是文档。
3. 标题首字母大写：中文标题不套英文式首字母大写，也不做中英混排标题。"""


def _speaker_map(outline_data: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """outline.characters 是 id→人名的唯一登记表；VO 与查不到的 id 原样放行"""
    if not outline_data:
        return {}
    return {
        c.get("id"): c.get("name")
        for c in outline_data.get("characters", [])
        if c.get("id") and c.get("name")
    }


def _resolve_speaker(speaker: str, mapping: Dict[str, str]) -> str:
    return mapping.get(speaker, speaker)


def export_to_markdown(script_data: Dict[str, Any], outline_data: Optional[Dict[str, Any]] = None) -> str:
    """导出为标准影视行业剧本排版 Markdown。

    说话人一律以人名呈现（经 outline 映射）；无 outline 时保留编号并在头部
    注明「人物表缺失」，绝不凭空猜名字。"""
    lines = []
    source = script_data.get("source", "短剧剧本")
    mapping = _speaker_map(outline_data)
    lines.append(f"# 《{source}》分集剧本\n")

    characters = (outline_data or {}).get("characters", [])
    if characters:
        lines.append("## 人物表\n")
        for c in characters:
            role = c.get("role", "")
            desire = c.get("desire", "")
            lines.append(f"- {c.get('id')} {c.get('name')}｜{role}｜{desire}")
        lines.append("")
    else:
        lines.append("> 注：人物表缺失，台词说话人以剧本内编号呈现，对应关系见大纲 outline.json\n")

    for ep in script_data.get("episodes", []):
        ep_num = ep.get("ep", 1)
        lines.append(f"## 第 {ep_num:02d} 集\n")
        if ep.get("hook"):
            lines.append(f"> **开篇钩子**：{ep['hook']}")
        if ep.get("cliff"):
            lines.append(f"> **结尾悬念**：{ep['cliff']}\n")

        for sc in ep.get("scenes", []):
            sc_id = sc.get("sceneId", "Sc")
            loc = sc.get("location", "日/内 场景")
            lines.append(f"### 场 {sc_id}  {loc}\n")

            for beat in sc.get("flow", []):
                b_type = beat.get("type")
                if b_type == "action":
                    lines.append(f"△ {beat.get('action', '')}\n")
                elif b_type == "dialogue":
                    spk = _resolve_speaker(beat.get("speaker", "角色"), mapping)
                    deliv = beat.get("delivery", "")
                    dlg = beat.get("line", "")
                    deliv_str = f"（{deliv}）" if deliv else ""
                    lines.append(f"{spk}{deliv_str}：")
                    lines.append(f"    {dlg}\n")

        lines.append("---\n")

    return "\n".join(lines)


def export_to_csv(script_data: Dict[str, Any], outline_data: Optional[Dict[str, Any]] = None) -> str:
    """导出为短剧剧组统筹/拍摄分场表格 CSV。说话人经 outline 映射为人名。"""
    mapping = _speaker_map(outline_data)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["集号", "场号", "场景地点", "节拍序号", "类型", "说话人/主体", "潜台词/动作指导", "台词/动作内容", "估算时长(秒)"])

    for ep in script_data.get("episodes", []):
        ep_label = f"E{ep.get('ep', 1):02d}"
        for sc in ep.get("scenes", []):
            sc_id = sc.get("sceneId", "S01")
            loc = sc.get("location", "未指定")
            for b_idx, beat in enumerate(sc.get("flow", []), start=1):
                b_type = beat.get("type", "unknown")
                if b_type == "action":
                    action_txt = beat.get("action", "")
                    writer.writerow([ep_label, sc_id, loc, b_idx, "动作", "环境/主体", "-", action_txt, 2.5])
                elif b_type == "dialogue":
                    spk = _resolve_speaker(beat.get("speaker", ""), mapping)
                    deliv = beat.get("delivery", "")
                    dlg = beat.get("line", "")
                    est_sec = round(len(dlg) / 4.5, 1)
                    writer.writerow([ep_label, sc_id, loc, b_idx, "台词", spk, deliv, dlg, est_sec])

    return output.getvalue()


def export_to_html(script_data: Dict[str, Any], outline_data: Optional[Dict[str, Any]], out_path: str) -> str:
    """调用成熟渲染管道生成单页现代化 HTML 仪表板"""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f_sc:
        json.dump(script_data, f_sc, ensure_ascii=False, indent=2)
        sc_path = f_sc.name

    out_arg = []
    temp_out_path = None
    if outline_data:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f_out:
            json.dump(outline_data, f_out, ensure_ascii=False, indent=2)
            temp_out_path = f_out.name
        out_arg = ["--outline", temp_out_path]

    try:
        cmd = ["node", RENDER_SCRIPT, "render", sc_path] + out_arg + ["--html"]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if res.returncode != 0:
            raise RuntimeError(f"HTML 渲染失败: {res.stderr}")

        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fp:
            fp.write(res.stdout)

        return out_path
    finally:
        if os.path.exists(sc_path):
            os.remove(sc_path)
        if temp_out_path and os.path.exists(temp_out_path):
            os.remove(temp_out_path)


def export_all(
    script_data: Dict[str, Any],
    outline_data: Optional[Dict[str, Any]],
    output_dir: str,
    formats: Optional[List[str]] = None
) -> Dict[str, str]:
    """同时导出多种格式交付物"""
    if formats is None:
        formats = ["markdown", "csv", "json", "html"]

    if outline_data is None and any(fmt in formats for fmt in HUMAN_READABLE_FORMATS):
        raise ValueError(
            "导出人类可读交付物（markdown/csv/html）必须随附 outline_data："
            "台词说话人在剧本里是角色编号（C01 式），只有大纲 characters 登记表能把编号翻成人名。"
            "宁可拒跑，不给人看满屏编号的剧本。"
        )

    os.makedirs(output_dir, exist_ok=True)
    exported_paths = {}

    if "markdown" in formats:
        md_text = export_to_markdown(script_data, outline_data)
        md_path = os.path.join(output_dir, "script.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_text)
        exported_paths["markdown"] = md_path

    if "csv" in formats:
        csv_text = export_to_csv(script_data, outline_data)
        csv_path = os.path.join(output_dir, "script_breakdown.csv")
        with open(csv_path, "w", encoding="utf-8-sig") as f:
            f.write(csv_text)
        exported_paths["csv"] = csv_path

    if "json" in formats:
        json_path = os.path.join(output_dir, "script.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(script_data, f, ensure_ascii=False, indent=2)
        exported_paths["json"] = json_path

    if "html" in formats:
        html_path = os.path.join(output_dir, "script-report.html")
        export_to_html(script_data, outline_data, html_path)
        exported_paths["html"] = html_path

    return exported_paths


def _one_line(value: Any) -> str:
    """压成单行：字段值里的换行会把「一行一字段」的结构击穿，甚至伪造出被禁的扩展段。"""
    return " ".join(str(value or "").split())


def _shot_secs_display(secs: Any) -> str:
    """时长渲染带 s 后缀（Omni 卡 :50 示例「6-8s」）；已带后缀的不重复加。"""
    text = _one_line(secs)
    if not text:
        return ""
    return text if text.lower().endswith(("s", "秒")) else f"{text}s"


def _shot_cast_display(cast: Any) -> str:
    """人物渲染：无人物写「无」（Omni 卡 :52 原文）。非文字项按 JSON 原样示众，不吐 Python 内部写法。"""
    if not isinstance(cast, (list, tuple)):
        return _one_line(cast) or "无"
    items = []
    for c in cast:
        text = _one_line(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
        if text:
            items.append(text)
    return "、".join(items) if items else "无"


def export_shot_groups_md(shot_groups: Dict[str, Any], title: str = "短剧") -> str:
    """机读镜头组 → Omni 极简五字段 Markdown（剧本转镜头组卡 v1.2.0 :57-71）。

    只保留「编号 / 时长建议 / 场景 / 人物 / 内容」五字段——映射信息（planSceneId 等）留在机读件里，
    不掺 叙事任务 / 导演校验 / 组内节拍 / 压缩方案 等扩展段（卡 :55、:71、:188 的 boilerplate 禁令）。

    所有字段值渲染前压成单行（`_one_line`）：`content` 里带的换行会把「一行一字段」击穿，
    甚至伪造出被禁的 boilerplate 段（评审 W5）。
    """
    groups = [g for g in (shot_groups.get("groups") or []) if isinstance(g, dict)]
    lines: List[str] = [f"# 《{title}》镜头组", ""]
    for g in groups:
        gid = _one_line(g.get("gid"))
        scene = _one_line(g.get("scene"))
        lines.append(f"## {gid}：{scene or gid}")
        lines.append("")
        lines.append(f"- 编号：{gid}")
        lines.append(f"- 时长建议：{_shot_secs_display(g.get('secs'))}")
        lines.append(f"- 场景：{scene}")
        lines.append(f"- 人物：{_shot_cast_display(g.get('cast'))}")
        lines.append(f"- 内容：{_one_line(g.get('content'))}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
