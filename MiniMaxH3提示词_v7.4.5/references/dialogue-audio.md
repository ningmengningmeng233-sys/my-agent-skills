# Dialogue & Audio — voices, lips, and the two-track mix

> Dialogue construction and the H3 two-track audio contract. Lines live in the
> timeline inside integrated_multimodal_description and in overall_soundscape;
> music goes in non_diegetic_music.

## 1. Dialogue line construction (fixed order)

A dialogue line in the prompt is always built the same way:

```
The voice and its emotion → the line in quotes → the physical action → the facial reaction
```

## 2. H3 dialogue syntax (official)

- Speaker IDs: `(S1)`, `(S2)`, multiple `(S1,S2)`.
- Content: `<d>[language] original text</d>` — verbatim, original language
  preserved (Chinese stays Chinese).
- Mixed-language policy: control instructions English; dialogue Chinese.
- Cross-shot line: `<scenetrans>` + `carries over from the previous shot`.
- Interruption: `<cutoff>`.

## 3. Voiceover (HARD requirement)

Voiceover MUST use `says in an off-screen voiceover` AND immediately declare
the lips stay closed: `while his lips remain completely closed`. Missing
either phrase risks visible lip movement.

## 4. Hard speech blocks

- Everyone speaks ONLY the line in quotes; whoever has no line stays
  completely silent.
- A "half-laugh" written in the action is a facial expression, with no sound.
- Rare names get a transcription, or the model breaks them.

## 5. Voice locking

The voice descriptor is pasted as-is every time the character speaks, never
changed: **register, tempo, accent, manner.**

```
Voice: deep, gravelly bass-baritone; slow, calculated pacing; London street
accent; menacing calm — he never raises his voice.
```

Stress-test the voice between generations the same way as the look. If it
drifts, lock it harder.

## 6. Mix writing

Write the mix too: voices clean and close to the microphone, ambience under
them, ambience dips when someone speaks.

**三层音频物理隔离与混音规约（来源：h3-prompt-journal 案例 004/006/007）**：
- **对白层（Dialogue）**：严格由 `(Subject N)` 与 `<dialogue>` 驱动，画外音统一使用双闭环句式（`says in an off-screen voiceover while his lips remain completely closed`）；未发声角色强制追加 `remains completely silent without lip movement`。
- **音景层（Soundscape）**：在 `overall_soundscape` 中只容纳物理介质声音（环境白噪、脚步摩擦、撞击声）。显式注明音量动态避让：“当对白发声时，环境音量自动衰减低伏（ambience dips under the spoken lines, dialogue remains prominent and isolated）”。
- **配乐层（Non-diegetic music）**：音乐按电影情绪功能分层递进（`build-up → rhythm pulse → dynamic release`），音乐重音精准锚定画面关键转折时刻，严禁在配乐字段混入角色台词或具体发声动作描述。

## 7. Seam tricks

- On wide shots of a dialogue, feed the tail of the previous line into the
  prompt — it helps the lips and the rhythm.
- Open every new generation with the line that closed the previous one, so the
  emotion crosses the seam together with the text.

## 8. Two-track audio contract

- `overall_soundscape`: environment / action / non-verbal human sounds, 1-4
  sentences (wind, rain, footsteps, impacts, breath). Named, ordered, timed to
  the timeline where it matters.
- `non_diegetic_music`: instrument + tempo + rhythm + dynamics, 1-3 sentences.
- Music policy is a project choice; when the project bans score, write
  diegetic-only and let music live in post-production.

## 9. Dialogue lock full form（逐字对白锁完整版 — 来源：地狱 dialogue-audio）

逐字对白必写：说话者（稳定 ID）/ 发声时间（开始-结束）/ 精确台词 /
语速音量情绪（可听参数）/ 口型（只在精确台词期间运动）/ 尾部（最后
一字后嘴唇静止并保留静默尾拍）。「只说这句」仍明确：无加词、无语气词、
无即兴、无额外笑声/叹气/呻吟。

**发声时间**：自然语速朗读实测（不按字符数猜）；台词前留吸气或决定
开口的短停顿；台词后留静默尾拍让表演落地；对话轮次不无意重叠（抢话
明确重叠区间）；动作与对白共享镜头时长（不能各自占满）。

**对白硬规则重复两次（来源：seedance2-chinese-guoman）**：对白锁在
提示词内**前后各完整重复一次**（时间轴前第一条：只允许出现以上 N 句、
严格按时间轴顺序、不得漏说改写、无字幕无旁白无内心独白；时间轴后第二
条：非说话人物必须闭嘴禁止张嘴）——模型无记忆，重复强化。
**对白不视觉化声明**（台词提过去事件/未在场人物时严格写）：
「对白内容只作为声音信息；对白不视觉化。没有闪回、插入画面、幻象、
额外人物或场景变化。画面只包含当前允许集合。」需要闪回=设计成明确新
镜头，不让台词自动决定画面。

**呼吸与声音边界**：呼吸在台词前/中/后写清；区分可见胸口起伏与可听
喘息；无脚本声音不自动加呻吟/笑声/叹气/语气词；「半笑」=面部表情
（除非明确要求可听笑声）。

