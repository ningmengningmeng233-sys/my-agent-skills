# 小说索引 v4.6.1 术语表

本文档记录小说索引技能中专有概念与用词规范。

## 编排与执行

**编排器（Orchestrator）**:
实现 SKILL.md 中定义的状态机（ROUTE → PREFLIGHT → PLAN → EXECUTE → VERIFY → PUBLISH）的入口脚本或模块。编排器负责任务分发、状态流转、断点恢复与最终交付，区别于可独立调用的底层脚本（如 split_segments.py、build_segment_index.py）。
_Avoid_: 调度器、主控、流程引擎

**底层脚本（Low-level Scripts）**:
完成单一原子操作的 Python 脚本，例如拆分源文件、生成章节索引、校准行号、聚合小说索引。它们可被编排器调用，也可由用户单独调用。
_Avoid_: 工具脚本、子命令

## 索引层级

**四级读取**:
小说索引技能定义的权限与成本递增的读取层级：元数据 → 索引段落 → 完整索引 → 原始文本。
_Avoid_: 四层检索、读取等级

## 检索方式

**关键词召回（Keyword Recall）**:
基于标签词汇表、章节索引 frontmatter 和 Grep 匹配进行的检索方式，依赖用户查询与索引文本中的关键词重叠。
_Avoid_: 文本检索、字面匹配

**语义召回（Semantic Recall）**:
基于文本 embedding 向量相似度进行的检索方式，能够匹配 query 与索引块在语义上接近但用词不同的内容。
_Avoid_: 向量检索（单独使用时不区分语义召回与向量索引整体）、AI 搜索

**混合检索（Hybrid Search）**:
同时使用关键词召回和语义召回，并对两路结果进行融合排序的检索方式。默认以关键词召回为主，语义召回为可选增强。
_Avoid_: 联合检索、双路搜索

**向量索引（Vector Index）**:
由 `scripts/semantic_index.py` 生成的、存储文本块 embedding 的索引数据，与现有 SQLite 断点库和 Markdown 索引文件并列存在。
_Avoid_: 语义索引、embedding 库

## 可观测性

**状态面板（Status Dashboard）**:
通过 `orchestrate.py status` 命令输出的结构化摘要，展示当前索引库中各书籍的处理状态、失败段、token 消耗、最近更新时间。
_Avoid_: 监控面板、仪表板

**结构化日志（Structured Logs）**:
使用 Python `logging` 输出、可选 JSON 格式的日志记录，便于宿主 Agent 或外部工具解析，与仅供人类阅读的 `print` 输出相对。
_Avoid_: 机器日志、JSON 日志（仅格式之一）

## 测试分层

**集成测试（Integration Tests）**:
现有 `scripts/_功能测试.py` 承担的端到端测试，验证多个脚本协同工作的真实流程。
_Avoid_: 端到端测试、全流程测试

**单元测试（Unit Tests）**:
新增 `tests/` 目录下的 pytest 测试，聚焦单个函数/模块的正确性，尤其是故障路径（LLM 异常、网络超时、磁盘错误、格式非法等）。
_Avoid_: 组件测试、模块测试

**故障路径测试（Fault-path Tests）**:
专门验证系统在异常输入或外部依赖失效时行为的测试，例如 mock LLM 返回非法 YAML、模拟 429/503 响应、构造只读目录等。
_Avoid_: 异常测试、负面测试

## 标签体系

**标签词汇表（Tag Vocabulary）**:
定义小说索引中可用的标签组及同义词集合的文档/数据，默认由 `templates/包含元素标签词汇表.md` 提供。
_Avoid_: 标签库、标签字典

**自定义标签扩展（Custom Tag Extension）**:
用户放在索引库根目录的 `tags.custom.yaml` 文件，用于在不修改 skill 内置标签词汇表的前提下追加或覆盖标签组。
_Avoid_: 用户标签、自定义词表

## 运维操作

**删除书籍（Delete Novel）**:
删除指定书籍的所有段文件、章节索引、小说索引，并清理 SQLite 断点记录的原子运维操作。
_Avoid_: 移除、清空

**重命名书籍（Rename Novel）**:
批量修改指定书籍的文件名、目录名以及所有相关 frontmatter 中 `小说名称`、`作者`、`源文件` 等字段的运维操作。
_Avoid_: 改名、更名

**迁移索引库（Migrate Library）**:
将索引库整体迁移到新根目录，并递归更新所有 `源文件` 路径前缀的运维操作。
_Avoid_: 搬库、目录迁移

## 文风蒸馏

**文风蒸馏扩展接口（Style Distillation Extension Hook）**:
小说索引 skill 为完整文风蒸馏预留的插件化接口，允许外部 skill（如 wenfeng-skill）或未来内置模块接管完整蒸馏流程，当前默认不实现内置降级方案。
_Avoid_: 蒸馏插件、风格接口
