# Performance & Physics — living faces, weighted bodies

> How to write performance the model can render: observable muscle work and
> causal physical chains — never abstract emotion words. Syntax stays H3-
> official; this module supplies the craft.

## 1. Reaction chain (performance in seven beats)

Write performance as a reaction chain in order:

```
trigger → micro-pause → eye shift → breath change → facial change →
body-weight shift → settle
```

Express emotions as visible behavior, never as abstract feeling words.
"looking surprised" is a failure; "she freezes mid-motion for half a second;
her eyes shift first toward the door gap, then her head follows" is not.

- The reaction starts before the other line ends: a listener gets the point
  mid-sentence and their face already answers.
- Emotion does not switch off instantly: after a heavy moment the breath is
  still uneven, the hands still unsteady — that tail carries into the next
  clip and stitches the cuts.

## 2. Micro-life rule (against frozen faces)

In static shots: **one visible micro-event every one or two seconds** — the
breath lifts the chest, a nostril moves, a brow tenses and relaxes.
微生命幅度要足够被镜头捕捉（实测教训：幅度含蓄会被模型忽略）——呼吸写
「胸口明显起伏」不写「呼吸平稳」；发丝写「扬起离开轮廓线再落下」不写
「微动」；衣摆写「摆动角度可见」不写「飘动」。

- Phased blinking: "one lazy blink → a quick DOUBLE-BLINK → one HARD
  reset-blink."
- A clear gaze direction, or darting eyes. A fixed stare reads dead.
- Describe stillness as held tension, never as a freeze. "Nobody moves"
  freezes the frame itself.

## 3. The five-stage physical chain

Write actions as a complete chain — contact and reaction are the most commonly
dropped stages; without them the action floats:

```
preparation → exertion → contact → reaction → settle
```

- Honor weight, inertia, and friction: heavy objects start and stop slowly and
  demand a weight shift; on a sudden stop the upper body and carried items
  lunge forward before settling; wet, icy, sandy, and metal surfaces have
  different foot feedback.
- Contact shadows and indentations help an object read its weight.
- Action beats = intent + named technique, not biomechanics: "spinning back
  kick connects" yes; "left forearm rotates 45° to deflect" no. If the user
  names a move, preserve it; if they describe joint mechanics, compress to the
  move's name or intent.
- Process continuity first (H3 official): write how the subject gets from A
  to B — the observable path — never the result alone.
- **因果动作链要素闭环（来源标定：`[COMMUNITY_CRAFT]` 高阶实践）**：动作描写拒绝结果化抽象跳跃，动作链必须形成可观察的因果闭环；复杂接触动作按需要补足部位、方向、速度、力度、惯性、接触和可见后果，不把七项机械罗列为标签清单：
  - *因果闭环核心*：发力主体/部位 -> 位移方向与速度 -> 接触目标与接触面 -> 受力对象的可视后果与惯性代偿。
  - *正例（自然融合因果）*："他的右手迅速向前推击，小臂随惯性微送，掌缘实打实撞上杯身，杯身骤然倾斜且茶水向外晃动溢出。"
  - *反例（无因果跳跃/仅写结果）*："他愤怒地打碎了杯子。"（缺少位移、接触瞬间及力学反馈，模型极易产生漂浮、变形或跳帧）。
  - *反例（机械标签堆砌）*："部位右手，方向向前，速度快，力度大，惯性前送，接触杯子，后果打碎。"（禁止堆砌技术名词，必须写为通顺的视觉描写句）。
- **非对称速度比与叙事动机锁定（来源：h3-prompt-journal 案例 002 — 双人/多人防动作平均化）**：
  多人交互或同台动作时，严禁使用“一快一慢/动作迅速”等虚浮词。必须设定明确的动作动作频率或比例（如 `3:1 action ratio`，主体 A 快速完成 3 次连击试探时，主体 B 仅做 1 次沉稳格挡），并为滞后者补充显式心理/物理因果动机（如“观察对方破绽并微滞后反应”），杜绝两主体被模型渲染成均速同步木偶。
