# 资产提取v2.17.0-rc.6（小说/剧本 → 国漫三维资产表 + 提示词 + 服饰设计）

> 作者：柠檬才不萌 · 联系：ningmeng2233@qq.com · 版本 2.17.0-rc.5（工程候选版）

本技能的首要职责是**从小说、剧本及其他叙事文本中反推生产资产**：人物、场景、道具、服装四类，以及原文证据、稳定编号、关系、连续性、优先级和冲突裁决。视觉提示词生产是可选的下游模块——**没有生图能力、没有 API 密钥、没有任何图片时，资产反推主流程仍然完整可用，并照常签发「资产资料已交付」**（双交付状态线，ADR-0085）。

## 支持的输入格式

| 格式 | 说明 |
|---|---|
| `.txt` / `.md` / `.markdown` | 保留原顺序与章节标题；单文件走直读路径 |
| `.docx` | 读取正文段落、标题层级与表格文本；忽略页眉页脚装饰 |
| `.epub` | 按 spine 顺序读取章节；剔除导航页与样式脚本 |
| 多文件项目 | 用 `--order-file` 指定顺序清单（每行一个相对路径）；未提供时自然排序并记录排序来源 |
| `.pdf` | **明确拒绝**（原因码 `UNSUPPORTED_SOURCE_FORMAT`）。请先转成 TXT / Markdown / DOCX / EPUB——本技能不支持 PDF，也不建设文字识别 |

所有格式统一规范化为带来源定位的 UTF-8 Markdown，产物写在 `<资产库>/.process/normalized_sources/`，原文件只读。每个段落都能回查原文件、文件哈希、顺序、卷/章/集/场、段落序号与原文摘句。

```powershell
# 单文件（TXT/MD）与既有流程完全一致
python scripts\maintenance.py init --lib <资产库> --input-source "剧本.docx" --genre 玄幻 --style 仙侠国风电影CG
python scripts\split_segments.py "小说.txt" --novel 小说名 --author 作者 --lib <资产库>

# 多文件项目：显式顺序清单
python scripts\maintenance.py init --lib <资产库> --source "卷1.txt" --source "卷2.txt" `
    --source-order-file "顺序清单.txt" --genre 玄幻 --style 仙侠国风电影CG

