# -*- coding: utf-8 -*-
"""mock OpenAI 兼容 API（单一来源）：e2e_flow 与发布级迁移/追更 runner 共用。

按请求特征返回对应阶段产物；摘句来自真实带行号原文，保证出处校验通过。
"""
import http.server
import json
import re
import threading
import time


def start_mock_server():
    """启动本地 mock 服务，返回 (server, base_url)。"""
    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"


RENDER_LOCK_A2 = ("渲染锁定：\n"
                  "1. 建模层：次世代高精度建模，物理渲染材质，真实布料/丝绸/金属/皮革质感，细腻毛发与皮肤纹理，八K超高清\n"
                  "2. 渲染层：写实三维渲染，真实光影，柔和主光 + 轮廓光/边缘光，玉清色调与神光氛围，体积光/氛围光，动态明暗对比，高饱和度色彩，拒绝非写实渲染/赛璐璐/卡通渲染/清晰轮廓描边/粗描边/平涂/硬边阴影色块/二维平面感\n"
                  "3. 场景/背景：云雾洞天、法阵符文与仙山按剧本描写，色调服从玉清与神光氛围\n"
                  "4. 镜头：大气构图与留白，史诗/写意镜头语言，御剑飞行与法阵等仙侠标志性画面按剧本取用")
IDENTITY_LOCK = "身份锁：脸型骨相鹅蛋脸清秀，肤色白皙肤质细腻，眉形细长，眼型杏眼温和，瞳孔色黑棕，鼻梁挺直，唇形饱满，发型高束发黑发，无疤痕痣胎记。"


def make_seg_products(user_prompt):
    """从请求体提取带行号原文，构造真实摘句（保证出处校验通过）。返回三张表。"""
    lines = []
    for ln in user_prompt.splitlines():
        m = re.match(r"^\d{5}: (.+)$", ln)
        if m:
            lines.append((int(ln[:5]), m.group(1)))
    if not lines:
        return "## 章节摘要表\n| 章节 | 概要 | 人物 | 场景 | 道具 | 服装 |\n|---|---|---|---|---|---|\n"
    # 章与剧本场次（第N场）都算章节单元，与拆段层 CHAPTER_RE 对齐
    chs = [(no, t) for no, t in lines if re.match(r"^第[一二三四五六七八九十0-9]+[章场]", t.strip())]
    summary_rows = []
    for i, (no, t) in enumerate(chs):
        end = chs[i + 1][0] if i + 1 < len(chs) else lines[-1][0]
        nxt = next((x for x, y in lines if x > no and x < end), None)
        summary_rows.append((t.strip(), nxt))
    recs = []
    seen_names = set()
    for no, t in lines:
        if not t.strip() or len(t.strip()) < 4 or no == lines[0][0]:
            continue
        text = t.strip()
        if text.startswith("第") and re.match(r"^第[一二三四五六七八九十0-9]+[章场]", text):
            continue
        for pat, typ in ((r"([一-龥]{2,4}(?:姑娘|公子|少年|老者|夫人|大夫|掌柜|船夫))", "人物"),
                         (r"([一-龥]{2,6}(?:街|厅|室|楼|城|码头|茶楼|客栈|天井))", "场景"),
                         (r"([一-龥]{0,1}(?:琴|剑|伞|壶|簪|灯|算盘|书|屏风|木门))", "道具")):
            nm = re.search(pat, text)
            if nm and nm.group(1) not in seen_names:
                seen_names.add(nm.group(1))
                recs.append((typ, nm.group(1), no, text[:35]))
        if len(recs) >= 10:
            break
    if not recs:
        recs = [("人物", "测试角色", lines[1][0], lines[1][1][:35])]
    rec_table = ["## 资产出现记录表",
                 "| 类型 | 名称 | 别名 | 出现章节 | 摘句 | 行号 | 备注 |",
                 "|---|---|---|---|---|---|---|"]
    ch_label = chs[0][1] if chs else "第1章"
    for typ, nm, no, quote in recs:
        rec_table.append(f"| {typ} | {nm} | | {ch_label} | {quote} | {no:05d} | |")
    sum_table = ["## 章节摘要表", "| 章节 | 概要 | 人物 | 场景 | 道具 | 服装 |", "|---|---|---|---|---|---|"]
    for i, (t, nxt) in enumerate(summary_rows):
        sum_table.append(f"| {t} | 本段剧情概要。 | {recs[0][1] if recs else ''} | | | |")
    need_table = ["## 变化需求记录表", "| 类型 | 资产 | 需求 | 章节 | 摘句 | 行号 |", "|---|---|---|---|---|---|"]
    scene_rec = next((r for r in recs if r[0] == "场景"), None)
    if chs and len(lines) > 1 and scene_rec:
        need_table.append(f"| 场景 | {scene_rec[1]} | 黄昏 | {ch_label} | {scene_rec[3]} | {scene_rec[2]:05d} |")
    return "\n".join(sum_table) + "\n" + "\n".join(rec_table) + "\n" + "\n".join(need_table)


