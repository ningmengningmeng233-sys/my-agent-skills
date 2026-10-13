import json


def _scene_text(lock_line: str) -> str:
    """造一份只差负面锁写法的场景产出（其余硬要求全部满足）。"""
    return (
        "【场景】临河茶楼\n"
        "景别：远景；角度：平视；布光：自然柔光\n"
        "负面提示词：" + lock_line + "\n"
    )


def test_negative_lock_accepts_equivalent_forms():
    """要求里的 `无低精度/塑料感` 含斜杠，模型写成等价自然写法也必须算合规。

    实测这是高频误判点：反代 Gemini 口径下场景 13 项全部因它被判「缺负面锁」而丢弃。
    """
    from engine import prompt_rules
    for written in ("无人物、无低精度、无塑料感、无光照均匀平坦",
                    "无人物、无低精度塑料感、无光照均匀平坦"):
        ok, missing = prompt_rules.check_compliance(_scene_text(written), "场景")
        assert ok, f"{written!r} 应判合规，实际缺失 {missing}"


def test_prop_negative_lock_accepts_equivalent_forms():
    from engine import prompt_rules
    text = "【道具】油纸伞\n正面图、侧面图、背面图\n素灰背景 #B8B8B8\n" \
           "负面提示词：无人物、无肢体、无复杂场景背景\n"
    ok, missing = prompt_rules.check_compliance(text, "道具")
    assert ok, missing


def test_prop_negative_lock_rejects_half_negated_form():
    """「无人物、肢体」只否定了人，肢体仍被正面提及——不算完整否定，不得过闸。"""
    from engine import prompt_rules
    text = "【道具】油纸伞\n正面图、侧面图、背面图\n素灰背景 #B8B8B8\n" \
           "负面提示词：无人物、肢体、无复杂场景背景\n"
    ok, missing = prompt_rules.check_compliance(text, "道具")
    assert not ok
    assert "负面锁" in missing


def test_slashed_token_still_accepted():
    """原字面量写法必须继续算合规（不许修出新回归）。"""
    from engine import prompt_rules
    ok, missing = prompt_rules.check_compliance(
        _scene_text("无人物、无低精度/塑料感、无光照均匀平坦"), "场景")
    assert ok, missing


def test_negative_lock_still_rejects_truly_missing():
    """真正缺失时不放水——闸门不许因为这次修改而失效。"""
    from engine import prompt_rules
    ok, missing = prompt_rules.check_compliance(_scene_text("无现代元素、无文字"), "场景")
    assert not ok
    assert "负面锁" in missing


def test_matcher_shared_by_checker_and_generator():
    """生成闸与事后检查器必须共用同一个匹配函数（单一真相源）。"""
    import scripts.check_schedule as cs
    from engine import prompt_rules
    assert cs.token_satisfied is prompt_rules.token_satisfied


def test_negative_lock_rejects_semantically_reversed_writing():
    """等价写法只收「完整否定」。语义相反（"塑料感"前面没有"无"）必须仍然判不合格。

    对抗评审 B1 的反例：「无人物、无低精度、塑料感明显、无光照均匀平坦」——
    若把「无低精度」与「塑料感」当成两个相邻片段收进等价组，这句会被误判为合规。
    """
    from engine import prompt_rules
    ok, missing = prompt_rules.check_compliance(
        _scene_text("无人物、无低精度、塑料感明显、无光照均匀平坦"), "场景")
    assert not ok, "语义相反的写法不得过闸"
    assert "负面锁" in missing


def test_rules_have_single_source():
    """检查器与生成端必须引用同一份规则常量（禁止两套口径）。"""
    import scripts.check_schedule as cs
    from engine import prompt_rules
    assert cs.SCENE_LAYER_TOKENS == prompt_rules.SCENE_LAYER_TOKENS
    assert cs.MAX_IDEOMATIC_MARKS == prompt_rules.MAX_IDEOMATIC_MARKS


def test_generation_requirements_cover_five_rules():
    from engine import prompt_rules
    scene = "\n".join(prompt_rules.generation_requirements("场景"))
    prop = "\n".join(prompt_rules.generation_requirements("道具"))
    person = "\n".join(prompt_rules.generation_requirements("人物"))
    assert "镜头体系" in scene
    assert "三视图" in prop
    assert "负面" in person
    assert "顿号" in scene + prop + person          # 标点约束对全部类型可见
    assert "雷同" in scene + prop + person


def test_request_payload_carries_requirements(fake_endpoint, monkeypatch):
    """生成请求里必须真的带上要求（不是只写在文档里）。"""
    monkeypatch.setenv("SUMMARY_API_URL", fake_endpoint.url)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("SUMMARY_API_MODEL", "m")
    fake_endpoint.script([{"text": "生成结果"}])
    from engine import llm_pool
    from engine import prompt_rules
    pool = llm_pool.from_env(lib=None)
    pool.call("系统提示", "用户提示", step="基础提示词",
              extra_requirements=prompt_rules.generation_requirements("场景"))
    sent = fake_endpoint.received()[-1]
    assert "镜头体系" in json.dumps(sent, ensure_ascii=False)


