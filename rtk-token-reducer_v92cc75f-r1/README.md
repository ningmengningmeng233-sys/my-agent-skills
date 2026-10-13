# RTK Token Reducer

Qoder CLI 扩展：在 **所有 bash 命令输出进入模型上下文之前**，先用 [`rtk`](https://github.com/rtk-ai/rtk) CLI 代理压一遍，
并在 PreCompact 事件里回报压缩统计。

## 它解决什么

Agent 跑一条 `find`/`grep`/构建/测试命令，几千行重复输出直接灌进上下文，token 与账单同步起飞。
本扩展挂一个扩展点钩子，把这段输出交给 rtk 的过滤器压成几行，同时 **逐字原文先落盘**，
所以压缩是「可回溯的折叠」，不是「丢数据」。

## 目录结构

```
rtk-token-reducer/
  .qoder-plugin/plugin.json            扩展清单（name/hooks/skills 指向）
  hooks/hooks.json                     扩展点声明：PostToolUse(Bash) + PreCompact
  hooks/common.py                      内核：定位 rtk、命令形态->过滤器映射、记账、自学规则
  hooks/rtk_compress.py                PostToolUse 钩子：压缩 bash 输出并改写 tool response
  hooks/compact_stats.py               PreCompact 钩子：回报累计压缩统计
  hooks/report.py                      命令行看统计（--json / --tail N / --rtk）
  hooks/selftest.py                    21+ 项断言的可运行自检（用临时数据目录）
  skills/rtk-token-reducer/SKILL.md    给 Agent 看的操作说明
  install.py                           把钩子登记进 settings.json（默认只预演）
```

## 安装

两条路，二选一：

1. **作为扩展装**（推荐）：把本目录放进 Qoder 插件目录后，`plugin.json` 里的 `hooks` 字段会让
   `hooks/hooks.json` 自动生效，无需改 settings.json。
2. **免安装登记**：直接改配置。改配置属高危写操作，所以 `install.py` 默认只打印影响清单与回滚命令，
   要你明确加 `--apply` 才落盘（落盘前自动留时间戳备份）：

```bash
python -X utf8 install.py --scope user                    # 预演：看要写什么
python -X utf8 install.py --scope user --apply            # 真写用户级 settings.json
python -X utf8 install.py --scope project --root . --apply # 只对本项目生效
python -X utf8 install.py --scope user --remove --apply    # 摘除本扩展钩子，其他钩子不动
```

依赖：`rtk` 在 PATH 上（本扩展**只从 PATH 查找 rtk**，不探测任何固定盘符路径；实测兼容 rtk 0.49.0）；`python` 在 PATH 上。
数据根：环境变量 `RTK_REDUCER_HOME` 优先，缺省为当前用户目录 `~/.qoder/rtk-reducer`。
找不到 rtk 时钩子安静放行原文，不会挡命令、不会丢数据。

## 宿主能力支持（截至 2026-10-11，接收侧以 NOT_RUN/UNSUPPORTED 如实登记）

| 能力 | Qoder CLI | Claude Code | Codex | Cursor |
|---|---|---|---|---|
| 技能加载（SKILL.md 可发现/点名） | 支持（.qoder-plugin 清单） | 支持 | NOT_RUN（未验） | NOT_RUN（未验） |
| 自动压缩（PostToolUse Bash hook 改写输出） | 支持（原生扩展形态） | hook 协议兼容，`install.py --scope user --apply` 登记 settings.json（作者环境实测过；接收侧 NOT_RUN） | UNSUPPORTED（未验） | UNSUPPORTED（未验） |
| PreCompact 统计钩子 | 支持 | UNSUPPORTED（未验） | UNSUPPORTED | UNSUPPORTED |

**技能加载 ≠ 自动压缩**：不支持 hook 的宿主只能把 SKILL.md 当操作说明加载，输出压缩不会发生。

## 与 rtk CLI 怎么配合（中文说明）

### 1. 钩子怎么选过滤器

按命令形态映射到 rtk 内置过滤器（`rtk pipe -f <名字>`）：

| 命令形态 | 过滤器 | 命令形态 | 过滤器 |
|---|---|---|---|
| `find` / `fd` | `find` | `git log` | `git-log` |
| `ls` / `dir` / `tree` | `find` | `git diff` | `git-diff` |
| `grep` / `rg` | `grep` | `git status` | `git-status` |
| `pytest` | `pytest` | `mypy` | `mypy` |
| `vitest` / `jest` | `vitest` | `ruff check` | `ruff-check` |
| `cargo test` | `cargo-test` | 形态不明且行重复多（构建日志） | `log`（去重折叠） |
| `go test` | `go-test` | 形态不明且行各不相同 | `find`（保行） |

规则：**只有压缩后真的比原文短才改写**；压不出收益就原样放行，绝不做「假绿」式压缩。

形态不明时的兜底按**行多样性**选：实测 rtk 的 `log` 过滤器会把「每行都是不同数据」的输出
（例如 `ls -la` 的 92 行清单）折叠成一句 `Log Summary`——省了 98% 但把内容全丢了。
所以非空行去重率 ≥0.6 的输出只走保行的 `find`，重复行多的日志才交给 `log` 折叠。

### 2. 何时用 plain（全文）输出

四种情况下你会拿到原文全文：

- 输出短于 `RTK_REDUCER_MIN` 字节（默认 700）——省不了多少，不值得冒裁掉信息的风险
- 命令命中保留清单：`md5sum`/`sha*sum`/`base64`/`xxd`/`od`/`openssl`/`git apply|show|format-patch`/`rtk recall`
- 命令行首加 `RTK_PLAIN=1 ` 随手豁免，例如 `RTK_PLAIN=1 find . -name "*.py"`
- 整体停用：`RTK_REDUCER_OFF=1`

需要引用 **文件名+行号** 的核对（例如「交付引用必须当场实测」这类纪律），一律用 `RTK_PLAIN=1` 前缀，
或按下一节从 spool 读原文。

### 3. 如何取回被省略的内容

压缩前，逐字原文已经写进数据目录：

```
<数据根>/spool/20260928-120330-a1b2c3d4.txt（数据根 = RTK_REDUCER_HOME，缺省 ~/.qoder/rtk-reducer）
```

改写后的输出第一行就带着这个路径（`[rtk-reducer｜...｜逐字原文 <路径>]`），直接读该文件即得全文。
注意 `rtk recall` 只对 rtk 原生过滤（`rtk <子命令>`）生效——本钩子走 `rtk pipe`，不进 rtk 的哈希库，
所以取回靠 spool，别去敲 `rtk recall`（钩子提示里也这么写）。

### 4. 如何核对压缩统计

```bash
python -X utf8 hooks/report.py                 # 累计次数/前后字节/压缩率/放行数 + 高频形态
python -X utf8 hooks/report.py --tail 20       # 再看最近 20 条逐命令账单
python -X utf8 hooks/report.py --json          # 机器可读
python -X utf8 hooks/report.py --rtk           # 附 rtk 自身 `rtk gain` 口径，两边对照
```

原始账单在 `<数据根>/ledger.jsonl`（一行一条 JSON：时间、命令、过滤器、前后字节、spool 路径、会话号）。
PreCompact 钩子会在上下文将要压缩时把同一行累计统计播报一次，Agent 与用户都能看到。

### 5. 自动学规则

同一命令形态累计压缩 ≥3 次（`RTK_REDUCER_RULE_N` 可调），钩子会按 rtk 官方 `filters.toml` 模板
往 `<数据根>/learned-rules.toml` 追加一条 `[filters."形态"]` 规则，并在输出尾部提示：
把它并进项目 `.rtk/filters.toml` 或 `%APPDATA%\rtk\filters.toml` 后，这类命令就由 rtk 原生压缩，不再依赖钩子。

## 需求偏差登记（实测为准，不编造）

| 需求原话 | 实测事实 | 本扩展的做法 |
|---|---|---|
| 「PreToolUse 是扩展点」 | Qoder CLI 的 **PostToolUse** 才支持 `hookSpecificOutput.updatedToolOutput`（可替换任意工具的输出）；PreToolUse 只能改 `updatedInput`（改命令本身，等于要在钩子里重实现 rtk 全套子命令路由，还会破坏 shell 管道/重定向语义） | 用 PostToolUse(Bash) 做输出压缩。命令原文保持不变，语义零风险 |
| 「压缩后提示用户跑 `rtk gain --save-rule`」 | rtk 0.49.0 的 `rtk gain` **没有** `--save-rule`（`rtk gain --help` 实测无此项）。落规则的正道是 `filters.toml`，或 `rtk learn --write-rules` | 自己生成规则文件 `learned-rules.toml` 并提示合并路径；统计提示用真实存在的 `rtk gain` |
| 「PreCompact 事件里 rtk 会显示压缩统计」 | rtk 自身没有 Qoder 的 PreCompact 集成（`rtk init --agent` 只支持 claude/cursor/… 无 qoder） | 本扩展自带 PreCompact 钩子，播报 rtk-reducer 累计 + 自学规则条数 |

## 自检

```bash
cd hooks && python -X utf8 selftest.py     # 全绿 exit 0；rtk 缺失 exit 2（不做假绿）
```

覆盖：长输出必压且原文逐字落盘、短输出放行、保留清单放行、`RTK_PLAIN=1` 豁免、坏 stdin 安静放行、
dict 形态 tool_response 保结构、压不出收益时不做假压缩、阈值触发自学规则、PreCompact 出 JSON、report 可跑。
