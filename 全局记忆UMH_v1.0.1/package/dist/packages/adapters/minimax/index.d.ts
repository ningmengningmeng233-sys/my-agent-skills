/**
 * packages/adapters/minimax — MiniMax Code 适配器（A07）：cli 自动写入端 / desktop 手工端。
 *
 * 核心语义（工单 A07 + A 阶段全局约束；官方源码/文档 2026-10-03 核实，见
 * .lemon/evidence/A07/research.md）：
 *
 * CLI（variant='cli'，自动写入端）：
 *   - 官方 profile 级 MCP 入口：选中 dataDir 下的 `mcp.json`（源码逐字：
 *     `local-mcp.service.ts filePath() = join(this.dataDir(), 'mcp.json')`；
 *     读兼容别名 `<dataDir>/mcp/mcp.json`（`runtime/config-file.ts` 逐字），写只落
 *     `mcp.json`）。owned pointer = `/mcpServers/umh`（stdio `{command,args,env}`，
 *     无 url 即按 stdio 读取 —— project-mcp 源码 `raw['command'] ? 'stdio'` 同语义）。
 *   - dataDir 解析冻结（三源逐字：`packages/config/src/data-dir.ts` +
 *     `packages/tui/src/runtime/data-dir.ts` + `docs/installation.md`）：
 *     非空 MINIMAX_DATA_DIR → 非空 MAVIS_DATA_DIR → 默认 `~/.minimax`
 *     （profile 选中时 `~/.minimax-<profile>`，`--profile` 旗标或 `PROFILE` 环境变量；
 *     git auto-detection 分支形态本单不消费）。空串/空白视为未设置（源码 `.trim()`）。
 *     MINIMAX 或 MAVIS 重定向在场 → 独占选中（redirected root），与 default/profile
 *     候选互斥（响应优先级本身就是排他的 —— 源码 `readDataDirOverride() ?? default`）。
 *   - 多 root（default + profile 候选并存）未显式选择 → PROFILE_REQUIRED
 *     （A03 同型先例：绝不隐式选第一项）；显式选择 = detect(opts.configPath) 独占
 *     （每 root 独立 plan/apply，绑定按 root 用不同 bindingId）。
 *     显式路径必须形如 `<root>/mcp.json`（basename `mcp.json` 且父目录名为
 *     `.minimax` / `.minimax-<profile>` / 重定向根）→ 否则 PROFILE_REQUIRED（绝不猜）；
 *     遗留别名 `<root>/mcp/mcp.json` → 手工片段（读兼容但写目标唯一，绝不写别名）。
 *   - 严格 JSON 才自动编辑：JSON 原生往返保留全部字段与键序（第三方字段/editor/
 *     未知字段 deepEqual 专测）；坏 JSON/非对象 → 手工片段（changes 恒空），绝不
 *     破坏性 stringify。owned 段纪律（A01）：已有非 UMH 内容 → OWNED_POINTER_OCCUPIED。
 *   - 项目级 `.mcp.json`（workspace 根，只读共享层，Desktop/TUI/exec/acp 共用同一
 *     Runtime 能力）UMH 永不写（写了就是改用户仓库）；`config.yaml`
 *     （custom_provider/minimax_api 凭据面）与 `sessions/`（聊天记忆）永不出现在
 *     检出面（结构性零触碰 + 专测钉死），零读零写。
 *   - **adapter 绝不调 mcode 写配置**（源码 `packages/tui/src/cli/` 无 mcp 子命令，
 *     MCP 管理走 TUI 内 `/mcp` 面板与直接编辑文件；官方 add 无 dry-run 亦无命令）。
 *
 * desktop（variant='desktop'，手工端）：
 *   - 已出版源码只覆盖 TUI/CLI/ACP，不含桌面应用源码（README 逐字）；桌面 profile
 *     配置入口 UNVERIFIED → 恒 makeManualPlan（changes 恒空），绝不借 CLI 目录推断
 *     （伪造/外来的 verified JSON 检出套 desktop 变体也一样手工 —— 本单 Review Focus
 *     主战场：CLI 成功永不构成桌面证据）。detect(desktop) 恒
 *     `{configPaths:[], format:'ui', state:'unverified', installed:false}`。
 *   - manualSteps 列确切需用户确认的入口（桌面 MCP 设置入口 + `/mcp` 核验 + 项目
 *     `.mcp.json` 只读说明），不逆向数据库，不接云记忆服务。
 *
 * 规则注入（policy-block 端内接线，A01 Minor#8）：记忆策略只进 owned 的
 * 「UMH MEMORY POLICY v1」标记块，落点项目 AGENTS.md（MiniMax 工作区指令文件：
 * 源码 `packages/local-runtime/src/project/instructions.ts`
 * `PROJECT_INSTRUCTIONS_FILENAME = 'AGENTS.md'`），用户手工粘贴（markdown 非
 * ConfigChange 支持格式，自动写入无合同通道）。existing 内容不替换（mergePolicyBlock
 * 幂等合同）。
 *
 * apply = parseConfigPlan fail-closed → 拒其它端计划 → 全新安装目标预创建缺失父目录
 * （重定向根/dataDir 可能不存在；A05/A06 先例）→ applyConfigPlan（A01 事务：锁内
 * beforeHash → 备份 → 原子发布 → published 语义）→ saveReceipt。受信绑定文件绝不
 * 由 adapter 写入（对齐 A02-A06 先例：绑定只能由 connect CLI 受信编排落盘 ——
 * 项目绑定不可被客户端请求更改）。
 *
 * remove：CLI 的 JSON 端直接复用 A01 removeOwnedConfig 三方比对（用户后改 owned 段 →
 * CONFIG_CHANGED 不触碰；无关编辑幸存；UMH 新建且未动的文件 → 整文件移除）；手工
 * 回执（desktop / 别名 / 坏 JSON / 未选 root 的手工路径，changes 恒空）→ removed:false
 * + 清理指引（A03 修复先例：声称自动移除成功即为误报）。
 *
 * validate 三项诚实：connection/policy=UNVERIFIED（真实客户端证据归 A09）、
 * sharedReadWrite=NOT_RUN（agent 未实际调用工具前不得判 PASS/FAIL）；cli/desktop
 * 两变体分别可验证（桌面行永不被 CLI 证据填充）。
 */
