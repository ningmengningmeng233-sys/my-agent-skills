/**
 * packages/adapters/dsh — DSH 原生 MCP Client 适配器（A08）：cli 单变体，profile 显式选择。
 *
 * 核心语义（工单 A08 + A 阶段全局约束；官方文档 2026-10-03 核实，见
 * .lemon/evidence/A08/research.md）：
 *
 * 官方三源逐字冻结：
 *   - plugin 键（mcp-client README 逐字）：每 server 一条 Cordis patch row，
 *     `{id, name:'@deepseek-ai/dsh-mcp-client', config:{serverName, transport,
 *     command/args/env/cwd}}`；UMH 的 owned row = `id=memory-umh`
 *    （与官方三记忆示例 `memory-memorix`/`memory-mcp-reference`/`memory-engram`
 *     错开）+ `config.serverName=umh`（`[A-Za-z0-9_-]{1,32}`，作用域内唯一）+
 *     stdio launch（command/args/env，env 恒空）。entry 形态照官方示例抄，
 *     **绝不凭 npm 包名猜 YAML key**。
 *   - patch 文件与 profile（CLI README §Profiles + reference README + mcp-memory
 *     指南逐字）：自动写入面 = 显式选中的 profile 补丁
 *     `<DSH_HOME>/profiles/<profile>/cordis.patch.yml`；DSH_HOME 缺省 `~/.dsh`
 *     （reference README 失败诊断 `default ~/.dsh/logs/` 逐字），非空 DSH_HOME
 *     环境变量迁址。家级 `$DSH_HOME/cordis.patch.yml`（全机共享层）永不自动写
 *     （命中即手工片段）。多 profile 未选 → PROFILE_REQUIRED（A03/A07 同型先例，
 *     绝不隐式选第一项）；显式选择 = detect(opts.configPath) 独占（preview 做
 *     basename/`profiles/<profile>/` 形态守卫）。
 *   - 工具命名（mcp-client README 逐字）：`mcp__<serverName>__<rawName>`；
 *     UMH 的 serverName=umh → A09 记录真实 qualified 工具名与 native DSH 调用。
 *
 * YAML 编辑策略（本单最难点，工单明示"成熟 YAML parser 保留 comments"）：
 * `yaml@2.9.1`（C01 固定版）的文档模型满足自动编辑门：
 *   1. 文本级行追加：安装 = 在**文档末尾**追加一个 `- insert:` op（含 owned 单
 *      row）；已有全部字节逐字保留 —— 测试断言 after.startsWith(before)；
 *   2. 每次编辑后 `YAML.parse` 校验语法有效；
 *   3. 语义 diff：parse(before) 去掉 owned row 与 parse(after) 去掉 owned row
 *      deepEqual（无关 plugin/注释的解析等价性），owned row 值 deepEqual 预期。
 * 任何一步不满足 → fail-closed（绝不发布）。owned 冲突（含第三方同名
 * serverName、异 id 同 serverName）→ OWNED_POINTER_OCCUPIED 拒绝；文件非严格
 * YAML → 手工片段（changes 恒空）。
 *
 * 严格 YAML 门（preview 只读判定，不写盘）：
 *   - 空文件 / 仅注释 / 非 list 顶层 / 解析错误（doc.errors 非空）→ 手工；
 *   - 顶层每 op 须为含唯一键 `insert` 的 map 且其值为 row 的 seq → 否则手工；
 *   - 每 row 须为 map 且 `id` 为非空 string → 否则手工；
 *   - owned row 若在场则其 `config` 须为 map 且 serverName/command/args/env
 *     四键全为标量可比形态（含 `!!js` 的行：无关 row 原样保留；owned row 含
 *     `!!js` 即视为占用 → OWNED_POINTER_OCCUPIED，绝不求值）；
 *   - 家级共享层路径（basename `cordis.patch.yml` 但不在 `profiles/<profile>/`
 *     下）→ 手工。
 * `remove` 侧同门复核（缺省坏文件 → conflict 不触碰，绝不整文件还原备份）。
 *
 * 序列化：自写 owned op 序列化器 —— 双引号基本字符串（`\\` `"` 与控制字符按
 * YAML 双引号规则转义，中文/空格原样）、args 流式 seq、env `{}` 空 map 行内。
 * owned op 内零注释（注释归属无法区分 → 新 op 不带注释是归属纪律）。
 * `cwd` 不写（gateway 参数全为绝对路径，不引入 `!!js` 求值面）。
 *
 * apply = parseConfigPlan fail-closed → 拒其它端计划 → 全新安装目标预创建缺失
 * 父目录（profile 目录可能不存在；A05/A06/A07 先例）→ applyConfigPlan（A01
 * 事务：锁内 beforeHash → 备份 → 原子发布 → published 语义）→ saveReceipt。
 * 受信绑定文件绝不由 adapter 写入（对齐 A02-A07 先例）。
 *
 * remove（YAML 版三方比对，adapter 端实现 —— A01 removeOwnedConfig 对非 JSON
 * 格式诚实拒绝 MANUAL_REMOVAL_REQUIRED）：值级 before/after/current 三方比对 +
 * 卸载前 owned row 文本与 canonical 序列化逐字比对守卫（行尾注释/行内注释/
 * 增删键/重排格式等"解析值不变但文本有别"的用户内容一律 CONFIG_CHANGED 不触
 * 碰 —— A05 修复轮 Important-1 同型）；切除动作复用 A01 applyConfigPlan（临时
 * 反转计划：锁/beforeHash 复核/备份/原子发布全部继承，计划本身即刻丢弃、不落
 * 回执）；UMH 新建且逐字节未动的文件 → 整文件移除（A01 语义，先留适配器本地备
 * 份）。手工回执（changes=[]）→ removed:false + 清理指引（A03 修复先例）。
 *
 * validate 三项诚实：connection/policy=UNVERIFIED（真实客户端证据归 A09）、
 * sharedReadWrite=NOT_RUN（agent 未实际调用工具前不得判 PASS/FAIL）。
 *
 * 规则注入（policy-block 端内接线，A01 Minor#8）：记忆策略只进 owned 的
 * 「UMH MEMORY POLICY v1」标记块，落点项目 AGENTS.md（官方记忆指南只给
 * additive 指导语，未约定指令文件 —— 按本工作区既有文件；markdown 非
 * ConfigChange 支持格式，用户手工粘贴）。existing 内容不替换（mergePolicyBlock
 * 幂等合同）。
 *
 * provider/凭据/记忆保护面：`$DSH_HOME/.credentials.yaml` 与 sessions 永不出现
 * 在检出面（结构性零触碰 + 专测钉死），零读零写；原生记忆不停用。
 */
