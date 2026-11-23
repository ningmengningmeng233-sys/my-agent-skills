# Graphify-Jev 接入约定

只复用既有能力。不重写解析、建图、遍历、循环检测或诊断引擎，不修改上游源码来迁就集成。

## 入口（由使用者自行填写，本包不含任何具体安装路径）

- CLI：`<GRAPHIFY_JEV>/.venv/Scripts/graphify-jev.exe`（非 Windows 为 `<GRAPHIFY_JEV>/.venv/bin/graphify-jev`）
- Python：`<GRAPHIFY_JEV>/.venv/` 下对应解释器
- 版本口径：本包的命令形态按 Graphify-Jev `1.1.0`（提交 `eeaab2f4b5e742132f842e35ac618aeeeb11b7b0`）实测；换版本需重核 `--help` 与本文「指标口径」表。

`<GRAPHIFY_JEV>` 是你本机该工具的根目录，用它自己的路径替换即可；本技能不读取、不假设任何具体盘符或家目录。

> 2026-10-04 在受控样例上实测通过（详见下节）。跑不了或没有的能力一律写 `NOT_RUN` / `UNVERIFIED`，不许用估计值代替。

## 实测过的调用形态（同一版本 1.1.0、提交 `eeaab2f4…`）

```
<J> extract <项目根> --code-only --out <输出目录> --exclude generated --exclude .lemon --exclude __pycache__ --exclude node_modules --exclude dist --exclude build --exclude cache
<J> affected "<符号>" --graph <输出目录>/graphify-out/graph.json
<J> god-nodes --top 10 --graph <输出目录>/graphify-out/graph.json --json
<J> audit --graph <输出目录>/graphify-out/graph.json --json
```

以上四条在同一样例上分别返回退出码 0 / 0 / 0 / 0；`extract` 缺失图时 `audit` 会退出 1 并提示先建图。`--exclude` 与 `audit` 都不在 `--help` 里，但源码有派发、实跑有效。

用 Python API 时注意：`graphify.affected.load_graph()` 要的是 `pathlib.Path`，传字符串会在读文件处抛 `AttributeError`。这是调用方写法问题，不是上游缺陷。

## 调用形态

按上游 `--help` 现状引用的命令：`extract <path>`、`affected "X"`、`god-nodes`、`query`、`explain`、`path`、`diagnose`、`update`、`cluster-only`。

存在但**未在 `--help` 列出**的能力（源码可查派发，不能因 help 缺项就否认）：`audit`（别名 `health`、`doctor`，走 `handle_audit`）、`extract --exclude`。调用形态以执行当时的 `--help` 与现场报错为准。

本机观察值（记为观察，不作断言）：`extract --code-only --out DIR` 不需要 API Key；`--exclude <pattern>` 与 `.graphifyignore` 都会真实改变进入图的源码集合。

## 范围与排除

候选扫描排除：`node_modules/`、`vendor/`、`dist/`、`build/`、`target/`、`cache/`、`generated/`、第三方源码、编译产物、迁移快照、lock 文件、大型静态资源，叠加项目 ignore（`.graphifyignore` 优先，另有 `.gitignore` / `.git/info/exclude`；`--no-gitignore` 可只认 `.graphifyignore`）。

引用取证范围**独立于**上面的排除表：`tests`、fixtures、配置、注册表、字符串路由、宿主契约必须查。被排除的文件若可能引用候选，记为盲区，不得当作「无引用」。

## 基线与复检

- 产物目录：目标项目 `.lemon/skill-optimization/<轮次>/before` 与 `/after`，用现有输出参数指定，不覆盖用户已有的图产物，不 force。
- Before/After 必须同工具版本、同扫描范围、同配置，否则比较结果写 `UNVERIFIED`。
- 基线要一并记录：目标修订号或文件快照、工具版本、扫描范围、图新鲜度、现成测试命令与其结果、依赖计数口径、公开接口清单。

## 指标口径（写报告时必须遵守）