- **极细部位安全象限与屏幕配额（来源：h3-prompt-journal 案例 008 — 手部及微道具防畸变）**：
  手部动作、系绳、握持道具等精细部位，强制限定屏幕面积占比区间（建议 `occupies 10%–18% of the frame`），并明确所处屏幕下半象限（如 `anchored in the lower-right quadrant, does not occlude the face`）。禁止超大特写（手指占全屏 >40% 极易生出第六指或骨骼融合）。
- **镜头动量与光学惯性联动（来源：h3-prompt-journal 案例 003 — 运镜与主体力学共振）**：
  摄影机并非悬浮无质量传感器。主体大位移急停时，运镜须带有同向微小惯性过冲并回弹稳定；主体由中景移入前景时，景深随之自然拉动（`shallow depth of field, focus smoothly pulls from background environment to the moving subject`），使光学景深与主体力学紧密锁定。
- At the contact moment, write force and direction as a single result state:
  "driven into the car, metal buckling" yes; "thrown into side door, glass
  shatters, uses rebound to sweep leg" no — multi-step destruction chains are
  banned (neither process continuity nor official vocabulary).
- Complex actions open the prompt already in motion ("he is ALREADY
  mid-swing"), then develop the process.
- Only describe what can be seen or heard. "the air smells of pine" no; "pine
  needles covering the ground, wind moving through branches" yes.

## 4. Special-action detail（专项动作清单 — 来源：地狱 action-physics-vfx）

### 投掷（必写项）
哪只手/怎样握持；准备姿态与重心；释放点、方向、速度与轨迹；物体旋转
或姿态；撞击目标、接触点与反弹/嵌入/破碎；投掷者随后的手臂、躯干与
重心。

### 撞击与碰撞（不只「爆发」）
接触前速度与方向；接触材料（肉体/布料/骨骼/金属/玻璃/混凝土）；瞬间
压缩/弯曲/破裂；受力者位移与旋转；施力者反冲或减速；声音/粉尘/碎片/
火花/液体；撞击后谁保持控制、谁失去平衡。摄影机震动只绑定到明确冲击
时刻并短时恢复——不整镜随机抖动。

### 爆炸时间线（毫秒/秒级阶段，不伪造精度）
预兆（光/压力/裂纹/声音变化）→ 起爆（明确源点与方向）→ 火球/冲击波
（传播速度与遮挡）→ 碎片（材料/尺寸/轨迹/危险区）→ 人物反应（先受光
声还是先受冲击）→ 环境变化（门窗/尘土/火焰/照明）→ 余波（烟/落尘/
耳鸣/物体落定）。不同时要求爆炸巨大+所有人不动+环境无变化+声音安静。

### 粒子/烟/雨/雪/液体（必须有来源、方向、尺度、速度、受力和结束）
雨打湿皮肤衣物地面并形成反光滴落；风统一头发衣摆烟雪方向；灰尘从撞击
点扩散、受光束显形并沉降；液体飞溅来自接触点并留下新状态；火花沿金属
切线或撞击方向飞出。只写「很多电影感粒子」= 无来源噪点。

### 环境视效反馈（强动作必查六项）
地面（脚步/压痕/碎裂/水花）；空气（尘/雾/烟/压力波）；光（火光/闪烁/
遮挡/反射变化）；声（接触/破裂/回声/底床变化）；道具（位移/旋转/损坏/
落定）；人物（衣物/头发/呼吸/姿态/伤势）。

## 5. Performance hierarchy

- Main performer: carries the shot's narrative change.
- Secondary performers: one visible reaction or hold state.
- Background characters: low-amplitude continuous action or stay still.
- Never let every character move, turn, and gesture at full intensity from
  frame one.
- **运动幅度分层（来源：remotion 分层视差 — 防「整体平移感」）**：主体
  大位移（反应幅度明显）/ 中景中等 / 背景仅缓慢推近或静止——指定各层
  运动幅度差，绝不写「全画面均匀微动」（AI 视频常见失败：所有元素一样
  动，画面像整体平移）。

## 6. Emotion translation dictionary（情绪转译词典 — 来源：地狱 performance-direction）

每镜选少量信号（2-3 通道），避免所有通道同时最大强度：

| 情绪 | 信号组合 |
|---|---|
| 克制恐惧 | 视线锁住威胁不快速乱看；吸气后短暂屏住、胸口轻微抬起；下颌收紧嘴唇微张不说话；重心后移脚未迈出 |
| 突然震惊 | 触发后短停顿；眼睑张开瞳孔目标固定；呼吸中断或快速吸气；肩头只做一次小幅后撤 |
| 压抑愤怒 | 视线持续不扫动；下颌和咬肌收紧；呼气更慢肩线压低；握紧物体后克制地放松（不挥舞） |
| 疲惫 | 眨眼变慢、视线抬起需要时间；肩膀下沉重心依赖支撑物；呼吸较深动作前有延迟；手部抓握不完全仍保持真实接触 |
| 决心形成 | 先看向障碍或目标；呼吸从短促变为一次稳定呼气；重心从后脚移到前脚；眼神下颌稳定后动作开始 |

**六个表演通道**（含风险）：视线（看谁/何时移开/是否锁住——「看左」含糊）/
面部（眉/眼睑/嘴角/下颌/吞咽——堆太多变夸张）/ 呼吸（吸气/屏住/呼出——
未授权声音污染对白）/ 停顿（反应前后留白——时长不足被压缩）/ 身体重心
（前倾/后移/脚步/肩线——与动作物理冲突）/ 手部（抓紧/松开/悬停/触碰——
左右和道具接触易漂移）。

**微停顿与反应因果**：触发 → 0.2 秒停顿 → 眼睛先 → 头肩随后 → 手收回 →
落定。时间短时至少保留触发+一个可见反应——不把五个表演节拍压在半秒内。

**视线细节**：目标（人物/道具/空间锚点/画外）；屏幕侧/是否移动；眼睛先
还是头先；何时断开；多人谁看谁谁回避。不写「看向左边」——写「眼睛先
转向屏幕右侧门缝，头部随后只转约四分之一侧面」。

**身体重心**：站立（重量在哪只脚/膝盖是否锁定/靠墙桌）；起步（重心先移
支撑脚另一脚才离地）；后退（身体先缩回还是脚先探路——决定恐惧程度）；
下蹲（髋膝脚上身协调）；撞击（重心被打断身体道具反作用）。无重心描述的
「突然下蹲」易成整体下沉或穿地。

**静止写法**（允许范围明确）：「身体位置、头部方向、双手和脚步保持不变；
允许自然眨眼和很弱的胸口呼吸；不说话、不转头、不做手势。」雕像级零微动
=明确禁眨眼呼吸（但降真人感）；「保持静止」与「持续强烈恐惧」可共存——
恐惧靠已建立表情+低幅呼吸保持，不新增动作。

**对话表演三阶段**：台词前（视线/吸气/决定开口微停顿）；台词中（自然
语速/重音/强度/是否保持视线）；台词后（嘴唇停止/呼气/视线或身体落点）。

## 7. Keep the hands busy

A character does not "have a conversation" — they fix, count, pour, and talk
over it. **The strongest accent of a scene is the moment they stop that work**
because of what they just heard.

## 8. States-not-transitions + three-beat rhythm（来源：higgsfield-acting/prompt）

- **状态而非过程**（表演核心）：写「投掷中、手臂伸展、重心已移前」——
  不写「伸手、掏出、抡起」的过程动词；模型渲染状态不渲染过程序列
- **三拍动作节奏**：蓄力 → 爆发 → 余波（力量/速度/后果三段）——与
  五段物理链互补（节拍级 vs 接触级）
- **情绪分解模板**（「哪种惊讶？」四选）：肌肉（下颌/眉/眼睑）/ 呼吸
  （屏住/急吸/缓呼）/ 眼神（瞳孔/视线/眨眼频率）/ 皮肤（汗珠/血色/
  鸡皮）——先问「哪种 X」再写信号
- **分层情绪三通道**：主导态 + 底层态 + 可见破绽（「表面平静（主导），
  底层紧绷（底层），指尖轻颤出卖了他（破绽）」）
- **眼部生命三要素**（防死脸）：微扫视（0.3s 级视线跳动）、眨眼质量
  （分阶段眨眼）、眼神领先转头

## 9. INNER (unspoken)

One line of inner monologue per stretch of action — what the character thinks
and wants — marked `INNER (unspoken)` so it is never voiced.

## 10. Named micro-expressions + recipes（命名微表情与身体微拍 — 来源：soul/facs）

**命名微表情库（精简 10 个，配生理描述——中文直用）**：
死板中性（目光平直、唇线松弛、无眨眼节奏变化）/ 凶猛专注（下颌前顶、
眉峰下压、瞳孔收窄）/ 微妙傲慢（单侧嘴角微抬、下巴微扬、视线抬高）/
捕食者凝视（头微低、眼白少露、身体前倾缓）/ 压抑微笑（嘴角上提但
眼轮匝肌不动、呼吸放慢）/ 安静毁灭（面无表情+指节缓慢握紧+视线不移）/
苦涩逗趣（笑意到一半塌掉、叹气带笑音）/ 沸腾怒意（咬肌搏动、颈动脉
可见、无眨眼）/ 疲惫强撑（眨眼变慢、视线聚焦延迟、肩线下沉）/ 决意
（一次稳定呼气、重心前移、下颌收紧）。

**情绪-AU 配方（中文化）**：真诚笑=眼轮匝肌收缩（眼角起皱）+颧肌上提；
礼貌假笑=只有颧肌；怒=皱眉压眼+睑裂变窄+咬肌收紧；悲伤=眉内侧上抬+
嘴角下拉+下唇微颤；恐惧=眼睑上提+瞳孔放大+嘴唇张开。

**身体微拍食谱**：怒=咬肌搏动+颈动脉跳动+无眨眼+汗珠渗出；抑制情绪=
咽口水+眼含泪不落+单次下巴颤动；紧张=指尖轻颤+重心频繁微移。

**表演纪律**：每镜 3-4 个表情上限（不堆）；群像反应错开 0.3-0.5s
（不同步）；默认防 AI 味（默认不流泪、无卡通怪相、虚化背景也有微
动作）；坏表演症状 15 种自检（僵硬=只有情绪词/过度=所有信号最大化/
抢戏=未分主次/嘴动=声音边界缺失/顺序错=无触发链/乱动=静止范围含糊/
断裂=无 continuity in-out…）；「However, when X…」面具裂缝句（正常
状态 + 什么时刻裂缝）；**in medias res**（默认从中段开始，不用建立镜
开场——开场 3 秒必须建立谁+在哪+异常）。

## 11. Quantification anchors

Give scene elements numeric anchors where they matter — measurable beats
adjectives:

- Distance in metres: "约三米宽" / "相距两步".
- Rain/snow/fog intensity as levels or percentages: "雨势渐强，从细密到
  倾盆" / "雾气密度约三成，近景清晰远景模糊" (Chinese output keeps the
  ramp; API English uses 20% → 70% style).
