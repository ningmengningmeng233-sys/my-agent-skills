# Camera & Editing — one primary move per shot

> Camera vocabulary lives in official-h3-grammar.md §4 (12 official types +
> 15 house phrases mapping). This module supplies the decision rules: how to
> pick, complete, and sequence camera work so shots render as intended.

## 1. Camera contract minimal loop（摄影机契约最小闭环）

完整镜头至少写：`起始构图 → 一个主运动 → 稳定方式与对焦 → 结束构图 →
下一镜接点`。只写「镜头推进」没有距离/速度/对焦/结束构图——无法评审。

**起始构图 7 要素**：机位方向（正面/侧面/肩后/主观/俯视/仰视）；机位
高度（地面/腰部/胸口/眼睛/头顶）；景别（大远景→极特写）；主体位置与
占比；前中后景关系；对焦对象与景深；屏幕方向与轴线。

**稳定方式 5 类**（不写含糊「电影感」）：锁定（可写「构图不漂移，仅有
很弱的人体呼吸微动」）/ 稳定跟随（平滑但有重量，写清加速减速停止）/
自然手持（身体来源：小幅水平垂直变化+步伐+呼吸节奏，写幅度与主运动
关系）/ 重度肩扛（适合混乱，保持主体可读，说明边缘切到与焦点追赶）/
故障抖动（无因果高频跳动=失败；冲击震动绑定明确碰撞时刻并恢复）。

**对焦纪律**：单一对焦对象优先；拉焦写起点/触发/终点/时长；复杂动作
不同时要求极浅景深与所有层清晰；对白镜头未说话者可保持层次反应可读；
结束对焦服务下一镜接点。

**camera_start / camera_path / camera_end**：
```
camera_start: [机位、景别、构图、对焦]
camera_path: [主运动、方向、速度、距离、稳定方式、对焦变化]
camera_end: [结束景别、主体关系、对焦、下一镜接点]
```
路径必须物理可走通（不穿墙/人物/唯一家具，除非明确非现实视效）。

## 2. One primary camera movement per shot

Compound movements ("push in plus arc") are a common failure source — split
them. For an orbital feel, write a lateral truck plus counter-pan rather than
relying only on "orbit." One primary move + one texture modifier is allowed
（「缓慢推近，略带手持感」——复合运镜 = 模型按序执行两向量导致抖动）.
相机站阴影侧（来源：地狱）：主光阴影侧放置摄影机并声明操作员轴线——
光向镜头包裹，脸保持立体。

Do not stack camera tags — write natural action sentences. The camera is
written inside the action, not as a separate aesthetic paragraph.

## 3. Complete every movement with its required parameters

| Movement | Required parameters |
|---|---|
| Pan | start and end targets |
| Push-in | distance, speed, end framing |
| Tilt | start and end heights |
| Tracking | relative position, distance, speed |
| Arc | angle, direction, radius |
| Pedestal | start and end heights |

- Amplitude (`with small/large amplitude`) and speed (`at slow/fast speed`)
  only when meaningful; medium amplitude and normal speed are usually omitted.
- A camera path must be physically walkable — no passing through walls,
  people, or the only prop.
- Focal behavior: prefer visual effect over millimeters — spatial
  exaggeration, natural perspective, or compressed background.

## 4. Camera house phrases (ADR 0001, accepted)

Write product-side phrases 推/拉/摇/移/晃/跟随/环移/跟移/上扬/降摇/聚焦/场景镜头/
升降/主观/横滚 in the prompt when the user uses them; the mapping table in
official-h3-grammar.md §4 resolves them to engine vocabulary. Blank areas
(聚焦, 场景镜头, 上扬, 降摇) fall back to official English or natural language.

## 5. Cuts and the double-contrast rule

- Hard cuts: `the camera cuts to` / `the shot cuts to` / `the shot transitions
  to` / `the shot changes to` / `the shot switches to`.
- **转场时长上限（来源：seedance2-chinese-guoman）**：硬切为默认；需可见
  转场时完成时间 ≤0.5s——禁慢淡入、慢淡出、长时间溶解；稳定机位是默认
  选择，不因时间推进自动移动，不为每段开头/每 3 秒强制安排运镜。
- **片段衔接帧（来源：seedance2-chinese-guoman）**：拆条片段开头承接上一
  段稳定构图/角色状态，结尾保留可复用稳定衔接帧；**片段首尾不安排运镜**
  （除非剧情明确需要）——为便于拼接。
- Double-contrast cut (production rule): every cut changes BOTH shot size
  (extreme wide → wide → medium → MCU → close-up → ECU) AND camera character
  (handheld | static | stabilized tracking | crane | aerial) — never repeat
  either across a cut.
- Non-first shots carry `At MM:SS.mmm` strictly increasing timestamps;
  internal beat notes may cover the planned duration, but final Ref2VA uses only
  official `[Shot N] At MM:SS.mmm` cut timestamps and never `[Xs-Ys]` windows.
- Spatial continuity breaks on cuts: re-anchor positions and facing direction
  after any cut.
- **剪辑静帧（来源：higgsfield-cinema — 移植未验证）**：跨条剪辑落在
  静止上而非动作上——镜头首尾显式安排 1-2 秒静止或近静止时刻（凝视/
  定姿/动作前停顿/动作后屏息），中间可激烈；**一镜一状态变化**，多节拍
  在第一节拍处切镜。（片内结尾 1s 沉默收束见 dialogue-audio——用途
  不同：前者跨条剪辑、后者片内收束，数值区间兼容）

## 6. Combined camera formulas (reference: 山田大师运镜库 — per-segment, never stacked)

