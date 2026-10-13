# 来源与授权说明（SOURCE-NOTES）— 成片合成 v0.1.2

目的：说清"哪些是自写、哪些是模板生成、哪些是文件外移"，不把整包冒称同一来源，
也不让收件人对着 `license` 字段猜条款。授权方式按作者 2026-10-02 裁决：
**本技能自有内容采用 MIT**，作者署名保留为 `柠檬才不萌`（与 `SKILL.md` frontmatter `author` 同值），
许可正文见 `LICENSE`。

## 1. 自有内容（MIT，作者：柠檬才不萌）

| 范围 | 证据 |
|---|---|
| `SKILL.md`（技能入口与红线） | 首笔提交 `2dd0459 feat(成片合成): v0.1.0 成片合成技能——可插拔骨架+桩渲染` |
| `renderplan/`（7 个 py：cli/plan/stages/adapters/ledger 等） | 同上；本仓自有实现，运行期只用 Python 标准库 |
| `tests/`（本仓自写用例，含 `tests/fixtures/`） | 逐文件随功能提交；无外部测试代码拷入 |
| `requirements.txt` | 自写说明件，标题不写版本（写死会腐坏） |

## 2. 由自有治理模板生成（不是第三方，但也不是手写）

| 文件 | 来源 | 注意 |
|---|---|---|
| `.github/workflows/ci.yml` | 本机柠檬治理仓 `gitguard.ci_workflow` 模板生成（文件头两行自述），提交 `d1bb754 A5-2: 落 CI 工作流（由治理仓模板生成，G7 钉一致性）` | 手改单仓工作流会被治理仓 G7 判"模板不一致"；要改条件须改模板再重新生成 |

## 3. 从 `SKILL.md` 外移的自有正文（只搬不删）

| 文件 | 证据 |
|---|---|
| `references/常见坑.md` | 文件头自述：「自 SKILL.md 外移，只搬不删」「writing-skills 第二轮：SKILL.md 正文逼近 6000 字符闸，本节从『成片合成』SKILL.md 文末整节原样搬入，正文留有指针可反查」 |

## 4. 第三方内容

**本包不含第三方许可代码。** 判据不是"我没记得有"，而是三条可重跑核查：
① 全仓 `grep Copyright/upstream/改编自` 仅命中本文件的作者行与 `LICENSE`；
② 运行期零第三方依赖（`requirements.txt` 注明只用标准库）；
③ 开发期依赖 `pytest`、`PyYAML` 按各自上游许可证发布，本声明不改变其条款、也不重新授权。

外部服务与本机工具（渲染端点、TTS、剪映 `jianying-editor` 运行环境）是**调用对象**而非本包内容：
它们的条款由服务方约束；缺件时报错给可行动提示（`tests/test_actionable_errors.py` 钉住），
桩产物与 `NOT_RUN/PARTIAL` 一律不得签发为正式交付。

## 5. 能力与交付边界（与许可分开声明）

- 桩渲染（stub）、剪辑工程导出（draft/assemble 半成品）、真实视频出片是**三个不同档位**，
  账本状态 `SKIPPED/NOT_RUN` 不算绿灯（`renderplan/ledger.py` 与 `renderplan/stages.py` 已如此实现）。
- 真实 TTS／真实视频渲染／真实剪辑交付：待服务与费用授权，本轮 **NOT_RUN**；
  签发拦截与单测证据见 `tests/`（提交 `7415e38 W09 真实适配器测试入库；SKILL.md 补签发拦截`）。
- 人工画质与观感评价属人工门，不由机械门代替。