- Light character: source, direction, hardness — "一盏暖琥珀色灯笼，光从
  画右上方斜落，硬边阴影朝向画左".
- 色温开尔文（场景内固定）: 3200K 暖钨丝 / 4000K 日光灯 / 5600K 正午
  日光 / 8500K 阴天冷调——「主光 5600K，烛光 3200K 局部」.
- Speed in km/h style where meaningful: "以约 40 公里时速掠过"（H3 官方
  at fast speed 保留，量化作补充）.
- Physical masses in real units (physics reads gravity): "剑重约 1.5 千克，
  出鞘带出明显下坠感；玉牌 30 克，落地轻响".
- Atmosphere ramped across shots: 雾密度 20% → 40% → 60%，跨镜递增，
  不写形容词.
- Wind level by effect, not speed: "风把衣摆横着吹平" / "灯笼剧烈摇摆".
- Timing anchors: beat start/end are described in natural language and official shot cut times; `[Xs-Ys]` windows are not part of the final Ref2VA prompt.
  "then quickly".

## 12. Spatial discipline (per-shot)

- Lock coordinates in natural language, never percentage coordinates alone:
  camera side, anatomical side, and nose direction written together when
  left/right matters.
- Declare the exact person count when it matters; mirrors, screens, posters,
  and glass reflections may be read as extra people.
