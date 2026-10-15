# Ref2VA 六段装配指南

先读 `official/ref-en.txt`。本文件只帮助执行，不增删官方字段。

## 1. 输入门

Ref2VA 至少需要一个真实参考资产。没有参考素材时，停止并请用户补素材，或
转到 T2VA 工作流；不得编造标签或用“零参考素材声明”伪装 Ref2VA。

为每个资产记录：文件类型、来源、用途、要继承的属性、要排除的属性、是否
直接复制、出现在哪些镜头。素材角色冲突时先让用户裁决。

## 2. subject_definitions

每个后续需要单独追踪的内容一行：

```text
<Subject 1> is the swordswoman whose face, hairstyle, costume, and color palette come from <Picture 1>.
<Picture 2> is the composition anchor for [Shot 1], defining the viewpoint and subject placement.
<Video 1> is the temporal-structure reference for the target video's camera rhythm and cuts.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1).
```

判断规则：

- 人、物、场景、服装、动作、表情、姿态、特效等可见内容用 Subject。
- 图片仅定义 Subject 时，在 Subject 句中注明来源，不再单列 Picture。
- 图片本身充当首帧、关键帧、尾帧或构图锚点时才单列 Picture。
- Video 只表示整段编辑、续写或时间结构关系；视频中的人/物仍是 Subject。
- Audio 表示独立声音信号的复制或参考。与说话者绑定时复用目标视频的 `(Sx)`。

## 3. summary

写一个简短英文段落，以官方任务类型前缀开头：

```text
[reference generation + audio reference] The target video shows ...
```

只使用实际成立的任务类型。不要因为上传了视频就写 `video editing`，也不要
因为视频文件带声音就写 `audio reuse`。本段只引用已在
`subject_definitions` 定义的标签。

## 4. retention_analysis

每个已定义标签一行，使用固定英文关系值：

```text
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - the face, hairstyle, costume, and color palette remain unchanged.
<Picture 2> ([Shot 1] composition anchor): partially_preserved - the viewpoint and placement are retained while the lighting is changed.
<Video 1> (camera rhythm and cut structure): weak_reference - only the broad pacing pattern is followed.
<Audio 1>: reference - its vocal timbre guides (S1) without copying the source signal.
```

新动作、新背景或新剧情并不自动意味着参考损失。关系值必须与定义中的参考
角色一致。

## 5. detailed_description

### 5.1 开头

先用一至两句英文确定全片风格，再开始 `[Shot 1]`：

```text
detailed_description:
The target video uses a polished 3D CG Chinese-animation style with stylized facial proportions, clean cel-influenced shading, and controlled cinematic lighting.
[Shot 1] A medium-wide shot establishes ...
```

不要建立独立 Style Block，不要使用中文“画风层”标题。

### 5.2 镜头

- `[Shot 1]` 不带时间。
- `[Shot 2] At 00:03.000, the shot cuts to ...`。
- 编号连续，切点严格递增并位于视频时长内。
- 不写 `[0s-1s]` 等镜内时间窗。
- 每次切镜应引入主体、空间、状态、视点或时间的新信息；只改变距离或小角度
  时优先用镜头运动。

每镜按真实需要覆盖：当前构图、主体外观与位置、环境与灯光、动作与状态变化、
相机运动、当下声音、参考内容何时出现或生效。避免只写剧情梗概，也避免把
内部检查清单直接塞入提示词。

### 5.3 动作、空间与相机

动作采用“起始状态 → 可观察变化 → 接触/触发 → 反应 → 落定”的因果路径。
只有复杂或高风险动作才补足部位、方向、速度、力度、惯性、接触点和后果；
不要求每句机械填满七项。

空间关系只锁必要事实：屏幕方位、深度层、朝向、距离、遮挡、接触点。相机
运动写成自然英文句；需要时加入小/大幅与慢/快速。每镜只设一个主要运镜。

### 5.4 对白

第一次实际发声时建立稳定身份与 `(Sx)`。描述和表演写在 `<d>` 外，只有
原对白放在标签内：

```text
<Subject 1> (S1), a young woman with a clear low voice and measured delivery, says, <d>[Chinese] 我会回来。</d> She closes her mouth and lowers the sword.
```

画外音：

```text
<Subject 1> (S1) says in an off-screen voiceover: <d>[Chinese] 我还记得那条路。</d> while her lips remain completely closed.
```

同一人物跨镜复用同一编号。参考音频只提供音色、节奏或情绪时，不得自动带入
原音频对白。

## 6. overall_soundscape

写 1–4 句连续英文段落，汇总环境声、物理动作声和非语言人声。镜头同步对白、
歌唱和具体声音事件留在 `detailed_description`。只有用户明确要求全程静音
才写 `N/A`。

## 7. non_diegetic_music

无观众侧配乐写 `N/A`。有配乐时写 1–3 句英文，明确乐器、速度、节奏和动态；
角色能听见的唱歌、乐器、广播、电视或手机音乐属于画内事件，放在
`detailed_description`。

## 8. 方法层使用边界

复杂镜头可读 `industrial-patterns.md`。先确定官方六段与参考角色，再选择一
到两个最相关模式。案例模式只改变描述内容，不得改变字段名、语言、标签、
保留值或镜头时间码。
