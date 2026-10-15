# QUICKSTART：五分钟上手（位置速查单一来源）

## 首次使用四步

1. **配置**：复制 `.env.example` 为真实 `.env`（推荐放技能包外 `~/.asset-extract/.env`，或技能目录内），填 `OPENAI_API_KEY` / `SUMMARY_API_URL` / `SUMMARY_API_MODEL`；依赖 `pip install requests python-dotenv`
2. **启动**：对编排器说「提取资产 <源文件路径> 题材 <题材>」——步骤 0 开始界面会展示输入源/画风/库内状态面板，确认后自动跑 8 步
3. **取产物**：全部在 `<资产库>` 下——`描述\`、`提示词\基础\`、`提示词\衍生\`、`服饰设计\`、`连续性\场景母版.md`；总表看 `反推_最终.md`（末尾「交付物说明」区分可直接使用与过程文件）；结构化关系与事实等级看 `.process\资产索引.json`；交付凭证看 `交付清单.json`
4. **追更**：连载新增章节后按序重跑步骤 1-7（断点自动跳过已完成部分，只有变化的部分重跑）

## 一键跑完全流程（可选，等价于手工跑步骤 0–8）

不想逐步驱动时用它——只做编排，不改任何步骤脚本的行为：

```powershell
python scripts\maintenance.py init --lib <资产库> --input-source <源文件> --genre 古风 --style 仙侠奇幻风
bash run_pipeline.sh --lib <资产库> --input <源文件> --genre 古风 --style 仙侠奇幻风
```

- Windows 无 bash 时用同目录的 `run_pipeline.bat`，参数一致。
- **失败即停**：任一步非零退出就停下来，打印原因码与失败步骤名，不会带着坏数据往下跑。
- **续跑 = 再敲同一条命令**：断点跳过由各步骤脚本自带（IndexDB 指纹 / `--skip-existing`），已完成的不重复出网。
- `--dry-run` 只列出将要执行的步骤、不出网不落账，可先拿它核对参数。
- `--force` 透传 `--force-retry`，用于强制重跑已被判失败的步骤。
- 想单独控制某一步，仍然直接跑上面「首次使用四步」里的各条命令即可，两者不互斥。

## 多端点与按角色分档（可选，零额外依赖）

只有一个端点时什么都不用配。想让**机械活走快模型、判断活留强模型**，在仓库根放 `llm_nodes.json`（照 `llm_nodes.json.example` 改）：

- 允许同一个 URL、同一把钥匙、只换模型名；但每个节点的 `name` 必须唯一。
- **默认 `role` 是 `judgement`**；要让分档真的生效，**两个节点都得显式写 `roles`**，否则都按判断活处理。
- 跑完用 `python scripts\maintenance.py perf-report` 看按角色分组的小表，确认分档确实命中。

## 位置速查

| 要找什么 | 位置 |
|---|---|
| 输入拆段 | `<资产库>\原文段\<小说名>-<作者>\` |
| 规范化中间产物（DOCX/EPUB/多文件） | `<资产库>\.process\normalized_sources\`（`规范化原文.md` + `来源清单.json`） |
| 全局资产主名表 | `<资产库>\<小说名>-<作者>-登记表.md` |
| 最终反推总表（12 模块） | `<资产库>\反推_最终.md` |
| 视觉描述 / 基础 / 衍生提示词 | `<资产库>\描述\`、`<资产库>\提示词\基础\`、`<资产库>\提示词\衍生\`（各 3 个合并文档） |
| 服饰设计 | `<资产库>\服饰设计\<剧名>\` |
| 过程文件（裁决/对账，勿删） | `<资产库>\.process\` |
| 断点库 | `<资产库>\.asset_state.db` |
| 画风卡与风格包 | `references\画风库.md`、`references\stylepacks\` |
| 资产库状态 / 手动初始化 / 备份 / 恢复 / 验证 / 迁移 | `python scripts\maintenance.py status|init|backup|restore|verify|migrate --lib <资产库>` |
| 资产索引 / 场景母版 / 交付清单 | `<资产库>\.process\资产索引.json`、`连续性\场景母版.md`、`交付清单.json` |

## 输入格式支持（v2.15.0）

| 格式 | 用法 |
|---|---|
| `.txt` / `.md` | 与旧版完全一致：`--input-source` 单文件直读，不产生中间产物 |
| `.docx` | 直接传 `--input-source "剧本.docx"`（读正文段落、标题层级与表格文本，忽略页眉页脚） |
| `.epub` | 直接传 `--input-source "小说.epub"`（按 spine 顺序读章节，剔除导航页与样式脚本） |
| 多文件项目 | `init --source 卷1.txt --source 卷2.txt --source-order-file 顺序清单.txt`；拆段同法加 `--order-file` |
| `.pdf` | **明确拒绝**（`UNSUPPORTED_SOURCE_FORMAT`）。先转 TXT/Markdown/DOCX/EPUB——不支持 PDF，也不建设文字识别 |

DOCX/EPUB/多文件会先规范化为 `<资产库>\.process\normalized_sources\规范化原文.md`（原文件只读），段 frontmatter 额外记录 `原始来源定位` 与 `来源清单`。只做规范化不拆段：

```powershell
python scripts\normalize_sources.py "卷1.docx" "卷2.epub" --lib <资产库> --order-file "顺序清单.txt"
```

## 三种产品结果与新增命令（v2.17.0-rc.1）

**先说清楚：这套流水线按你要的结果分三种走法，用命令或设置显式选，不会替你猜。**

| 你要的结果 | 怎么选 | 跑到哪一步算完 |
|---|---|---|
| 只要资产反推（四表、索引、描述） | 默认即是；或 `configure-visual --mode data-only` | `maintenance.py verify` 签发 `交付清单.json` |
| 反推 + 提示词 | `configure-visual --mode prompt-only`；提示词走步骤 4/5/6 | 同上（提示词纳入签发契约） |
| 反推 + 提示词 + 生图 | `configure-visual --mode direct-generate`，再 `run-visual` | 资料线与视觉线**各自**签发；图片必须过主模型审核 |

三条新命令（编译链的显式入口）：

```powershell
# 由资产索引重建唯一类型化总账与依赖图，并报告数量守恒（谁都没漏）
python scripts\maintenance.py build-ledger --lib <资产库> --json