- The exit state of the previous shot is the open state of the next shot — do
  not just write "continuous."
- Off-screen = nonexistent: state changes must be shown on camera before being
  referenced.
- **存在感/整合锁（来源：官方中文版示例「存在感/整合」— 防贴背景平面感）**：
  角色/道具必须实际存在于场景中——大气雾或烟雾穿过角色前后并偶尔遮挡
  边缘；双脚踩实地面、有接触阴影；背光时边缘轮廓成清晰光脊；耳/鼻尖
  次表面散射；与场景同光源、同氛围、同运动物理。写接地、遮挡、投影三
  件事即可。
  **适配边界（2026-08-16 拷问确认）**：官方写法依赖雾+背光场景；**无雾/
  无背光场景**（如室内白日戏）同样要写存在感，换成三件事——①接地：
  脚踩地面+接触阴影（鞋底与地面接触点）；②环境互动光：光从门缝/窗/
  灯投到角色身上与地面上，证明他在空间里（有光源才有受光面）；③遮挡
  关系：角色/道具挡住部分背景元素（身体边缘切割门框/墙面），或前景
  穿行掠过。
- **高精度光学物理光场与明暗交界线规约（Key Light World-Vector & Terminator Line Dynamics）**：
  - **世界坐标主光源三要素绑定**：绝对禁止“光线柔和/光影交织”等无物理意义废话。主光源必须在世界固定坐标中声明**三维向量（方位角与俯角）**、**色温属性**与**物理衰减介质**。例如：`"A single dominant tungsten key light (3200K) is anchored rigidly at the upstage-left ceiling corner (45-degree azimuth, 60-degree elevation), casting sharp geometric drop-shadows across the wet wooden floor."`
  - **明暗交界线（Terminator Line）动态位移演算**：当主体发生旋转、转头或位移时，明确描写明暗交界线如何沿骨骼表面物理扫掠，彻底消除光斑闪烁与死黑：`"As <Subject 1> turns his head 30 degrees toward the camera, the sharp terminator line between the illuminated cheek and the shadow side sweeps smoothly across his facial contours, progressively revealing the micro-texture of his skin without lighting flicker."`
  - **菲涅尔边缘光与次表面散射（Fresnel Rim & Subsurface Scattering）**：强背光或侧逆光环境下，明确耳垂、鼻翼与发丝的半透明光晕（`subsurface scattering glows warm amber through the edges of the ears and nostrils, while crisp razor-thin Fresnel rim lighting defines the silhouette separation against the dark background`）。
  - **焦平面与三层物理散景阶梯（Three-Plane Optical Bokeh & Focal Pull）**：强制划分三层光学空间，拒绝全景均焦塑料感：
    - *前景（Foreground）*：近镜头物体化为巨大柔软散景（`ultra-soft out-of-focus bokeh disc`）；
    - *中景（Focal Midground）*：焦点死锁在主体瞳孔与发丝，呈现刀锋级锐利（`tack-sharp critical focus on the pupils`）；
    - *背景（Deep Background）*：随距离呈物理平方反比衰减模糊，将杂乱光源化为圆形焦外光斑（`background streetlights dissolve into creamy circular light circles`）。
