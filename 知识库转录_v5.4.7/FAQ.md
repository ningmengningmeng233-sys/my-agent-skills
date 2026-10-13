# 知识库索引 v5.0 · 常见问题与排错（FAQ）

> 按“症状 → 原因 → 处理”查表。向模型求助时，请贴出完整命令与报错第一屏，不要只说“报错了”。

## 一、命令与配置

### Q1. orchestrate.py 报 `unrecognized arguments: --lib`

`--lib` 是全局参数，必须放在子命令**前面**。

```text
# 错误
orchestrate.py build --source xxx --novel 书名 --author 作者 --lib "<知识库>"
# 正确
orchestrate.py --lib "<知识库>" build --source xxx --novel 书名 --author 作者
```

### Q2. 建库不调用 API，或一直提示要配置

- 检查 `<技能目录>\.env` 是否存在；
- `OPENAI_API_KEY` 是否还是占位符 `sk-your-key-here`；
- `SUMMARY_API_URL` / `SUMMARY_API_MODEL` 是否填了。
- 只报告“已配置/未配置”，**不要把 key 贴进对话或日志**。

### Q2.5. 自定义标签报 `No module named 'yaml'`

`tags.custom.yaml` 需要 PyYAML。用 uv 临时环境时在命令里加 `--with pyyaml`：

```text
uv run --with requests --with python-dotenv --with pyyaml python "<技能目录>/scripts/build_segment_index.py" ...
```

或直接 `pip install -r requirements.txt`。

### Q2.6. Python 版本不兼容 / 语法报错

本技能需要 **Python 3.10+**（代码使用 `str | None` 与内置泛型注解）。请升级 Python 后再运行。

### Q3. .env 找不到 / 复制模板

`.env` 必须位于 `SKILL.md` 所在目录，不在 cwd。初始化也会自动复制：

```text
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<知识库>" init
```

### Q4. 设置了 INDEX_LIB_DIR 但脚本还用默认库

脚本读取的是 `<技能目录>\.env`，不是当前目录的 `.env`。确认 `INDEX_LIB_DIR` 写在技能目录的 `.env` 里，并用绝对路径。

## 二、独立文档与长书结构

### Q5. 独立文档和长书有什么区别？怎么管理？

- **长篇专著（长书）**：包含章节结构（如第一章、第二章），一个文件就是一本书，拆段存放在 `<知识库>/原文/<书名-作者>/`，拥有独立书导航；
- **单篇独立文档**：包含笔记、论文、技术白皮书、操作手册等，通过 `--doc` 参数切段并自动汇入虚拟书「独立文档集」，存放在 `<知识库>/原文/独立文档集/`，统一生成 `<知识库>/书导航/独立文档集.md`。

### Q6. 如何单独删除某一篇独立文档？

使用两级删除中的单篇文档删除功能：
```text
python scripts/delete_novel.py "独立文档集" --doc "文档名" --lib "<知识库>"
```
系统会清理该文档的原文段、段索引及向量数据，并自动刷新独立文档集的书导航。整集删除则需要二次确认。

## 三、位置、缓存与备份

### Q7. 哪些缓存可以删？删了会怎样？

| 路径 | 可否删除 | 影响 |
|---|---|---|
| `__pycache__\`、`.pytest_cache\` | 可随时删 | 无 |
| `<知识库>\.semantic_index\` | 可删 | 语义向量辅助检索失效，重新 `semantic build --force` |
| `<知识库>\.index_state.db` | 可删，但别随便删 | 断点与 hash 锚点丢失；`--skip-existing` 需要库里已有 hash 记录才会跳过，丢失后大概率会重新调用 API。先恢复备份的 DB，或接受重建 |

### Q8. 怎么备份和恢复？

备份 = 复制整个 `<知识库>`（原文、段索引、书导航 + `.index_state.db`）。恢复后必须重新运行：

```text
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<知识库>" verify
```

## 四、建库与维护

### Q9. 源文件读取乱码 / UnicodeDecodeError

脚本内置编码回退：UTF-8 → GB18030 → GBK。仍失败时，用记事本“另存为 UTF-8”后再跑。

### Q10. 多卷长书章号错乱 / 整卷丢失

多卷且卷内章号重复时，每个卷文件必须单独处理并加 `--volume`：

```text
split_segments.py "第5卷.txt" --novel 书名 --author 作者 --lib "<知识库>" --volume 第5卷
```

### Q11. 验证失败，状态是 NEEDS_REVIEW

按顺序看这三条的输出，哪条非 0 修哪条：

```text
_验证批次.py --lib "<知识库>"
verify_and_fix_lines.py "<知识库>/段索引" --lib "<知识库>"
check_line_refs.py --lib "<知识库>" --detail
```

失败段可用 `orchestrate.py --lib "<知识库>" resume` 续跑；未通过验证不得宣布“已完成”。

## 五、问答与授权协议

### Q12. 问答时为什么提示“涉及 N 个段落，已核对最相关的 3 个，继续读完吗？”

这是知识库 v5.0 的核心授权预算机制（宋氏思考决议 7 与 13）。问答默认精度优先，需核验原文保证回答准确，但单次核查上限为最相关的 3 个段落或 3 万行原文。超限时会主动向用户汇报并暂停，等待用户确认是否继续扩展核对。

### Q13. 库是空的，提问问答怎么办？

先建库。空库时系统按协议给出“知识库当前未建立索引”的提示，不会凭空捏造知识。