# 由总账确定性编译提示词。默认 shadow：只写 .process\shadow-提示词差异.md，不动正式产物
python scripts\maintenance.py compile-prompts --lib <资产库> --shadow
# 确认差异没问题后再并入正式提示词（先备份原文件，只改已存在节的正文）
python scripts\maintenance.py compile-prompts --lib <资产库> --apply

# 按当前产品结果派发生图。--batch 只是「这批同时生成几张」（1–4）
python scripts\maintenance.py run-visual --lib <资产库> --batch 3
```

**视图规格不可调**：人物要哪几张视图由 Omni 标准定死（核心四张、重要配角两张起、普通/群演一张），
命令不提供改视图的参数，也不会问你。

### 状态怎么读

`python scripts\maintenance.py status --lib <资产库> --json` 一次给出四条事实线：

- `pipeline_state`：资料线的总状态（`DELIVERED` / `VERIFIED` / `PROMPTS_READY` / `BLOCKED` …）。
- `data_delivery`：资料交付线（与上面同源）。
- `visual_delivery`：视觉交付线，另有 `stage` = `PLANNED`（已规划）→ `GENERATING`（生成中）
  → `REVIEWING`（待审核）→ `REWORK`（要返工）→ `SIGNED`（已签发）；未启用时是 `NOT_ENABLED`（合法状态）。
- `compiler_stages`：编译链八段（事实库 / 总账 / 提示词 / 图片计划 / 生成 / 审核 / 返工 / 签发），
  每段 `state` 是 `DONE` / `PENDING` / `BLOCKED` / `NOT_APPLICABLE`。

**最新失败会立刻撤销交付**：只要视觉事件日志里某任务**最新一条**事件是传输失败或任务阻断，
视觉线立刻离开 `DELIVERED`，不必等计划文件更新——别拿上一次的截图当当前状态。

### 出问题怎么修

| 症状 | 原因 | 怎么办 |
|---|---|---|
| `status` 不再是 `DELIVERED`，原因 `SOURCE_CHANGED` | 原始输入或段文件变了 | 重跑受影响的段与后续步骤，再 `verify` |
| 原因 `ARTIFACTS_CHANGED` / `INDEX_CHANGED` | 产物或索引被改动 | 重跑对应步骤；不要手改产物 |
| `verify` 报 `check_asset_coverage` 未通过 | 正式资产既没提示词也没豁免理由 | 为该资产生成基础提示词，或在 `提示词\基础\跳过清单.md` 写明非资产/取消理由 |
| `verify` 报 `check_artifact_graph` 未通过 | 有总账但没依赖图（半截编译态） | 跑一次 `build-ledger` |
| `verify` 报 `check_visual_review` 未通过 | 选了直接生成图片，但图片没过主模型审核 | 走 `review-export` → `review-import` → `publish` |
| 升级到 v2.17 后旧交付清单显示「待验证」 | 签发契约由 3 项扩为 9 项，旧签发没核验数量守恒与图片审核 | 重跑一次 `maintenance.py verify` 重新签发（上一份自动留档到 `交付清单历史\`） |

### 离线验证（不花钱、不需要密钥）

```powershell
python -m pytest -q 测试\unit                       # 全量单测
python 测试\e2e_flow.py --snapshot                  # 端到端（mock 端点，零真实请求）
python 测试\run_all_checks.py                       # 本地 16 项质量门
python 测试\run_asset_compiler_benchmark.py --fixture 测试\fixtures\asset_compiler_regression --out .process\benchmark.json
python -m compileall -q scripts 测试
```

上面每条都不发真实网络请求、不需要密钥。真实出图与真实图片内容审核是**另外**的事，
没跑过就一律记 `NOT_RUN`，不会被写成「已通过」。

## 双交付状态线（v2.15.0）

资产资料与视觉资产是两条独立状态线，互不阻塞：

- `status --json` 的 `data_delivery`：四类资产、证据、关系、连续性、描述与提示词是否通过资料门。
- `status --json` 的 `visual_delivery`：未创建视觉计划时为 `NOT_ENABLED`（「视觉生产：未启用」）——**合法状态，不是失败**。

没有生图能力、没有 API 密钥、没有任何图片时，资料交付照常完成。视觉生产只接管下游，不接管原文事实判断、稳定编号、关系、连续性与迁移。

## 启动选项：这次是否直接生成图片（v2.16.0）

技能的首要职责始终是**从剧本/小说反推四类资产**；生图是可选项，选「不生成」不影响资料交付。

交互宿主按人话提问；非交互宿主用一条命令落库（存到 `.process\项目设置.toml`，之后自动沿用）：

```powershell
# 只反推资产，不生图（默认；不创建视觉计划、不调用图片 API）
python scripts\maintenance.py configure-visual --lib <资产库> --mode data-only

