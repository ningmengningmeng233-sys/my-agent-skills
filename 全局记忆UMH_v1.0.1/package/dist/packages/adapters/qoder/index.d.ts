/**
 * packages/adapters/qoder — Qoder 适配器（A03）：CLI 自动写入端 / IDE 手工端。
 *
 * 核心语义（工单 A03 + A 阶段全局约束；官方文档 2026-10-03 核实，见
 * .lemon/evidence/A03/qoder-docs-research.md）：
 *
 * CLI（variant='cli'，自动写入端）：
 *   - 官方配置：user 级 `~/.qoder/settings.json → mcpServers`（"Available to all
 *     projects"；QODER_CONFIG_DIR 可迁址；项目级文件需逐项目审批）→ UMH 冻结写
 *     user 级，owned pointer = `/mcpServers/umh-memory`（A01 createConfigPlan 默认）；
 *   - preview 走 makeOwnedEntryPlan：只添加已冻结 pointer 的 umh 服务项
 *     （{command,args,env}，与 makeLaunchSpec 严格同源），existing-server/未知字段/
 *     原生记忆字段（autoMemoryEnabled、context.* 等）一律不动（JSON 往返字段保留
 *     可证明，键序保持；2 空格重排为 A01 已披露 Minor#4 语义）；
 *   - 官方支持 JSONC // 注释（docs.qoder.com/cli/settings："Configuration files
 *     can contain comments (ignored during parsing)"）→ 严格 JSON.parse 成功才自动
 *     编辑；注释/解析失败 → 降级手工片段（changes 恒空），绝不破坏性 stringify；
 *   - 多 profile（CN/国际版、多账号、QODER_CONFIG_DIR 迁址）：官方未记录 profile
 *     机制（UNVERIFIED），UMH 把 profile 建模为「候选配置根」。detect 只发现候选
 *     （绝不隐式选第一项）；detection.configPaths 多于一个 → preview 抛
 *     PROFILE_REQUIRED，要求以显式 detect(opts.configPath) 选定后再接入；显式
 *     configPath 即独占选择（每 profile 独立 plan/apply，绑定按 profile 用不同
 *     bindingId，例如 qoder-cli / qoder-cli-cn）；
 *   - apply = parseConfigPlan fail-closed → 拒其它端计划 → applyConfigPlan（A01
 *     事务：锁内 beforeHash → 备份 → 原子发布 → published 语义）→ saveReceipt。
 *     受信绑定文件绝不由 adapter 写入（对齐 A02 先例：绑定只能由 connect CLI 受信
 *     编排经 writeTrustedBindingFile 落盘 —— 项目绑定不可被客户端请求更改）；
 *   - 原生 Auto-Memory（autoMemoryEnabled，默认 false，需重启生效）绝不修改、绝不
 *     停用；provider/认证配置一律不碰。
 *
 * IDE（variant='ide'，手工端）：
 *   - 官方只有 settings UI（Ctrl+Shift+, → MCP → My Servers → + Add → JSON → Save），
 *     无文档化的用户可编辑配置文件，IDE 与 CLI 是否共享 settings.json 亦无文档
 *     → 形态未核实：恒 makeManualPlan（changes 恒空、只给已核实的 UI 步骤），
 *     绝不套 CLI 写入路径（伪造 json 检出也不能）；detect 只探测安装痕迹
 *     （AppData 等），configPaths 恒 []、format 'ui'、state 'unverified'。
 *
 * 规则注入（policy-block 接线，A01 Minor#8 端内行为测试）：
 *   - 记忆策略只进 owned 的「UMH MEMORY POLICY v1」标记块（BEGIN/END HTML 注释），
 *     落点为项目 AGENTS.md（Qoder CLI context.fileName 默认值）——AGENTS.md 由用户
 *     手工粘贴（markdown 不是 ConfigChange 支持的格式，自动写入无合同通道）；
 *     QODER_POLICY_INSTRUCTIONS + buildPolicyBlock 渲染、{{policy}} 占位插值；
 *   - preview 的 manualInstructions 逐字来自 QODER_RECIPE（docs/clients/recipes/
 *     qoder.json 的代码镜像，测试强制 deepEqual 同步）：[cli]/[cli-auto]/[ide] 三节
 *     按 variant/路径选择，{{command}}/{{args}}/{{env}}/{{policy}} 由 plan.launch
 *     与模块策略插值 —— 手工文案与自动接入同一 launch 来源（A01 头部合同）。
 *
 * validate 三项诚实：connection/policy=UNVERIFIED（真实客户端证据归 A09）、
 * sharedReadWrite=NOT_RUN（Qoder 未实际调用工具前不得判 PASS/FAIL）。
 */