- Exit-frame = implicit cut: a character who leaves frame is gone for the
  remainder of that shot.
- Keep one primary narrative function per shot, one main visible event, a
  clear ending point. Acceptance test: after removing style words, the shot's
  reason to exist is still clear.

## 13. Wording discipline（措辞纪律 — 聚合节，唯一维护点）

### 13.1 Lead with the load-bearing element（首句承重）

Every shot's first sentence carries the single most load-bearing element —
the subject's key action, or the primary camera move when the shot lives on
it. Attention weight decays left to right; past ~3 sentences detail stops
being primary instruction. 中文：每镜首句写承重元素（主体关键动作或主运镜）；
首秒宽镜自然承重（[0s-1s] 第一句锁站位）。

### 13.2 Empty-adjective blacklist（空形容词黑名单）

`epic` / `beautiful` / `stunning` / `breathtaking` / 震撼 / 绝美 / 史诗感 —
high-frequency labels sample a diffuse distribution and move output toward
nothing. Substitute a named, observable referent: a lighting setup, a
material fact, a composition fact. H3 official style words (7 类：电影感/
真人实拍/2D 动画/3D CG/定格黏土/水彩/复古胶片) are legal vocabulary and are
NOT blacklisted. production-discipline §7 ban dictionary defers to this
section. 抽象词的替代翻译（情绪/心理/氛围 → 可拍画面，含三步决策树与
示例库）见 `abstract-translation.md`。

