# v4 资产整合策略

## 1. 目标

v4 必须最大程度吸收已有资产，但不能把未经验证的文档描述当成已实现能力，也不能让外部资产绕过 Core 事实门槛。

资产分三类：

1. **v3 legacy 参考**：只读，用于迁移规则语义、历史问题和失败案例；不作为 v4 状态或交付判断依据。
2. **用户索引库 Enhance**：用户自备索引库（`--index-lib` / `INDEX_LIB_DIR` 指定），只读强参考；用于表达、节奏、爽点、文风和样本学习，不替代事件图事实验收。
3. **写作指导 Enhance/方法论**：用户自备写作指导库（`WRITING_GUIDE_DIR` 指定），只读；结构性规则吸收进 Core 旁车契约，主观风格和语料检索走 Enhance。

## 2. 路径与来源哈希

路径优先级：

```text
CLI 参数 > 工作区配置 > .env INDEX_LIB_DIR / WRITING_GUIDE_DIR > 不启用
```

不得把任何本机索引库/写作指导路径硬编码为唯一运行路径。发现默认路径时可以在本机生产 profile 推荐启用，但必须记录实际路径和完整 SHA-256。

增强适配器必须：

- 只读打开资产；
- 记录来源绝对路径和工作区内归一化路径；
- 记录文件 SHA-256、读取时间、用途和章节 ID；
- 读取失败时返回 `blocked`/`warning`，不得写“已使用”；
- 禁止修改索引库、写作指导原文件、卡库 frontmatter、轮换记录或源语料。

## 3. v3 资产复用边界

### 可复用思想和纯函数

- `scripts/split_ja_segments.py`：章节标题识别、数字规范化、超长章内部技术分段思想；禁止复用无章节按字数伪造工作项。
- `scripts/check_translation_quality.py`：机器味、漏译和矛盾检测思想；必须重写结果语义，区分序数/数量和疑似问题。
- `scripts/check_localization.py`：称谓、文化、台翻腔、格式规则表；必须改造成三级策略，不能照搬文化一刀切。
- `scripts/check_source_health.py`：乱码、空输入、重复标题等边界检查。
- `scripts/v333/index_db.py`：断点和 hash 失效思想；v4 使用完整 64 位 SHA-256、manifest 原子写和项目锁。
- `scripts/v333/verify_and_fix_lines.py`：文本归一化和就近定位思想；v4 使用稳定段落 ID，不把行号当事实依据。

### 明确不复用

- 批次 frontmatter 作为状态真相；
- `merge_batches.py` 依赖文件名猜批号/盲审；
- `.classified.md` 作为正文输入；
- “无索引库暂停全部流程”；
- “清除一切日本痕迹”的一刀切；
- 80%~120% 篇幅作为事实硬门槛；
- 退出码 0/1 直接等价于最终通过/失败。

## 4. 用户索引库接入

### 4.1 目录和用途

| 资产 | 用途 | 允许引用内容 | 禁止事项 |
|---|---|---|---|
| `小说原文/` | 回查上下文和学习段落结构 | 结构、节奏、信息投放、动作/对白组织 | 抄桥段、抄名场面、抄句面 |
| `章节索引/` | 按标签查爽点/看点、金句/名场面、伏笔/线索 | 场景功能、节奏规律、结构拆解 | 把索引描述当原作事实或直接拼接 |
| `小说索引/` | 全书导航、标签、亮点范围 | 选书、选章、定位原文 | 仅读导航不回查原文就生成参考结论 |
| `文风蒸馏卡/` | 静态文风/场景基线 | 已验证卡的可执行技法 | 未验证卡作基线；复制锚点长句 |
| `判官验证/` | 了解卡和样本的验证结果 | 验证状态、评分、风险提示 | 把旧评分伪装成当前章节通过 |

### 4.2 强参考流程

启用索引库时，每个有风格/结构需求的章节优先：