Emotion-narrative:
- slow push-in → hold → slow pull-out: gaze → settle → withdraw (dialogue
  arc). 缓慢前推→静止→缓慢后拉
- pan to reveal → push-in emphasis → hold close-up: environment to detail.
  摇镜揭示→推进强调→特写停留
Epic-visual:
- slow-motion orbit + speed-lines: heroic-moment ritual feel. 慢动作环绕+
  升格
- crane-up reveal → slow descent: small-to-large epic reveal. 拉升俯瞰→
  缓慢下降
Action:
- large whip-pan + slow-motion segment: impact / loss of control. 大幅甩
  镜+升格段
Note: formulas implement as SEGMENTS (one primary move each, connected by
cuts) — never stacked inside one segment.

## 7. One shot, one narrative function

A shot that simultaneously introduces a location, completes a complex action,
delivers a long line, and reveals a twist is overloaded. Split it.

## 8. AI-video camera notes (reference: 山田大师运镜库)

1. Be specific, not vague: 「镜头向前推」不如「从全景缓慢向前推进至面部
   特写」.
2. Name the speed explicitly: 缓慢 / 快速 / 匀速 / 先慢后快.
3. Compound moves are written as separate steps: 「先环绕半圈，再向前推进」,
   never 「推轨+环绕」 as one.
4. **Motion blur is the seasoning**: fast moves add 动态模糊/motion blur.
5. **Stabilizer is the safe card**: AI overdoes handheld; when unsure use
   smooth steadicam-style movement (「平滑稳定器感」), reserve handheld for
   deliberate energy.
6. **摄影风格声明（来源：官方中文版示例「摄影」段 — 每项目一句全片锚）**：
   题材层声明一句全片摄影锚并逐镜保持——「手持呼吸式，有机微动，剪影为
   构图主锚，镜头对主体运动做细微偏移反应」「平滑稳定器感，呼吸式运镜，
   从不锁定」——绝不中途换摄影风格；剪影锚点：光线给主体边缘而非表面。

## 9. Cut-format ladder（切点阶梯 — 4 级，选镜头需要的精度）

1. **单镜 oner** —「一镜连续，相机不自行切」
2. **顺序切、无时间码** — 镜头切至…按序描述，切点时间不重要时
3. **计时多镜** — 明确秒级切点（[镜头 N] 于 MM:SS.mmm），节拍必须落在
   时钟上时（H3 官方时间码两层）
4. **自由 b-roll** — 不锁切点，让模型自己找角度

指定切点时附加：「只在指定点切，相机不自行切」（防模型自行加切）。
切点表达用 H3 官方 5 种（镜头切至/画面切至…）；叠化/淡入淡出/划像仅
显式要求时用。

## 10. Beat budgeting

Budget beats to the official segmentation density: ~5s: 2-3 segments; ~8s:
3-4; ~10s: 4-5; ~15s: 5-8 (each 2-3s, 1-3 sentences). Overload a beat and the
model smears it.（口径提醒：技能默认有效片段范围为 4—10 秒，见
config/duration-policy.json；15s 只是模型能力上限，不作为默认预算。）

## 11. Camera-emotion sync + shot-duration table（来源：higgsfield-camera）

**机位-情绪同步表**：愤怒=手持喘息抖动；平静=平滑呼吸式运镜；震惊=
定格+极慢推；崩溃=拉远留白。镜头是焦点角色情绪的双胞胎。

**镜头时长表**：闪景 0.3-0.5s；对白 3-7s；无台词反应 5-10s；完整情绪
弧特写 8-15s。微距移动写精确距离+时间。

## 12. High-risk camera work (flag at authoring time)

- **Handheld**: describe amplitude and rhythm ("subtle handheld, operator
  breath") — intentional handheld is not failure jitter. Impact shocks bind to
  the event and recover.
- **Shake**: `Shake Slightly / Strongly`; never write continuous violent
  jitter without a source.
- **Roll**: use deliberately (dutch angle), never casually.
- **Reflections** (mirrors/water/blades): never reflector + reflected in one
  continuous shot — shoot the reflection as its own shot, then cut.
- **Same-character instances**: separate named roles, prefer cuts over
  continuous two-instance frames.

## 13. Montage organization (multi-shot)

- Multi-shot prompts declare the total contract: exact shot count, each shot's
  duration window, cut points, final landing.
- One main camera movement per shot even inside a montage; vary sizes and
  camera characters across the double-contrast rule.
- Single-shot editing feel: internal beats, no hard-cut vocabulary mixed into
  one continuous shot.

## 14. 空间运镜与光时段策略（来源：jineng 15-real-estate — 按实际情况选用）

**空间镜头运动库（精选 6 种 — 悬疑/室内/建筑场景适用）**：门槛揭示
（过门即见关键元素——走廊进房间）/ 窗景接近（慢推至窗前，窗外信息
渐进）/ 垂直倾斜（俯→仰揭示空间高度）/ 转角揭示（过转角见新空间——
悬念常用）/ 拉回（从细节拉远显示全貌——规模与位置关系）/ 环绕 180°
（展示空间与主体关系——一镜一主运镜，环绕为唯一主运动）。

**光时段策略**（按项目时段锁光不锁时间词）：黄金光=暖调低角度（情绪/
外观）；冷青硬光=肃杀紧张（悬疑）；明亮日光=功能清晰（信息）；暖夜=
宜居/亲密——同一场景跨时段需写明变化起因与方向（shot-structure §4
连续性）。

**逐空间要点**（室内戏）：走廊/过道=纵深引导+门槛揭示；雅间/房间=
先环境锚点后人物（场景图锁）；楼梯=垂直倾斜或俯视层次；门窗边界=
窗景接近或门槛揭示——一个空间一个主运镜意图。
