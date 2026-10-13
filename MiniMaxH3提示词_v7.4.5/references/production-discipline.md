# Production Discipline — the layer above the prompt

> Adapted from the Hell Grind feature-film pipeline (platform-neutral rules).
> The model has no memory. Every rule below exists because a shot failed
> without it. Syntax stays H3-official (see official-h3-grammar.md); this
> module supplies the production method.

## 1. The model has no memory

If a character is not fully described in EVERY prompt, the next shot gives
them a different face and a different jacket.

**Describe everything, every time.** The character descriptor goes into every
prompt word for word, never shortened. Consistency is not a setting; it is
repetition.

**Context isolation（上下文隔离）**: every generation is a blank slate. Never
carry in scene numbers, script headings, prior-scene summaries, unused tags
or characters, or "as above / continues" phrasing. Everything is rewritten
verbatim — the seam tricks (feeding the tail line) write the actual text in;
they never reference metadata.

## 2. Assets first

An asset is a pair: **text + image**. The text (the descriptor) is pasted
verbatim into every prompt. The image is the reference the model anchors to.
Neither works alone. References are assets only: characters and locations.
Everything else is prompt text.

- **Character sheet = three images, one headless**: (1) face close-up,
  (2) full body front — **headless**, (3) full body back. The headless front
  figure forces the model to source the face only from the close-up.
- **Keep the sheet deliberately boring**: neutral grey background, flat light,
  real skin with visible pores, no retouch. The cinema look lives in the
  locations and the video prompts.
- **Point changes go on with masks, never a second full pass.** Clothes,
  scars, blood are point changes. An image never runs through a model twice in
  full — after two passes the face turns plastic and lifeless.
- **Voice is a locked descriptor, not an asset**: register, tempo, accent,
  manner. Pasted into the audio field as-is every time the character speaks,
  never changed.
- **Acting is locked the same way**: one behavior paragraph per character
  (movement, hands, habits, nervous gestures, eye behavior, how they break
  under pressure). Scenes adapt the moment; the core never changes. A behavior
  physically impossible in a scene is transferred, not deleted.
- **Location sheets**: shoot in 3/4, not frontal. Leave an anchor in every
  location (a column, a lamp, a sofa) and tie staging to it. Keep ONE light
  logic: one source, one shadow direction, never two suns.
- **Name the role of every reference** — the model decides for itself if you
  do not, and it decides wrong. Location references get an explicit ban on
  inheritance: take only the space and the texture; do not inherit composition,
  angle, or grade.
- **One dictionary of names for the whole project.** The same tags everywhere:
  documents, prompts, interface.

## 2b. 参考资产构造法（来源：higgsfield-cinema Reference Sheet Family +
higgsfield-character-design — 2026-08-16 复核织入）

- **场景参考表五视图**：正面宽/左右斜/反向/细节特写 5 视角；复用只锁
  「建筑/光质/色调/环境细节」四属性，镜头只写 delta（不重释场景）
- **钢琴测试（服装复杂度=渲染预算）**：服装只留剪影+单一识别特征——
  细节多到「钢琴弹不出来」就删（繁复衣褶≠越多越好）
- **尺寸参照帧**：双主体同框时合成一帧「两角色+比例正确」的尺寸参照帧
  资产挂到每镜 + 人体高度比较描述（「翼展=20 人首尾相接」）
- **不对称锁**：尺度不确定时渲染**更小**绝不**更大**（缩小读作距离，
  放大读作假——双主体相对尺度是第一个漂移且文字修不了的项）

## 3. Scene map paragraph (scene-level, pasted unchanged into every shot)

A floor plan of the place in a few sentences: landmark objects, what is on
the right, what is on the left, where the camera stands. **No characters, no
action — only the place itself.** Written as flowing H3 prose — never as a
labeled block (the Seedance block scaffold is foreign syntax to H3; H3 reads
paragraphs).

```
The alley is a narrow stone lane, rain-soaked, about 3 meters wide. Warm
amber lanterns hang along the frame-right wall; cold blue rain light falls
from above. The camera stays on the south side of the alley and never
crosses the line between the two figures.
```

Rules:
- The map is only the map; the look of the place still comes from the
  location descriptor + reference, written next to it.
- Sides exist only from the camera: "frame-left" / "frame-right". The model
  does not understand "to the left of the character."
- Positions are set from landmarks and in metres: "at the altar", "three
  metres away."