# 只做规范化（不拆段），用于核对来源定位
python scripts\normalize_sources.py "卷1.docx" "卷2.epub" --lib <资产库> --order-file "顺序清单.txt"
```

## 可选扩展：视觉生产

视觉生产**不接管**原文事实判断、资产分类、稳定编号、关系、连续性和迁移。它只在用户显式开启后进入：

任务规划 → 参考图路由 → 生成 → 质检 → 定向返工 → 视觉签发。

开启方式是人话的第 0 步「这次是否直接生成图片」，对应三种内部模式与一条非交互命令：

```powershell
python scripts\maintenance.py configure-visual --lib <资产库> --mode data-only        # 只反推（默认）
python scripts\maintenance.py configure-visual --lib <资产库> --mode prompt-only      # 只要提示词任务包
python scripts\maintenance.py configure-visual --lib <资产库> --mode direct-generate  # 直接生成
python scripts\maintenance.py configure-visual --lib <资产库> --show                  # 查看当前设置
```

设置存放在 `.process\项目设置.toml`，继续同一项目时自动沿用，只有用户主动修改时才重问。`data-only` 下不创建视觉计划；
`prompt-only` 下 `run` 拒绝自动生成（零 API 调用）而 `export` 照常可用。选择「不生成」时核心反推与资料交付完全不受影响。

批量生产与审核（v2.16.0 起，替代逐个 `--task`）：

```powershell
python scripts/visual_production.py status     --lib <资产库> --json   # 状态（人话 / 机器可读）
python scripts/visual_production.py run-ready  --lib <资产库> [--parallel 1-4]  # 按依赖批量生成
python scripts/visual_production.py resume     --lib <资产库>          # 崩溃后恢复，不重复生成
python scripts/visual_production.py pause      --lib <资产库> [--clear]# 暂停后续派发
python scripts/visual_production.py review-export --lib <库> --task <任务ID>    # 导出审核请求
python scripts/visual_production.py review-import --lib <库> --task <任务ID> --result <审核结果.json>
python scripts/visual_production.py publish    --lib <库> --task <任务ID>      # 发布正式版本
python scripts\check_visual_delivery.py --lib <资产库>                          # 视觉交付门
```

- **生成与终审分离**：Gemini（含 Codex 转接）只生成，**不终审**；终审由当前主模型读图后回写结构化结果。
- **硬门**：手部、比例、身份、画风、裁切、站位、视角、视线、轴线、遮挡、道具数量、场景结构、文字水印、参考链
  存在任一即不得「通过」，也不得「通过但有备注」。
- **并发与恢复**：并发 1–4，连续限流自动降级（4→3→2→1）后稳定恢复；传输失败有上限退避重试且**不消耗**画面轮次；
  进程中断后 `resume` 从事件日志恢复、不重复请求已生成的图片。
- **可选双审核子代理**（默认关闭）：美术代理只提供画风/身份/服装/场景/连续性证据，技术代理只提供手部/比例/
  接触/构图/道具数/伪影证据；两者都没有签发权，主模型重新看图后终裁。

旧库非破坏迁移（第一集 82 张旧图）：

```powershell
python scripts\migrate_visual_library.py scan   --lib <资产库> --out 迁移提案.json   # 只读，不写库内文件
python scripts\migrate_visual_library.py apply  --lib <资产库> --proposal 迁移提案.json
python scripts\migrate_visual_library.py verify --lib <资产库> --baseline 迁移前清单.json
```

只对**唯一匹配**的候选做复制登记（原文件零改动、不自动通过）；歧义候选留给人或主模型决定。

对应的第二条交付状态线独立于资料交付：未启用时显示「视觉生产：未启用」（合法状态，不是失败），部分完成时显示已通过 / 需修改 / 待生成数量。`status --json` 同时给出 `data_delivery` 与 `visual_delivery` 两条线（后者带当前 `mode`）。

四类顶层资产保持四类，细化通过**中文子类型**（如 人物·非人角色、道具·载具、场景·建筑、服装·甲胄）与**视觉需求**表达；法术、天气等一律作为挂在资产上的视觉需求，**不新增第五类资产**。

## 版本历史摘要

v2.17.0-rc.2（工程候选）：**真实链路签发收口 · 单一真相源收口 · 代码瘦身 · Python 3.10 可用性修复**——①18 项缺陷收口（F15–F32）：配置来源、规则常量、跳过清单文法、豁免判据、占位写法、衍生通道类型、资产类型权威来源各收成一份，根治「同一件事多份口径」（服装无衍生通道却被检查器索要，实测就是停链点）。②新库在真实端点跑完整链路并 `DELIVERED` 签发（12 项产物哈希 + 9 项机械检查全绿 + 覆盖率 97.1%）。③代码瘦身：删 28 个零引用函数/属性、12 处手搓路径转换改用标准库 `as_posix()`、生成端与检查器各一份的重复解析器收进 `scripts/engine/prompt_rules.py`，净 −215 行、不新增依赖。④**修复 Python 3.10 起不来（两层）**：`tomli` 被 8 个模块隐式依赖却从未声明，3.10 上连 `maintenance.py status` 都直接崩；补条件依赖后真 3.10.21 又暴露测试码另有 5 处裸写 `import tomllib` 没有回退——"支持 3.10"此前从未成立。现测试侧统一走 `engine.common.toml_module()`，并加守卫禁止"引用 tomllib 却不处理 tomli"。实测修复前 `status` 退出码 1、修复后 `status`/`verify` 均 0 并正常签发。⑤**发布物完整性修复**：打包白名单曾漏掉仓库根的 `run_pipeline.sh/.bat` 与 `llm_nodes.json.example`——一键入口和节点池样例**根本不随包发布**，而发布门跑在仓库里所以无人发现；现已补齐并加守卫（根级 `.sh`/`.bat`/`.example` 未登记即红）。⑥**文档路由补齐**：`run_pipeline`、`llm_nodes.json`、`migrate_asset_ledger.py`、`references/风格蒸馏流程.md` 此前在全仓任何 .md 中出现 0 次，能力已交付却对用户不可见；现 QUICKSTART 新增「一键跑完全流程」「多端点与按角色分档」「旧库总账迁移」三节，SKILL.md 加对应路由。**未执行（NOT_RUN）**：十项人工画质审查表未留档、Linux 实跑未做。真实生图链路已由维护者人工验收通过（口述，非机器留档）。 v2.17.0-rc.1（工程候选）：**证据驱动资产编译器与 Omni 质量闭环**——①**源事实库 → 类型化资产总账 → 确定性提示词编译**：`源事实.jsonl` 带出处、`.process/asset-ledger.json` 成为机器层唯一库存源（五类记录 + 生产模式 + 必需产物，旧四表由它渲染）、提示词按九分区 IR 固定顺序编译且相同输入逐字节一致（模型只补审美候选与歧义裁决，格式化/合并/负面词全部由代码完成，规则优先级 `用户明确要求 > Omni标准 > 原文事实 > 项目美术总则 > 唯一主风格包 > 类型模板 > 必要补全`）。②**依赖图与内容寻址缓存**：`.process/artifact-graph.json` 把「源事实→总账项→提示词→图片任务→审核结果」建成有向图，src 变化即下游失效；缓存按输入指纹复用，指纹相同而载荷不同时报「指纹碰撞」而不是静默沿用旧内容；索引数据库引入节点指纹，避免无关任务重跑。③**三队列与自适应调度**：提取/生成/审核三队列各自并发上限、请求预算（超限切分而不截断事实）、失败分类（可重试/切换端点/需人工/未知）。④**CLI 与签发门**：新增 `build-ledger` / `compile-prompts`（默认 shadow，`--apply` 才并入正式且先备份）/ `run-visual`（只调单批数量，视图规格不可调）；`status --json` 给出 `pipeline_state` / `data_delivery` / `visual_delivery`（含 `stage`：已规划→生成中→待审核→要返工→已签发）/ `compiler_stages`（事实库→总账→提示词→图片计划→生成→审核→返工→签发）；`verify` 的机械检查由 3 项扩为 **9 项**（数量守恒、美术总则指纹、依赖图完整、必需提示词、视觉审核、包内文件），最新失败立即撤销实时交付，历史交付清单只作快照留档。⑤**旧库迁移**：总账迁移 `scan|apply|rollback|shadow` 非破坏迁移、可回滚、可影子预演。**未执行（NOT_RUN）**：真实图片内容审核与真实端点抽样需能看图的主模型与付费调用。**旧版本签发的交付清单需重跑一次 `verify` 重新签发**（旧签发未核验新增的六项）。 v2.16.0-rc.1（工程候选）：**工业化视觉管线**——批量入口 `run-ready` 与崩溃恢复 `resume`、单写者调度器（内核级计划锁 + 事件日志写前日志 + revision）、自适应并发与有上限退避重试（传输失败不消耗画面轮次）、暂停 `pause`；主模型审核往返（`review-export`/`review-import`/`publish`）与六项哈希绑定；**16 个硬门码 + 四种判定**，存在硬门即不得「通过」或「通过但有备注」；可选双审核子代理（默认关闭、无签发权）；导演构图卡与场景站位图进提示词编译；旧库非破坏迁移预演器（只读扫描 + 唯一匹配复制 + 迁移前清单校验）；`SKILL.md` 主入口瘦身到约 11KB 并按需加载 `references/` 契约。真实图片内容审核与第一集迁移 `apply` **未执行（NOT_RUN）**：本版为工程候选。 v2.15.0-rc.2：**终验修复**——参考图路由真正接入 `run`/`export`（导出正脸任务不再出现「本任务无参考图」，参考图逐文件校验存在性/哈希/MIME，缺图即拒绝而非发空图）；权威 Omni 包成为新任务唯一风格源（旧 H7 不再参与编译，全身任务不再继承正脸裁切）；Gemini 只发送已解析参考的真实字节与真实 MIME，质检模型改为 `gemini-3.1-pro-preview`，视觉 CLI 遵循统一 `.env` 链，新增 `maintenance.py visual-doctor` 零收费体检；手工任务包自包含（包内 `参考图/` + `任务包清单.json`，可跨机转交）；`verify-visual` 只有 DELIVERED 退出 0（`NOT_ENABLED` 退出 3）；新增 `install_stylepack.py` 与发布前置逐文件校验。真实 API 与人工画质审查未执行（NOT_RUN）。 v2.15.0-rc.1：核心反推与可选视觉解耦（双交付状态线）、DOCX/EPUB 与多文件项目正式输入、PDF 明确拒绝、四类资产中文子类型与视觉需求契约、剧本场次识别（详见 `docs/adr/0085`）。v2.15.0 视觉扩展：风格包版本化与内容哈希固定、视觉计划 TOML 唯一事实源、参考图按职责最小路由、Gemini/手工双执行器、结构化质检与三轮定向返工、拼版只由已通过图片本地合成、视觉交付独立签发（详见 `docs/adr/0086`）。v2.7.0：裁决过程文件归档 .process（根目录不再堆积）、人物/场景/道具三类型各一个合并文档（描述/基础/衍生三层均 3 个文件、按序号分节）、裁决自动静默执行、所有提示词全程纯中文（英文标签块仅内部索引不注入）。核心防幻觉体系：出处强制 + 全局登记表 + [新增]严格申报闭环。v2.14.0-rc.4：跨版本验收器判定收紧（三版本全 PASS 才退 0，SKIP/FAIL 分列且一律非零）与抽样手册运行隔离（唯一运行目录、不递归删除固定路径、十项人审机械留空）。v2.14.0-rc.2：签发门（`verify` 缺状态文件/缺原始源/画风冲突一律非零拒签且不覆盖历史清单）、手动初始化入口（`maintenance.py init`）与三类误签发负例长期守卫。v2.13.0：跨平台流水线状态机（`status --json` 与交付清单）、四级事实等级与规范化资产索引（`.process/资产索引.json`）、显式资产关系（穿戴/出场/定位/连通；变体归属现以 variant_needs 表达，四类资产暂无独立变体节点、不产生 STATE_OF 有效边，契约详见 docs/adr/0084）、场景空间母版（`连续性/场景母版.md`，未知填“未说明”）、老库一键迁移（`migrate` 先备份再生成索引与母版）。

## 快速开始

```powershell
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API(复制 .env.example 填写;推荐放技能包外)
Copy-Item .env.example "$env:USERPROFILE\.asset-extract\.env"
# 编辑该文件:OPENAI_API_KEY / SUMMARY_API_URL / SUMMARY_API_MODEL