1. 判断章节场景和问题层级；
2. 全库检索章节索引标签；
3. 选择 2–4 个近似正例和 1–2 个反例；
4. 读取章节索引“爽点/看点”“金句/名场面”“伏笔/线索”；
5. 回查对应 `小说原文/<书>-<作者>/` 前后上下文；
6. 若使用卡库，只选已验证卡，先读索引再读卡技法；
7. 总结可迁移的结构、节奏、信息投放、动作/对白方法；
8. 将引用、文件 hash、用途、回查状态和“不复制句面”写入审计旁车；
9. 只把提炼出的规则/方法传给改写，不把原句或具体桥段直接传入正文。

索引库参考不会改变事件图和事实硬门槛；如果参考结论与事件图冲突，以事件图和用户决策为准。

## 5. 写作指导接入

### 5.1 必须吸收的结构规则

来自用户写作指导库的 `references/modules/`：

- `plot_logic/runtime.md`：动机→触发→决策→后果→兑现五环；作为因果风险和写前计划字段。
- `character_consistency/runtime.md`：目标→情绪→关系→身体/信息→声音五维；作为人物状态连续卡。
- `consistency_review/runtime.md`：剧情逻辑、人物目标、情绪关系、身体信息、场景转场、章末承接六项；作为 Core checks 的结构化旁车检查。
- `README.md`/`SKILL.md`：先判断问题层级，再调用专项模块；先修底层因果/人物，再修转场/对白/章末，最后修 AI 味。

这些规则属于“检查框架”，不等于自动保证文风；它们必须落在 `chapter_plan.json`/`checks.json` 中，有检查结果和证据。

### 5.2 可选风格增强

- `anti_ai_voice/runtime.md`：空泛总结、人物声音同质、套话氛围、平均节奏四层诊断。
- `transition/runtime.md`：时间/空间/情绪/视角/信息桥梁和六句局部计划。
- `dialogue/runtime.md`：对白压力、关系、身份和信息推进。
- `chapter_ending/runtime.md`：余韵、反打一刀、判决、轻喜回钩和下章第一拍。

### 5.3 本地语料

读取：

- `analysis/article_profiles.csv`；
- `analysis/excerpts.csv`；
- `analysis/imitation_index.md`；
- `data/articles/`。

参考协议：

- 先按题材/结构/问题层级检索；
- 2–4 个正例 + 1–2 个反例；
- 摘录必须回查原文前后上下文；
- 只总结结构、节奏、信息投放、角色压力和章末停点；
- 禁止复制句面、拼贴多个作者表面文风、借具体剧情；
- 记录资产 hash 和回查位置。

## 6. 增强审计字段

每条 `enhance_use` 至少记录：

```json
{
  "type": "enhance_use",
  "chapter_id": "ch-001",
  "asset_kind": "index_chapter|index_original|style_card|writing_module|corpus_excerpt|corpus_original",
  "asset_path": "归一化路径",
  "asset_sha256": "完整64位SHA-256",
  "purpose": "scene|dialogue|transition|chapter_ending|consistency|anti_ai_voice|mapping",
  "selection_reason": "为什么选择该资产",
  "location": "章节索引/段落/模块定位",
  "context_rechecked": true,
  "borrowed_layer": "structure|rhythm|information_flow|diagnostic",
  "copied_surface_text": false,
  "chapter_id": "ch-001"
}
```

若配置了增强资产但本章未使用，记录 `enhance_skipped` 及原因；若路径不可用，记录 `enhance_unavailable`，不得伪造使用结果。

## 7. 未决和禁止事项

- Core 进入 beta 前，不实现平台专项审核、文风蒸馏、发布拆章和复杂轮换。
- 索引库可作为强参考，但“强参考”不等于“硬依赖”；无库时 Core 仍须运行。
- 写作指导库的盐选短篇语料不能直接决定西幻/传统文学的目标声线，必须按题材和场景筛选。
- 任何影响事件图的参考改写都必须升级为剧情级变更并请求用户确认。
