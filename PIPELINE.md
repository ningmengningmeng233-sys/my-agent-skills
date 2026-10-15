# 组合交付协议（PIPELINE）— 桌面技能柜 2026.10.13.2

本目录收录 17 套技能候选，其中知识转录索引仍待独立核验。**没有统一运行时**：每个包独立安装、独立卸载、独立验证；
`catalog.json` 记录各套技能的当前版本、目录及候选状态。原包及校验证据保留在交付证据目录。

## 上游协议（schemas/）

`schemas/` 是共享协议权威副本（来源：本轮冻结维护副本，与 run02 同字节，共 15 件，含
`storyboard.schema.json`、`asset-ledger.schema.json`、`contract-exceptions.json`、
`shot-table.md` 等；测试留维护侧，不随组合分发）。以下产品按真实消费关系使用它：

- 小说转短剧（dramacraft）：镜头组/分镜与资产链的 schema 消费（tests/test_anthology_e2e 等 453 项离线覆盖；包内 5 子技能自检另计）
- MiniMax H3（minimax-h3-craft）：episode/时长合同（_审查测试 601 项 + 254 subtests 离线覆盖）
- 素材提取（asset-extract）：asset-ledger 与签发门（源树 unit 1996 项 + run_all_checks 17/17；分享包 15/17，缺口为禁分享媒体，包内声明）
- 成片合成（media-assembly）：vendored ledger（tests/test_vendored_ledger，摘要锁死；离线演练用包内 tests/fixtures 三件）

独立包各自带消费到的协议受控副本（vendor/ 或包内 schemas/），不依赖本组合目录；
本目录 `schemas/` 仅作组合对账权威。升级协议必须同步升级合同（禁止单方面改定义）。

## 安装顺序建议（有依赖关系的工序链）

1. 写戏：dramacraft（小说转短剧）
2. 提示词：minimax-h3-craft（视频提示词）
3. 资产：asset-extract（素材提取，shareable 档：视觉能力低于私有档，见包内声明）
4. 成片：media-assembly（需自备平台凭据或用 stub 离线演练；缺凭据 BLOCKED-CRED）

其余产品（知识库/成人索引/日系/去AI味/UMH/RTK/TG/柠檬/技能优化/B站转录/抖音转录/小说索引）按各自说明独立使用。知识转录索引是独立技能，不能作为知识库转录的替代入口；其历史来源和独立验收未闭合，目录收录不抬升验收状态。

## 组合边界

- 组合视图不重复注册旧版本或备份（技能优化旧版、UMH backup 不在 packages/）。
- 短剧 5 个子技能可随父包整体安装，也可按各自 SKILL.md 单独安装（单独安装时以各自 name 注册，见包内 INSTALL）；
  素材 `服饰设计/` 子入口如需独立触发须复制到宿主技能根并改名 `drama-costume-studio`；
  素材风格包 `private/SKILL.md` 为 reference-only（风格包 manifest 登记 required，随包，5 层深处，不会被宿主发现）。
- 任何包的显式加载成功 ≠ 自然触发；触发验收见各包说明与 SUPPORT 矩阵。
- 组合 INSTALL 不再宣称包内不存在的 `tests/` 自验；各包真实自验命令见各自包内 INSTALL.md。