def test_dunhao_requirement_text_matches_checker_limit():
    """文案里的顿号数字必须等于判据实际容许的上限。

    判据是 `s.count("、") >= MAX_IDEOMATIC_MARKS` → 恰好 MAX 个即违规，
    **实际只容许 MAX-1 个**。文案若写 MAX，模型照写就被自己的判据拒掉，平白返工。
    """
    from engine import prompt_rules
    allowed = prompt_rules.DUNHAO_ALLOWED_MAX
    assert allowed == prompt_rules.MAX_IDEOMATIC_MARKS - 1
    for atype in ("人物", "场景", "道具"):
        text = "\n".join(prompt_rules.generation_requirements(atype))
        assert f"不得超过 {allowed} 个" in text, f"{atype} 文案未用实际容许上限：{text}"
        assert f"不得超过 {prompt_rules.MAX_IDEOMATIC_MARKS} 个" not in text


def test_dunhao_boundary_agrees_with_requirement_text():
    """判据边界与文案一致：恰好 allowed 个合规，多一个即违规。"""
    from engine import prompt_rules
    allowed = prompt_rules.DUNHAO_ALLOWED_MAX
    ok = "景别：全景" + "、层次" * allowed
    bad = "景别：全景" + "、层次" * (allowed + 1)
    assert ok.count("、") == allowed
    assert prompt_rules.dunhao_violations(ok) == []
    assert len(prompt_rules.dunhao_violations(bad)) == 1


def test_desc_compliance_is_shared_single_source():
    """描述判据只有一份定义：check_schedule 引用 prompt_rules，不另写一套（工单 T2）。

    背景：描述模板要求输出「负面影响限制」段、校验器也查它，唯独**生成时不检查**
    （`guard_on` 只含基础/衍生提示词）→ 35 项漏 1 项，白跑到 verify 才发现。
    修法是让生成侧能复用同一份判据，所以先把它变成单一真相源。
    """
    from engine import prompt_rules
    from check_schedule import DESC_CHECKS
    assert DESC_CHECKS is prompt_rules.DESC_COMPLIANCE_MARKERS


def test_desc_compliance_requires_negative_limit():
    from engine import prompt_rules
    ok, missing = prompt_rules.check_desc_compliance("面部骨相：方圆。\n", "人物")
    assert not ok and any("负面限制" in m for m in missing), missing
    ok2, missing2 = prompt_rules.check_desc_compliance(
        "面部骨相：方圆，脸型：鹅蛋，肤色：冷白。\n负面限制：无网红脸、无随机换脸。\n", "人物")
    assert ok2, missing2


def test_desc_compliance_per_type_fields():
    """三类各自的必填字段（与 check_schedule 的三组标记同源）。"""
    from engine import prompt_rules
    assert prompt_rules.check_desc_compliance("景别：远景，角度：平视，布光：冷白。\n负面限制：无人物。\n", "场景")[0]
    assert prompt_rules.check_desc_compliance("基材：素灰。\n负面限制：无人物肢体。\n", "道具")[0]
    # 反例：场景缺镜头预写、道具缺材质身份，都必须判不合格
    assert not prompt_rules.check_desc_compliance("负面限制：无人物。\n", "场景")[0]
    assert not prompt_rules.check_desc_compliance("负面限制：无人物肢体。\n", "道具")[0]


def test_desc_compliance_unknown_type_falls_back_to_person():
    from engine import prompt_rules
    ok, missing = prompt_rules.check_desc_compliance("面部骨相：方圆。\n负面限制：x\n", "不存在的类型")
    assert ok, missing


def test_abstract_word_list_is_single_source():
    """词表只有一份定义：校验器引用 prompt_rules，不另写一套（否则生成闸与校验器会分叉）。"""
    from engine import prompt_rules
    from check_schedule import ABSTRACT_WORDS
    assert ABSTRACT_WORDS is prompt_rules.ABSTRACT_WORDS
    for word in ("极致", "唯美", "震撼"):
        assert word in prompt_rules.ABSTRACT_WORDS


def test_generation_requirements_announce_abstract_words():
    """生成请求必须把词表原样告知模型——实测模型写过「极致/唯美」，而请求里一个字都没提。"""
    from engine import prompt_rules
    for atype in ("人物", "场景", "道具"):
        text = "\n".join(prompt_rules.generation_requirements(atype))
        assert "抽象空洞词" in text, atype
        assert "极致" in text and "唯美" in text, atype


def test_abstract_word_hits_exempts_negative_and_metadata_lines():
    """判定口径与检查器一致：负面提示词行（那里的词是禁令）与【】元数据行豁免。"""
    from engine import prompt_rules
    assert prompt_rules.abstract_word_hits(
        "## 001. 甲\n负面提示词：无极致滤镜、无唯美光效。\n【资产编号：CH_甲】\n") == []
    assert prompt_rules.abstract_word_hits("## 001. 甲\n气质唯美、光影极致。\n") == ["唯美", "极致"]


