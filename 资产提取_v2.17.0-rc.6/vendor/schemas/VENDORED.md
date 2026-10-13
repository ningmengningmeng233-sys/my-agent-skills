# 包内受控副本

来源：同柜 `schemas/ledger.py`（跨技能统一总账写入器）。
运行依赖：同柜 `schemas/run-ledger.schema.json` 必须与写入器一起分发；写入器按其自身目录读取这份 schema。两份文件均为受控副本，不重造字段或判定规则。
理由：`scripts/engine/perf_ledger.py` 需要在收件人只装本技能时也能补记总账；组合安装优先用同级 `../schemas/ledger.py`，其次才用本副本。
许可与出处：随柜自制部分，Proprietary；如上游有独立许可，以 `schemas/` 产品登记为准。

## 剧本/资产总账契约副本（Q04 独立包闭合）

`SKILL.md` pipeline-contract 消费的 `script.schema.json`（输入）与
`asset-ledger.schema.json`（产出）同样在此受控复制，独立包不再依赖技能柜相邻目录。
协议定义以组合权威 `W/schemas/` 为准，本包不平行修改；权威更新后须同步刷新。

| 文件 | sha256 | 字节 |
|---|---|---|
| script.schema.json | 4f29355e8c566a6fa68fe8791257d099aa8b53c3e11b1408d94d1529c8e0b0f8 | 7284 |
| asset-ledger.schema.json | df7bf9a7ae0b6548ed211d94a66c9dd13919d866adba1daf0e5142973b8f1ec5 | 7536 |
