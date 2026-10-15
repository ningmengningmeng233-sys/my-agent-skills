# v4 CHANGELOG

## v4.4.0-dev（2026-08-21 · 索引库管理与卡工厂）

- `library_registry.py` + CLI `lib` 子命令；
- `card_factory.py` + `card_index.py` + CLI `card` 子命令；
- `resolve_index_lib` 识别已登记主库；
- 卡工厂状态机与注册流程。

## v4.3.0-dev（2026-08-20 · 审查整改）

- `localization_tone.py` + `rules/tone-scan.json`：v3 台翻腔硬门槛回迁；
- `prompt_assets.py`：本地规则默认注入 LLM prompt；
- `rewrite_backend.py`：词典锁定替换、索引库/写作指导更宽检索与多模块注入；
- `checks.py`：tone_result/tone_findings + 章末钩子预警；
- `consistency.py`：全书台版腔扫描 + 重复标题防御；
- `merge.py`：合并前剥离重复标题；
- `fingerprints.py`：规则联合指纹（dictionary + tone-scan）；
- `cli.py`：`--require-index-lib`/`--no-index-lib`，参考资产接入写 manifest；
- `cli.py`：新增 `assets` 启动检查命令；`rewrite` 默认要求索引库；
- `SKILL.md`：新增启动检查清单，删除“索引库尚未接入 Core pipeline”的过期表述；
- `index_lib.py`：重写 `get_style_card` 解析器（嵌套规则/跨书共识/分书样本）；
- `rewrite_backend.py`：场景信号检索 + 文风卡/章节索引高信号注入；
- `prompt_assets.py`：新增「中国网文感改写指令」；
- `checks.py`：新增 `style_depth` 改写深度门（fail/blocked/warning/pass）；
- `rewrite_backend.py`：分块容差（全章 ±8%、分块 ±10%）+ 失败分块局部回退 + 台版腔定向返修 pass；
- `index_lib.py`：本地真实索引库优先级高于内置兜底；
- `tone-scan.json`：`的说` 上下文收紧、`嘛` 转密度 blocked、`主君` 豁免；
- `precheck.py`：`reconcile_auto_decisions` 幂等修复（按 dec-pc- 识别已有预检决策）；
- 真实验收：DeepSeek 12/12 passed/delivered，consistency error=0；
- `prompt_assets.py`：逐段重写/翻译腔黑名单/动作戏/声线差异化四段指令；
- `checks.py`：新增 `unchanged_paragraph_ratio` 未重写段占比门；
- `voice-hints.json`：项目级可选角色声线表（工作区根目录）；
- 章末钩子：prompt 专项指令 + `_repair_chapter_hook` 定向返修 + 钩子启发式增强；
- 索引库：反例/禁止清单注入、`original_structure_digest` 原文结构摘要、`index_surface_similarity_audit` 句面相似度 warning 审计；
- 卡工厂：`card_factory.py` + `card_index.py` + CLI `card` 子命令；已验证卡优先自动入选；
- 索引库登记：`library_registry.py` + CLI `lib` 子命令；`resolve_index_lib` 识别已登记主库。


## v4.2.0-dev（2026-08-19）— 附录策略、发布拆章、风格软评估与审核执行器

- 后记/附录/致谢/人物介绍/设定集/书评走完整本地化并作为附录输出；插图/目录/版权/彩页跳过。
- 新增 `publish-split` 发布拆章；新增 `v4/core/style_eval.py` 6 维风格软评估。
- 新增 `blind-review` 与 `platform-review` 执行器；`resolve-events` 支持 --list/--event-id。
- 完成真实书籍 12/12 delivered（含附录），全书一致性 error=0。

## v4.1.0-dev（2026-08-18）— 质量优先加固

- 新增 `publish-split` 发布拆章；附录作为附录文件保留前缀。
- 后记/附录改为走完整本地化并作为附录输出；真实书籍 12/12 delivered。
- 新增 `v4/core/style_eval.py`：6 维风格启发式指标写入 checks.soft_evaluations。
- `resolve-events` 支持 --list / --event-id [--reject] 逐事件确认。
- 新增 `platform-review` 平台审核执行器；C-062/C-063/F-016 去除 skip。
- 新增 `blind-review` 盲审执行器；C-061/F-019 已去除 skip 并补真实测试。
- Enhance 接入：`rewrite --index-lib/--writing-guide` 只读参考写入 prompt 并审计；`check` 写作指导旁车加载。
- 新增 `consistency` 全书级一致性检查；单字译名映射增加复合词豁免（君王/君子/夫君等）。
- 全书实验：11 章一致性 error=0、warning=0。
- 安全加固：`align-events` 必须 `--confirm` 并写审计；`refresh` 检测 output/checks 指纹；LLM 事件图调用写入审计。
- 书籍闭环：ingest 自动分离后记/插图/目录等非正文文件（manifest.source.excluded_files）；core_version 升到 v4-core-3。
- 事件图、预检决策、改写后端、运行时恢复与锁自愈全面升级，见根 CHANGELOG。
## 4.0.1-dev（2026-08-18）— 版本号同步与校验脚本适配

- 根目录与 `v4/` 版本号统一为 `4.0.1-dev`，`pyproject.toml` 同步为 `4.0.1.dev0`。
- 适配契约/文档一致性脚本对预发布版本格式与 v4 锚点的检查。

## 4.0.0-dev（2026-08-18）— 根入口同步

- 根目录入口已切换到 v4 开发版；v3 文件保持在原位置作为 legacy 只读参考。
- v4 Core、Enhance、需求追踪和资产整合策略已同步到根目录可见入口。

## 0.1.0（开发中）

- 建立独立 v4 Core 实现目录，与 v3 legacy 隔离。
- 固化 manifest、旁车契约、需求追踪与资产整合策略。
- 新增 Enhance 只读适配层（v4/enhance）：
  - `index_lib.py`：索引库解析（CLI/工作区/`.env`/默认发现优先级）、资产 SHA-256 记录、标签/场景检索、章节索引/文风卡解析。
  - `writing_guide.py`：写作指导解析、专项模块 runtime/README 结构规则吸收、excerpts.csv 与模块例库 2-4 正例/1-2 反例检索、摘录原文回查。
  - `reference_audit.py`：工作区 audit.jsonl 的 enhance_use / enhance_unavailable / enhance_skipped 记录。
  - 全部只读外部资产，不参与 Core 事实门槛；自测见 `v4/tests/test_enhance_adapters.py`。
# 4.5.0-dev

- 动态参考预算：按章节复杂度自动选择 1+1 或 2+2 风格/章节索引。
- 参考块按完整块装配，避免总字符串截断产生半条规则。
- 新增长篇连续性账本，并将快照写入时机后移到质量门通过之后。
- 新增 `enhance.providers` 统一外部知识库接口基础实现。