# 不直接生成，只要提示词任务包（可拿去 Codex/Gemini 手工出图）
python scripts\maintenance.py configure-visual --lib <资产库> --mode prompt-only

# 直接生成
python scripts\maintenance.py configure-visual --lib <资产库> --mode direct-generate `
       --parallel 3 --composition auto --review-subagents off

python scripts\maintenance.py configure-visual --lib <资产库> --show      # 查看当前设置
```

交互宿主对应四问（只问一次，之后沿用）：

1. 「这次是否直接生成图片？」
2. 选「是」后才问：「同时生成几张？默认 3，可选 1 到 4。」
3. 「直接自动生成，还是每批先看构图卡？默认自动生成。」
4. 「是否启用两个审核子代理？默认关闭。」

选择「不直接生成」时**不再追问**并发与审核设置，原有提示词能力继续可用。

- `data-only`：`visual-plan` 会以 `VISUAL_MODE_DATA_ONLY` 拒绝创建视觉计划；
- `prompt-only`：`run` 会以 `VISUAL_MODE_PROMPT_ONLY` 拒绝自动生成（零 API 调用），`export` 照常导出任务包；
- 未做过选择（没有设置文件）时行为与旧版一致，显式 `visual-plan` 仍可用。

## 视觉生产速查（v2.15.0，可选下游模块）

```powershell
python scripts\maintenance.py visual-plan --lib <资产库> --style 仙侠国风电影CG   # 规划 + 渲染阅读稿
python scripts\visual_production.py export --lib <资产库> --task <任务ID>          # 无密钥：导出任务包
python scripts\visual_production.py import --lib <资产库> --task <任务ID> --image <图片>
python scripts\visual_production.py check  --lib <资产库> --task <任务ID> --qc <检查结果.json>
python scripts\visual_production.py run    --lib <资产库> --task <任务ID>          # 有密钥：自动生成
python scripts\visual_production.py check  --lib <资产库>                           # 查看全部任务状态
python scripts\compose_reference_board.py  --lib <资产库> --task <任务ID>          # 只由已通过图片拼版
python scripts\maintenance.py verify-visual --lib <资产库>                          # 视觉交付门（独立）
python scripts\manage_styles.py list                                                 # 风格清单（中文正式名）
```

