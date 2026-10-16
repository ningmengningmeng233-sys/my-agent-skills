# Knowledge Base Index · 知识库索引 v5.4.8

这是一个用于长篇专著与独立文档知识建库、追更·重建、带精准原文引用的知识问答、知识提取卡构建与知识库运维的专业 Agent Skill。

完整的人类使用文档、配置和变更说明见 [README_CN.md](./README_CN.md)。5 分钟上手见 [QUICKSTART.md](./QUICKSTART.md)，排错见 [FAQ.md](./FAQ.md)。模型执行协议见 [SKILL.md](./SKILL.md)。

## 核心特性

- **资料形态**：长书（专著书籍，按章节切分）为主，独立文档（文章/论文/技术文档，按 `--doc` 汇入《独立文档集》）为辅。
- **知识提取卡**：本段概要、带类型（[概念]/[方法]/[数据]/[观点]）与行号的知识点条目、逐字关键引用、实体、交叉引用、通用知识标签。
- **带引用精准问答**：精度优先（准一些、贵一些，回答到点子上），四级渐进读取，答案直出经过校验的原文行号与逐字引用；无法唯一确认的引用会进入复核状态。
- **严格授权预算**：单次最多深入核对 3 个相关段落原文（或 3 万行），超限主动熔断并汇报确认。
- **标准三目录**：`原文/`、`段索引/`、`书导航/`。

## Quick start

```text
# 1. 长书拆段
uv run --with requests --with python-dotenv python "<技能目录>/scripts/split_segments.py" "<源文件>" --novel "书名" --author "作者" --lib "<知识库>"

# 2. 独立文档拆段（汇入独立文档集）
uv run --with requests --with python-dotenv python "<技能目录>/scripts/split_segments.py" "<文档路径>" --novel "文档名" --doc --lib "<知识库>"

# 3. 抽取段索引（知识提取卡）
uv run --with requests --with python-dotenv python "<技能目录>/scripts/build_segment_index.py" "<知识库>/原文/<资料目录>/" --lib "<知识库>" --skip-existing

# 4. 发布书导航
uv run --with requests --with python-dotenv python "<技能目录>/scripts/build_novel_index.py" --novel "<资料名-作者>" --lib "<知识库>" --force
```

安全声明：不要把 `.env`、API key 或实际索引库数据放入分发包。
