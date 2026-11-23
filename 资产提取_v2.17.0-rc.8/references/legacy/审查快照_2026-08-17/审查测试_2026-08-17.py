# -*- coding: utf-8 -*-
"""资产提取v2.4 外部审查对抗测试（2026-08-17）— 独立于既有测试套件

覆盖：
A. engine/llm.call_llm 错误路径（本地 mock OpenAI 兼容 API）：
   429 退避重试 / 400 stream_options 降级 / finish_reason=length 截断重试 /
   空响应重试 / min_len 过短重试 / SSE 中文分帧（0x85 字节）解析 / [DONE] 后尾帧丢弃
B. split_segments：cn2ar 数字边界 + CHAPTER_RE 无编号前瞻 + 编号列表防误判
C. engine/common.safe_name：路径逃逸 / 空名 / 截断
D. engine/index_db：mark_done UPSERT / should_skip hash 变更 / _migrate 补列
E. aggregate.parse_verdict：dropped 防御 / 附录回填 / 占位行过滤
F. aggregate.verify_citation：前缀截断匹配
G. 服饰设计 check_costumes：黑名单禁忌句剥离 / 主色撞车 / 配色出界 / 头身比 / 三视图
H. style.find_card_by_name 前缀规则 / prepare_costume_input 剧种子串兜底 / check_schedule 解析

用法: python 测试/审查测试_2026-08-17.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL, "scripts"))
sys.path.insert(0, os.path.join(SKILL, "scripts", "engine"))

PASS = 0
FAIL = []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name} {('| ' + detail) if detail else ''}")


# ============ A. call_llm 错误路径（mock 服务器） ============
print("\n== A. engine/llm.call_llm 错误路径（本地 mock） ==")


class MockHandler(http.server.BaseHTTPRequestHandler):
    """按路径行为：
    /ok            → 正常流式（支持中文分帧：把 UTF-8 字节按任意 3 字节边界切块）
    /429           → 前 1 次 429，之后正常
    /400_no_usage  → 第一次带 stream_options 时 400，第二次正常
    /trunc         → 第一次 finish_reason=length，第二次正常
    /empty         → 第一次无 content，第二次正常
    /short         → 第一次输出过短，第二次正常
    /tail          → [DONE] 后再发一帧垃圾数据（应被丢弃）
    """
    protocol_version = "HTTP/1.1"
    calls = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        req = json.loads(body.decode("utf-8"))
        key = self.path
        MockHandler.calls[key] = MockHandler.calls.get(key, 0) + 1
        n = MockHandler.calls[key]
        has_usage = "stream_options" in req

        def sse(payload):
            data = "data: " + json.dumps(payload) + "\n\n"
            return data.encode("utf-8")

        if key == "/always429":
            self.send_response(429)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.close_connection = True
            return
        if key == "/429":
            if n == 1:
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = sse({"choices": [{"delta": {"content": "好"}, "finish_reason": None}]})
            payload += sse({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
            payload += b"data: [DONE]\n\n"
        elif key == "/400_no_usage":
            if n == 1 and has_usage:
                self.send_response(400)
                body = b'{"error": {"message": "stream_options not supported"}}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            payload = sse({"choices": [{"delta": {"content": "乙"}, "finish_reason": None}]})
            payload += sse({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
            payload += b"data: [DONE]\n\n"
        elif key == "/trunc":
            if n == 1:
                payload = sse({"choices": [{"delta": {"content": "半截"}, "finish_reason": "length"}]})
                payload += b"data: [DONE]\n\n"
            else:
                payload = sse({"choices": [{"delta": {"content": "完整"}, "finish_reason": None}]})
                payload += sse({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {}})
                payload += b"data: [DONE]\n\n"
        elif key == "/empty":
            if n == 1:
                payload = sse({"choices": [{"delta": {}, "finish_reason": None}]})
                payload += b"data: [DONE]\n\n"
            else:
                payload = sse({"choices": [{"delta": {"content": "有内容"}, "finish_reason": None}]})
                payload += sse({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {}})
                payload += b"data: [DONE]\n\n"
        elif key == "/short":
            if n == 1:
                payload = sse({"choices": [{"delta": {"content": "短"}, "finish_reason": None}]})
                payload += sse({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {}})
                payload += b"data: [DONE]\n\n"
            else:
                payload = sse({"choices": [{"delta": {"content": "足够长的回复内容满足下限要求"}, "finish_reason": None}]})
                payload += sse({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {}})
                payload += b"data: [DONE]\n\n"
        elif key == "/chinese_split":
            # 模拟 iter_lines 会踩坑的场景：把「雨巷青石板上」的 UTF-8 字节按 3 字节切，
            # 每次 write 都从中间切——任何按 0x85 字节分割的实现都会拆断 JSON
            text = "雨巷青石板上雨水滴答滴答落个不停，夜风穿过长廊"
            data = "data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": None}]}) + "\n\n"
            b = data.encode("utf-8")
            chunks = [b[i:i + 7] for i in range(0, len(b), 7)]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            for c in chunks:
                self.wfile.write(c)
                self.wfile.flush()
            time.sleep(0.05)
            self.wfile.write(b"data: [DONE]\n\n")
            return
        elif key == "/tail":
            payload = sse({"choices": [{"delta": {"content": "前段"}, "finish_reason": None}]})
            payload += b"data: [DONE]\n\n"
            payload += ('data: ' + json.dumps({"choices": [{"delta": {"content": "残余污染"}}]}) + "\n\n").encode("utf-8")
        else:
            payload = sse({"choices": [{"delta": {"content": "未知"}, "finish_reason": None}]})
            payload += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


import subprocess
import time
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
port = server.server_address[1]
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()


def llm_call(path, retries=3, min_len=0, timeout=120):
    """子进程跑 call_llm（engine.llm 模块级读 env，每场景独立进程注入 URL 路径）。"""
    code = (
        "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace'); "
        "sys.path.insert(0, 'scripts/engine'); sys.path.insert(0, 'scripts'); "
        "from engine.llm import call_llm; "
        "c, u = call_llm('s', 'u', retries=%d, min_len=%d); print('CONTENT|' + c)" % (retries, min_len)
    )
    env = {**os.environ,
           "OPENAI_API_KEY": "test-key",
           "SUMMARY_API_URL": f"http://127.0.0.1:{port}{path}",
           "SUMMARY_API_MODEL": "test-model",
           "SUMMARY_MAX_TOKENS": "8000"}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=SKILL, env=env, timeout=timeout)
    content = ""
    for ln in r.stdout.splitlines():
        if ln.startswith("CONTENT|"):
            content = ln[len("CONTENT|"):]
    return content, r.returncode, (r.stderr or "")[-300:]


check("A0 mock 服务器就绪", port > 0)

MockHandler.calls.clear()
c, rc, err = llm_call("/429", retries=3)
check("A1 429 首次退避后成功", c == "好" and MockHandler.calls.get("/429") == 2, f"content={c}, calls={MockHandler.calls.get('/429')}, err={err}")

MockHandler.calls.clear()
c, rc, err = llm_call("/400_no_usage", retries=3)
check("A2 400 stream_options 降级重试成功", c == "乙" and MockHandler.calls.get("/400_no_usage") == 2, f"err={err}")

MockHandler.calls.clear()
c, rc, err = llm_call("/trunc", retries=3)
check("A3 finish_reason=length 截断视为失败并重试", c == "完整" and MockHandler.calls.get("/trunc") == 2, f"err={err}")

MockHandler.calls.clear()
c, rc, err = llm_call("/empty", retries=3)
check("A4 空响应重试", c == "有内容" and MockHandler.calls.get("/empty") == 2, f"err={err}")

MockHandler.calls.clear()
c, rc, err = llm_call("/short", retries=3, min_len=8)
check("A5 min_len 过短重试", c == "足够长的回复内容满足下限要求" and MockHandler.calls.get("/short") == 2, f"err={err}")

MockHandler.calls.clear()
c, rc, err = llm_call("/chinese_split", retries=1)
check("A6 SSE 中文 3 字节分帧解析完整", c == "雨巷青石板上雨水滴答滴答落个不停，夜风穿过长廊", f"{c[:20]} err={err}")

MockHandler.calls.clear()
c, rc, err = llm_call("/tail", retries=1)
check("A7 [DONE] 后尾帧丢弃", c == "前段", f"{c} err={err}")

MockHandler.calls.clear()
c, rc, err = llm_call("/always429", retries=2)
check("A8 持续 429 重试耗尽抛错", c == "" and rc != 0 and "429" in err, f"rc={rc} err={err[-120:]}")

server.shutdown()

# ============ B. split_segments 数字与章节识别 ============
print("\n== B. split_segments 边界 ==")
sys.path.insert(0, os.path.join(SKILL, "scripts"))
from split_segments import CHAPTER_RE, cn2ar, find_chapter_lines  # noqa: E402

cases = {"一千二百三十": 1230, "一万零五": 10005, "十万": 100000, "三万二千": 32000,
         "二〇二四": 2024, "二十三": 23, "一百零三": 103, "一千零821": 1821,
         "一零二四": 1024, "十": 10, "两万三千": 23000, "三百": 300, "五": 5}
ok = all(cn2ar(k) == v for k, v in cases.items())
bad = [k for k, v in cases.items() if cn2ar(k) != v]
check("B1 cn2ar 13 组边界全对", ok, f"错误: {bad} → {[(k, cn2ar(k)) for k in bad]}")

check("B2 CHAPTER_RE 序章前瞻（后跟标点）",
      bool(CHAPTER_RE.match("序章 月下"), ) and bool(CHAPTER_RE.match("番外。")),
      "序章/番外应命中")
check("B3 CHAPTER_RE 不误判'番外内容,继续写正文'",
      not CHAPTER_RE.match("番外内容,继续写正文"), "前瞻约束")
check("B4 CHAPTER_RE 千章级", bool(CHAPTER_RE.match("第一千二百三十章 风起")))

# 编号列表不应误判为章节；带章节标记的编号仍应保留
body = ["第一章 开端", "他走进茶馆。", "1. 他说：要点茶。", "2. 小二应声。", "1. 第三章 转折"]
chs = find_chapter_lines(body)
check("B5 行首数字列表不再误判章节", [c[1] for c in chs] == ["第一章 开端", "1. 第三章 转折"], str([c[1] for c in chs]))

# ============ C. safe_name ============
print("\n== C. safe_name ==")
from engine.common import safe_name  # noqa: E402
check("C1 路径逃逸被清洗", safe_name("..\\..\\evil") == ".._.._evil" and "/" not in safe_name("a/b"), safe_name("..\\..\\evil"))
check("C2 空名兜底", safe_name("   ") == "未命名")
long_name = "长" * 100
check("C3 60 字符截断", len(safe_name(long_name)) == 60)
check("C4 非法字符清洗", ":" not in safe_name("名字:带冒号") and "*" not in safe_name("a*b"))

# ============ D. index_db ============
print("\n== D. index_db ==")
from engine.index_db import IndexDB  # noqa: E402
tmp = tempfile.mkdtemp()
try:
    seg = os.path.join(tmp, "seg.md")
    with open(seg, "w", encoding="utf-8") as f:
        f.write("内容A")
    out = os.path.join(tmp, "out.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("产物")
    with IndexDB(tmp) as db:
        check("D1 初始 should_skip=False", not db.should_skip(seg, out))
        db.mark_done(seg, out, 100, 50)
        check("D2 done+文件在+hash 一致 → 跳过", db.should_skip(seg, out))
        with open(seg, "w", encoding="utf-8") as f:  # 内容变更
            f.write("内容B（追更）")
        check("D3 段内容变更 → 重跑", not db.should_skip(seg, out))
        db.mark_done(seg, out)
        check("D4 变更后重标记 → 跳过", db.should_skip(seg, out))
        # 断点库被清但段文件在 → mark_done UPSERT 应插入
        db.conn.execute("DELETE FROM segment_tasks")
        db.conn.commit()
        db.mark_done(seg, out, 7, 3)
        row = db.get(seg)
        check("D5 行不存在时 mark_done 插入（UPSERT）", row is not None and row[2] == "done")
        # 迁移：旧库无新列
        db.conn.execute("ALTER TABLE segment_tasks DROP COLUMN seg_hash")
        db.conn.execute("ALTER TABLE segment_tasks DROP COLUMN prompt_tokens")
        db.conn.execute("ALTER TABLE segment_tasks DROP COLUMN completion_tokens")
        db.conn.commit()
    with IndexDB(tmp) as db2:
        cols = {r[1] for r in db2.conn.execute("PRAGMA table_info(segment_tasks)").fetchall()}
        check("D6 _migrate 自动补列", {"seg_hash", "prompt_tokens", "completion_tokens"} <= cols)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ============ E. aggregate.parse_verdict ============
print("\n== E. parse_verdict 防御 ==")
from aggregate import norm_text, parse_verdict, verify_citation  # noqa: E402

v, i, p, dropped = parse_verdict("""
## 资产裁决
| 组ID | 类型 | 主名 | 等级 | 合并 | 别名 | 绑定 | 处理 | 备注 |
|---|---|---|---|---|---|---|---|---|
| G01 | 人物 | 沈昭 | S | | | | | |
| G02 | 场景 | （空） | A | | | | | |
| G03 | 坏类型 | 李四 | S | | | | | |
| G04 | 道具 | 玉牌 | A | | | | 附录 | 孤证 |
| G05 | 人物 | 王五 | | | | | 附录 | |
| G06 | 场景 | （无） | | | | | 附录 | |
""")
check("E1 正常行解析", len(v) == 4, f"v={len(v)}, dropped={dropped}")
check("E2 正式行占位主名 → dropped", dropped == 2, f"dropped={dropped}（应为 G02/G03 两行）")
check("E3 附录行占位主名回填组ID", any(x["名称"] == "G06" for x in p), str(p))
check("E4 附录行带名保留", any(x["名称"] == "玉牌" for x in p), str(p))

# 待确认节占位过滤
v2, _, p2, _ = parse_verdict("""
## 待确认
| 名称 | 类型 | 说明 |
|---|---|---|
| （无） | 人物 | 占位 |
| 沈昭 | 人物 | 真实 |
""")
check("E5 待确认节占位行过滤", len(p2) == 1 and p2[0]["名称"] == "沈昭", str(p2))

# verify_citation 前缀截断
texts = {"seg.md": norm_text("第1章\n沈昭推开门走进茶馆，店小二迎上来问好。\n")}
check("F1 出处精确匹配通过", verify_citation({"段文件": "seg.md", "摘句": "沈昭推开门走进茶馆，店小二迎上来问好。"}, texts)[0])
check("F2 前缀截断（90%）匹配通过", verify_citation({"段文件": "seg.md", "摘句": "沈昭推开门走进茶馆，店小二迎上来问好。（多了尾巴）"}, texts)[0])
check("F3 摘句不在原文 → 失败", not verify_citation({"段文件": "seg.md", "摘句": "完全不存在的句子内容。"}, texts)[0])
check("F4 摘句过短 → 失败", not verify_citation({"段文件": "seg.md", "摘句": "一"}, texts)[0])

# ============ G. check_costumes ============
print("\n== G. check_costumes ==")
sys.path.insert(0, os.path.join(SKILL, "服饰设计", "scripts"))
from check_costumes import check_role, load_standard_lib, main_color_hex, parse_hex  # noqa: E402
lib_text = """# 服饰标准库
## 配色板
主色：#E63946 辅色：#1D3557 #457B9D 点缀：#F1FAEE

