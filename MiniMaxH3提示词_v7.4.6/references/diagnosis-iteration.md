# Diagnosis & Iteration — locate the layer, change one thing

> When a generation fails, locate the responsibility layer FIRST, then apply
> the minimal fix. "The result is bad" is not an error code — describe the
> observable symptom first.

## 1. Responsibility layers (check in order)

| Layer | Question | Typical fix |
|---|---|---|
| Assets | Are approved identity, state, references sufficient? | Fix asset, state version, inherit/exclude |
| Shot contract | Are count, space, action, camera, sound executable? | Reduce complexity, split shot, fix open/close state |
| Prompt | Is the contract clearly expressed? Conflicts or repeats? | Reorder, compress, fill missing fields |
| Platform | Are params, reference slots, duration, model capability matched? | Change only the adapter layer |
| Randomness | Is it sporadic within the same control batch? | Compare candidates with variables fixed |
| Post | Is the defect better fixed in edit/compositing/color/sound? | route_post and log it |

Decision tree: symptom violates approved asset facts → fix asset; contract
contradictory/overloaded → simplify; prompt vague/redundant → minimal rewrite;
platform beyond capability → adapter only; sporadic in batch → sample more;
better in post → route_post.

## 1b. Additional failure modes（来源：地狱 FAILURE-MODES）

- **FPS 帧重复**（输出卡顿=重复帧）：提示词体内写「视频以 24fps 运行，
  无帧重复」（不止参数行）；B-roll 帧级去重补救，英雄镜重生成
- **帧级审查强制**：每 10 秒剪辑 ≈ 30-60 秒逐帧审查时间（计入迭代预算）；
  一帧坏帧（闪烁的手/扭曲的脸）= 拒收整段——AI 剪辑每帧是独立生成，
  不信任直到逐帧审查过
- **失败生成抢救**：拒绝的镜头常有 1-3 秒可用片段——丢弃前逐帧过一遍，
  标记 in/out 点切出保存（可作连接镜头/反应节拍）——长片从抢救片段
  出货是正常手段
- **NSFW 误报重写**（无 NSFW 内容被误拒）：泛化解剖细节（裸露肩膀→
  肩膀）、替换感性形容词（诱惑→直接）；**绝不原样重试同一提示词**
- **源参考缺失元素显式声明**（提示词要求元素但参考没有）：写显式负向
  引用条款——「该玉牌在参考图中不可见；本镜角色从画下抬手时它出现」——
  让模型知道是有意添加而非匹配失败

## 1c. Failure-timing diagnosis（失败时序诊断 — 来源：地狱 Filter Model）

| 失败时序 | 含义 | 处理 |
|---|---|---|
| 快速失败（提交后 <10 秒即拒） | 内容过滤器拒绝——提示词没到生成 | 改措辞（场景完整可读原则）+ 移除风险词；**绝不原样重试** |
| 慢速失败（>30 秒后失败/超时） | 复杂度/基础设施问题——通过了过滤但渲染失败 | 简化动作密度、裁剪长度、拆条重试 |

场景完整可读原则：提示词必须描述「场景」不是「主体」——六要素（相机/
主体/动作/环境/风格/光）齐备时过滤器有完整上下文（本技能的 12 维限制
天然保证）。快速失败循环时先改措辞再重试，绝不原样重生成。

## 2. Error-code table (H3 project codes)

### Assets & identity

| Code | Symptom | Minimal fix |
|---|---|---|
| `F-ID-DRIFT` | face, build, hair, core silhouette drift | keep only approved identity, explicit reference scope |
| `F-STATE-DRIFT` | clothing, injury, wetness, carried items revert | reference correct state version, write must_hold |
| `F-REF-SCOPE` | reference composition/background/light inherited | inherit only needed attributes, exclude the rest |
| `F-DUP-SUBJECT` | same character twice | exact count, one appearance per ID, flag reflections |
| `F-ASSET-MERGE` | two characters/props blend | separate asset injection, positions, contact edges |

### Space & continuity

| Code | Symptom | Minimal fix |
|---|---|---|
| `F-COUNT` | wrong person count, extra people | exact count + IDs, each appears once |
| `F-SCREEN-DIR` | screen direction / anatomical left-right flipped | write camera side + anatomical side + nose direction together |
| `F-AXIS` | axis jump, relationships confuse | hold direction or design a visible axis bridge |
| `F-SPATIAL-RESET` | scene layout rebuilt | GEO map, fixed anchors, exclude reference composition |
| `F-PROP-DUP` | furniture/key props duplicated | declare unique objects and contact relationships |
| `F-CONTINUITY` | actions, injuries, weather, sound don't connect | align open/close state, register changes |