| 事实 | 口径 |
|---|---|
| `god-nodes` / `audit` | 按度数排序取前 `top_n`（默认 10）个枢纽节点，是**抽样检查面**。实测证据：一个 23 节点的样例图上 `audit` 返回 `total_scanned=10`，其余 13 个节点没进外部诊断。结论写成「本次 top N 内的结果」，不得声称全仓 God Class / 高复杂度类审查完成；无结果不等于全仓无问题 |
| God Node | 不等于 God Class。连接度可能包含测试、外部依赖或不同边类型；职责是否过载要另看代码归属与语义 |
| 有向性 | **逐图判定，且注意两个口径会打架**：实测同一产物 `graph.json` 的 `directed` 字段是 `False`，而 `load_graph()` 建出的对象 `is_directed()` 是 `True`、`neighbors()` 返回空但 `degree()` 有值。根因是上游刻意 force（`graphify/affected.py` 注释「Force directed so stored caller->callee direction survives the round-trip」，并把 `directed` 改写成 True），不是图坏了；所以不能因为加载对象是有向的就宣布能报扇入扇出。所以下面这条是硬规矩：出入度只能在**明确限定单一 relation 且方向语义核对过**时报告；否则写「不适用（方向语义未确认）」，没读过就写 `UNVERIFIED`。不得因为 JSON 字段或加载对象某一处显示有向，就宣布整个版本支持可靠的 fan-in/fan-out |
| 边的关系类型 | 报告度数必须附 relation 分布。实测同一样例图里混着 `calls`(7)、`imports`(7)、`imports_from`(2)、`contains`(6)、`method`(5)、`rationale_for`(4) 六种边——总 degree 是跨类型混合值，把它当调用扇入扇出就是造假 |
| 总 degree | 任何情况都不得冒充调用关系的出入度；报数须说明边类型 |
| `find_import_cycles(G, max_cycle_length=5, top_n=20)` | 输出是「该配置下检出的循环」，不是全仓循环总数；空结果不等于无循环 |
| 静态缺边 | 不证明没有动态调用；`getattr` / `importlib` / 注册表字符串 / DI / entry point / 反射都能造出图上看不见的边 |
| 未知、启发式、不支持、服务失败 | 原样标注，不得包装成确诊 |

## 外呼与凭据约束

机制事实：`jev_bridge` 会读取项目及用户级 `.env` 取 `TYPESAFE_API_KEY` / `OPENCODE_API_KEY`，端点默认 `https://api.typesafe.ai/v1/systemone`，可由 `TYPESAFE_API_URL` / `OPENCODE_API_URL` 覆盖；`is_available()` 只看有没有 Key。

本技能的动作约束（不是「Jev 永不联网」的保证）：

1. 不配置、不索取、不写入任何新凭据，不新增模型，不改全局环境或上游源码。
2. 优先使用不外呼的本地能力：`extract --code-only`、`affected`、`god-nodes`、`analyze` 结构 API。
3. `is_available()` 只用于 **Jev `audit` 的凭据判断**，不是其它模型后端的通用闸门，本技能也不为它新增凭据扫描器。其它 LLM 支撑路径改用形态禁令直接封住：本版**禁止**不带 `--code-only` 的 `extract`、**禁止** `label`；确需 `cluster-only` 时**只允许** `--no-label`（跳过 LLM 社区命名）。调 `audit` 前在独立子进程只调用既有 `is_available()`，仅记录布尔值，不输出凭据、不输出 `.env` 内容。端点除 `api.typesafe.ai` 外还有一条回退：只设 `OPENCODE_API_KEY` 而未设 `TYPESAFE_API_KEY` 时路由到 `https://opencode.ai/zen/v1/systemone`；两者都可被 `*_API_URL` 覆盖。
4. `is_available()` 为假时才运行 `audit`；为真或检查本身失败时**不运行可能外呼的 `audit`**，改用既有 `build_node_profile` 与本地图事实，并把外部诊断记 `NOT_RUN`。本地结构证据充分时审计可继续——外部诊断未运行不等于建图失败。
5. 实际运行了 `audit` 才记录其 `mode`（如 `failsafe_heuristic` / `jev_system_one`）与 `total_scanned`、生效的 `top_n`。两种 mode 的结论不得混写成同一种可信度；`mode` 只说明分析模式，不证明零网络请求。

## 失败即停

Jev 缺失、建图失败、图过期或语言不支持 → 停在结构事实这一步，报告原因与已完成部分，不让候选生成抢跑，不用「反正我已经手工看过」继续整条链路。