**声音空间**：屏幕内/外；摄影机前后左右；近中远；室内反射或室外开放；
声源是否移动；遮挡/门墙/头部转向的影响。画外喊声来自摄影机后方时，
明确说话者不在画面边缘/背景/倒影中。

**环境底床分类**（写声源远近方向，不用无来源「电影氛围音」）：室内
（HVAC/冰箱/灯具电流/管道/房间反射）；城市（交通/远处人声/风/雨/信号）；
荒野（风/昆虫/水/植被/远雷）；工业（机器低频/金属振动/蒸汽/警报）。

**混音优先级公式**：关键逐字对白 > 叙事关键音效 > 表演呼吸/拟音 >
环境底床 > 音乐（非固定，声音主导镜头可让环境高于对白，但必须明确）。

**音乐/字幕/旁白边界**：无音乐写「无配乐、无乐谱、无自动情绪音乐」
一句即可（不重复十种同义句）；字幕与画面文字是不同轨道（无字幕≠招牌
消失）；无旁白明确无额外解释声；发布字幕属交付阶段，生成画面不烧录
字幕。

## 10. Offscreen voice-only character（画外纯声音角色 — 来源：PRODUCTION-PATTERNS）

说话但绝不入镜的角色，显式声明（调和「画外=不存在」规则——不让模型
发明身体或拉入画面）：

```
（母亲是画外声音——无画面参考、绝不入镜、画面中不出现她的任何部分）
```

## 11. Dialogue budget + lip rule（来源：higgsfield-audio）

- **对白预算**：每角色**每镜头（单镜）**1-2 句；单个片段（多镜合计口径）4—10s 总量
  3-4 句；片段内对白约 25-30 词（中文按自然朗读实测，机器判定口径 3—4 字/秒见
  config/duration-policy.json）；保留权力反转句（删装饰不删关键句）
- **时间片分配表（来源：guoman storyboard-template）**：10s=2-3 句 /
  15s=3-4 句（15s 是 H3 模型能力上限，非技能默认目标；技能默认片段范围 4—10s——
  原 20s 档不适用）——0-2s 开场（无对白）+
  每句约 3s（含反应镜头）+ 最后 1s 沉默收束（结尾衔接帧）
- **一镜头一说话脸 + 锁相机**：唇音可靠需 MCU 构图、单说话脸、锁相机、
  删除所有点头/转头 token——多动作时唇音漂移
- **diegetic-only 约定**：提示词只写场景内物理存在的声音（对白/拟音/
  环境），配乐属后期叠加

## 12. Speaker identity establishment (H3 official)

On a speaker's FIRST appearance, provide enough information: role type, age,
gender, whether on-screen, pitch, timbre, tempo, accent.

```
The old fisherman in a patched raincoat, around sixty, hoarse and slow, (S1)
says: <d>[Chinese] 起风了。</d>
```

Age is allowed and required by H3 official docs — the Seedance age-blind rule
does not apply. Keep the ID stable across shots.

## 12b. 画内念白 vs 画外音判别（2026-08-16 端到端测试 F15）

「抬头念白/画内念白」= 画内对白——**唇动**、首次说话者 8 项身份
（dialogue-audio §12）、锁相机（一镜头一说话脸）；「画外音」= 脸不在
画面——**嘴唇闭合声明**（§3 HARD）。
**判别**：说话者的脸在本镜画面内 → 画内（唇动+8 项身份+锁相机）；
脸不在画面 → 画外（嘴唇闭合）。同一术语不因执行者不同而两种处理。

## 12c. 人声绑定（voice binding — 2026-08-17 用户反馈补）

有对白/画外音的角色必须做**声音绑定**：把角色的声音特征与其台词绑定，
同角色跨镜（含续写/多块）声音一致——模型无记忆，不绑定则每镜换声。

**绑定三式（选一，画内画外都适用）**：
1. **音色描述绑定**：首次说话时在 8 点身份里给声音四要素（pitch/
   timbre/tempo/accent）——「沙哑低沉的男声，语速偏慢，无口音」
2. **参考音色绑定**：引用输出契约第 1 项参考音色栏——「参考音色：用户
   提供且已授权的参考声线，男中音偏哑」——正文写「声音按参考音色栏绑定」。
   不得默认模仿未授权真人/演员声线；voice_rights_status=unknown 时先停下确认。
3. **参考音频绑定**：有 `<Audio N>` 时，交付提示词使用官方英文标签，例如：
   “<Audio 1> is the voice reference; the dialogue in this shot matches its
   timbre and delivery.”

**纪律**：绑定一次全程有效（跨镜/跨块不重写不换声）；画外音角色
绑定音色 + 嘴唇闭合（§3）双写；无参考音色/音频时按角色描述推断并在
假设清单标注（推断音色=假设，漏列=违规——与输出契约第 5 项一致）。

## 13. Lip-sync discipline

- Only the speaker whose line is in the window has visible mouth movement.
- Non-speaking characters: no lip movement; their "reaction" is facial only.
- Do not write "no sound" to fix extra music while dialogue is still present —
  that contradicts; scope the audio tracks instead.
