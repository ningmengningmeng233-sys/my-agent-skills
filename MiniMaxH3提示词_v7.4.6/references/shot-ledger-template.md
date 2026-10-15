# Shot Ledger — 项目级镜头台账模板

> 多镜/多集项目的逐镜台账——prompt 版本、生成、裁决、缺陷全可追溯。
> 每镜一行；跨镜问题（F-CONTINUITY 等）靠台账定位。复制到项目目录
> （如 `<你的项目目录>\shot-ledger.csv`）使用。

## 台账表格（CSV 格式）

```csv
镜头号,条目,prompt版本,生成ID,裁决,已知缺陷,waiver,连续性影响,日期
SC001-SH037,probe-1,v6.0.0,gen-001,shortlist,无,无,无,2026-08-18
SC001-SH037,probe-2,v6.0.0,gen-002,reject,衣摆穿帮,无,需重生成,2026-08-18
SC001-SH038,probe-1,v6.0.0,gen-003,select,字幕边缘略糊,后期加字,衔接 SH039 需验证,2026-08-18
```

## 字段说明

| 字段 | 含义 | 填写标准 |
|---|---|---|
| 镜头号 | 分镜编号 | 与分镜输入模板一致 |
| 条目 | 本轮动作 | probe（试生成）/ fix（修复）/ select（选片）/ supersede（替代） |
| prompt版本 | 提示词版本 | vX.Y.Z（变更即升版，一次改一行） |
| 生成ID | 生成实例 | gen-NNN（同一 prompt 版本多次抽样为不同 NNN） |
| 裁决 | 评审决定 | shortlist / needs_verification / reject / select / supersede / pending_feedback |
| 已知缺陷 | 未解决的缺陷 | 具体描述；「无」也要写 |
| waiver | 豁免 | 进入豁免的缺陷；无则写「无」 |
| 连续性影响 | 对前后镜的影响 | 无 / 具体描述 |
| 日期 | 记录日期 | YYYY-MM-DD |

## ID 体系与状态机（来源：地狱 project-schemas / production-workflow）

**ID 格式**：镜头 `SC###-SH###`（如 SC012-SH004）；提示词 `<shot_id>-P###`；
生成 `GEN-<shot_id>-####`；选择 `SEL-<shot_id>-###`；豁免 `WVR-<shot_id>-###`。
ID 一经引用不重命名；变化用版本与状态表达。
**编号对应**：单块内输入分镜「1、2、3…」与提示词 `[镜头 N]` 对应；跨块时
`[镜头 N]` 每块重新编号，跨块追踪以全局台账 `SC###-SH###` 为准。

**镜头状态机**：planned → contract_ready → prompt_ready → generated →
awaiting_video_feedback → reviewed → needs_verification → selected → locked。
任一 generated/reviewed/needs_verification/selected 可进入 needs_revision
（必须记录失败码、上一版本、next_action）；用户未回传视频结果时保持
awaiting_video_feedback/pending_feedback，不自动 select。selected/locked 不等于
published，发布动作在技能范围外。
`ready_for_review`、`ready_for_delivery`、`published` 是不同状态——
交付完成不自动授权上传或发布。

## 使用规则

- 每次生成/评审后**立即**记一行（不攒批）
- **一问上限（来源：地狱 Generation Ledger 5-second rule）**：用户报生成
  结果时至多问一个问题（哪失败了/选哪个）——台账由技能自记，绝不呈
  表单让用户填
- **分母原则**：成功与失败同记——只有成功也入库，takes-per-kept 才有
  分母；只记失败会让通过率失真
- 裁决为 reject 时：记录责任层与 changed_variables（对照
  diagnosis-iteration.md 8 字段）
- 已知缺陷必须进入 waiver/后期修复/返工——不在备注中消失
- 跨镜问题（F-CONTINUITY）：在连续性影响列标记，靠台账横向比对定位
- 迭代停止条件照常（连续两批无改善 → 回责任层或拆镜）

## 关联

- 诊断与迭代记录格式：diagnosis-iteration.md §4
- 选片记录格式：review-checklist.md §4
