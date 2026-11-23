# 知识库索引 v5.0 · 快速开始（QUICKSTART）

> 面向人类的 5 分钟上手指南。需要 Python 3.10+；完整规则见 `SKILL.md`（模型执行协议）与 `README_CN.md`（完整文档）。

## 一页流程

```text
没有 .env？
  └─→ Copy-Item .env.example .env，填写 OPENAI_API_KEY / SUMMARY_API_URL / SUMMARY_API_MODEL
        ↓
准备源文件：长篇专著（.txt/.md）或独立文档（.md/.txt）
        ↓
orchestrate.py --lib "<知识库>" init
# 长书建库：
orchestrate.py --lib "<知识库>" build --source "<源文件>" --novel "书名" --author "作者"
# 独立文档建库（汇入独立文档集）：
split_segments.py "<文档路径>" --novel "文档名" --doc --lib "<知识库>"
build_segment_index.py "<知识库>/原文/独立文档集/" --lib "<知识库>"
build_novel_index.py --novel "独立文档集" --lib "<知识库>" --force
        ↓
build 内部自动：拆段 → 生成知识提取卡（段索引） → VERIFY 验证 → 发布书导航
        ↓
建库完成后：带精准引用知识问答（精度优先 / 3段或3万行预算核对 / 直出原文行号）
```

## 位置速查

| 项 | 默认位置 |
|---|---|
| 配置文件 | `<技能目录>\.env` |
| 源文件输入 | 你指定的任意绝对路径（skill 不搬移原文件） |
| 原文分段 | `<知识库>\原文\<书名-作者>\` 或 `<知识库>\原文\独立文档集\` |
| 段索引（提取卡） | `<知识库>\段索引\` |
| 书导航 | `<知识库>\书导航\<书名-作者>.md` 或 `<知识库>\书导航\独立文档集.md` |
| 断点/缓存 | `<知识库>\.index_state.db`、`<知识库>\.semantic_index\` |
| 备份 | 复制整个 `<知识库>` 目录；恢复后重新 verify |

## 第 1 步：配置（一次即可）

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` 里至少填 3 项；`INDEX_LIB_DIR` 留空则默认使用 `<技能目录>\索引库`：

```ini
OPENAI_API_KEY=你的key
SUMMARY_API_URL=https://api.deepseek.com/v1/chat/completions
SUMMARY_API_MODEL=deepseek-chat
INDEX_LIB_DIR=
```

## 第 2 步：长书建库

所有命令使用绝对路径；`orchestrate.py` 的 `--lib` **必须放在子命令前**：

```text
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<知识库>" init
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<知识库>" build --source "D:\books\某专业著作.txt" --novel "书名" --author "作者"
```

## 第 3 步：独立文档建库（汇入独立文档集）

```text
# 切段（天然并入虚拟书「独立文档集」）
uv run --with requests --with python-dotenv python "<技能目录>/scripts/split_segments.py" "D:\docs\架构白皮书.md" --novel "架构白皮书" --doc --lib "<知识库>"
# 提取知识卡
uv run --with requests --with python-dotenv python "<技能目录>/scripts/build_segment_index.py" "<知识库>/原文/独立文档集/" --lib "<知识库>" --skip-existing
# 刷新独立文档集导航
uv run --with requests --with python-dotenv python "<技能目录>/scripts/build_novel_index.py" --novel "独立文档集" --lib "<知识库>" --force
```

## 第 4 步：带引用精准问答

在对话中直接提问：
- “在知识库里查一下分布式事务两阶段提交的具体实现步骤和核心风险，附带原文行号。”
- “系统限流算法中，漏桶与令牌桶的核心差异是什么？引述权威定义。”

执行规则：
- 默认精度优先，逐字引用与出处行号对齐；
- 遵循预算授权（最多深入核对最相关 3 个段落原文或 3 万行）；
- 涉及更多时会主动提示：`涉及 N 个段落，已核对最相关的 3 个，继续读完吗？`。

## 日常运维

```text
# 1. 库状态查看
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<知识库>" status

# 2. 全库健康体检与行号校准
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<知识库>" verify

# 3. 独立文档单篇删除（支持高危操作两级确认）
uv run --with requests --with python-dotenv python "<技能目录>/scripts/delete_novel.py" "独立文档集" --doc "架构白皮书" --lib "<知识库>"
```