### Action & performance

| Code | Symptom | Minimal fix |
|---|---|---|
| `F-ACTION-OVERLOAD` | actions compressed, dropped, reordered | delete, merge, or split shot |
| `F-ACTION-REVERSAL` | action finishes early, rest of clip plays in reverse (walks forward then steps back) | chain 2-3 connected actions along the same vector to spend the whole clip; there-and-back = two shots, not one; name the camera endpoint |
| `F-PHYSICS-ADJACENT` | adjacent-object physics violations (magnet flies with the Polaroid; hat lifts with the head turn) | name the invariant explicitly: "磁铁始终贴在冰箱表面，只有拍立得移动" |
| `F-PHYSICS` | floating, weightless, no collision reaction | complete physical chain + environment feedback |
| `F-ACTION-ORDER` | result before trigger | write trigger and stages in time order |
| `F-PERFORMANCE` | stiff, overacted, emotion unreadable | pick few signals: gaze, breath, pause, weight |
| `F-UNSCRIPTED-MOVE` | static character turns/speaks on its own | list allowed micro-movements and forbidden actions |

### Camera & editing

| Code | Symptom | Minimal fix |
|---|---|---|
| `F-CAMERA-CONFLICT` | multiple primary moves fight | keep one primary move or split |
| `F-CAMERA-PATH` | start, route, distance, landing wrong | write a physically walkable complete path |
| `F-FRAMING` | size, occupancy, direction wrong | drawable composition instead of "cinematic" |
| `F-FOCUS` | focus wrong or drifting | one primary focus object, one explicit rack |
| `F-CUT` | shot count, cut points, transitions wrong | exact count, per-segment durations, cut points |
| `F-JITTER` | handheld becomes fault jitter | body amplitude/rhythm; shocks bind to events and recover |

### Dialogue & audio

| Code | Symptom | Minimal fix |
|---|---|---|
| `F-DIALOGUE-TEXT` | words added, dropped, wrong speaker | verbatim lock, one speaking window, speaker ID |
| `F-DIALOGUE-TIME` | line too fast, truncated, crushes action | lengthen/shorten line or split shot |
| `F-DIALOGUE-VISUALIZED` | line content becomes flashback/extra people | state dialogue is not visualized, current allowed set |
| `F-LIPSYNC` | non-speaker lips move, wrong mouth | speaking window + silent characters |
| `F-VOICEOVER` | voiceover lips visible | off-screen voiceover + lips completely closed |
| `F-AUDIO-POLLUTION` | auto music, narration, subtitles, stray sounds | keep specified tracks, ban extra tracks |
| `F-AUDIO-MIX` | voice/SFX/bed priority wrong | reorder foreground/mid/bed + duck |

### Light, color, material

| Code | Symptom | Minimal fix |
|---|---|---|
| `F-LIGHT-DIR` | light swaps sides, no source | real source, direction, relation to people |
| `F-EXPOSURE` | highlights blown, shadows crushed | choose what must survive and its range |
| `F-COLOR-DRIFT` | palette, temperature, accent drift | lock narrative colors and change events |
| `F-MATERIAL` | skin/cloth/metal/bone all plastic | per-zone roughness, reflection, translucency, wear |
| `F-WEATHER` | rain/snow/fog/wind like background layer | weather acts on people, ground, light, sound |

### 2a. 每域自毁式写法禁令（不建议列 — 来源：地狱 failure-diagnosis）

- 不同时堆 locked+orbit+push-in+whip pan 让模型自选（一镜一主运镜）
- 不用「更激烈更震撼」补动作因果（因果必须来自物理链）
- 不写 no sound 又保留对白（冲突压缩：改限定轨道，见 ref2va-guide §4）
- 不堆自然光+棚拍三点光+霓虹+月光+火光而无主次（光源五问只留一个主光）
- 身份参考不稳时不堆 same face 同义句（回资产层修参考，不是加词）
- 不用一个构图参考强控所有机位（参考只管它该管的）

## 2b. Iteration heuristics（迭代启发 — 来源：地狱）

- **Prompt-reset（迭代变差时）**：迭代越改越差时，停止堆修复——把提示词
  剥回「主体+动作+相机」三要素，重新只加必要的。密度是钟形曲线，过峰后
  分不清模型丢了哪个元素。
