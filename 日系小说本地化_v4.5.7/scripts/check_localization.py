#!/usr/bin/env python3
"""
日系小说本地化 — 硬门槛质检脚本 v1.1

对改写后的批文件执行硬门槛检查（见 references/质检规范.md）：
1. 称谓残留：桑/酱/君（人名后）/前辈/お兄ちゃん + 日系姓氏高频字（崎/藤/井/泽/田做姓）
2. 文化符号残留：神社/和服/武士/樱花祭/便利店/榻榻米等（拉面/便当/电车为大陆通用词不检）
3. 翻译腔：省略主语/“……的样子/……的感觉/……呢”/“但是但是”
4. 台翻腔：台湾语气词/台版用词/繁体残留
5. 去AI味：stop-slop 中文规则速扫（空转开场/模板句尾/网络词/句式公式）

用法:
  python check_localization.py "<批文件>"... [--json]

退出码: 0=全部通过, 1=发现问题
"""
import argparse
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 硬门槛 1: 称谓残留 ─────────────────────────────
# "君"仅当人名后（XX君）视为日式称谓；排除夫君/圣君/仁君/明君/暴君/国君/天君/君王/君临/君子/君主
TITLE_RESIDUE = {
    "日式称谓": r"桑|ちゃん|先輩|後輩|お兄ちゃん|お姉ちゃん|さん|様|(?<![夫圣仁明暴公国天])君(?![子王临主臣公侯将相])",
    "日系姓氏字(作姓)": r"(?:^|，|。|；)\s*(?!(?:田野|山村|井水|木村|川谷|村头|村口|木门|木床|谷口|村正|井边))[崎藤井泽田野佐渡辺邊村谷川木本](?=[一-龥]{1,2}(?:说|道|走|站|看|坐|来|去|是|的|，|。))",
}

# ── 硬门槛 2: 文化符号残留 ─────────────────────────
CULTURE_RESIDUE = {
    "建筑场景": r"神社|鸟居|绘马|和室|榻榻米|障子|居酒屋|便利店|電車|駅|站台",
    "服饰": r"和服|浴衣|木屐|下駄",
    "饮食": r"寿司|天妇罗|味增|关东煮|荞麦面|丼",
    "人物/身份": r"武士|忍者|幕府|大名|艺伎|舞妓|浪人",
    "节庆": r"樱花祭|花见|盂兰盆|祇园祭|初詣|文化祭|学园祭",
    "意象": r"樱花(?![瓣落雨飘])|初雪|物哀|一期一会|侘寂|幽玄",
}

# ── 硬门槛 3: 翻译腔 ───────────────────────────────
TRANSLATION_TONE = {
    "日式句尾": r"……的样子|…的样子|……的感觉|…的感觉|(?<![。！？…])呢(?=[。！？…」』\"\n])|的样子(?=[。！？…」』\"\n])|的感觉(?=[。！？…」』\"\n])",
    "省略主语痕迹": r"(?:然后|接着)就(?:被|把|让)",
    "翻译腔连接词": r"但是但是|然而然而|虽然但是(?!……)",
    "日式省略(句首)": r"(?<=。|；)\s*明明(?:说|想|知道)",
}

# ── 硬门槛 3 扩充: 翻译腔密度检测（2026-08-16 增补，防批间漂移）──
# 单次出现不报（中文网文同样使用这些词），密度超阈值才报——
# 日译文的连接词/“说着”/破折号密度显著高于中文原创
FREQ_RULES = {
    "日式连接词密度": {"words": ["随即", "紧接着", "与此同时", "下一刻", "就这样", "于是乎"], "per_wan": 10},
    "说着衔接密度": {"words": ["说着，", "说着就"], "per_wan": 5},
    "破折号密度": {"words": ["——"], "per_wan": 20},
}

def check_frequency(text: str) -> list:
    """翻译腔密度检查：万字内出现次数超阈值才报"""
    hits = []
    wan = max(1.0, len(text) / 10000)
    for name, rule in FREQ_RULES.items():
        count = sum(text.count(w) for w in rule["words"])
        if count / wan >= rule["per_wan"]:
            hits.append((name, f"{count}次/{wan:.1f}万字", f"阈值 {rule['per_wan']}/万字"))
    return hits