# 3. 在 Cherry Studio 中对该技能说「提取资产」开始
#    流程:确认输入 → 拆段 → 登记表 → 段级提取 → 聚合裁决 → 描述/提示词 → 完整性检查 → 报告
```

完整操作手册见 `SKILL.md`(编排器执行规范);术语表见 `CONTEXT.md`;架构决策见 `docs/adr/`(86 份)。

## 文档地图

| 文件 | 用途 |
|---|---|
| `SKILL.md` | 主文档:8 步流程 + 断点定位 + 错误矩阵 + 新增/组装画风 |
| `CONTEXT.md` | 术语表(直调引擎/出处强制/登记表/画风卡片/风格组装…) |
| `docs/adr/0001-0086` | 架构决策(直调引擎/分块裁决/登记表/画风库/风格扩展/服饰设计融合/纯中文/合并文档/裁决静默/过程归档/三类型文件/输入哈希断点/混合题材桥接/认证快速失败/画风简写匹配/状态机与关系网络等) |
| `references/画风库.md` | 风格卡片(A-H 系,新增走 H 系;加卡后跑 `check_style_lib.py` 确认) |
| `references/styles/` | 风格登记表 + 版本化风格包（`registry.toml` / `<版本>/manifest.toml` / `share-policy.toml`） |
| `references/video_styles/` | 视频风格配置（静态解析器不读取；规格 2.10） |
| `references/题材-画风映射.md` | 题材 → 默认画风(外部可编辑) |
| `references/画风卡片模板.md` | 新增风格照此填写 |
| `references/风格组装参考.md` | 混合风格组装(内容×渲染×色调组件表) |
| `references/题材-剧种映射.md` | 题材 → 服饰剧种换算(服饰设计衔接) |
| `服饰设计/` | 服饰设计模块(短剧服装设计,独立完整:8 剧种白/黑名单 + 校验脚本,提取后自动衔接) |
| `templates/` | prompt 模板单源真相(唯一修改点)；`视觉任务/`、`视觉质检/`、`视觉计划模板.toml` 为可选视觉模块 |
| `scripts/` | 直调引擎 + 视觉生产（`visual_production.py` / `check_visual_delivery.py` / `manage_styles.py`） |
| `测试/` | 离线回归(run_split/run_style/run_verdict/run_e2e/run_source_formats/run_visual_offline)+ e2e 快照 |

## 常用命令

```powershell
python scripts\split_segments.py "源.txt" --novel 小说名 --author 作者 --lib 资产库 [--overlap 1]
python scripts\scan_registry.py "资产库\原文段\小说-作者" --lib 资产库
python scripts\extract_segments.py "资产库\原文段\小说-作者" --lib 资产库
python scripts\aggregate.py "资产库\段产物\小说-作者" "资产库\原文段\小说-作者" --lib 资产库
#    默认精简输出(裁决自动静默执行);调试过程明细加 --verbose
python scripts\generate_prompts.py --enrich / --base / --init-schedule / --deriv --genre 题材 --lib 资产库 [--style 画风] [--view-mode full|crop]
python scripts\check_schedule.py "资产库\反推_最终.md" --lib 资产库 --style 画风 [--style-lib]
python scripts\check_style_lib.py        # 画风库健康自检(新增风格后必跑)
python scripts\check_final.py "资产库\反推_最终.md"  # 反推 12 模块校验
python scripts\check_asset_index.py --lib 资产库  # 资产索引/母版校验（ID/关系/事实等级/漂移/P3 说明）
python scripts\prepare_costume_input.py "资产库\反推_最终.md" --genre 题材 --lib 资产库  # 服饰设计衔接(角色清单+剧种)
python 服饰设计\scripts\check_costumes.py <服饰标准库> <角色服饰标准表>  # 服饰校验(阻断/警告分级)
```

## 状态机与交付（v2.14.0）

```powershell
python scripts\maintenance.py init --lib 资产库 --input-source 素材\小说.txt --genre 古风 --style 仙侠奇幻风  # 手动初始化：原子写 .start-state.json
python scripts\maintenance.py status --lib 资产库 --json  # 流水线状态机（WAITING_INPUT→…→DELIVERED/BLOCKED/NEEDS_REVIEW）
python scripts\maintenance.py verify --lib 资产库 --style 画风  # 全绿签发 交付清单.json，失败写 .process/验证报告.md
python scripts\maintenance.py migrate --lib 资产库  # 老库迁移：先备份再生成资产索引与场景母版（旧表不动）
```

macOS/Linux 用同一命令，仅把路径分隔符换为 `/`（如 `python scripts/maintenance.py status --lib 资产库 --json`）。

**签发门（v2.14.0）**：`verify` 在启动证据不可核验时非零退出且**拒绝生成交付清单**——`.start-state.json` 缺失、其记录的原始输入不存在，或 `--style` 与启动状态画风不是同一张画风卡（`A2` 与「仙侠奇幻风」属等价别名，不判冲突）。原因码 `SOURCE_UNVERIFIABLE` / `STYLE_UNVERIFIABLE` / `STYLE_MISMATCH` 写入 `.process/验证报告.md`。原始输入须留在原位：源被移动或改写会让已签发的交付清单立即失效。

## 测试

```powershell
python -m pytest                  # 单元测试（解析/调度/画风/服饰/状态机/索引/母版/版本一致性等；用例数以运行输出为准）
python 测试\run_all_checks.py    # 本地质量门:flake8+pytest+离线回归一键跑
python 测试\run_split_test.py    # 拆段层回归(千/万章、番外、冲突、GBK)
python 测试\run_style_test.py    # 画风库解析回归
python 测试\run_verdict_test.py  # 聚合裁决离线链路
python 测试\run_e2e_check.py     # e2e 快照自动复验(全量)
python 测试\e2e_flow.py          # 全流程触发测试(mock API，含服饰设计与真实 verify 签发→status DELIVERED)
python 测试\e2e_flow.py --snapshot  # 重建 测试/e2e 静态快照(CI 同款)
python 测试\run_linux_acceptance.py --zip <候选包zip> --expected-sha256 <hash> --out <目录> --python-list "python3.10 python3.11 python3.12"
                                 # 候选包异目录验收实跑器：核对 SHA-256 → 逐解释器全新解压 → 快照重建+总门+compileall，
                                 # 退出码写 results.tsv、日志落 logs/；解释器缺失或缺依赖记 SKIP，不冒充通过
```

项目已初始化 Git 仓库（以下为维护源仓库工程约定，不随包；收件人按自有仓库配置）:
- `pre-commit` 钩子:提交前自动运行 `python 测试/run_all_checks.py`,未通过则阻断提交。
- `.github/workflows/quality.yml`:push/PR 到 `master` 时自动运行 flake8、pytest，并先 `e2e_flow.py --snapshot` 重建快照再跑离线回归(矩阵 Python 3.10/3.11/3.12)。

项目使用 `pyproject.toml` 管理元数据与工具配置(`pytest`/`flake8`);`requirements.txt` 保留为应用依赖清单。

## 安全与分发

- 真实 `.env`(API 密钥)存放于技能包外 `~/.asset-extract/.env`;技能包内不带密钥(`.gitignore` 已排除)
- 分发安装时可整体删除 `测试/e2e/`(运行快照),保留 `run_*.py`、`e2e_flow.py`、`触发顺序.md` 与 `样例/`
- 一个 `ASSET_LIB_DIR` 只对应一个小说/剧本项目；多项目请使用不同资产库目录，避免根目录固定产物覆盖
- 资产库(`ASSET_LIB_DIR`)为用户数据,默认技能目录 `资产库/`,可外置