import type { ClientAdapter, ConfigPlan, Detection, LaunchSpec } from '../contracts.js';
/**
 * DSH 的 owned 段：profile 补丁 `<DSH_HOME>/profiles/<profile>/cordis.patch.yml`
 * 的 `- insert:` op 下 `id=memory-umh` 的 row（plugin 键
 * `@deepseek-ai/dsh-mcp-client`，`config.serverName=umh`，stdio launch）。
 * pointer 写法沿用 A01 JSON-pointer 风格（remove/计划内定位用）。
 */
export declare const DSH_OWNED_POINTER = "/insert/memory-umh";
/** owned row 的冻结 id（与官方三记忆示例 id 错开，不冲突）。 */
export declare const DSH_OWNED_ID = "memory-umh";
/** 冻结的 plugin 键（mcp-client README 逐字，绝不凭 npm 包名猜）。 */
export declare const DSH_PLUGIN_NAME = "@deepseek-ai/dsh-mcp-client";
/** 冻结的 serverName（官方 `[A-Za-z0-9_-]{1,32}` 规则内，作用域内唯一）。 */
export declare const DSH_SERVER_NAME = "umh";
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点项目 AGENTS.md）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export declare const DSH_POLICY_INSTRUCTIONS: readonly string[];
/**
 * DSH 配方（= docs/clients/recipes/dsh.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[manual]=手工路径（未选 profile/家级共享层/非严格
 * YAML）、[auto]=自动路径附加指引。占位符
 * {{command}}/{{args}}/{{env}}/{{policy}} 由 preview 用 plan.launch 与模块策略插值。
 * 步骤按 2026-10-03 官方文档核实措辞（profile 补丁层/合并纪律/serverName 规则）。
 */
