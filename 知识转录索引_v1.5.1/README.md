# 知识转录索引（knowledge-transcript-index）

面向本地 Obsidian 目录中 B 站视频转录 TXT（`标题_作者_日期_BV号.txt`）的专用主题条目索引与本地语义检索技能。

## 定位与分工

- **本技能（`knowledge-transcript-index`）**：只处理 `SRC_DIR` 内文件名带合法 `BV` 号的 B 站视频转录 TXT，采用「校对 → 切条目 → 本地向量库」三段流水线，支持主题聚类与双路召回。
- **与 `知识库转录`（`knowledge-index`）的区别**：`knowledge-index` 面向通用长篇非虚构资料/PDF/DOCX/TXT 建库与可追溯问答；本技能专用于已归档 B 站视频转录 TXT 的按主题与作者快速检索。

## 安装与验证

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env   # 填入 SRC_DIR 与摘要模型配置（建议放于 ~/.config/转录索引/.env）
python -m pytest -q    # 离线核心回归测试（不依赖真实 SRC_DIR 与网络）
```

## 使用与维护

- 预览变更：`bash build.sh --dry-run`
- 增量更新：`bash build.sh`（Windows：`build.bat`）
- 检索入口：见 [SKILL.md](SKILL.md) 与 [references/维护.md](references/维护.md)

## 许可

本项目为作者 `柠檬才不萌` 自有开发技能（Proprietary），详见 [LICENSE](LICENSE)。