- State which side the camera stands on and which line it never crosses.
- After every cut, name again who stands where and where they look.
- Give a static dialogue a corner of the room, not the whole room.

## 4. The first second is always a wide

One second at the start of a scene, no lines and no action: the model
"photographs" the arrangement — who stands where, what lies where, where the
light comes from — and holds it through every following shot.

- The "hm" trick: have someone say one short word during that second; it makes
  the model treat the wide as a separate shot.
- If the shot answers the previous one, feed the tail of the previous clip's
  line into that first second.
- This is a populated wide — full of people from frame one; it withholds
  ACTION, not people.

## 5. The character-count sentence

The scene description opens with a count sentence, not a bare description:

```
Exactly two characters appear in frame, no duplicates: SHEN and CLONE.
```

- Only those whose references are in the prompt exist in the frame.
- Counted props get a direct lock with a count: `Exactly one mannequin, never
  render a second one.` / `Five smashed mannequins, never re-rendered intact,
  never multiplied. Two trays, never more.`
- Counted objects are phrased as what IS in the frame.

## 6. Character acting block (production form)

Per character: emotional state · what they want in this moment · what they are
hiding · dominant body rhythm · visible habits in this beat · what changes
across the shot.

## 7. Wording rules

- Present tense. Short sentences.
- The camera is written inside the action, not as a separate aesthetic
  paragraph.
- Keep each beat light: up to three sentences per beat. Overload a beat and
  the model smears it.
- Length is not the enemy — an overloaded beat is. Structure replaces the word
  cap. (Hell Grind prompts ran 3,000-4,000 words; H3's hard cap is 7,000
  characters — budget inside it.)
- Actions only in positive form. The model ignores "does NOT fall on his
  back" — or does the opposite. Write "falls on his stomach."
- The character is in frame from the first frame, and never looks into the
  camera unless asked.
- Character identity follows H3 official: first-time speakers get the
  official 8-point identity (role type, age, gender, on-screen presence,
  pitch, timbre, tempo, accent) — see dialogue-audio.md §12. Age is NOT
  banned in H3 (official docs require it); the Seedance age-blind rule does
  not apply here.
- Keep a ban dictionary of words the model punishes, and grow it:
  `dark` → `low key`; `jolting` → `rapid motion`.
- Empty adjectives, fast-stacking limits, homograph disambiguation, and
  anti-impact locks live in performance-physics.md §13 (the single
  maintenance point for wording discipline) — defer to it.

## 8. The iteration loop

- Generate in batches, scene by scene.
- Every iteration is surgical: ONE line changes, everything else stays word
  for word. A prompt is a working mechanism — rewrite it fully and you lose the
  parts that worked.
- Everything goes into the log: prompt version, what changed, verdict.
- The ten-to-fifteen rule: if a shot has not come together in 10-15
  iterations, the problem is not the wording. Simplify the shot: split it in
  two, remove an action, change the angle.
- **单次生成策略（来源：higgsfield-cinema — 2026-08-16 打磨）**：默认
  一次生成=一次评审；第一次生成就按交付标准写——批量抽卡（生成 20 个
  挑 1 个）是预算失败；不「多生成几个碰运气」。

## 9. Deadline-born solutions

- Complex action never sits in the middle of the timing: the action opens the
  prompt — "he is ALREADY mid-swing, the door ALREADY cracking" — and the
  approach becomes a separate shot.
- A crowd is one "character" asset with a range of heights and clothes. On
  medium shots state the number directly — "20+" — otherwise the model gives
  you three people in one take and a hundred in the next.
- Transitions between two spaces hold on a threshold: both locations in one
  prompt, the seam is a doorway with a light contrast across it — "a warm
  amber room, a cold blue corridor beyond the arch."
- Giants live on scale anchors: a size comparison in every prompt PLUS a human
  figure in frame to measure against. Name the failure condition too.

## 10. Engine-risk table (flagged at authoring time)

| High-risk shot | Why it breaks | Mitigation |
|---|---|---|
| Reflections (mirrors, water, blades) | Scene geography breaks | Never show reflector + reflected in one continuous shot; shoot the reflection AS the scene, then cut |
| Two instances of the same character | Identity tracking assumes one body | Treat each instance as a separate named role with own wardrobe anchors; prefer cutting between instances |
| Crowds | Only ≤3 characters track | Crowd = environment, not characters ("the crowd surges left as one"); track ≤3 named individuals |
| Text rendering | Glyph fidelity unreliable | Keep story-critical text out of generation; in-scene text short, large, verified at final resolution |
