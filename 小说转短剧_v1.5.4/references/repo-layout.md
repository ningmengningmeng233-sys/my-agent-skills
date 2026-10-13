# SKILL.md 概述·工作目录与仓内布局（原文全文）

> 归属：`小说转短剧_v1.5.2`（dramacraft）技能仓 SKILL.md「SKILL.md 概述·工作目录与仓内布局」段，governance 第2轮 REFACTOR 自 SKILL.md 整体逐字挪入（一字未改），SKILL.md 原位留指针。

---

**工作目录**：先 `cd` 到本 SKILL.md 所在目录（下称技能根）。下文所有路径都相对技能根，命令必须在技能根执行。

```
run_batch.py                  # 批量跑批唯一主控入口
dramacraft/                   # 引擎包：ingestor / gateway / self_healing / continuity_lint / exporter
skills_runtime/dramacraft_patch/validate.mjs   # 12 道门 CLI（G1~G10 + G11 文学病 + G12 AI味）
skills_runtime/dramacraft_patch/ai_tone_examples.json  # AI 腔 24 类改写示范（回炉指令自动引用）
skills_runtime/novel-script/  # 上游原生 10 门与 HTML 渲染器（勿动相对结构）
fixtures/                     # 3 集都市短剧基准样例（大纲/小说/满分剧本）
tests/                        # 单测与端到端自检（条数以 `python -m pytest tests -q` 实测为准）
```
