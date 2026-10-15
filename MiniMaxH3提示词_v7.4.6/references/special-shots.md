# Special Shots — recipe library for hard scenes

> Six recipes for shot types that need their own structure: action/fight,
> transformation, screens, crowds, props, and prompted imperfection. Adapted
> from production-validated patterns; syntax rewritten in H3 official
> vocabulary (H3's recognition range accepts these). Default output Chinese.

> **⚠️ 实测状态（2026-08-15）**：本模块全部配方为**跨模型移植**
> （Seedance 生产语料改写），**尚未在 H3 上实测验证**——就像教训库 #2
> 画风迁移在 H3 失败过一样，移植经验可能失效。实测后按状态更新下表：

| 配方 | 来源 | 实测状态 |
|---|---|---|
| §1 打斗/复杂动作 | 地狱 prompt-examples Scene 5（Seedance） | 移植未验证 |
| §2 变形/变身 | 地狱 prompt-examples Awakening II | 移植未验证 |
| §3 屏幕/监控 | 地狱 prompt-examples Scene 5b | 移植未验证 |
| §4 人群 | 地狱 prompt-examples Scene 8 | 移植未验证 |
| §5 道具细部 | 地狱 prompt-examples Scene 6 | 移植未验证 |
| §6 Prompted imperfection | 地狱 prompt-examples Scene 4 | 移植未验证 |
| §7 身份纪律 | H3 官方×地狱共识 | 官方语法层，无需移植验证 |
| §8 特效注入 | higgsfield-seedance-vfx | 移植未验证 |

## 1. Fight / complex action sequence

> **节奏层指针（2026-08-16 用户拍板吸收）**：打斗镜头的**节奏与打击感
> 设计**（节奏合同/两级慢拍/命中一拍四段/三大铁律/回合单元/破坏三档/
> 终结设计/玩法档裁决）走 `action-director.md`（动作导演打法模块）——
> 本节约管结构（切分/命名切点/方向锁定/能力清单），节奏层在
> action-director。
> **冲突仲裁细则（2026-08-16 端到端测试补）**：同时属结构+节奏的冲突
> （如速度模式）→ **结构规则优先**（本节为准），action-director 在其
> 框架内实现——「每镜一个速度」指该镜的主速度模式：慢镜头蓄力
> （快→慢→快）是一个完整速度模式「蓄力爆发镜」，不视为镜内速度
> 切换；B 类低幅度慢拍（画面照常动）不涉及速度模式，无冲突。

Recipe: segmented timeline + named cut types + locked directions + triple
locks.

**能力清单前置（来源：higgsfield-cinema — 打斗配方先选层）**：
- 可靠：对峙 / 贴墙压制 / 倒地 / 走开 / 冲突能量（无接触的紧张）
- 不可靠：精确拳脚接触 / 踢击 / 道具打斗 / 多人缠斗 / 逐帧冲击
- 不可靠动作一律**重排为可靠镜头序列，用剪辑代替单镜**（切点做模型
  做不了的事——接触点切走、反应镜代替打击镜）
- **与战斗分类的衔接（2026-08-16 拷问补 — 防两节矛盾）**：战斗分类
  （拳击组合/剑术弧线）描述的是**战斗特征用于选型**；渲染实现仍受能力
  清单约束——组合拳写「出拳节奏+防守反应+脚步工作」，**精确接触点
  用剪辑代替**（接触前切走/反应镜代替打击镜）——分类选型 + 清单实现，
  两节各管一层
- **攻防循环秒级量化（来源：jineng 05-fight-scenes — 仅作内部编排）**：设置
  缓冲 0.2s → 执行延伸 0.3s → 冲击 0.1s → 反应 0.2s →
  恢复 0.2s（五段约 1s 一拍）；打击类型×视觉映射（直刺=线性加速/挥扫=
  圆弧/切割=刃先/冲击=肩先）；碰撞物理三型（硬=火花弹开/软=肉体吸收/
  部分=格挡——格挡时拳臂接触一次即分）；节奏-镜头分配（开口宽镜/交换
  跟踪/高峰推进/快交换快切/呼吸撤回/高潮慢镜）
- **战斗分类 7 类**（先选类再写——国漫 3D 最常用 剑术/武术）：拳击 MMA=
  快速爆炸力量（模糊运动+冲击闪光+打击呼吸声）/ 武术功夫=流动精确线性
  （速度线+旋转踢延伸+「哈！」音效）/ 剑术=意图重量碰撞（缓慢强大弧线+
  火花+叮当金属声）/ 枪战=快速远程（枪口闪光+后坐力+烟雾滞后）/ 街头=
  混乱肮脏（笨拙绝望能量）/ 体操特技=浮华高飞（翻滚+轻着陆）/ 超级英雄=
  VFX 重型（能量爆炸+低频轰鸣）
- **打斗 2 秒钩子**（内部编排三要素；最终提示词使用官方切点）：对手建立+
  距离+光照强度（我在看谁打谁）→ 初始动作+速度提示
  （什么战斗类型）→ [2s-3s] 冲击或动作转向+视听反应（快还是重）——首三秒
  不拍花架子；更细的 0.2/0.5 秒节拍只作为内部编排，不写入最终提示词的
  非官方 `[Xs-Ys]` 窗口。
- **武器指南**：剑/刀——刀锋清晰可见、挥动走肩到腕的圆、每次打击前
  位置调整、碰撞火花、**重剑不快（物理权重——快交换中重剑明显更慢）**；
  长武器（枪/棍）——旋转流体、范围广、靠核心肩腿发力、运动连续少
  停顿；拳击——快速组合（3-5 连击无暂停）+ 脚步工作（前后摇晃/枢轴）+
  变招（直拳钩拳上勾）+ 防守（躲闪滑步格挡）
- **受伤反应 4 级**（写清等级+对下一镜状态影响）：轻击=摇头回位无真
  影响 / 中击=退一步两步+可能失焦 / 重击=被击倒+缓慢恢复 / 击倒=在
  地上可能无意识——伤势写进状态版本与连续性
- **长打斗节奏量化**（6s+ 必用）：缓慢建立 1s → 升级速度 1s → 打击率
  增加 1-2s → 高峰 1s → 呼吸停顿 0.5s → 最终交换 1s——节奏变化是长
  打斗的生命线，禁止全程同速
- **多角色同步编舞**（3+ 对手）：交错攻防写法——「A 攻 B 左侧，B 防守
  A 同时攻 C，C 闪避 B 同时攻 A」——所有动作显示互动与间距意识
- **打斗音效分级**：直接打击=噼啪(轻)/砰(中)/砰砰(重)；武器碰撞=叮当
  (金属)/咔(木材)；脚步=地板冲击节奏；呼吸痛苦=吸入/喘息；**「冲击
  同步到音效」显式写**（音效与冲击不对齐是常见失败）
- **打斗常见陷阱表**（自检 6 条）：打斗无力=缺冲击反应→加「可见冲击+
  对手反应」；不清谁赢→显示一方压倒或防守；武器塑料→加「武器有重量+
  碰撞反应」；相机混乱→简化跟踪或绕行避免过度切割；音效不同步→指定
  冲击同步到音效；时长不清→明确秒数与节奏变化
- **玄幻能量战斗**（专属能量体/法术对轰 — 无物理接触时用，2026-08-16
  用户确认）：攻防循环适配为能量版——**蓄能**（能量汇聚/能量体显形）→
  **放出**（能量射出/领域展开）→ **对撞**（能量交汇点=接触点——碰撞
  火花/冲击波）→ **余波**（环境受击：地面裂纹/气浪吹衣）→ **落定**；
  「接触点」=两股能量的**交汇位置**（锁交汇点，不是肢体接触）；特效
  注入按 guoman-3d §5d（能量光环=高饱和发光+粒子点缀+作用于环境）；能力清单
  适配：能量对轰属「冲突能量」可靠类，**无需剪辑代替接触**——但多段
  能量变化（蓄能→爆发）按升级构建锁显式递增

**打斗节拍结构（来源：地狱 action-physics-vfx）**：复杂打斗先拆成可读
节拍——`威胁建立 → 一次攻击 → 一次防守/躲避 → 一次明确反击 → 落定`。
规则：每拍只有一个动作中心；明确谁先、谁因何反应；写出手脚的解剖侧和
屏幕方向；避免多人同时做不同复杂动作；时长不足时拆镜（绝不「高速连续
完成十个动作」）；打击伤势影响下一镜时写入状态版本与连续性。

- **Segmented timeline with named cuts**: split the action into segments,
  each with a start time and a named cut type using H3 official phrases,
  with a rhythm qualifier: `the camera cuts to` (快速切入 hard smash feel) /
  `the shot transitions to slow motion` (进入慢动作) / `the shot changes to`
  (快速变向). Cut points at integer seconds; every cut introduces new
  information (official cut principle).
- **Direction lock**: state screen direction once and repeat it in every
  segment: "全程从画面左向右运动，方向锁定，绝不回退" — never leave a chase
  direction unstated.
- **One primary camera move per segment**: segment A push-in, segment B
  low tracking, segment C arc — never compound inside a segment.
- **Per-segment visual lock** (replaces FOV-degree locks — H3 has no FOV
  system): declare the visual character per segment — "广角空间夸张" /
  "自然透视" / "背景压缩的远景" — locked inside the segment, changing only
  on the cut.
- **Triple lock for the money beat**: the key action (one press, one strike,
  one catch) is locked THREE times — in the description segment, in the
  internal beat timeline, and in a positive lock sentence at the end; do not put
  `[Xs-Ys]` windows in the final Ref2VA prompt.
- **Complete physical chain per strike**: preparation → exertion → contact →
  reaction → settle; contact and reaction are never dropped.
- **Open mid-motion**: complex actions open the prompt already in motion
  ("他已经在挥剑途中"), the approach becomes its own shot.
- **One speed per shot**: real-time and slow-motion never mix inside one
  shot — each shot is one speed start to finish; speed modes connect only
  via hard cuts. 每镜一个速度从头到尾；速度模式之间只用硬切连接。

**官方真实数据增补（来源：磨盘 13_官方制作提示词 115,446 条分析，
2026-08-10 — 与能力清单/攻防循环/节拍结构互补，不重复）**：

- **官方打斗六镜模板（Fight Sequence Template — Multi-Shot Manual）**：
  对峙（锁角色）→ 首次交锋（写战斗能量，不写具体动作）→ 升级（身体
  碰撞）→ 特写时刻（按墙/凝视）→ 收尾（有人倒下）→ 结果（英雄走开）；
  **@元素镜头（锁脸）+ 纯文本镜头（锁动作）交替**——观众在切镜间补全
  连续性，正是真实剪辑逻辑（多镜头序列用，单镜内仍走节拍结构）
- **慢动作兜底（真实数据：563 条打斗 94% 含 slow motion）**：打戏默认
  慢动作主导；**相位分层**——常规慢 → 极慢（特写细节/血滴飞溅）→
  恢复，相位间硬切（H3 写效果「升格/极慢/慢速」，不写拍摄 fps——
  100/200fps 是 Seedance 侧参数）
- **macCU 插入（Scene 25/51 全覆盖）**：快速运动镜头每 0.5-1s 插入一个
  微距特写——掩盖细节不足 + 增加节奏（写进内部节拍笔记，不写进最终提示词）
- **镜头运动代替动作（Scene 72：whip-pan 359 条/snap 527 条）**：快速
  运动不拍动作本身——whip-pan 甩镜转场 + snap-zoom 骤切在刺击瞬间 +
  荷兰角翻转，运动模糊天然掩盖中间帧
- **COMBO 循环（Scene 72 BOSS 战范例，9-cut/0.5s 粒度）**：每次换招先
  「闪避/格挡」再接「快速刺击」——用回避代替对打；穿插穿人群广角镜头
  稀释打斗帧（也是剪辑节奏点）；深度刺击写全程动量（「刀从前胸进后背
  出，贯穿整个胸腔」）——用结果代替过程
- **撞击结果化（Scene 69）**：命中瞬间 = 结果（弹开/裂纹/碎片四散），
  因果由镜头拼接补全——格挡无伤型写法：「斩击干脆利落，但刀弹开如击
  石——骑士纹丝不动、不眨眼」
- **资产 × 打戏解耦（Scene 69 生产提示词 20,000+ 字）**：资产描述
  （外观/武器/状态版本）逐字复用、动作只写分镜区——身份稳定与动作
  表达解耦（与 ref2va 参考承载记忆一致；Seedance 侧长提示词结构，H3
  按 7000 预算裁——纪律本体照收）

Worked example (fight beat, Chinese output):

```text
[0s-1s] 宽镜锁定：两人相距三米对峙，夜雨巷中，全程从画面左向右运动方向
已锁定。 [1s-2s] 镜头切至中近景（快速切入的硬切感）：黑衣客前踏步
（准备），右拳直出（发力）——拳面砸中沈昭格挡的小臂（接触），沈昭上身后
仰半拍，脚下后滑半步稳住（反应+落定），衣摆甩动带起雨珠。 [2s-4s]
镜头转场至慢动作：玉牌从黑衣客怀中滑出，翻转半周，雨珠在玉面上弹开，
沈昭的眼神先于头锁住玉牌。 [4s-5s] 镜头切至中近景：沈昭沉身拉架，剑柄
咔哒转过四分之一圈。 正向锁定：这一击只有一次拳臂接触，玉牌只出镜一次，
绝不重复。
```

## 2. Transformation / shape change

Recipe: explicit midpoint — without it the model snap-cuts or renders an
ambiguous blur.

- **The midpoint is mandatory**: describe the visible intermediate state —
  seams pulling tight, fabric stretching at the shoulders, fissures of light
  tracing along the body. H3's process-continuity reading handles this
  naturally (how the subject gets from A to B).
- **Sound transforms with the body**: voice descends in pitch, gains a
  harmonic, resolves as something other-than-human; ambient recedes.
- **End state fully declared**: height, posture, light cast, eye state.
- **Staged off-screen variant**（来源：Field patterns）: 变形不在中途展示
  除非作为影子剪影——否则发生在画外，只给变形前与变形后（避开模型变形
  失败模式的设计选择）。

## 3. Screen / monitor / in-frame display

Recipe: 1:1 content lock + screen realism.

- **1:1 content lock**: the in-frame screen shows the source content EXACTLY
  — same image, timing, framing, motion, colors — never reinterpreted,
  re-edited, cropped, looped early, or replaced. Never leave in-frame screen
  content unspecified.
- **Duration match**: the new generation's duration must exactly match the
  referenced clip's (otherwise the model improvises).
- **Name what plays, in sequence**: never leave an in-frame screen's content
  to the model — unspecified screens fill with text-slop. Name every item:
  「电视先播新闻、再播香水广告、最后是 <图片 4>」.
- **Selective motion blur as concealment**（来源：Field patterns）: where the
  model is weakest（镜头穿行的肢体、水遮挡的脸）— prompt blur onto exactly
  those elements to mask artifacts.
- **Reverse effects are post, never generated**（倒放=后期）: the model only
  renders forward-time physics — a rewind/time-reversal effect is forward
  plates reversed manually in the edit.
- **Screen realism**: the display reads as a physical panel being filmed —
  glass sheen and soft glare, dim reflections of the room on the surface,
  subtle pixel grid and gentle moiré, slight bloom, mild panel contrast shift,
  small off-axis perspective.

## 4. Crowds

Recipe: VARIETY reference + collective motion + ≤3 tracked individuals.

- **VARIETY reference (H3-compatible, within the 9-image cap)**: one
  character reference = one identity. To seed a population, attach a
  multi-character lineup sheet and name its role as a VARIETY-style
  reference — diversity lives in the pixels, not the prose.
- **Crowd = environment, not characters**: a single mass with collective
  motion — "人群整体向左涌动，如一体" — never name and track more than 3
  individuals.
- **State the number on medium shots**: "20+" — otherwise the model gives
  three people in one take and a hundred in the next.
- Foreground the tracked pair against the mass.

## 5. Prop detail / single critical action

Recipe: annotation in the reference + triple lock + positive steering.

- **Annotate the reference**: draw a red arrow onto the prop reference sheet,
  then encode the annotation in the prompt text so the model reads it.
- **Triple lock the single action**: once in the description, once in the
  timeline, once in a positive lock sentence.
- **Steer away positively**: "拇指避开左侧的音量键与下方的频道减键，只按
  频道加键" — never negation ("don't press").
- **Causal prop lock（因果道具锁）**: objects never move on their own —
  every prop event needs a visible physical cause with event order:
  「杯子只在可见袖口接触时翻倒」/「按键=完整机械事件——接触、2-3mm
  行程、咔哒、回弹；屏幕只在咔哒后亮」/「剑在手中纹丝不动，直到持剑
  人手臂发力」.

## 6. Prompted imperfection

Recipe: write the real flaws of the lens in — the clip then reads as
captured, not generated.

- Long-telephoto: heat-haze swim, constant nervous micro-tremor, mushy
  digital-zoom smear, a brief focus hunt before settling.
- Quantify atmosphere: "薄雾密度从 20% 渐增至 70%" — ramp percentages, never
  adjectives.
- Handheld: body amplitude and rhythm ("轻微手持，操作者的呼吸节奏");
  impact shocks bind to the event and recover.

## 7. Reference-mode identity discipline (H3 official × house consensus)

- Identity travels with the reference: when a character reference is
  attached, the prompt does NOT re-describe face, build, or wardrobe —
  re-description conflicts with the reference (both H3 official and the
  production corpus agree).
- State the inherit/exclude scope for every reference: "只取角色的外观与
  服装；不继承参考图的构图、机位、光线" — location references get an
  explicit ban on inheritance.
- Label rules stay H3 official (`<Subject N>` / `<Picture N>` / `<Video N>` /
  `<Audio N>` + retention markers).

## 8. Effect injection（特效注入 — 来源：higgsfield-seedance-vfx）

**蓝图：锁一半，改一半。** 提示词两半都写：锁（身份/服装/表演/构图/
运镜不变）+ 改（只改命名的那一个元素），最脆弱护栏（「面部与服装不变」）
在动作结尾**重复一次**。

### A. 加元素模式（在画面里加一个特效元素）
- 特效**行为随时间演化**，不只是存在：从哪开始、怎么蔓延、怎么移动、
  投什么光——「火从衣角燃起，沿衣摆蔓延，火焰在风中偏向画左，火光
  映在脸上」
- 特效**与画面互动**（成败关键）：火光映脸、接触阴影、玻璃折射背景
  ——模型会自动把特效光弹到主体上，命名互动使其可靠
- **尺度显式**：巨型物写「巨大，其身体使建筑显得矮小，相对桅杆明显
  庞大」，否则模型渲染成正常尺寸
- 主体通常保持 oblivious（无视特效、继续做事）——写明

### B. 换环境模式（保留主体换整个世界）
- 新世界以**视差一致**的速度流过（车在开，新环境要有路面和以正确速度
  掠过的景物）
- 运动越大，锁清单越长（不只脸——车/安全带/机位/全程运动全锁）
- **暖调方向光世界对身份更安全**；夜/霓虹强制重打光提高漂移风险——
  用户要夜/霓虹时标注权衡并写明重打光指令

### C. 光影集成（成败点）
- 特效光与场景光互作用：写「玉牌青光映在脸上、照亮周围一圈地面」——
  命名互动使其可靠

### D. 行为化音效（SFX line）
- 不写「火」，写「火燃起时一声轻柔的呼响，随后低沉平稳的火焰轰鸣与
  噼啪，偶有火星爆裂」——行为化 + 同步到可见动作

### E. 前置 intro 预算
- 特效镜的 intro（前置铺垫）+ 剩余 = 总长——intro 时长先定，特效主体
  在剩余时间内完成，不挤压

### F. 光影集成叉路（先选叉路再写 — 来源：seedance-vfx）
- 叉路 1：保留主体光，只调新元素的光影（锁定主体原光，新元素匹配现有
  键光——身份风险最低）
- 叉路 2：全帧统一重打光（主体含在内——脸风险高，身份/表情/服装显式
  锁定，只允许光和调色变）
- 颜色匹配不够（「看起来像贴的」最常见原因）——完整配方：同键光方向
  （命名屏幕左/右）+ 同柔和度 + 同阴影密度方向；环境反弹（冷天光从
  上方/暖反弹从地面）；光学大气匹配（微反差/大气雾/景深/胶片颗粒）；
  边缘接地（去硬边光晕/不匹配边缘光/可信深度）

### G. 生物/硬表面元素集成
- 真实解剖细节 + 刻意不完美（皱纹/裂纹/下垂/不对称/泥污/哑光——真实
  生物不均匀）；物种行为匹配（蛇不眨眼用不眨眼凝视）
- 尺度参照（树上/建筑/主体对比）+ 长焦幻觉浅景深遮巨型物 + 运动模糊
  手持柔和——融入不漂浮
- 入板：与主体同太阳方向同色温；真实软边接触阴影；同雾同景深
- CG 感修复=第二输入（真实动物/材质参考照片，声明为纹理参考）——
  先展示比描述好

### H. 定时相机同步对白（双重锚定）
- 语义线索：「在台词『<逐字>』处，镜头[急推|平稳推近]」+ 数字线索：
  「约 T 秒处镜头……」——双锚定即使内部定时漂移也落点
- crash zoom（快速硬冲）vs smooth push-in（缓慢稳定）——匹配用户用词；
  地标保持可见显式声明（「塔全程在框内，绝不裁切」）；触发后留 2-3s
  尾巴；短片段在台词首词触发
- 揭示拉出：紧开新增元素隔离特写（主体出画）→ 拉出落真实板——落点
  要求 100% 匹配源构图（同角度/头顶空间/地平线/镜头性格）
- **定时锚定 3 细则（来源：higgsfield dialogue-timing）**：①锚定台词必须
  保留对白轨（不写纯音乐覆盖——对白不存活则无法锚定）；②唇形窗口检查
  ——台词自然朗读时长必须 ≤ 剩余窗口（约 6s 台词不能承诺进 5s 窗口）；
  ③时长任何变化必须重算数字锚 T——先算 总长−前置 intro=剩余窗口，再
  承诺台词落点

## 9. Edit Shot 配方（定向补丁 — H3 任务类型「视频剪辑 video editing」）

**用途**：修改既有生成的特定元素（换服装颜色/移除背景人物/换道具/改
表情），其余一切保持不变。改一处，重生成。

**Keep Rule（核心）**：同时声明「改什么」与「保持什么」——只列变更
清单 + 显式保持清单（身份/构图/光线/相机行为/其余全部）。

```
变更：<Picture N> 服装参考，本镜改为深蓝色（其余不变）。
保持：面部身份、构图、光线方向、相机运动、场景布局、其余道具——与
原生成完全一致。
```

**H3 写法**（Ref2VA 六段内的视频剪辑任务类型）：
- 摘要：任务类型：视频剪辑 + 参考生成；目标：修改 [元素]，保持其余
- 保留分析：<Video N>/<Picture N> attribute_transfer：只改 [element]，其余完全保留
- 详细描述：只写变更元素的描述（+ 保持声明），不重写已保持的内容
- 变化必须单一（一次改一个元素；多个改动拆多次生成——模式互斥）

## 10. 类型节拍（收窄版 — 来源：官方 templates 03 恐怖/08 喜剧/01 追逐）

写类型戏前查命中模板；**原分镜已写内容优先，模板只补缺**（不覆盖原镜、
不篡改——补的是分镜未覆盖的节拍惯例）。

**恐怖（氛围型）**：假平静开场（日常动作建立真实感）→ 慢运镜积累（观众
先于角色察觉「有什么不对」）→ 一个「错误细节」（微妙异常，非血腥）→
摄像机倾斜/定格=心理失衡；一镜只放一个错误细节（一镜堆多个惊吓=失败）；
恐惧靠氛围与留白（安全改写：氛围不血腥）。

**喜剧**：节奏即笑点——延迟/死板停顿（「长时间啜咖啡，缓慢眨眼」）；
「非反应就是笑点」（明确写「他不回头」）；静止相机+中景（让观众当
剪辑）；高调亮光；一镜一个笑料（一镜多笑料=失败）。

**追逐**：一个障碍+一个回应=干净动作节拍（「金属门落下，她不停步滑铲
钻过」）；每镜一主一辅动作上限；一镜一个相机运动；环境细节填死空间
（蒸汽/湿面反光）。