import type { ClientAdapter, ConfigPlan, Detection, LaunchSpec } from '../contracts.js';
/**
 * MiniMax CLI 的 owned 段：选中 dataDir 下 `mcp.json` 的 `mcpServers` 对象内 `umh` 条目
 * （源码写路径唯一：`join(dataDir, 'mcp.json')`；stdio `{command,args,env}`，无 url 即
 * 按 stdio 读取；`type` 可省）。
 */
export declare const MINIMAX_OWNED_POINTER = "/mcpServers/umh";
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点项目 AGENTS.md）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export declare const MINIMAX_POLICY_INSTRUCTIONS: readonly string[];
/**
 * MiniMax 配方（= docs/clients/recipes/minimax.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[cli]=CLI 手工路径（未选 root/坏 JSON/别名/缺失）、
 * [cli-auto]=CLI 自动路径附加指引、[desktop]=桌面手工路径（恒 UNVERIFIED）。
 * 占位符 {{command}}/{{args}}/{{env}}/{{policy}} 由 preview 用 plan.launch 与模块策略插值。
 * 步骤按 2026-10-03 官方源码/文档核实措辞（dataDir 优先级/profile mcp.json/项目 .mcp.json/
 * /mcp 核验流/桌面源码边界）。
 */
export declare const MINIMAX_RECIPE: {
    readonly variant: "cli|desktop";
    readonly version: "0.6.2";
    readonly officialSource: "https://github.com/MiniMax-AI/minimax-code; profile MCP: https://github.com/MiniMax-AI/minimax-code/blob/main/packages/local-runtime-v2/docs/project-mcp.md; project MCP: https://github.com/MiniMax-AI/minimax-code/blob/main/docs/examples.md#5-connect-an-authenticated-project-mcp-server; data dirs: https://github.com/MiniMax-AI/minimax-code/blob/main/docs/installation.md#accounts-and-data";
    readonly verifiedAt: "2026-10-03";
    readonly configLocation: "cli: <dataDir>/mcp.json where <dataDir> resolves as non-empty MINIMAX_DATA_DIR, else non-empty MAVIS_DATA_DIR, else ~/.minimax (or ~/.minimax-<profile> when a profile is selected) — frozen from packages/config/src/data-dir.ts + packages/tui/src/runtime/data-dir.ts + docs/installation.md; read-compatible legacy alias <dataDir>/mcp/mcp.json is never an auto-edit target; project-level .mcp.json in the session workspace is read-only shared runtime state (UMH never writes it); desktop: no verified profile configuration entry (the published source covers TUI/CLI/ACP only, not the desktop app) — desktop is manual-only";
    readonly format: "json|ui";
    readonly ownedPointer: "/mcpServers/umh — a stdio server named 'umh' ({command,args,env}; an entry without a url is read as a stdio server) inside mcpServers of <dataDir>/mcp.json; config.yaml (custom_provider/minimax_api credentials) and sessions/ are never detection targets and are never read or written";
    readonly policyLocation: "project AGENTS.md (MiniMax workspace instruction file: packages/local-runtime/src/project/instructions.ts PROJECT_INSTRUCTIONS_FILENAME = 'AGENTS.md') — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers";
    readonly manualSteps: readonly ["[cli] Choose the data directory explicitly first: default ~/.minimax, or ~/.minimax-<profile> when a profile is selected, or a redirected root via non-empty MINIMAX_DATA_DIR (preferred) / MAVIS_DATA_DIR. When several of these hold a config, UMH refuses to guess and reports PROFILE_REQUIRED — re-run detection with the explicit mcp.json path of the data directory you want to connect.", "[cli] UMH did not auto-edit a MiniMax mcp.json file (no safely auto-editable strict-JSON file was found for the selected data directory — it may be missing, live under a custom MINIMAX_DATA_DIR/MAVIS_DATA_DIR, use the legacy mcp/mcp.json alias, or not be strict JSON, and UMH will not risk losing your fields). Hand-edit the selected <dataDir>/mcp.json as described next; UMH never edits <dataDir>/config.yaml (provider credentials) or sessions.", "[cli] The entry to add is exactly: command = {{command}}; args = {{args}}; env = {{env}}. It is a stdio server named umh under the top-level mcpServers object of the selected <dataDir>/mcp.json. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.", "[cli] Inject the memory policy into the project AGENTS.md (the MiniMax workspace instruction file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[cli] Restart MCode (or run /mcp reload) and confirm the umh entry: /mcp lists it as configured, then available after tool discovery or a call. Project-level .mcp.json in the workspace is a separate read-only shared layer — UMH never writes it.", "[cli] Verify: ask MiniMax to run memory_status; do not report onboarding success until the agent actually calls the tool.", "[cli-auto] UMH wrote the umh stdio entry into the explicitly selected <dataDir>/mcp.json (a pre-edit backup and a rollback receipt were recorded under the UMH home). Provider configuration (config.yaml), sessions and every other field were preserved untouched; UMH never reads sessions or credentials.", "[cli-auto] Restart MCode (or run /mcp reload) and confirm the umh entry in /mcp: configured before connection, available after tool discovery or a call.", "[cli-auto] Inject the memory policy into the project AGENTS.md (the MiniMax workspace instruction file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[cli-auto] Verify: ask MiniMax to run memory_status; do not report onboarding success until the agent actually calls the tool.", "[desktop] The MiniMax desktop configuration entry is UNVERIFIED: the published source covers the terminal TUI, headless CLI and ACP but does not include the desktop application's source, so UMH cannot verify which profile file (if any) the desktop reads. UMH therefore never auto-writes for the desktop — a CLI success is never desktop evidence.", "[desktop] Confirm in the desktop app itself where it manages MCP servers (official UI / verified configuration only — do not assume it reads the CLI <dataDir>/mcp.json). Add a stdio server named umh with exactly: command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file.", "[desktop] The project-level .mcp.json in the session workspace is shared runtime state consumed by Desktop, TUI, mcode exec and mcode acp alike — UMH never writes it and never reverse-engineers any desktop database; no cloud memory service is contacted.", "[desktop] Inject the memory policy into the project AGENTS.md (the MiniMax workspace instruction file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[desktop] Verify in the desktop app: check the umh entry in its MCP panel, then ask the agent to run memory_status; do not report onboarding success until the agent actually calls the tool."];
    readonly schemaValidated: true;
};
/**
 * 构造 MiniMax 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 minimaxAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export declare function makeMinimaxAdapter(options?: {
    umhHome?: string;
}): ClientAdapter;
/** MiniMax 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeMinimaxAdapter 绑定）。 */
export declare const minimaxAdapter: ClientAdapter;
export type { ClientAdapter, ConfigPlan, Detection, LaunchSpec };