- 计划机器事实源是 `<资产库>\视觉资产\计划.toml`；`计划阅读版.md` 由它渲染，改 Markdown 不改变状态。
- 每项资产单独生成、单独检查、单独返工；**最多自动 3 轮**，之后转「需要人工决定」。
- 禁止让模型一次生成多格人物板；参考板只能由已通过图片本地合成。
- 视觉未启用/失败都不影响「资产资料已交付」；两条交付线分别签发。

## 视觉批量、审核与迁移速查（v2.16.0-rc.1）

```powershell
python scripts/visual_production.py status    --lib <资产库> --json       # 现在能做什么、卡在哪
python scripts/visual_production.py run-ready --lib <资产库>              # 批量生成可执行任务
python scripts/visual_production.py resume    --lib <资产库>              # 上次中断后接着跑
python scripts/visual_production.py pause     --lib <资产库> [--clear]    # 暂停 / 继续派发
python scripts/visual_production.py review-export --lib <库> --task <任务ID>
python scripts/visual_production.py review-import --lib <库> --task <任务ID> --result <结果.json>
python scripts/visual_production.py publish   --lib <库> --task <任务ID>
python scripts\migrate_visual_library.py scan   --lib <资产库> --out 迁移提案.json
python scripts\migrate_visual_library.py apply  --lib <资产库> --proposal 迁移提案.json
python scripts\migrate_visual_library.py verify --lib <资产库> --baseline 迁移前清单.json
```

- 没有密钥时 `run-ready`/`run` 会明确失败并指路；`export`/`export-all` 与 `status` 永远可用。
- 审核必须由**当前主模型**读图后回写；Gemini 不终审。硬门问题一票否决，不能「通过但有备注」。
- 迁移 `scan` 是只读的，先把提案拿来核对；`apply` 只复制唯一匹配项，原文件不动、不自动通过。

## 四级事实与生产优先级（v2.13.1）

- 事实等级：剧本事实（有原文摘句）/ 强推断（两条以上线索）/ 制作提案（美术填空，不入事实字段）/ 待定冲突（阻断生成，进附录）
- 生产优先级：P0（被依赖）/ P1（核心）/ P2（辅助）/ P3（文字足够，跳过独立提示词并记录原因）
- 老库升级：`python scripts\maintenance.py migrate --lib <资产库>`（先备份，旧表不动，缺证据标制作提案）

## 画风怎么选

- 不指定：按 `references\题材-画风映射.md` 自动命中（玄幻/仙侠等→H7，古风→A1，现代都市→C6 …）
- 指定：`--style` 支持卡片编号（H7）、新名（高精度三维国漫设定集风）、别名（斗罗风、儿童插画、水墨 等旧名俗称）
- 46 张可选画风见 `references\画风库.md`；新增画风蒸馏卡走 `references\stylepacks\README.md` 零代码流程

## 日常维护五条命令

```powershell
python scripts\maintenance.py init --lib <资产库> --input-source <原始txt/md> --genre <题材> --style <画风>   # 手动初始化（写 .start-state.json）
python scripts\maintenance.py status --lib <资产库>          # 断点定位 + 产物统计
python scripts\maintenance.py backup --lib <资产库>          # 打包为时间戳 zip（--out 指定目录）
python scripts\maintenance.py restore --zip <备份zip> --lib <资产库> --force   # 恢复（覆盖前必须 --force）
python scripts\maintenance.py verify --lib <资产库> [--style <画风>]    # 验证收口：一键跑全部检查，输出交付契约验证栏
```

### 手动初始化（非交互宿主必做）

编排器（步骤 0 开始界面）会自动写 `.start-state.json`；无自动技能触发时手动补这一步：

```powershell
python scripts\maintenance.py init --lib "D:\资产库" --input-source "C:\素材\小说.txt" --genre 古风 --style 仙侠奇幻风
```

```bash
python scripts/maintenance.py init --lib "/资产库" --input-source "/素材/小说.txt" --genre 古风 --style 仙侠奇幻风
```

- `--style` 用画风库编号或卡名均可（`A2` 与 `仙侠奇幻风` 是同一张卡）；未命中卡片会**显眼提示**并降级为自由描述模式，不静默丢画风锁。