def test_abstract_word_hits_matches_checker_output():
    """生成闸与校验器对同一段文本必须给出同一结论（分叉过就白跑一趟）。"""
    from engine import prompt_rules
    from check_schedule import check_ai_flavor
    text = "## 001. 甲\n画面极致震撼，气质唯美。\n"
    hits = prompt_rules.abstract_word_hits(text)
    assert hits, "这段必须命中"
    label = [l for l in check_ai_flavor(text) if "抽象空洞词" in l]
    assert label and all(w in label[0] for w in hits)


def test_desc_compliance_covers_dunhao_limit():
    """描述自检必须含单行顿号上限——校验器对描述也按这条查（实测：道具描述被它拦在 verify）。"""
    from engine import prompt_rules
    ok_text = "面部骨相：方圆，脸型：鹅蛋，肤色：冷白。\n负面限制：无网红脸。\n"
    assert prompt_rules.check_desc_compliance(ok_text, "人物")[0]
    dense = "面部骨相：方圆，脸型：鹅蛋，肤色：冷白。\n" \
            + "负面限制：" + "、".join(f"无要素{i}" for i in range(12)) + "\n"
    # 负面清单行豁免（与检查器同口径）
    assert prompt_rules.check_desc_compliance(dense, "人物")[0], "负面行豁免顿号"
    dense_body = "面部骨相：方圆，脸型：鹅蛋，肤色：冷白。" + "、".join(f"细节{i}" for i in range(12)) \
                 + "\n负面限制：无网红脸。\n"
    ok, missing = prompt_rules.check_desc_compliance(dense_body, "人物")
    assert not ok and any("顿号超限" in m for m in missing), missing


def test_exempt_line_ignores_list_number_prefix():
    """豁免判定必须先剥列表序号。

    描述模板写的是 `7. 负面限制：负面提示词：无人物、无肢体、…`——带序号时按前缀匹配会漏掉豁免，
    于是**负面清单被判成"顿号罗列过密"命中**（实测：道具描述.md#灯笼 就是这样被拦住的，属判据误报）。
    """
    from engine import prompt_rules
    for line in ("7. 负面限制：负面提示词：无人物、无肢体、无手部",
                 "- 负面提示词：无人物、无肢体",
                 "负面限制：无网红脸",
                 "1) 负面提示词：无人物",
                 "【资产编号：CH_甲｜稳定键：x】"):
        assert prompt_rules.is_exempt_line(line), line
    for line in ("7. 建模层：次世代高精度建模，布料、皮革、金属、陶土、木料、丝",
                 "面部骨相：方圆，脸型：鹅蛋"):
        assert not prompt_rules.is_exempt_line(line), line


def test_numbered_negative_line_is_not_a_dunhao_violation():
    """**反例在场**：非豁免的正文行顿号超限仍必须被判违规（豁免不能变成整条规则失效）。"""
    from engine import prompt_rules
    negative = "7. 负面限制：负面提示词：" + "、".join(f"无要素{i}" for i in range(12))
    assert prompt_rules.dunhao_violations(negative) == []
    body = "1. 材质层：" + "、".join(f"细节{i}" for i in range(12))
    assert prompt_rules.dunhao_violations(body), "正文行顿号超限仍须判违规"


def test_style_lock_layer_checks_are_single_source():
    """锁定分层判据只有一份：校验器与生成侧共用 prompt_rules 的实现。

    标签名＝分层行冒号前的原文（真实 A2 卡的分层依次是 建模层/渲染层/场景-背景/镜头），
    所以实测报的是「缺画风锁定段-**镜头**」。夹具照抄真实命名，避免测试与产线口径分叉。
    """
    from engine import prompt_rules
    from check_schedule import build_style_lock_checks
    card = {"id": "A2", "name": "仙侠奇幻风", "pack": "", "layers": [
        "1. 建模层：次世代高精度建模", "2. 渲染层：物理渲染材质", "4. 镜头：大气构图与留白"]}
    shared = prompt_rules.style_lock_layer_checks(card, "人物")
    via_checker = [c for c in build_style_lock_checks(card, "人物", "古风") if isinstance(c[1], tuple)]
    tags = [t for t, _ in shared]
    assert "画风锁定段-建模层" in tags, tags
    assert "画风锁定段-渲染层" in tags, tags
    assert "画风锁定段-镜头" in tags, tags
    for item in shared:
        assert item in via_checker, f"{item} 未出现在校验器的检查集里"
    # 道具跳过镜头层（SKIP_LAYERS_BY_TYPE），人物不跳
    assert not any("镜头" in t for t, _ in prompt_rules.style_lock_layer_checks(card, "道具"))
    assert not prompt_rules.style_lock_layer_checks(None, "人物")