import type { ClientAdapter, ConfigPlan, Detection, LaunchSpec } from '../contracts.js';
/** Qoder CLI 的 owned 段：user 级 settings.json → mcpServers 命名空间下的 umh-memory。 */
export declare const QODER_OWNED_POINTER = "/mcpServers/umh-memory";
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点 AGENTS.md）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export declare const QODER_POLICY_INSTRUCTIONS: readonly string[];
/**
 * Qoder 配方（= docs/clients/recipes/qoder.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[cli]=CLI 手工路径、[cli-auto]=CLI 自动路径附加指引、
 * [ide]=IDE 手工路径。占位符 {{command}}/{{args}}/{{env}}/{{policy}} 由 preview 用
 * plan.launch 与模块策略块插值。步骤按 2026-10-03 官方文档核实措辞。
 */
export declare const QODER_RECIPE: {
    readonly variant: "cli|ide";
    readonly version: "0.4.3";
    readonly officialSource: "https://docs.qoder.com/cli/mcp-reference; https://docs.qoder.com/cli/settings-reference; https://docs.qoder.com/cli/settings; ide: https://docs.qoder.com/user-guide/chat/model-context-protocol";
    readonly verifiedAt: "2026-10-03";
    readonly configLocation: "cli: user-level ~/.qoder/settings.json → mcpServers (official: 'Available to all projects'; the user config dir can be relocated with QODER_CONFIG_DIR; JSONC // comments are officially supported; project-level settings/.mcp.json require approval before use). ide: settings UI only (Ctrl+Shift+, → MCP → My Servers); no user-editable config file is documented, UMH never writes it.";
    readonly format: "json|ui";
    readonly ownedPointer: "cli: /mcpServers/umh-memory inside ~/.qoder/settings.json (auto-edit only for strict JSON; JSONC/commented files get a manual fragment instead); ide: ui://mcp (the server entry named 'umh-memory' in IDE Settings → MCP → My Servers)";
    readonly policyLocation: "project AGENTS.md (Qoder CLI context.fileName default) — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers";
    readonly manualSteps: readonly ["[cli] Decide the target profile explicitly first (CN or international edition, several accounts, or a relocated QODER_CONFIG_DIR): when several Qoder config roots exist, UMH refuses to guess and reports PROFILE_REQUIRED — re-run detection with the explicit settings.json path of the profile you want to connect.", "[cli] UMH did not auto-edit a Qoder settings file (no safely auto-editable strict-JSON settings file was found — it may be missing or contain JSONC comments, and UMH will not risk losing comments or fields). Open the user-level settings file at ~/.qoder/settings.json (create it if missing; this scope is 'Available to all projects'; project-level files require per-project approval).", "[cli] Under the top-level mcpServers object add a stdio server named umh-memory with exactly: command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.", "[cli] Restart Qoder CLI (mcpServers changes require restart), then run /mcp to check the server is listed and /mcp reload after later edits; the UMH tools include memory_status.", "[cli] Inject the memory policy into the project AGENTS.md (Qoder's default memory file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[cli] Verify from a Qoder session: ask it to run memory_status; do not report onboarding success until the CLI actually calls the tool.", "[cli-auto] UMH wrote the umh-memory entry into the user-level settings file (a pre-edit backup and a rollback receipt were recorded under the UMH home). Restart Qoder CLI (mcpServers changes require restart) and run /mcp to confirm the server; the UMH tools include memory_status.", "[cli-auto] Inject the memory policy into the project AGENTS.md (Qoder's default memory file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[cli-auto] Verify from a Qoder session: ask it to run memory_status; do not report onboarding success until the CLI actually calls the tool.", "[ide] The Qoder IDE form is verified only as a settings UI; no user-editable config file is documented, so UMH never writes IDE configuration. Open IDE Settings (Ctrl+Shift+, on Windows) and navigate to MCP in the left-side navigation pane.", "[ide] On the My Servers tab click + Add; in the JSON file that appears add a server named umh-memory with transport type STDIO and exactly: command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file.", "[ide] Close the file and click Save when prompted; a link icon means the connection is successful. Tool calls prompt for confirmation (Ctrl+Enter) before running.", "[ide] Inject the memory policy block (see docs/clients/qoder.md) into your instruction file and verify: ask the IDE agent to run memory_status; do not report onboarding success until it actually calls the tool."];
    readonly schemaValidated: true;
};
/**
 * 构造 Qoder 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 qoderAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export declare function makeQoderAdapter(options?: {
    umhHome?: string;
}): ClientAdapter;
/** Qoder 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeQoderAdapter 绑定）。 */
export declare const qoderAdapter: ClientAdapter;
export type { ClientAdapter, ConfigPlan, Detection, LaunchSpec };
