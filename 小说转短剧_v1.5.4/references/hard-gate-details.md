# SKILL.md 开工硬门禁·反塞防御与节点池（原文全文）

> 归属：`小说转短剧_v1.5.2`（dramacraft）技能仓 SKILL.md「SKILL.md 开工硬门禁·反塞防御与节点池」段，governance 第2轮 REFACTOR 自 SKILL.md 整体逐字挪入（一字未改），SKILL.md 原位留指针。

---

**禁止反塞业务参数**：`targetSeconds` 只能写用户确认过的值。时长门不过只能把剧本写厚或删水，**严禁为了让时长门通过去改 targetSeconds**。引擎两层硬防御：`run_batch.py` 缺 `--outline`/`--target-seconds` 直接拒跑、大纲逐集目标与统一值冲突拒跑；质量门报告无论过不过都裸露逐集「估算 vs 目标」对照，凑数用户一眼看穿。

模型节点池不需要问：按三级链自动发现——环境变量 `DRAMACRAFT_NODES` → 技能根 `llm_nodes.json` → 用户家目录 `~/.agents/skills/bilibili-auto-transcript/llm_nodes.json`（本机复用 B 站转录技能的池）。都没有就请用户把 `llm_nodes.example.json` 复制为 `llm_nodes.json` 填入自己的 API，或用 `--nodes` 指定。