### 旧库总账迁移（v2.17 起；与上面的 `migrate` 不是一回事）

`maintenance.py migrate` 处理的是 v2.13 的四张表；**把旧库升级成类型化总账 + 依赖图**用的是独立入口，四个动作都非破坏：

```powershell
python scripts\migrate_asset_ledger.py scan     --lib <资产库>              # 只读：报共多少项、哪些有歧义
python scripts\migrate_asset_ledger.py apply    --lib <资产库>              # 迁移前自动备份，且不覆盖旧 Markdown
python scripts\migrate_asset_ledger.py rollback --lib <资产库>              # 按清单还原并逐文件核对哈希
python scripts\migrate_asset_ledger.py shadow   --lib <资产库> --work <目录>  # 只在副本上跑，绝不改动原库
```

- 加 `--json` 则 stdout 只有一个合法 JSON，方便脚本消费；`apply` 重复执行是幂等的（已是当前内容会明确说"未改写"）。
- 建议顺序：先 `shadow` 预演看清差集与预计节省，再 `scan` 核对歧义项，最后 `apply`。
- 想反悔就 `rollback`——它会还原文件并移除本次迁移新增的文件，哈希逐项核对，不一致即非零退出。
- **原始输入必须留在原位**：`verify` 的签发门（v2.14.0）会重新核验源文件；源被移动/删除会使交付清单立即失效并拒签（`SOURCE_UNVERIFIABLE`）。
- 已存在且参数不同的状态文件必须加 `--force` 才覆盖（覆盖即改变源证据，旧交付清单随之失效）。

## 跨宿主最小入口（无自动技能触发时照此执行）

- **环境要求**：Python ≥ 3.10；依赖仅 `requests`、`python-dotenv`（开发/测试另装 `pytest`、`flake8`，或 `pip install -e .[dev]`）。
- **配置优先级**（与 `scripts/engine/common.py::load_env` 一致）：环境变量 `ASSET_ENV_PATH` 指向的文件 → 技能目录内 `.env` → 用户目录 `~/.asset-extract/.env`。密钥放技能包外优先。
- **资产库放外面**：`--lib` 指向任意空目录（建议技能包外），技能目录保持只读。
- **只读 vs 需要 API**：`split_segments`（拆段，纯本地切分）与 `init` / `status` / `verify`（含 `check_*`）/ `backup` / `restore` / `migrate` 均可离线运行；`scan_registry`（登记表）、`extract_segments`（段级提取）、`aggregate`（自动裁决含 LLM 分块）与 `generate_prompts`（描述/基础/衍生）需要 OpenAI 兼容 API。未配置 API 时 `status` 会显示 `NEEDS_CONFIG`，生成步骤无法执行——这不是故障。
- **状态语义**：`status --json` 的 `DELIVERED` 表示「交付清单与当前产物指纹一致**且启动证据可核验**」——它是**可核验的文件状态，不是模型内容品质承诺**；真实生成内容质量需另行人工验收（见 `docs/验收/`）。
- **签发门（v2.14.0）**：`verify` 在 `.start-state.json` 缺失、原始输入不可读、或 `--style` 与启动状态画风不是同一张卡时**非零退出并拒绝生成交付清单**（原因码 `SOURCE_UNVERIFIABLE` / `STYLE_UNVERIFIABLE` / `STYLE_MISMATCH` 写入 `.process\验证报告.md`）。修复后重跑同一命令即可。

Windows PowerShell（路径含空格时必须引号）：

```powershell
cd "D:\路径\资产提取技能包"
python scripts\maintenance.py init --lib "D:\资产库" --input-source "C:\素材\小说.txt" --genre 古风 --style 仙侠奇幻风
python scripts\split_segments.py "C:\素材\小说.txt" --novel 小说名 --author 作者 --lib "D:\资产库"
python scripts\scan_registry.py "D:\资产库\原文段\小说名-作者" --lib "D:\资产库"
python scripts\extract_segments.py "D:\资产库\原文段\小说名-作者" --lib "D:\资产库"
python scripts\aggregate.py "D:\资产库\段产物\小说名-作者" "D:\资产库\原文段\小说名-作者" --lib "D:\资产库"
python scripts\generate_prompts.py --enrich "D:\资产库\反推_最终.md" --genre 古风 --lib "D:\资产库" --style 仙侠奇幻风 --yes
python scripts\generate_prompts.py --base "D:\资产库\反推_最终.md" --genre 古风 --lib "D:\资产库" --style 仙侠奇幻风 --yes
python scripts\generate_prompts.py --init-schedule "D:\资产库\反推_最终.md" --genre 古风 --lib "D:\资产库"
#      ⚠️ 生成后先人工核对 调度清单.md 的「母版ID」列已填、变体不越界，再跑衍生
python scripts\generate_prompts.py --deriv --genre 古风 --lib "D:\资产库" --style 仙侠奇幻风 --yes
python scripts\maintenance.py verify --lib "D:\资产库" --style 仙侠奇幻风
python scripts\maintenance.py status --lib "D:\资产库" --json
```

