# Review Checklist — hard gates and observation points

> Used to review a generated prompt (before submission) and a generated video
> (after submission). Hard gates are not averaged away by a high total score.

## 1. Pre-submission prompt review (100-point rubric)

| Dimension | Points | Check question |
|---|---:|---|
| Intent & hard constraints | 25 | Faithful, no silent rewrites, no contradictions |
| Subject & asset consistency | 15 | Identity, count, version, contact relationships explicit |
| Space / shot executability | 15 | Composition, path, action, duration all executable |
| Light, color, material, style | 10 | Specific, no conflicting style words |
| Continuity & physics | 10 | Axis, state, weight, causality stable |
| Sound & dialogue | 10 | Tracks, timing, lips, silence clear |
| Negative constraints | 5 | Few and accurate, from real risks |
| Platform layering | 5 | Main prompt separated from private params |
| Readability & checkability | 5 | Producer/director/reviewer can check item by item |

Below 80: fix the named deductions first; do not bury structural defects under
more adjectives.

**提示词评分门（交付判定）**: ≥80 交付；60-79 交付但显著标注「需重点验证项」；
<60 返工不交付（修完扣分项再交付）。提示词自评只决定提示词是否可交付，不能
触发视频选片。

**Never mark as generatable when**: hard-constraint conflicts, illegal
references, or unexecutable timelines exist — even with a high total.

Severity: **error** (must fix: hard conflicts, unknown subject/reference,
timeline overflow, state contradicting project facts) / **warning** (may
proceed after judgment) / **info** (optional enhancement).

## 2. Selection gates (video review — hard gates first)

| Gate | Core question |
|---|---|
| Narrative function | Does the audience read this shot's goal? |
| Identity / state | Approved character and current version? |
| Count / space | Person count, directions, layout, unique objects correct? |
| Performance / action | Causality, center of gravity, contact, settle hold? |
| Camera | Start, path, end, focus, cut point correct? |
| Light / color / material | Source logic, exposure, surface response hold? |
| Dialogue / sound | Verbatim content, lips, environment, mix correct? |
| Continuity | Does it connect to the shots before and after? |
| Technical defects | Morphing, flicker, jitter, text, compression? Plus AI 畸形观察：死鱼眼/不对称面部/多余手指/肢体扭曲/塑料感 |

Hard gates: identity, exact person count, verbatim dialogue, key continuity —
never averaged away.

**视频审核评分（2026-08-16 用户决策 — 成品判定）**：9 项 gate 折算 100
分——四硬门（身份/精确人数/逐字对白/关键连续性）任一不过=拒收不计分；
必须先有用户或独立审核提供的视频 gate 结果。视频折算分 **≥80 自动 select 写入
chengpin**（无需再次口头确认）；60-79 记 `needs_verification`，留在 daiheshen
并显著标注「需重点验证项」；<60 返工。提示词自评分不触发 select。用户仍可
人工推翻（用户裁决优先于自动评分），推翻记 supersede/reject。

## 3. Observation points (suspense-shot test kit)

When reviewing generated output, check each point explicitly:

- [ ] Performance chain 逐节可见（7 节定义见 performance-physics §1——单点维护）
- [ ] Physics: contact + reaction present; objects have weight
- [ ] Screen direction: camera side / anatomical side / nose direction consistent
- [ ] Person count: exact; no duplicates; reflections excluded
- [ ] Single-shot feel: one primary camera move, internal beats only
- [ ] Sound: named tracks, dialogue verbatim, lips correct, music policy kept
- [ ] GEO: same room in every shot; axis never crossed
- [ ] First second: wide locks positions before action begins
- [ ] State: clothing/injury/carried items match the state version
- [ ] Style: global prefix present, unchanged, one style only

## 4. Selection record

```
selection_id / shot_id / generation_id
decision: shortlist | needs_verification | reject | select | supersede | pending_feedback
passed_gates
known_defects
continuity_impact
rationale / reviewer / reviewed_at
```

Selected ≠ perfect. Known defects must enter waivers, post-fix, or rework —
never disappear in a note.

## 5. Continuity audit order（连续性审核顺序 — 来源：地狱 continuity-control）

先身份与精确人数 → 再状态版本与道具 → 再空间/轴线/屏幕方向 → 再动作
接点 → 再光线/天气/环境 → 最后声音与剪辑桥。先修高影响事实——不用
调色或风格掩盖身份/人数/动作错误。豁免必须记录（问题/影响/原因/批准人/
范围/后期修复）——口头「没关系」不能让镜头跳过质量门。

## 6. Failure distribution (for method improvement, not blame)

- Failure count per shot; per-layer share; whether the same error improved
  across consecutive batches; which prompt versions solved or introduced
  problems; attempts per kept shot; recorded cost/time.

## 7. Mechanical self-check（机械自检 — 交付前必过）

- [ ] 字符数 ≤ 7000（超出先砍非承重细节，不砍对白与关键动作）
- [ ] 时间码：[0s- 开头，连续无缺口，覆盖全时长
- [ ] [镜头 N] 于 MM:SS.mmm 严格递增，首镜无时间戳
- [ ] 画外音含「嘴唇保持完全闭合」
- [ ] 最终提示词为官方英文六段；仅 `<d>` 内对白/歌词和双引号内画面文字保留原语言
- [ ] 画风前缀逐字粘贴一次（[镜头 1] 处）
- [ ] 无空形容词/反 AI 味词（黑名单见 performance-physics §13.2 +
  abstract-translation §4d）
- [ ] 参考模式：身份不重述（挂了参考图不写面容/服装细节）
- [ ] 限制维度覆盖声明已列（每镜：已锁维度+依据 / 未锁维度+原因）

**P- 审计清单（来源：地狱 audit_prompt.py 13 条规则扩展为 14 条 — 中文手检版）**：
- [ ] P-EMPTY：提示词非空，主体定义段有内容
- [ ] P-MISSING-SUBJECT：每镜有明确主体（谁在画面）
- [ ] P-MISSING-DURATION：时长明确且每个片段在有效片段范围（默认 4—10 秒，config/duration-policy.json）内；总素材时长 ≥ 剧本声明目标
- [ ] P-MISSING-CAMERA-END：每个运镜有落幅（结束景别/构图）
- [ ] P-MISSING-AUDIO：对白与声音有归属轨道（说话者/环境）
- [ ] P-REFERENCE-SCOPE：每个参考声明了继承/排除范围
- [ ] P-CONFLICT-MOTION：无「静止∧移动」并存
- [ ] P-CAMERA-CONFLICT：一镜一主运镜（无多主运镜争夺）
- [ ] P-TIMELINE-OVERFLOW：节拍总和 ≤ 镜头时长，不越界
- [ ] P-NEGATIVE-DUPLICATE：负向句已去重（无同义堆叠）
- [ ] P-PLATFORM-MIXED：主提示词无平台私有参数混入
- [ ] P-DIALOGUE-VISUALIZATION：对白不视觉化（无闪回/幻象/额外人物）
- [ ] P-ABSTRACT-ONLY：无纯抽象词（情绪/氛围已物理化）
- [ ] P-VOCAB-UNVERIFIED：字段名/枚举/相机词/时间码可溯源至
  official-h3-grammar——发现自己在猜语法（「这个 flag 大概长这样」）就
  停下读文件；grepped 片段不算读过（来源：地狱 HARD RULES —
  plausibility-over-verification）