# ── 硬门槛 6: 标点规范（2026-08-16 增补）────────────
# 中文出版物标准：弯引号“”‘’；全角标点；场景分隔用空行或***（不用■）
PUNCTUATION = {
    "ASCII直引号": r'"|\'',
    "半角逗号句号": r"[,.]",
    "■场景分隔符": r"^■",
}


# ── 硬门槛 4: 台翻腔（台版翻译特征）─────────────────
TAIWAN_TONE = {
    "台湾语气词": r"(?<![哗唰喀啪沙咕])啦[！。！？，～]|喔[！。！]|耶～|嘛[，。！]|(?<!喽)喽[。！]",
    "台版用词": r"小钢珠|机车|吐槽(?![的着过])|奥特曼|高达|部落格|计画|软体|网路|滑鼠|硬碟|程式|超商|捷运|影音|数位|云端|加值|公车|脚踏车|课业|学长姐|手作|文创|(?<![倒坐站躺卧趴])在地|业者|民众|台湾|台湾人|正妹|鲁蛇|好康|揪团|阿桑|欧巴桑",
}

# 繁体特有字形检测（繁体→简体 对照表）
# 仅收录"简体中不存在该字形"的繁体字；繁简同形字（都/情/金/冰/那 等）不在此表
TRADITIONAL_TO_SIMPLE = {
    "這": "这", "們": "们", "說": "说", "時": "时", "還": "还", "裡": "里",
    "來": "来", "會": "会", "對": "对", "樣": "样", "點": "点", "從": "从",
    "問": "问", "沒": "没", "當": "当", "話": "话", "讓": "让", "應": "应",
    "該": "该", "體": "体", "無": "无", "處": "处", "寫": "写", "讀": "读",
    "聽": "听", "聞": "闻", "語": "语", "車": "车", "風": "风", "飛": "飞",
    "馬": "马", "龍": "龙", "鳥": "鸟", "魚": "鱼", "鹽": "盐", "銀": "银",
    "銅": "铜", "鐵": "铁", "電": "电", "雲": "云", "熱": "热", "溫": "温",
    "愛": "爱", "愁": "愁", "夢": "梦", "發": "发", "頭": "头", "開": "开",
    "間": "间", "關": "关", "門": "门", "學": "学", "書": "书",
    "邊": "边", "錢": "钱", "長": "长", "東": "东", "戰": "战", "爭": "争",
    "氣": "气", "題": "题", "現": "现", "與": "与", "為": "为", "個": "个",
    "後": "后", "國": "国", "過": "过", "機": "机", "經": "经", "濟": "济",
    "實": "实", "際": "际", "應": "应", "許": "许", "認": "认", "識": "识",
    "讓": "让", "請": "请", "謝": "谢", "幫": "帮", "還": "还", "夠": "够",
    "隻": "只", "雙": "双", "萬": "万", "歲": "岁", "親": "亲", "愛": "爱",
}


def find_traditional(text: str) -> list:
    """扫描繁体特有字形，返回命中列表（含对应简体）"""
    hits = []
    for ch in text:
        if ch in TRADITIONAL_TO_SIMPLE:
            hits.append(f"{ch}→{TRADITIONAL_TO_SIMPLE[ch]}")
    return hits

# ── 硬门槛 5: 去AI味（stop-slop 中文规则速扫）────────
# 只检真正"AI 味"标记词（空转开场/模板句尾/网络词/结构公式）；
# 语气副词（真的/其实/居然）与量化虚词（微微/渐渐）是大陆网文高频词，
# 不列入硬门槛（堆砌类由 phrases-zh.md 语义规则在改写阶段执行，脚本不误报）
AI_FILLERS = {
    "空转开场": r"众所周知|值得一提的是|值得注意的是|不可否认|毫无疑问",
    "AI模板句尾": r"总而言之|归根结底|从某种程度来说|一言以蔽之",
    "网络词": r"绝了|yyds|破防|天花板|顶流|出圈|内卷|躺平",
}
AI_STRUCTURES = {
    "对仗三连": r"(?:不是[^。！？]{2,12}，而是[^。！？]{2,12}[。！？]){2,}",
    "否定转折连用": r"不是[^。！？]{2,15}，而是[^。！？]{2,15}[。！？]\s*不是",
    "设问自答": r"[^。！？]{2,20}[?？]\s*答案(?:只有|就是|唯一)",
    "转折堆砌": r"(?:然而|但是|不过|可是)[^。！？]{0,20}(?:然而|但是|不过|可是)",
}