macOS / Linux（bash；命令相同、路径分隔符不同）：

```bash
cd "/path/to/资产提取技能包"
python scripts/maintenance.py init --lib "/资产库" --input-source "/素材/小说.txt" --genre 古风 --style 仙侠奇幻风
python scripts/split_segments.py "/素材/小说.txt" --novel 小说名 --author 作者 --lib "/资产库"
python scripts/scan_registry.py "/资产库/原文段/小说名-作者" --lib "/资产库"
python scripts/extract_segments.py "/资产库/原文段/小说名-作者" --lib "/资产库"
python scripts/aggregate.py "/资产库/段产物/小说名-作者" "/资产库/原文段/小说名-作者" --lib "/资产库"
python scripts/generate_prompts.py --enrich "/资产库/反推_最终.md" --genre 古风 --lib "/资产库" --style 仙侠奇幻风 --yes
python scripts/generate_prompts.py --base "/资产库/反推_最终.md" --genre 古风 --lib "/资产库" --style 仙侠奇幻风 --yes
python scripts/generate_prompts.py --init-schedule "/资产库/反推_最终.md" --genre 古风 --lib "/资产库"
#      ⚠️ 生成后先人工核对 调度清单.md 的「母版ID」列已填、变体不越界，再跑衍生
python scripts/generate_prompts.py --deriv --genre 古风 --lib "/资产库" --style 仙侠奇幻风 --yes
python scripts/maintenance.py verify --lib "/资产库" --style 仙侠奇幻风
python scripts/maintenance.py status --lib "/资产库" --json
```

对编排器「一句话启动」的说明保留（见 SKILL.md 步骤 0），但它不是唯一入口——以上命令在任何宿主手动可执行。离线自检：`python 测试/run_all_checks.py`（含 fresh 真实签发与回退矩阵）。

排错见 `FAQ.md`；历史版本变更见 `docs/CHANGELOG.md`。

## 可选视觉生产（不阻塞资料交付）

视觉生成是可选下游模块。没有 Gemini 密钥时，核心反推流程照常完成并签发「资产资料已交付」，
视觉线单独记为未启用——两条线互不影响。

```powershell
# 体检（零收费）：权威包 / SDK / 配置 / 模型 / 参考图解析
python scripts\maintenance.py visual-doctor --lib <资产库> --json

# 规划任务（TOML 是唯一机器事实源）
python scripts\maintenance.py visual-plan --lib <资产库> --style 仙侠国风电影CG

# 自动生成（需 GEMINI_API_KEY）
python scripts\visual_production.py run --lib <资产库> --task <任务ID>
```

**视觉交付门退出码**：`0` 仅当已签发（DELIVERED）；`3` = `VISUAL_NOT_ENABLED`（尚未创建计划，
视觉未启用是合法状态但不等于交付通过）；`1` = `BLOCKED`（未完成，或权威包缺失/产物失效）。

**权威风格包安装**：参考图体积大且被 `.gitignore` 排除，因此 Git 检出不是完整视觉安装。

```powershell
python scripts\install_stylepack.py --from <私有包.zip> --style 仙侠国风电影CG --version 1.0.0
python scripts\install_stylepack.py --verify --style 仙侠国风电影CG --json
```

安装命令只接受本地 zip 路径，不会从网络下载。

**输入格式**：`.txt`、`.md`、`.docx`、`.epub` 与多文件项目（可用顺序清单指定次序）；
**PDF 不支持**（不做 OCR），以 PDF 为输入会以 `UNSUPPORTED_SOURCE_FORMAT` 拒绝。
DOCX 需 `python-docx`，EPUB 需 `ebooklib` + `beautifulsoup4`，见 `requirements.txt`。
