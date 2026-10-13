# 上游来源与本地改动声明

## 〇、三层模型（L/A/U）

| 层 | 内容 | 同步策略 |
|---|---|---|
| L 本地总控层 | 根 SKILL.md：注册身份、9 触发与排除路由、物理状态机、中文工件路径、门槛与审批/裁决合同 | 禁止上游覆盖；仅版本号、upstream-pin、来源说明一句话随同步更新 |
| A 本地适配层 | 7 条机械映射、门面改名 SKILL.md→MODULE.md、中文路径、`.lemon/`、`柠檬裁决:`、`[柠檬思考]` | 每次同步重新核查；映射外本地差异登记为 overlay 保全，不自动清除 |
| U 上游模块层 | modules/ 下 15 个模块 = 上游 skills/ 原样 + A 层映射 | 仅合入上游实际变化文件，其余模块原字节保留 |

7 条映射只描述模块层的机械适配，不概括本技能全部自有定制；根总控规则、触发路由、审批/裁决合同均为柠檬自有。禁止全局 superpowers→柠檬 替换：上游官方仓库链接、命名空间（`superpowers:xxx` 引用）与许可保留原文。

## 一、上游

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/obra/superpowers.git |
| commit | `8ca22dba9a94f28898bbce59f2537ff4d87c747d` |
| 日期 | 2026-09-25（v6.4.2，package.json） |
| 核验 | 2026-10-03，独立 clone；`git ls-remote` 远端 HEAD 与本 pin 一致，无更新漂移 |
| 许可 | MIT（Copyright (c) 2025 Jesse Vincent），全文见 `LICENSE` |

## 二、modules/ 与上游的关系

`modules/` 下 15 个模块 = 上游 `skills/` 原样内容 + 下列 **7 条机械映射**，逐行可验证（映射外逻辑差异=0）：

| # | 上游原文 | 本地替换 | 用例 |
|---|---|---|---|
| 1 | `docs/superpowers/plans/` | `文档/柠檬/工单/` | 计划/工单落盘路径 |
| 2 | `docs/superpowers/specs/` | `文档/柠檬/设计/` | 设计文档落盘路径 |
| 3 | `.superpowers/` | `.lemon/` | 运行期工作目录（sdd 工作区等） |
| 4 | `Ruling:` | `柠檬裁决:` | 自主裁决记录格式 |
| 5 | `Using superpowers:` | `[柠檬思考]` | 模块交接语 |
| 6 | `bash scripts/` | `scripts/` | 脚本调用（本机 git-bash 可直呼） |
| 7 | `YYYY-MM-DD-<topic>-design.md` | `YYYY-MM-DD-<主题>-设计方案.md` | 中文文件名约定 |

**文件级改动**：
- 每个模块的门面文件由上游 `SKILL.md` 改名为 `MODULE.md`——治理需要：宿主会把任意 `SKILL.md` 注册成独立技能，改名可避免 15 个内部模块变成幽灵技能条目（内容零改动）。
- 上游新增模块（如 diagnosing-superpowers）随同步一并纳入；6.4.1 批次新增的 `executing-plans/scripts/task-start`、`task-done` 与 `using-superpowers/references/claude-code-tools.md`、`muse-tools.md` 保持纳入。

**本次 6.4.1 → 6.4.2 同步（2026-10-03）**：
- 上游 skills/ 变化仅 2 项：`M skills/writing-plans/SKILL.md`、`D skills/writing-plans/plan-document-reviewer-prompt.md`。
- writing-plans 变化要点：计划须写明精确文件、接口、测试名与断言、验证输出；函数体只允许出现在接口与测试无法唯一决定的算法处；计划粒度按「可检查结果」拆步而非固定时长。不取消 TDD、用户审阅或执行方式选择（Execution Handoff 一节原样保留）。
- plan-document-reviewer-prompt.md 为上游弃用模板，全树检索无本地引用，随上游退役；本地任务评审/代码评审/先问后做等合同不受影响。安装时移入恢复区，不永久删除。
- 未变化 14 模块与根附属文件原字节保留（候选逐文件 SHA 对账）。

**冲突规则**：上游改进与用户明确的本地规则冲突时，先记录冲突与影响，不以「原版更新了」直接删本地规则；映射外本地差异登记为 overlay 保全。

**验证深度（2026-10-03 r2）**：三方差异（old/new/local）逐文件按「映射后字节等价 + 归一双轨」复核；保护器先在合成变异上校准（改名/删触发/删排除/改路径/加孤儿/改行尾/删模块均拒收）再核验候选；C01—C12 保留合同逐项验证。审计证据存 F 盘 evidence 区，不随技能分发。

## 三、本地原创部分（非上游）

- 根 `SKILL.md`：物理状态机路由、中文工件路径合同、四大门槛摘要、模块索引表——柠檬原创；v1.1.0 起只保留「单一事实源」骨架，细则指向 modules。
- `UPSTREAM-PIN.txt`、`UPSTREAM.md`、`LICENSE` 的本地说明段。
- 交付物命名约定：`文档/柠檬/设计/YYYY-MM-DD-<主题>-设计方案.md`、`文档/柠檬/工单/YYYY-MM-DD-<任务>-实施计划.md`。

## 四、升级姿势（下次同步上游）

1. `git clone --depth 50 https://github.com/obra/superpowers.git`（走 10808 代理），固定目标 commit 后做三方差异（旧上游 / 新上游 / 本地）。
2. 逐文件比较采用「映射后字节 + 归一双轨」双口径；本地映射外差异先保全登记，不得静默丢弃。
3. 校验：modules/**/SKILL.md 计数=0；保护器对候选 rc=0；C01—C12 矩阵全绿。
4. 更新 `UPSTREAM-PIN.txt` 的 commit/日期/版本；候选经用户确认后才安装。
