# SKILL.md 轨道 B 完整跑批命令块（原文全文）

> 归属：`小说转短剧_v1.5.2`（dramacraft）技能仓 SKILL.md「轨道 B：无人值守批量跑长篇」命令块，governance 第2轮 REFACTOR 自 SKILL.md 整体逐字挪入（一字未改），SKILL.md 原位留指针。

---

```bash
cd <技能根>
python run_batch.py --input "小说.txt" --outline "outline.json" \
  --workdir workspace --target-seconds 150 --formats markdown csv json html
# 可选：--art art.json 让门10实扫场景/光照/道具对账（契约见 WORKFLOW；样例 fixtures/sample_art.json）
# 可选：--concurrency 3（默认 3 并发提速；传 1 为纯串行）
```