export declare const DSH_RECIPE: {
    readonly variant: "cli";
    readonly version: "0.0.1-rc.1";
    readonly officialSource: "https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md; profiles: https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md; memory overlays: https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/mcp-memory.md";
    readonly verifiedAt: "2026-10-03";
    readonly configLocation: "cli: <DSH_HOME>/profiles/<profile>/cordis.patch.yml where <DSH_HOME> resolves as non-empty DSH_HOME, else ~/.dsh (official: apps/cli README 'A profile directory holds a package.json and a cordis.patch.yml'; reference README 'boots the profile at $DSH_HOME/profiles/<name>' and 'default ~/.dsh/logs/'). The machine-wide $DSH_HOME/cordis.patch.yml shared layer is never an auto-edit target; provider credentials in $DSH_HOME/.credentials.yaml are never detection targets and are never read or written";
    readonly format: "yaml";
    readonly ownedPointer: "/insert/memory-umh — one '- insert:' patch row with id 'memory-umh' and name '@deepseek-ai/dsh-mcp-client' (the frozen plugin key, never guessed from an npm package name), whose config carries serverName 'umh' ([A-Za-z0-9_-]{1,32}, unique inside one registration scope) with a stdio transport ({command,args,env}; env stays {}). UMH appends only this row via text-level append + parse validation (never whole-file stringify; unrelated plugins, comments and !!js lines are preserved byte-for-byte)";
    readonly policyLocation: "project AGENTS.md (the official memory guide ships only an additive model-instruction sentence and documents no instruction-file convention — verify against your own setup) — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers";
    readonly manualSteps: readonly ["[manual] Choose the profile explicitly first: the per-profile patch <DSH_HOME>/profiles/<profile>/cordis.patch.yml (shipped names include web/headless/sdk/sdk-minimal/acp; custom names are user-created). When several profiles hold a patch file, UMH refuses to guess and reports PROFILE_REQUIRED — re-run detection with the explicit cordis.patch.yml path of the profile you want to connect. The machine-wide $DSH_HOME/cordis.patch.yml shared layer is never auto-edited.", "[manual] UMH did not auto-edit a DSH patch file (no safely auto-editable profile cordis.patch.yml was found — it may live under a custom DSH_HOME, use an unsupported shape, or not be parseable YAML, and UMH will not risk losing your plugins). Hand-merge one '- insert:' row as described next; UMH never edits $DSH_HOME/cordis.patch.yml (machine-shared layer) or $DSH_HOME/.credentials.yaml (provider credentials).", "[manual] The row to add is exactly: id = memory-umh; name = @deepseek-ai/dsh-mcp-client; config.serverName = umh; config.transport = stdio; command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config. After discovery the tools appear as mcp__umh__<tool> (for example mcp__umh__memory_status).", "[manual] Inject the memory policy into the project AGENTS.md: paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[manual] Reload the profile (restart DSH or enable HMR) and confirm the umh tools are listed; then ask the agent to run memory_status. Do not report onboarding success until the agent actually calls the tool.", "[auto] UMH appended the memory-umh row to the explicitly selected <DSH_HOME>/profiles/<profile>/cordis.patch.yml (a pre-edit backup and a rollback receipt were recorded under the UMH home). Every other plugin row, comments and expression lines were preserved byte-for-byte; UMH never reads sessions or credentials.", "[auto] Reload the profile (restart DSH or enable HMR) and confirm the umh tools are listed as mcp__umh__<tool>; then ask the agent to run memory_status. Do not report onboarding success until the agent actually calls the tool.", "[auto] Inject the memory policy into the project AGENTS.md: paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}"];
    readonly schemaValidated: true;
};
/**
 * 构造 DSH 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 dshAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export declare function makeDshAdapter(options?: {
    umhHome?: string;
}): ClientAdapter;
/** DSH 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeDshAdapter 绑定）。 */
export declare const dshAdapter: ClientAdapter;
export type { ClientAdapter, ConfigPlan, Detection, LaunchSpec };
