import json


def _inject(tmp_path):
    """注入五类缺陷，返回受影响文件路径列表。"""
    d = tmp_path / "提示词" / "基础"
    d.mkdir(parents=True)
    bad_dunhao = "人物提示词.md"
    bad_scene = "场景提示词.md"
    bad_prop = "道具提示词.md"
    # ① 顿号超限（负面行豁免，普通行 12 个顿号）
    f1 = d / bad_dunhao
    f1.write_text("## 001. 张三\n" + "长袍、玉佩、腰带、发簪、长靴、护腕、披风、令牌、面具、手甲、耳环、扳指，皆为国漫三维。\n", encoding="utf-8")
    # ② 场景缺镜头体系（缺角度、布光）
    f2 = d / bad_scene
    f2.write_text("## 001. 茶楼\n景别：全景。物理渲染材质。负面提示词：无人物、无低精度/塑料感、无光照均匀平坦。\n", encoding="utf-8")
    # ③ 道具缺三视图
    f3 = d / bad_prop
    f3.write_text("## 001. 算盘\n正面图、侧面图。负面提示词：无人物/肢体、无复杂场景背景。\n", encoding="utf-8")
    # ④ 负面锁缺失（人物缺负面段）
    f4 = d / "人物提示词2.md"
    f4.write_text("## 002. 李四\n身份锁：鹅蛋脸。三格图板：正面特写、正视图、后视图。\n", encoding="utf-8")
    # ⑤ 层次雷同（同一句换词复读）
    f5 = d / "场景提示词2.md"
    f5.write_text("## 002. 天井\n前景茶楼入口的石阶，中景茶楼入口的栏杆，后景茶楼入口的屋脊。景别：全景。角度：平视。布光：晨光。"
                  "负面提示词：无人物、无低精度/塑料感、无光照均匀平坦。\n", encoding="utf-8")
    return [f1, f2, f3, f4, f5]


def test_five_rules_fixed_once(tmp_path):
    _inject(tmp_path)
    from engine import autofix
    report = autofix.run(tmp_path, dry_run=False)
    assert report["fixed_count"] >= 5
    # 修完必须复检通过
    assert autofix.recheck(tmp_path)["failures"] == 0


def test_idempotent(tmp_path):
    _inject(tmp_path)
    from engine import autofix
    autofix.run(tmp_path, dry_run=False)
    again = autofix.run(tmp_path, dry_run=False)
    assert again["fixed_count"] == 0          # 第二次零改动


def test_dry_run_writes_nothing(tmp_path):
    files = _inject(tmp_path)
    before = {p: p.read_bytes() for p in files}
    from engine import autofix
    autofix.run(tmp_path, dry_run=True)
    assert {p: p.read_bytes() for p in files} == before
    assert not (tmp_path / ".process" / "自动修复记录.json").exists()


def test_negative_lines_are_never_rewritten():
    """F18：负面对列行的豁免必须与校验器同源（含序号/短横前缀，且认「负面限制」）。

    实测旧行为（2026-09-21）：`7. 负面提示词：…`、`- 负面提示词：…`、`负面限制：…`
    三种写法校验器全都豁免，autofix 却照改——**改坏已经合规的交付物**。
    """
    from engine import autofix, prompt_rules
    many = "、".join("无" + chr(65 + i) for i in range(12))
    for line in (f"负面提示词：{many}", f"7. 负面提示词：{many}",
                 f"- 负面提示词：{many}", f"负面限制：{many}"):
        assert prompt_rules.is_exempt_line(line), line
        _after, fixed = autofix.fix_dunhao(line + "\n")
        assert fixed == 0, f"autofix 改了校验器已豁免的行：{line}"
        assert len(autofix.detect_dunhao(line)) == 0


def test_body_line_over_limit_is_still_rewritten():
    """反例（判据不削弱）：非豁免的正文行超限仍必须改写。"""
    from engine import autofix
    many = "、".join("甲" + str(i) for i in range(12))
    after, fixed = autofix.fix_dunhao(f"正文描写：{many}\n")
    assert fixed == 1
    assert after.count("、") < 12


def test_trace_written(tmp_path):
    _inject(tmp_path)
    from engine import autofix
    autofix.run(tmp_path, dry_run=False)
    trace = json.loads((tmp_path / ".process" / "自动修复记录.json").read_text(encoding="utf-8"))
    for item in trace["items"]:
        for key in ("file", "rule", "before_hash", "after_hash"):
            assert key in item
