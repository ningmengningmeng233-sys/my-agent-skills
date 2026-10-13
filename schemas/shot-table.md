# shot-table.md — 六列分镜表（storyboard.json 的人类可读投影）

> 本表是 `schemas/storyboard.schema.json` 与 MiniMaxH3 提示词输入之间的**唯一文件级交接模板**：
> 六列定义以 `storyboard.schema.json` 顶层 `x-shot-table.columns` 为准（本文件与它由
> `contract_check.py` 三方对账：schema ↔ 本文件 ↔ MiniMaxH3提示词_v7.4.1/SKILL.md 声明）。
> 数据事实源永远是 `storyboard.json`（三层：集→段→分镜），本表只是**逐切（cut）摊平**的视图，
> **Markdown 不是事实源**——改内容请改 storyboard.json 再重新生成本表。

## 六列模板（表头一字不能改，机器按列名对账）

| 镜头号 | 时长 | 景别·角度 | 画面内容 | 对白/台词 | 声音 |
|---|---|---|---|---|---|
| E01-01-f1 | 3s | 平视·中景（medium） | medium shot of a young woman …（cuts[].frame 原文） | （无） | （无） |
| E01-01-f2 | 3s | 平视·大远景（extreme-wide） | extreme wide shot of a fog-drowned pier … | 老周："上船喽——"（回查 script.json 第2拍 line） | 水声、橹声（反解 h3Prompt overall_soundscape） |

## 列 ↔ 字段对照（与 storyboard.schema.json `x-shot-table` 一一对应）

| 列 | schema 路径 | 现状 | 生成规则 |
|---|---|---|---|
| 镜头号 | `episodes[].segments[].id` | ✅ 有字段 | 镜头号 = `段号 + "-f" + 段内切序(1起)`，即 `E01-01-f2`；并可映射输出 H3 台账 `SC###-SH###` 编号 |
| 时长 | `episodes[].segments[].cuts[].seconds` | ✅ 有字段 | 逐切照抄（2–5 秒）；段（块级）时长 = Σ段内秒数，storyboard.json 不单独存 |
| 景别·角度 | `episodes[].segments[].cuts[].angle` | ✅ 有字段 | 直读 `cuts[].angle` 拍摄角度与 `cuts[].size` 景别枚举，机器字段对齐 |
| 画面内容 | `episodes[].segments[].cuts[].frame` | ✅ 有字段 | frame 提示词文本，转表时可直读或经提示词工程转写 |
| 对白/台词 | `episodes[].segments[].cuts[].dialogue` | ✅ 有字段 | 直读 `cuts[].dialogue`，与 script.json 该场 flow 的逐字台词及段级 h3Prompt <d> 块逐字对账 |
| 声音 | `episodes[].segments[].soundscape` | ✅ 有字段 | 直读段级 `segments[].soundscape`，结构化输出，不从自由提示词反解 |

## 与 MiniMaxH3 模板（references/storyboard-input-template.md）的一致性

- 六列**列名完全一致**，每列在 `storyboard.schema.json` 中均有权威机器字段支撑。
- 分镜导出命令 `novel-storyboard.mjs shot-table` 支持直接输出 Markdown 与 JSON 两形态，H3 消费端通过 `storyboard_reader.py` 直接解析并自动对账。


<!-- shot-table:begin
columns:
  - { column: 镜头号, path: "episodes[].segments[].id" }
  - { column: 时长, path: "episodes[].segments[].cuts[].seconds" }
  - { column: 景别·角度, path: "episodes[].segments[].cuts[].angle" }
  - { column: 画面内容, path: "episodes[].segments[].cuts[].frame" }
  - { column: 对白/台词, path: "episodes[].segments[].cuts[].dialogue" }
  - { column: 声音, path: "episodes[].segments[].soundscape" }
shot-table:end -->