### 13.3 One fast element per shot（高速限量 — 经验规则）

H3 official supports `at fast speed`, but multiple fast elements stacking is
a degradation source (empirical, not official): two competing fast elements
jitter; three compound into hard-to-salvage error. Rule: ONE fast element
per shot; describe physics, not speed — "feet striking hard, each stride at
full extension" reads fast with no degradation.

### 13.4 Chinese homograph disambiguation（中文消歧）

Camera verbs and action verbs coexist — disambiguate by naming the object:
「镜头推近」vs「他推门」、「镜头摇过」vs「他摇头」。歧义词加宾语/语境消歧；
黑名单靠实测积累（当前无禁用清单）。

### 13.5 Anti-impact locks（防撞击锁）

Destruction / breakage scenes describe pressure and stress origin, not an
impact point: 「裂纹从边缘应力起源，自边缘向中心蔓延」/「人群按压而非
撞击」/「无撞击点 — 压力式破裂」。This is a result-state description and
merges with process continuity (never a multi-step destruction chain).

### 13.6 运动质感词表（来源：jineng 02-3d-cgi 12 原则 + 03-cartoon 描述符）

**运动质感描述词**（各带定义，直用）：弹性（压缩-拉伸-回弹，如 果冻/皮筋）/
流体（连续变形，如水/丝绸）/ 僵硬（无缓冲直停，如 木偶）/ 漂浮（无重力
感，如 羽毛/幽灵）/ 冲击（骤停+反震，如 撞击/落地）/ 摇晃（低幅高频，
如 颤栗/疲劳）。

**12 条动画原则提示词化**（动作写法可直接引用）：压缩拉伸（落地压扁-
弹起拉长）/ 预期（发力前蓄势停顿——与五段物理链「准备」互补）/ 跟随
重叠（主动作后衣发尾随半拍）/ 弧形（手臂挥动走弧线不走直线）/ 缓冲
进出（起止慢中间快）/ 次要动作（主角动时头发衣摆独立小动）/ 呼吸
（静态时胸口起伏）。

**材质数值转自然语言（H3 无粗糙度/金属感参数）**：PBR 量化改「低粗糙
度/高金属感/次表面透光」等自然描述（与 lighting-color-material §6 材质
响应表配合）。
