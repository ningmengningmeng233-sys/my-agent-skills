/**
 * packages/adapters/zcode — ZCode 适配器（A04）：desktop 主形态 / cli 单列形态。
 *
 * 核心语义（工单 A04 + A 阶段全局约束；官方文档 2026-10-03 核实，见
 * .lemon/evidence/A04/zcode-docs-research.md）：
 *
 * 官方文档以单一配置面描述 ZCode（"ZCode manages the MCP server configuration used
 * by ZCode Agent in one place"），四个配置文件对 desktop/CLI 不分列 → UMH 建模：
 *   - variant='desktop'（工单指定的主形态）与 variant='cli'（单列形态）**共享同一
 *     写入路径与语义**——同一官方文档化文件、同一 owned pointer；差异仅在 variant
 *     独立标识。未知变体响亮 INVALID_ARGUMENT（variant 决定写入路径，A03 先例）；
 *   - 唯一自动写入目标 = user 级原生 `~/.zcode/cli/config.json`（官方作用域
 *     "User (all workspaces)"，官方原话 "you can also edit these files by hand to
 *     manage servers in bulk" → 自动写入有文档依据）。owned pointer =
 *     `/mcp/servers/umh-memory`（嵌套结构 mcp → servers → 'umh-memory'，官方示例
 *     逐字核实；**不是**扁平的 mcpServers 键）；
 *   - workspace 级 `<project>/.zcode/config.json` 与 `.agents/mcp.json` 兼容文件
 *     绝不写入：workspace 是逐项目配置（官方安全注记：打开仓库即自动连接其 MCP
 *     服务器）；`.agents` 是回退面（官方：同作用域 `.zcode` 读到任何服务器时
 *     `.agents` "is skipped entirely — no merging"；"Panel edits always write to
 *     .zcode; .agents files are never modified"）→ UMH 同纪律，避免造出被静默
 *     忽略的条目；
 *   - preview 走 makeOwnedEntryPlan：严格 JSON（JSON.parse 成功的对象）才自动编辑，
 *     只添加冻结 pointer 的 umh 服务项（{command,args,env}，与 makeLaunchSpec 严格
 *     同源）；第三方 server、未知字段、用户的 enable 开关一律不动（OWNED_POINTER_
 *     OCCUPIED 守卫继承 A01 —— 用户先前在 UI 中禁用过 umh-memory（enable:false
 *     在场）时拒绝覆盖，绝不吞掉用户的禁用决定）；JSONC/解析失败 → 手工片段
 *     （changes 恒空），绝不破坏性 stringify；
 *   - 多候选（detection.configPaths.length>1，伪造/外来检出）→ PROFILE_REQUIRED
 *     （绝不隐式选第一项）；显式 detect(opts.configPath) 即独占选择；
 *   - apply = parseConfigPlan fail-closed → 拒其它端计划 → applyConfigPlan（A01
 *     事务：锁内 beforeHash → 备份 → 原子发布 → published 语义）→ saveReceipt。
 *     受信绑定文件绝不由 adapter 写入（对齐 A02/A03 先例：绑定只能由 connect CLI
 *     受信编排经 writeTrustedBindingFile 落盘 —— 项目绑定不可被客户端请求更改）；
 *   - 已有皮肤/CDP/Node 进程不是接入面：本适配器不修改、不停止任何运行中的 ZCode
 *     进程；不读历史聊天/原生记忆（detect 仅 stat + 严格 JSON 判读）。
 *
 * Review Focus（目标 Agent 是否真正启用工具）：ZCode 无 per-assistant 绑定（与
 * Cherry 的"连接不等于绑定"相反），启用门在**服务器级**——官方机制：禁用 = 该
 * 服务器的配置对象被写入 "enable": false，"without the field the server counts as
 * enabled"；添加流程官方收尾 = "confirm the server is enabled in the list"。
 * manualSteps 的 [manual]/[auto] 两节均覆盖该确认环节；会话加载依据官方
 * "connect automatically at session start"（热加载未文档化 → 要求开新会话）。
 *
 * 规则注入（policy-block 端内接线，A01 Minor#8）：记忆策略只进 owned 的
 * 「UMH MEMORY POLICY v1」标记块，落点为工作区指令文件（本工作区 AGENTS.md；官方
 * MCP 页未记录指令文件约定 → recipe.policyLocation 如实标注，用户手工粘贴，
 * markdown 非 ConfigChange 支持格式，自动写入无合同通道）。
 *
 * validate 三项诚实：connection/policy=UNVERIFIED（真实客户端证据归 A09）、
 * sharedReadWrite=NOT_RUN（agent 未实际调用工具前不得判 PASS/FAIL）。
 */