def make_candidate(user_prompt, seg_path):
    lines = []
    for ln in user_prompt.splitlines():
        m = re.match(r"^\d{5}: (.+)$", ln)
        if m:
            lines.append(m.group(1))
    cands = []
    for t in lines:
        for pat, typ in ((r"([一-龥]{2,4}(?:姑娘|公子|少年|老者|掌柜|船夫))", "人物"),
                         (r"([一-龥]{2,6}(?:街|厅|室|楼|码头|茶楼|客栈|天井))", "场景"),
                         (r"([一-龥]{0,1}(?:琴|剑|伞|壶|簪|灯|算盘|书|屏风))", "道具")):
            m = re.search(pat, t)
            if m and m.group(1) not in [c[0] for c in cands]:
                cands.append((m.group(1), typ))
        if len(cands) >= 4:
            break
    if not cands:
        cands = [("测试角色", "人物")]
    out = ["## 候选主名表", "| 候选主名 | 别名 | 首现章 | 一句定性 | 类别 |", "|---|---|---|---|---|"]
    for nm, typ in cands:
        out.append(f"| {nm} | | 第1章 | {typ} | {typ} |")
    return "\n".join(out)


class Handler(http.server.BaseHTTPRequestHandler):
    # 实测计数：基准脚本靠它拿到「文本调用次数」，而不是靠估算或声称。
    call_count = 0
    call_lock = threading.Lock()
    # 端点延迟注入（性能基准专用）：默认 0，不改变任何既有调用的时序。
    # 真实模型端点每次调用要 1–10 秒；本地 mock 是瞬时的，不注入延迟就测不出
    # 「少调用 17 次」在墙钟上的意义。
    endpoint_delay_ms = 0.0

    @classmethod
    def reset_calls(cls) -> None:
        with cls.call_lock:
            cls.call_count = 0

    @classmethod
    def calls(cls) -> int:
        with cls.call_lock:
            return cls.call_count

    def do_POST(self):
        with Handler.call_lock:
            Handler.call_count += 1
        if Handler.endpoint_delay_ms:
            time.sleep(Handler.endpoint_delay_ms / 1000.0)
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        user_prompt = ""
        try:
            req = json.loads(raw.decode("utf-8"))
            for msg in req.get("messages", []):
                user_prompt += chr(10) + msg.get("content", "")
        except Exception:
            pass
        seg_path = ""
        m = re.search(r"路径: (.+)", user_prompt)
        if m:
            seg_path = m.group(1).strip()
        if "候选主名表" in user_prompt and "合并" not in user_prompt:
            content = make_candidate(user_prompt, seg_path)
        elif "表 A（" in user_prompt and "合并" in user_prompt:
            names = re.findall(r"\| ([^|]+?) \| [^|]*? \| 第\d+章", user_prompt)
            out = ["## 候选主名表", "| 候选主名 | 别名 | 首现章 | 一句定性 | 类别 |", "|---|---|---|---|---|"]
            for i, n in enumerate(dict.fromkeys(n.strip() for n in names if n.strip())):
                out.append(f"| {n} | | 第1章 | 合并定性 | 合并 |")
            content = "\n".join(out)
        elif "资产出现记录表" in user_prompt:
            content = make_seg_products(user_prompt)
        elif "资产裁决" in user_prompt:
            gids = re.findall(r"### (G\d{2}) \[[^\]]+\] 候选主名: ([^ |]+)", user_prompt)
            rows = ["## 资产裁决", "| 组ID | 类型 | 主名 | 等级 | 合并 | 别名 | 绑定 | 处理 | 备注 |", "|---|---|---|---|---|---|---|---|---|"]
            types = {}
            for gid, name in gids:
                m2 = re.search(rf"### {gid} \[([^\]]+)\]", user_prompt)
                types[gid] = m2.group(1) if m2 else "道具"
            for gid, name in gids:
                rows.append(f"| {gid} | {types.get(gid, '道具')} | {name} | B | | | | 保留 | mock 裁决 |")
            rows += ["", "## 项目基础信息", "| 项目名称 | 测试全流程小说 |", "| 集数/章节数 | 5 |", "| 题材类型 | 古风 |", "| 时代背景 | 古代 |", "| 核心设定 | 测试 |", "| 主线剧情 | 测试主线。 |", "| 视觉风格 | 国漫三维 |", "", "## 待确认", "| 名称 | 类型 | 说明 |"]
            content = "\n".join(rows)
        elif "资产 × 变体" in user_prompt:
            # 衍生提示词（先于人物/场景/道具基础分支命中）
            m_type = re.search(r"类型: (.+)", user_prompt)
            dtype = m_type.group(1).strip() if m_type else "人物"
            crop_mode = "裁头版" in user_prompt and "脸的唯一真相" in user_prompt
            render_lock = RENDER_LOCK_A2
            if dtype.startswith("人物"):
                if crop_mode:
                    content = (render_lock + "\n" + IDENTITY_LOCK + "\n整体三格图板布局：左面一格人物正面特写（头顶到锁骨，全脸清晰），"
                               "右面上下两格正视图（全身）+后视图（全身），两格等大、身高比例一致；"
                               "两格全身头部与面部一律裁除不入画，脸的唯一真相是左面正面特写。\n"
                               "左面人物正面特写：正面，中性表情，全脸清晰，素灰背景#B8B8B8，均匀柔光，"
                               "物理渲染材质，写实三维渲染，国漫三维。图中不要有任何文字。\n"
                               "右面正视图与后视图：两格头部与面部一律裁除不入画，从颈根到脚完整入画，"
                               "正视图展示正面全身服装，后视图只露后脑勺发型轮廓与服装背面，"
                               "素灰背景#B8B8B8，均匀柔光，物理渲染材质，写实三维渲染，国漫三维。图中不要有任何文字。\n"
                               "负面提示词：无现代元素、无科技感、无文字、无写实噪点、后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。")
                else:
                    content = (render_lock + "\n" + IDENTITY_LOCK + "\n整体三格图板布局：左面一格人物正面特写（头顶到锁骨，全脸清晰），"
                               "右面上下两格正视图（全身）+后视图（全身），两格等大、身高比例一致；"
                               "两格全身均保留头部（不裁头）。\n"
                               "左面人物正面特写：正面，中性表情，全脸清晰，素灰背景#B8B8B8，均匀柔光，"
                               "物理渲染材质，写实三维渲染，国漫三维。图中不要有任何文字。\n"
                               "右面正视图与后视图：全身立像，从头到脚完整入画，正视图展示正面全身服装与姿态，"
                               "后视图只露后脑勺发型轮廓与服装背面，两格服装与配饰完全一致，"
                               "素灰背景#B8B8B8，均匀柔光，物理渲染材质，写实三维渲染，国漫三维。图中不要有任何文字。\n"
                               "负面提示词：无现代元素、无科技感、无文字、无写实噪点、后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。")
            elif dtype.startswith("场景"):
                content = (render_lock + "\n国漫三维场景衍生提示词。保持基础空间结构与材质色调一致，"
                           "景别：全景；角度：平视；布光：黄昏暖光从西侧低角度照射。"
                           "物理渲染材质，写实三维渲染，国漫三维。画面中无任何人物；单画面；图中不要有任何文字。"
                           "负面提示词：无现代元素、无科技感、无文字、无人物、无低精度/塑料感、无光照均匀平坦。")
            else:
                content = (render_lock + "\n国漫三维道具衍生提示词。正面图、侧面图、背面图三视图保底，"
                           "素灰背景#B8B8B8，均匀柔光，物理渲染材质，写实三维渲染，材质纹理清晰。"
                           "图中不要有任何文字。负面提示词：无现代元素、无科技感、无文字、无人物/肢体、无复杂场景背景。")
        elif "脸部参考图" in user_prompt:
            # 基础提示词-人物（三格图板：左面正面特写 + 右面正视图/后视图）
            m2 = re.search(r"(?:角色|名称): ([^\n]+)", user_prompt)
            name = m2.group(1).strip() if m2 else "测试人物"
            crop_mode = "裁头版" in user_prompt and "脸的唯一真相" in user_prompt
            render_lock = RENDER_LOCK_A2
            if crop_mode:
                content = (render_lock + "\n" + IDENTITY_LOCK + "\n整体三格图板布局：左面一格人物正面特写（头顶到锁骨，全脸清晰），"
                           "右面上下两格正视图（全身）+后视图（全身），两格等大、身高比例一致；"
                           "两格全身头部与面部一律裁除不入画，脸的唯一真相是左面正面特写。\n"
                           f"左面人物正面特写：{name}，正面，中性表情，全脸清晰，素灰背景#B8B8B8，"
                           "均匀柔光，物理渲染材质，写实三维渲染，国漫三维。图中不要有任何文字。\n"
                           f"右面正视图与后视图：{name}，两格头部与面部一律裁除不入画，从颈根到脚完整入画，"
                           "正视图展示正面全身服装与姿态，后视图只露后脑勺发型轮廓与服装背面，"
                           "素灰背景#B8B8B8，均匀柔光，物理渲染材质，写实三维渲染，国漫三维。图中不要有任何文字。\n"
                           "负面提示词：无现代元素、无科技感、无文字、无写实噪点、后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。")
            else:
                content = (render_lock + "\n" + IDENTITY_LOCK + "\n整体三格图板布局：左面一格人物正面特写（头顶到锁骨，全脸清晰），"
                           "右面上下两格正视图（全身）+后视图（全身），两格等大、身高比例一致；"
                           "两格全身均保留头部（不裁头）。\n"
                           f"左面人物正面特写：{name}，正面，中性表情，全脸清晰，素灰背景#B8B8B8，"
                           "均匀柔光，物理渲染材质，写实三维渲染，国漫三维。图中不要有任何文字。\n"
                           f"右面正视图与后视图：{name}，全身立像，从头到脚完整入画，"
                           "正视图展示正面全身服装与姿态，后视图只露后脑勺发型轮廓与服装背面，"
                           "素灰背景#B8B8B8，均匀柔光，物理渲染材质，写实三维渲染，国漫三维。图中不要有任何文字。\n"
                           "负面提示词：无现代元素、无科技感、无文字、无写实噪点、后视图无面部、无额外第四格、无三格身份不一致、无三格服装不一致。")
        elif "主视图" in user_prompt:
            # 基础提示词-场景（镜头填空体系）
            m2 = re.search(r"场景: ([^\n]+)", user_prompt)
            sname = m2.group(1).strip() if m2 else ""
            scene_layers = {
                "茶楼": ("青石板台阶与褪色幌子旗", "木构楼阁飞檐与临街栏杆", "街市屋脊连绵入天际线"),
                "天井": ("石缸水痕与青苔", "四面回廊格窗", "一方天光自井口落下"),
                "码头": ("系船石桩与缆绳", "木栈桥与堆叠货箱", "江面帆影与远山"),
            }
            tri = next((v for k, v in scene_layers.items() if k in sname),
                       ("石板地面与散落杂物", "主体建筑立面", "天际线与天色"))
            # 真实提示词的层次取自每个场景各自的视觉描述；把场景名锚进每一层，
            # 保证不同场景的前/中/后景不逐字雷同（否则命中 check_scene_duplicates 层次雷同硬门）。
            sk = re.sub(r"[^\u4e00-\u9fa5]", "", sname)[:8] or "此景"
            layers = f"前景{sk}入口的{tri[0]}，中景{sk}立面的{tri[1]}，后景{sk}之外的{tri[2]}"
            content = (RENDER_LOCK_A2 + "\n国漫三维写实场景主视图概念图，仙侠奇幻基调，玉清色调，神光氛围。"
                       "景别：全景；角度：平视；布光：清晨暖光从东侧低角度照射，薄雾中可见光路。"
                       + layers + "。明度组织：亮部集中于天光与光路，中间调为木构与石面，暗部收在廊下与檐底，天空留白。物理渲染材质自然磨损，"
                       "体积光洒落，镜头带景深，写实三维渲染。画面中无任何人物；单画面；图中不要有任何文字。"
                       "负面提示词：无现代元素、无科技感、无文字、无人物、无低精度/塑料感、无光照均匀平坦。")
        elif "动态视图规划" in user_prompt:
            # 基础提示词-道具（三视图保底 + 按剧本可选格）
            m2 = re.search(r"道具: ([^\n]+)", user_prompt)
            name = m2.group(1).strip() if m2 else "测试道具"
            prop_profiles = {
                "算盘": "紫檀木框体配黄铜包角，框沿倒角圆润而档杆端头轮廓锐利，算珠油亮包浆只出现在指触部位，铜档与木框榫装接缝可见，算盘扎实落于案面、接触阴影清晰",
                "琴": "桐木面板配鹿角琴徽，岳山边缘锐利而琴体侧边柔和，弦路磨痕集中在按弦区，蚕丝弦缠绕构造可见，通体哑光缎面",
                "旧书": "蓝布函套配素纸书页，书口磨圆而函套折边利落，虫蛀与泛黄集中在书口边缘，线装订线可见，纸面干燥哑光",
            }
            profile = next((v for k, v in prop_profiles.items() if k in name),
                           "主体轮廓完整可读，焦点边缘锐利其余边缘柔和，使用痕迹只在接触点，构造接缝可见，接触阴影清晰")
            content = (RENDER_LOCK_A2 + f"\n国漫三维道具动态视图规划设定图。{name}，正面图、侧面图、背面图三视图保底，{profile}，"
                       f"素灰背景#B8B8B8，均匀柔光，物理渲染材质，写实三维渲染，材质纹理清晰。"
                       f"图中不要有任何文字。"
                       "负面提示词：无现代元素、无科技感、无文字、无人物/肢体、无复杂场景背景。")
        elif "视觉描述" in user_prompt:
            # 描述补全（按类型返回，供 check_schedule 描述层必查）
            m2 = re.search(r"资产名称: ([^\n]+)", user_prompt)
            name = m2.group(1).strip() if m2 else "测试资产"
            m3 = re.search(r"资产类型: ([^\n]+)", user_prompt)
            dtype = m3.group(1).strip() if m3 else "人物"
            if dtype.startswith("人物"):
                content = (f"角色视觉描述。{name}：面部骨相鹅蛋脸，肤色白皙肤质细腻，眉眼温柔含情，鼻梁挺直唇形饱满，"
                           f"体型修长七头身，站姿端庄，发型高束发配玉簪，材质以丝绸锦缎为主辅以薄纱，"
                           f"配色玉清色调，妆容淡雅，符合角色社会背景与年龄设定。"
                           f"专属负面限制：避免写实噪点、比例失调、二维平面感、欧美化五官、"
                           f"非写实渲染/赛璐璐/卡通渲染/清晰轮廓描边/硬边阴影色块。")
            elif dtype.startswith("场景"):
                content = (f"场景视觉描述。{name}：高精度三维建模，写实比例结构，物理渲染材质，真实光影与氛围，"
                           f"景别：全景；角度：平视；布光：清晨暖光从东侧低角度照射；环境具体化：清晨、庭院、"
                           f"可测量尺度明确；标志性视觉符号为木质飞檐斗拱。"
                           f"专属负面限制：无人物、无低精度/塑料感、无光照均匀平坦、"
                           f"避免非写实渲染/赛璐璐/卡通描边/硬边阴影色块/二维平面感。")
            else:
                content = (f"道具视觉描述。{name}：高精度三维建模，写实比例外观，关键细节清晰，"
                           f"材质身份：旧皮革、哑光面、边缘磨白、铜扣氧化；数字信息类道具含国漫界面样式。"
                           f"专属负面限制：无人物肢体、无复杂场景背景、无低质感、"
                           f"避免非写实渲染/赛璐璐/卡通描边/硬边阴影色块/二维平面感。")
        else:
            content = "通用响应。"
        payload = {
            "id": "f",
            "choices": [{"delta": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        body = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        body += "data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *a):
        pass
