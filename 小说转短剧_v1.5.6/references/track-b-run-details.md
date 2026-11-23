# SKILL.md 轨道 B 无人值守批量跑长篇（原文全文）

> 归属：`小说转短剧_v1.5.2`（dramacraft）技能仓 SKILL.md「SKILL.md 轨道 B 无人值守批量跑长篇」段，governance 第2轮 REFACTOR 自 SKILL.md 整体逐字挪入（一字未改），SKILL.md 原位留指针。

---

大纲加载即过 O1-O6 预审门（不过拒跑）；单元剧模式（`mode: "anthology"`）下自动静默 X1 跨集回声告警，连续剧模式下提示「上一集悬念开场无人接住」（刻意换线转场可忽略，否则按告警补承接）。

- **多格式小说直接输入**：`--input` 零依赖原生支持 `.docx`（自动过滤批注与页脚广告）、`.epub`（按书籍目录结构精准切章）以及 `.txt`、`.md` 纯文本，自动清洗 Windows 鼠标拖拽产生的多余引号。
- **环境健康体检**：`python run_batch.py --doctor`（可选 `--probe-api`）一键检查 Python、Node.js、质量门与模型节点池连通性。跑批时首毫秒静默预检 Node.js，若缺失直接退出码 2 大白话拦截，未花一分钱额度前止损。
- **原文章节细节对齐**：输入小说章节自动透传关联至分集自愈生成中，模型写剧本时自动吸收原著冲突与对白，告别凭空胡编。
- **断点续跑**：已缓存的 `state/episodes/ep###.json` 自动跳过，失败重跑只补欠账。
- **自愈循环**：每集生成后自动跑 12 道门，未过门则把违规坐标打包成定向修改指令回炉，最多 3 次，仍失败才停批报错。
- **分集并发提速**：`--concurrency 3`（默认 3 并发工作线程，传 1 为纯串行模式）。采用原子文件落盘（`safe_write_json`）与线程安全锁，多集同时推进，长篇跑批提速 60% 以上且绝无半截坏文件。
- **预算熔断**：`--max-calls` 设定模型调用上限（缺省自动按待跑集数装设保险丝）。触顶安全停批（退出码 3），已产出集数完整保全，重跑即可断点续跑，不重复计费。
- **缓存安检**：断点检测对缓存执行版本与大纲哈希对账。通过 `--stale-cache`（ask 默认拒跑提示 / redo 清理重产 / trust 信任沿用）杜绝带病通关。改动过大纲结构后走变更单登记（模板见 `references/大纲变更单模板.md`）。
- **伏笔账本（埋了必须收）**：大纲 `episodes[].foreshadows`（可选）登记伏笔，每条形如 `{"id": "F001", "text": "玉佩夜里发烫——身世线索", "payoffInEp": 8}`；第 N 集写完后在 `foreshadowsPaid` 里认领兑现 `[{"id": "F001", "evidence": "第8集第2场：玉佩特写+台词认领"}]`。
  **平时只提醒不拦**（认领缺证据、超期未填只在报告里 ⚠，绝不触发回炉——防为过闸硬塞假填坑）；**终验清零是硬闸**：交付前仍有未回收伏笔就拒交付、点名清单、退出码 1，**12 道门全绿也不豁免**。放弃伏笔必须写明原因且 `decidedBy: "user"`——放弃是用户的决定，不是模型的。
- **命名与术语哨兵**：大纲 `characters[].forbiddenNames`、可选 `terms`（标准写法/禁用变体/替换记录三表）以及 `--art` 里的场景道具名共同构成术语锁定表；成稿命中禁用变体、泛化命名（小X/大X/老X）或同名不同人 → 单集 ⚠ 回炉改名（确定性修补无凑闸风险），终验残留 → 退出码 1。未提供人物/美术种子时显式输出「跳过（未提供人物/美术种子）」，不误报也不静默。
- **写手上下文包**：原著章节参考 + 本集应回收 + 全剧未回收概览三段，**初稿与每一轮回炉共用**（回炉不再丢上下文）；回炉指令命中 AI 腔新类别时自动附「改前→改后」示范。
- **续跑前先看恢复卡**：启动即打印 4 行白话卡（剧名 / 已完成 x/y 集 / 未回收伏笔 n 条 / 下一步），数字全来自本次实扫磁盘缓存与账本重放，禁估算。
- **单集创作纵深体检**：`python -m dramacraft.continuity_lint <workdir> --outline outline.json --art art.json`（白话报告，`--json` 落盘）。
- **生产记录**：每次跑批收尾均产出白话小结 `production_log.md`，每集执笔节点与回炉自愈次数一清二楚。
- 仅聚合导出（不再调模型）：`python run_batch.py --outline outline.json --target-seconds 90 --assemble-only`
- 仅校验任意剧本：`python run_batch.py --validate-only <剧本.json> --outline <大纲.json>`（拿仓内基准样例自测：`python run_batch.py --validate-only fixtures/sample_script.json --outline fixtures/sample_outline.json`，实测 12 门全过 exit 0）
- 纪要预审：`python -m dramacraft.premise_lint <premise.json>`（转译无命令行入口，在 Python 内调 `dramacraft.premise_to_outline.convert_premises_to_outline(premises)`）
- 词表体检工具：`python -m dramacraft.vocab_stats <workspace>`