# 省略号密度检查（规则H：句内>1个才违规；段落内>2处；万字>10处）
ELLIPSIS_PATTERN = re.compile(r"…{2,}")


def check_ellipsis(text: str) -> list:
    """省略号检查：句内叠加 >1 才报（单处使用是合法叙事停顿）。
    对话内（「」『』""内）的省略号是台词节奏，不检查。"""
    hits = []
    # 先剔除对话内容（引号内），只检查叙述层
    narrative = re.sub(r"[「『“][^」』”]*[」』”]", "", text)
    for m in ELLIPSIS_PATTERN.finditer(narrative):
        # 同一句内（往前到句号/换行）是否还有省略号
        before = narrative[max(0, m.start() - 60):m.start()]
        if not re.search(r"[。！？!?\n]", before) and ELLIPSIS_PATTERN.search(before):
            hits.append(("省略号句内叠加", m.group(0), text[max(0, m.start()-15):m.end()+30]))
    # 段落内密度（叙述层）：同一段内 >3 处省略号才报（宽容阈值）
    for para in narrative.split("\n"):
        if para.count("…") > 6:  # 3个省略号=6个点
            hits.append(("省略号段落过密", para[:30], f"段内省略号过多: {para[:60]}"))
    return hits


def scan(text: str, patterns: dict, context: int = 60) -> list:
    """扫描文本，返回命中列表 [(规则名, 匹配词, 上下文)]。re.M 保证 $ 锚定到行尾（句尾检测生效）"""
    hits = []
    for name, pat in patterns.items():
        for m in re.finditer(pat, text, re.M):
            start = max(0, m.start() - 10)
            end = min(len(text), m.end() + context)
            ctx = text[start:end].replace("\n", " ")
            hits.append((name, m.group(0), ctx))
    return hits


def check_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()

    # 跳过 frontmatter（元数据区不算正文）
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    # 跳过"本批说明"元信息区（风格对照表/映射记录不算正文）
    note_idx = text.find("## 本批说明")
    if note_idx != -1:
        text = text[:note_idx]
    # 跳过正文前的"本批爽点对照"元信息区（标题+清单行，不算正文）
    fm_idx = text.find("## 本批爽点对照")
    if fm_idx != -1:
        lines = text[fm_idx:].splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line == "" or line.startswith("- ") or line.startswith("## "):
                i += 1
                continue
            break
        text = text[:fm_idx] + "\n" + "\n".join(lines[i:])

    results = {}
    results["称谓残留"] = scan(text, TITLE_RESIDUE)
    results["文化符号残留"] = scan(text, CULTURE_RESIDUE)
    results["翻译腔"] = scan(text, TRANSLATION_TONE)
    results["翻译腔"] += check_frequency(text)
    results["标点规范"] = scan(text, PUNCTUATION, context=40)
    results["台翻腔"] = scan(text, TAIWAN_TONE)
    # 繁体检测（独立处理，返回字符列表）
    trad = find_traditional(text)
    if trad:
        unique = sorted(set(trad))
        results["台翻腔"].append(("繁体残留", "".join(unique[:20]), f"共发现{len(trad)}个繁体字符（示例: {unique[:10]}）"))
    results["去AI味-填充词"] = scan(text, AI_FILLERS)
    results["去AI味-句式"] = scan(text, AI_STRUCTURES)
    results["去AI味-句式"] += check_ellipsis(text)
    return results


def main():
    ap = argparse.ArgumentParser(description="本地化硬门槛质检")
    ap.add_argument("files", nargs="+", help="批文件或中国化版文件")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    all_pass = True
    summary = {}
    for fpath in args.files:
        if not os.path.exists(fpath):
            print(f"错误: 文件不存在 {fpath}")
            all_pass = False
            continue
        results = check_file(fpath)
        total_hits = sum(len(v) for v in results.values())
        if total_hits:
            all_pass = False
        summary[os.path.basename(fpath)] = {k: len(v) for k, v in results.items()}
        print(f"\n=== {os.path.basename(fpath)} ===")
        for cat, hits in results.items():
            if hits:
                print(f"  ❌ {cat}: {len(hits)} 处")
                for name, word, ctx in hits[:5]:
                    print(f"     - [{name}] 「{word}」…{ctx}")
            else:
                print(f"  ✅ {cat}: 通过")

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    print(f"\n{'✅ 全部通过' if all_pass else '❌ 存在残留，需返工'}")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
