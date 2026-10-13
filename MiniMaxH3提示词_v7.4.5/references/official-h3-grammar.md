# MiniMax H3 Ref2VA 官方语法索引

本文件是中文索引，不替代官方原文。发生冲突时，以同目录下的
`official/ref-en.txt` 与其明确引用的 `official/base-en.txt` 为准。

## 来源快照

- 仓库：`MiniMax-AI/MiniMax-H3`
- commit：`d21241f0a4b3acbb34c97dae47fa417b7065e438`
- 获取日期：2026-09-10
- Ref2VA 原文：`references/official/ref-en.txt`
- 共用基础规则：`references/official/base-en.txt`

更新官方快照时，必须先比较原文差异，再同步本索引、`SKILL.md`、
`ref2va-guide.md`、审计器和测试。不能只改中文摘要。

## 1. 六段与语言

完整 Ref2VA 输出固定为六段，顺序与字段名精确如下：

1. `subject_definitions:`
2. `summary:`
3. `retention_analysis:`
4. `detailed_description:`
5. `overall_soundscape:`
6. `non_diegetic_music:`

六段正文使用英文。只有 `<d>` 内的原对白/歌词和双引号内真实可见的画面
文字保留原语言。中文字段名、中文标签或“交给 API 时再翻译”均不符合官方
rewrite 输出格式。

## 2. 参考标签

- `<Subject N>`：将出现在目标视频中的可复用可见内容。
- `<Picture N>`：图片本身承担具体帧或构图锚点时使用。
- `<Video N>`：整段视频承担编辑源、续写源或时间结构源时使用。
- `<Audio N>`：复制或参考的独立音频信号。

四类标签独立编号。一个文件可提供多个 Subject，一个 Subject 可来自多个文件。
仅用于定义 Subject 的图片不另建 Picture 条目。参考视频包含声音，不等于自动
创建 Audio 条目。

## 3. summary 任务类型

方括号前缀只使用以下英文值，可用 ` + ` 组合且不得重复：

- `keyframe completion`
- `reference generation`
- `video editing`
- `video continuation`
- `audio reuse`
- `audio reference`

素材“存在”不自动产生任务类型；按它在目标视频中的实际角色选择。

## 4. retention_analysis 关系值

可见内容：`fully_preserved`、`partially_preserved`、`attribute_transfer`、
`weak_reference`。

音频：`fully_copy`、`partially_copy`、`reference`、`weak_reference`。

每个参考标签一行，说明出现位置、关系值与具体保留/迁移内容。

## 5. detailed_description

- 风格用一至两句英文写在 `[Shot 1]` 之前。
- `[Shot 1]` 无时间戳。
- 后续镜头用 `[Shot N] At MM:SS.mmm, ...`，编号连续、时间严格递增。
- 官方没有 `[Xs-Ys]` 镜内整数窗口；禁止添加。
- 运镜写在自然英文动作句内。需要量化时使用运动类型、幅度、速度。
- 参考标签在首次明确出现及其作用发生处自然引用。
- 生成任务通常写 350–500 英文词；对白密集内容优先完整时间线，剪辑任务按
  源视频复杂度调整。

## 6. 对白与声音

- 发声者按目标视频中首次实际发声顺序分配 `(S1)`、`(S2)`；不发声者不编号。
- 对白/歌词：`<d>[Language] original text</d>`，保留原词与原语言。
- 画外音使用 `says in an off-screen voiceover`，紧随每个 `<d>` 块说明对应
  画面人物 `lips remain completely closed`。
- 跨切对白使用 `<scenetrans>` 并明确声音跨切连续；片尾截断使用 `<cutoff>`。
- `overall_soundscape`：1–4 句连续英文段落，汇总环境、物理动作和非语言人声；
  只有用户明确要求全程静音才用 `N/A`。
- `non_diegetic_music`：1–3 句英文，写乐器、速度、节奏、动态；无配乐用
  `N/A`。

## 7. 不属于官方语法的内容

下列内容可以作为内部生产方法或交付报告，但不能冒充官方字段：项目档案、
风险清单、场景图锁、七要素因果链、Creative DNA、评分、负面约束清单、
`[Xs-Ys]` 窗口、中文六段标题、独立 Style Block。