import type { ClientAdapter, ConfigPlan, Detection, LaunchSpec } from '../contracts.js';
/**
 * ZCode 的 owned 段：user 级原生 config.json → 嵌套 mcp.servers 命名空间下的
 * umh-memory（官方示例逐字核实：{"mcp":{"servers":{"<name>":{command,args,env}}}}）。
 */
export declare const ZCODE_OWNED_POINTER = "/mcp/servers/umh-memory";
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点工作区指令文件）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export declare const ZCODE_POLICY_INSTRUCTIONS: readonly string[];
/**
 * ZCode 配方（= docs/clients/recipes/zcode.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[manual]=手工路径（配置缺失/非严格 JSON）、[auto]=自动
 * 路径附加指引。两节共享于 desktop|cli 双形态（官方单一配置面）。占位符
 * {{command}}/{{args}}/{{env}}/{{policy}} 由 preview 用 plan.launch 与模块策略插值。
 * 步骤按 2026-10-03 官方文档核实措辞。
 */
export declare const ZCODE_RECIPE: {
    readonly variant: "desktop|cli";
    readonly version: "3.14.4";
    readonly officialSource: "https://www.zcode.network/en/docs/mcp-services/; cn: https://www.zcode.network/cn/docs/mcp-services/";
    readonly verifiedAt: "2026-10-03";
    readonly configLocation: "user-level native ~/.zcode/cli/config.json → mcp.servers (official scope 'User (all workspaces)'; hand-editing these files is officially supported 'to manage servers in bulk'; workspace scope <project>/.zcode/config.json is per-project and UMH never edits it; same-scope .agents/mcp.json compat files are fallback-only and are never written by UMH or the ZCode panel)";
    readonly format: "json";
    readonly ownedPointer: "/mcp/servers/umh-memory inside ~/.zcode/cli/config.json — note the nested native structure mcp → servers → 'umh-memory' (not the flat mcpServers key); auto-edit only for strict JSON, anything else gets a manual fragment instead";
    readonly policyLocation: "workspace agent-instruction file (AGENTS.md in this workspace; the official MCP docs do not document an instruction-file convention — verify against your own setup) — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers";
    readonly manualSteps: readonly ["[manual] UMH did not auto-edit the ZCode user-level native config (no safely auto-editable strict-JSON ~/.zcode/cli/config.json was found — it may be missing or not strict JSON, and UMH will not risk losing fields or comments). Prefer the panel route: Settings → MCP Servers → New MCP Server, scope User (all workspaces), name umh-memory, type stdio.", "[manual] Fill the form exactly: command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config. Hand-editing is officially supported ('to manage servers in bulk'): alternatively add the entry under mcp.servers in ~/.zcode/cli/config.json (the workspace scope <project>/.zcode/config.json is per-project; UMH never edits it).", "[manual] Enable the tools for the target agent: click Add, then confirm the umh-memory entry is enabled in the MCP Servers list — a disabled server is stored as \"enable\": false in its config object, and a server without the field counts as enabled. All configured servers are available to ZCode Agent; there is no per-assistant binding step.", "[manual] Start a new session: configured servers connect at session start (the official docs state workspace servers connect automatically at session start); confirm the umh-memory entry shows enabled.", "[manual] Inject the memory policy: paste the following block into the workspace agent-instruction file you keep (AGENTS.md in this workspace; the official MCP docs do not document an instruction-file convention — verify against your own setup), keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[manual] Verify from a ZCode session: ask the agent to run memory_status; do not report onboarding success until the agent actually calls the tool.", "[auto] UMH wrote the umh-memory entry into the user-level native config ~/.zcode/cli/config.json under mcp.servers (a pre-edit backup and a rollback receipt were recorded under the UMH home). Open Settings → MCP Servers and confirm the entry is enabled in the list: a disabled server is stored as \"enable\": false in its config object, and a server without the field counts as enabled.", "[auto] Inject the memory policy: paste the following block into the workspace agent-instruction file you keep (AGENTS.md in this workspace; the official MCP docs do not document an instruction-file convention — verify against your own setup), keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[auto] Start a new session and verify: ask the agent to run memory_status; do not report onboarding success until the agent actually calls the tool."];
    readonly schemaValidated: true;
};
/**
 * 构造 ZCode 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 zcodeAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export declare function makeZcodeAdapter(options?: {
    umhHome?: string;
}): ClientAdapter;
/** ZCode 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeZcodeAdapter 绑定）。 */
export declare const zcodeAdapter: ClientAdapter;
export type { ClientAdapter, ConfigPlan, Detection, LaunchSpec };
