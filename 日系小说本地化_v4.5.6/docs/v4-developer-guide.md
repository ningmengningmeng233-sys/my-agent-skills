---
name: 日系小说本地化 v4
version: "4.4.0-dev"
description: 中文日系小说稿的可审计中国化本地化；Core 与用户索引库/写作指导只读 Enhance 分离。
---

# 日系小说本地化 v4（开发版）

v4 当前已覆盖到 skill 根目录。根入口见上级 `SKILL.md`；本文件是实现入口和开发说明。

## 运行入口

```bash
uv run --project v4 python -m v4.core.cli --help
uv run --project v4 python -m v4.core.cli assets
uv run --project v4 python -m pytest v4/tests -q
```

## 运行边界

- 输入必须是中文小说稿；不负责从日文原稿翻译。
- 普通本地化保留事件图、因果链、人物目标、关系变化、情感结果和世界观功能。
- 现实日本外壳按三级策略处理；功能性文化元素不机械删除；西幻/架空设定默认保留。
- 正式输出只合并 manifest 判定为 `passed`/`delivered` 且指纹复核通过的章节。
- Core 事实门槛不依赖索引库或写作指导；但 `rewrite` 默认要求索引库（质量优先，`--no-index-lib` 显式关闭）；Enhance 启用时必须记录来源、哈希、用途和“不复制句面”审计。

## 实现目录

- `core/`：快照、切分、预检、事件图、决策、改写、检查、状态和合并；
- `enhance/`：用户索引库与写作指导只读适配器；
- `rules/`：三级本地化和结构化写作检查规则；
- `tests/`：Core 与 Enhance 冒烟/契约测试；
- `contracts/`：v4 实现契约说明。

## 外部参考资产

配置：

```text
INDEX_LIB_DIR=<你的索引库路径>
WRITING_GUIDE_DIR=<你的写作指导路径>
```

用户索引库适配器会读取章节索引、小说索引、原文上下文、文风卡和判官验证信息；写作指导适配器会读取专项模块结构规则和 166 篇本地语料。所有外部文件只读并记录 SHA-256。

## 当前状态

当前版本为 `4.4.0-dev`。真实小说已在 v4.3.0 新硬门槛下完成 12 章（含附录）DeepSeek 全量改写与合并：12/12 delivered，style_depth 0.45~0.58，consistency error=0；新增场景信号检索、网文感改写指令、`style_depth` 改写深度门、分块容差与台版腔定向返修；盲审/平台审核/风格软评估/发布拆章已实现；文风蒸馏与高级 AI 味检测暂未接入。
