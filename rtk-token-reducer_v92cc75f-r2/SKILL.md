---
name: rtk-token-reducer
description: 检查与操作 RTK Token Reducer 扩展：bash 输出进上下文前经 rtk 压缩、原文 spool 回溯、压缩统计与自学规则。当用户问「压缩了多少 token」「bash 输出被裁了吗」「取回被省略的原文」「关掉/启用 rtk 钩子」「把这条命令的压缩规则固化」时使用。
metadata:
  version: "92cc75f-r2"
  display_name: RTK Token Reducer

---

# RTK Token Reducer

扩展点钩子（`PostToolUse` + `PreCompact`）把每条 bash 命令的输出先过一遍 `rtk pipe`，
只有真省下字节才改写进上下文；改写前逐字原文落 spool，所以任何被裁掉的内容都能取回。

## 数据目录

默认 `~/.qoder/rtk-reducer/`（当前用户目录）；环境变量 `RTK_REDUCER_HOME` 可显式覆盖。
`RTK_REDUCER_HOME` 可覆盖。内含：

- `spool\<时间>-<哈希>.txt` —— 压缩前的逐字原文
- `ledger.jsonl` —— 每次压缩的账单（命令、过滤器、前后字节、spool 路径）
- `stats.json` —— 累计：次数 / 前字节 / 后字节 / 放行次数 / 各命令形态命中数
- `learned-rules.toml` —— 同一形态压缩 ≥3 次后自动生成的 rtk 过滤规则

## 常用动作

| 想做什么 | 怎么做 |
|---|---|
| 看压缩统计 | `python -X utf8 "<插件目录>/hooks/report.py"`；对照 rtk 自身口径加 `--rtk` |
| 取回被裁的原文 | 提示行末尾就是 spool 路径，直接读那个文件（`rtk recall` 只对 rtk 原生过滤生效，钩子走的是 pipe 模式，不落它的哈希库） |
| 临时停用 | 环境变量 `RTK_REDUCER_OFF=1` |
| 调压缩门槛 | `RTK_REDUCER_MIN=<字节数>`（默认 700；短输出一律原样放行） |
| 固化某类命令的压缩 | 把 `learned-rules.toml` 里对应 `[filters."..."]` 并进项目 `.rtk/filters.toml` 或 `%APPDATA%\rtk\filters.toml` |
| 跑自检 | `python -X utf8 "<插件目录>/hooks/selftest.py"`（用临时目录，不污染真实账单；全绿 exit 0） |

## 绝不压缩的口径

- 短输出（低于 `RTK_REDUCER_MIN`）—— 省不了几个字节，不值得冒裁掉信息的风险
- 保留清单命中的命令 —— `md5sum/sha*/base64/xxd/od/openssl/git apply|show|format-patch/rtk recall` 这类输出必须逐字进上下文
- 选中的 rtk 过滤器压不出收益（输出不比原文短）—— 宁可放行也不做假压缩
- 已经是 `rtk` 开头的命令 —— 它自己已经压过一遍

## 与 rtk 的分工

rtk 负责压缩算法与规则引擎（`rtk pipe -f <filter>`，实测可用过滤器：
`cargo-test, pytest, go-test, go-build, ctest, tsc, vitest, grep, rg, find, fd, git-log, git-diff, git-status, log, mypy, ruff-check, ruff-format, sqlfluff-lint, prettier, phpunit, pest, paratest, php-test, ecs, phpstan, pint`）。
本扩展只负责在钩子点把命令形态映射到过滤器、把前后字节记账、并在 PreCompact 时回报统计。

注意：rtk 0.49 的 `rtk gain` 并**没有** `--save-rule` 参数（实测 `rtk gain --help` 无此项）。
需求里的「压缩后提示保存规则」由本扩展自己实现：规则落 `learned-rules.toml`，
提示合并进 `.rtk/filters.toml`；要 rtk 从历史里反向学规则可用 `rtk learn --write-rules`。