- **草稿验证边界（无 seed 重跑=新摇）**：H3 无 seed 参数——重跑已批准
  的草稿是全新生成（新表演/新相机微轨迹/新时机）。低分辨率草稿能验证的
  只有：镜头数与结构服从、站位服从、标签污染、对白位置、过滤通过；
  **不能验证**：表演细节、相机微轨迹、节拍时机、精细细节。细部判断必须
  在最终分辨率复检。
- **坐标漂移应对**：坐标是意图不是保证——漂移时收紧定性锚点或**加接触
  点条款物理接地**（「脚踩地面标记」「右手扶桌沿」），不是把坐标写更硬。

## 3. Iteration discipline

- **三类批次（来源：地狱 iteration-selection）**：探索批次（低复杂度低预算
  验证方向——**结果不直接进入 locked**）；控制批次（提示词/参数/参考
  完全相同——只观察随机差异）；修复批次（验证单一失败假设——必须记录
  上批失败码与 changed_variables）。任何一项改变=新批次，绝不在同一
  batch_id 下偷改提示词。
- **One batch fixes one variable**: a batch fixes prompt version, provider,
  model, main params, references + weights, duration, aspect, resolution.
  Observing random differences within a batch is sampling, not iteration.
- **Every iteration is surgical**: one line changes, everything else word for
  word. Never "changed_variables = all optimized."
- **Hypothesis is falsifiable**: "If I reduce the primary camera move from
  push+arc to slow push-in, subject exit-frame and path drift should decrease
  while identity and action stay unchanged."
- **Stop conditions**: candidate passes hard gates; two consecutive batches no
  improvement on the same error → return to layer or split shot; budget or
  capability boundary reached; post-fix cheaper.
- **重试阶梯终点条款（来源：higgsfield-troubleshoot Retry Ladder）**：同一
  提示词+参考映射重跑一次（合法方差）→ 第二次失败=证据过载，拆镜 →
  **三次付费尝试后停止**，给命名选项（接受最佳 / 重定范围 / 延期 /
  交付显式标注 `placeholder: missing clip`）——缺片绝不静默缺失。

## 3b. 用户实测反馈格式（2026-08-16 用户确认 — 加强复审）

用户报实测结果时按固定格式收集（防信息丢失），技能据此走责任层诊断：
```
哪一镜：[SC###-SH### 或 镜头 N]
失败现象：[一句话可观察描述，如「眼睛自发光」「手指六根」]
截图/视频路径（可选）：[放待审核区或直接给路径]
按 selection gates 报失败项（可选）：[身份/计数/动作/光线/对白/连续性/
技术缺陷——哪项没过]
```
- 复审加强：反馈记入台账（失败现象+责任层），下次同类镜头产出前扫教训
  库+台账——防同病复发
- 用户只说「失败」没说细节时，技能先问一个问题（哪失败了——一问上限）
- 生成后没有视频或用户审核结果：台账保持 `awaiting_video_feedback`，用户不回应时
  保持 `pending_feedback`，不自动 select；只说“失败”且无镜头/现象时最多追问一次。
- 视频审核有四硬门结果且折算分 ≥80：才可自动 select 写入 `chengpin`；60-79
  记 `needs_verification` 留在 `daiheshen`；<60 进入 `needs_revision` 回 `zhuanhuanzhong`。

## 3c. 教训回写（修复确认后执行）

满足任一条件才回写：同一错误码可复现两次以上、单次修复批次已验证有效，或用户
明确要求“记下来/下不为例”。回写顺序：
1. 在 `lessons-learned.md` 当前日期分区追加「现象/结论/织入位置」；已有同质
   教训则更新实例和结论，不重复新增。
2. 在织入位置补一条短规则，并运行对应回归测试；未验证有效前不把猜测写成教训。
3. 在 `CHANGELOG.md` 追加批次摘要，记录触发、修复、验证和未解决边界。

## 4. Iteration record format (12 fields)

```
iteration_id / shot_id / prompt_id / batch_id
observed_failure_codes
responsibility_layer
changed_variables
hypothesis
expected_improvement
result_generation_ids
decision: continue_same_batch | new_prompt_version | new_adapter_version |
          revise_asset | split_shot | select | stop_budget | route_post
next_action
```

## 5. The 10-15 rule

If a shot has not come together in 10-15 iterations, the problem is not the
wording. Simplify the shot: split it in two, remove an action, change the
angle.