## 剧种黑名单（玄幻仙侠）: 机甲、机械装甲、赛博义体、现代枪械
## 用户额外禁忌: 露肩装
"""
_lib_fd, _lib_path = tempfile.mkstemp(suffix=".md")
os.close(_lib_fd)
with open(_lib_path, "w", encoding="utf-8") as f:
    f.write(lib_text)
palette, blacklist = load_standard_lib(_lib_path)
check("G1 黑名单/配色板提取", "E63946" in palette and {"机甲", "露肩装"} <= blacklist, str(blacklist))

role_ok = """## 角色 沈昭
主色：#E63946
身高 178cm，6.5 头身，体型修长
生图提示词：左半区域居中展示角色面部特写，右半区域整齐排布角色三张全身设定图，包含正面、侧面及背面三个维度的全身站姿视图
"""
f = check_role("沈昭", role_ok, palette, blacklist)
check("G2 合规角色无阻断无警告", not f, str(f))

role_bad = """## 角色 反派
主色：#E63946
身高 170cm
身穿机甲装甲，装配赛博义体，参考露肩装设计
（禁忌句测试：严禁出现机甲装甲元素——本段禁止使用任何枪械造型）
生图提示词：正视图
"""
f = check_role("反派", role_bad, palette, blacklist)
sevs = [s for s, _ in f]
check("G3 黑名单命中 → 阻断（含'严禁'句剥离后仍命中）", "阻断" in sevs, str(f))
role_bad_pal = """## 角色 撞色
主色：#E63946 装饰：#123456
身高 170cm，6.5 头身
生图提示词：三视图包含正面、侧面及背面三个维度
"""
out_pal = parse_hex(role_bad_pal) - palette
check("G4 配色出界警告（#123456 不在板内）", out_pal == {"123456"}, f"出界: {out_pal}")
check("G5 头身比缺失警告", "头身比" in str(f))
check("G6 三视图缺失警告", "三视图" in str(f) or "角度" in str(f))

role_bad2 = """## 角色 另一反派
主色：#E63946
身高 170cm，6.5 头身
生图提示词：三视图包含正面、侧面及背面三个维度
"""
f2 = check_role("另一反派", role_bad2, palette, blacklist)
check("G7 合规（含主色撞车候选）", not f2, str(f2))

# 主色撞车检测（main_color_hex 复用主流程逻辑）
m1, m2 = main_color_hex(role_ok), main_color_hex(role_bad2)
check("G8 主色撞车可检出（两角色同 #E63946）", m1 == m2 == "E63946", f"{m1} {m2}")

# 回归观察：含「避免不了」的正常叙述仍必须检测黑名单词
role_trap = "## 角色 甲\n主色：#1D3557\n生图提示词：他避免不了这身机甲。\n"
f3 = check_role("甲", role_trap, palette, blacklist)
check("G9 '避免不了'叙述仍命中黑名单阻断", any(sev == "阻断" for sev, _ in f3), str(f3))

# ============ H. style / 剧种 / schedule 解析 ============
print("\n== H. 画风/剧种/调度解析 ==")
from engine import style as s  # noqa: E402

card, mode = s.resolve_style("古风")
check("H1 古风 → A1 卡片", mode == "card" and card["id"] == "A1", f"{mode} {card and card['id']}")
card, mode = s.resolve_style("仙侠")
check("H2 仙侠 → A2", mode == "card" and card["id"] == "A2")
card, mode = s.resolve_style("玄幻")
check("H3 玄幻 → H1（v2.4 兼容映射）", mode == "card" and card["id"] == "H1", f"{card and card['id']}")
c2, m2 = s.resolve_style(None, "仙侠奇幻风 但要更写实")
check("H4 前缀规则：名称+追加内容 → 自由描述", m2 == "free" and c2 is None, f"{m2}")
c3, m3 = s.resolve_style(None, "国风水墨")
check("H5 精确名命中 B1", m3 == "card" and c3["id"] == "B1", f"{c3 and c3['id']}")
c4, m4 = s.resolve_style(None, "中式国漫3D 基础风")
check("H6 标题带括号后缀前缀命中 A1", m4 == "card" and c4["id"] == "A1", f"{c4 and c4['id']}")

# 剧种判定（prepare_costume_input 逻辑复现）
from prepare_costume_input import load_genre_map  # noqa: E402
gmap = load_genre_map()
check("H7 剧种精确命中", gmap.get("玄幻") == ("玄幻仙侠", False), str(gmap.get("玄幻")))
hits = [k for k in gmap if k in "都市玄幻"]
k = min(hits, key=lambda x: ("都市玄幻".index(x), -len(x)))
check("H8 都市玄幻 → 子串兜底取位置靠前", gmap[k][0] == "都市现代", f"{k} → {gmap[k]}")
check("H9 完全未命中 → None", gmap.get("美食种田文") is None)

# check_schedule 解析（复用 e2e 快照）
from check_schedule import parse_asset_names, split_names  # noqa: E402
snap = open(os.path.join(SKILL, "测试", "e2e", "资产库", "反推_最终.md"), encoding="utf-8").read()
names, pending, aliases = parse_asset_names(snap)
check("H10 快照反推解析出人物表", len(names.get("人物", [])) >= 1, f"类型: {list(names.keys())} 人物: {names.get('人物', [])}")
check("H11 占位排除不误伤'无名氏'", "无名氏" in split_names("无名氏") or "无名氏" not in {"无", "无出场", "未知"}, "")

server.server_close()
n = PASS
print(f"\n结果: {n} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项:", FAIL)
sys.exit(1 if FAIL else 0)
