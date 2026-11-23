/**
 * packages/adapters/codex — Codex 适配器（A05）：cli / desktop 双变体，共享官方文档化的
 * user 级 TOML 配置面 `~/.codex/config.toml → mcp_servers`。
 *
 * 核心语义（工单 A05 + A 阶段全局约束；官方文档 2026-10-03 核实，见
 * .lemon/evidence/A05/codex-docs-research.md）：
 *
 * 官方文档（developers.openai.com/codex/mcp/ → 现址 learn.chatgpt.com/docs/extend/mcp）：
 *   - "Codex stores MCP configuration in config.toml … By default this is
 *     ~/.codex/config.toml, but you can also scope MCP servers to a project with
 *     .codex/config.toml (trusted projects only)."
 *   - 桌面覆盖范围（冻结依据）："The ChatGPT desktop app, Codex CLI, and IDE extension
 *     share this configuration." → variant='desktop' 与 'cli' 共享同一写入路径与语义
 *     （A04 zcode 双变体共享先例）；未知变体响亮 INVALID_ARGUMENT；
 *   - stdio 服务项 = `[mcp_servers.<name>]` 表：command（必需）/args/env（官方参考
 *     推荐 inline 形态 `env = { KEY = "value" }`）；可选 `enabled` 子键（用户自禁
 *     场景由 OWNED_POINTER_OCCUPIED 守卫保护，绝不吞掉用户的禁用决定）；
 *   - config 根 = CODEX_HOME（官方："Codex stores its local state under CODEX_HOME
 *     (defaults to ~/.codex)"；非绝对路径忽略，绝不按任意 cwd 解析）?? ~/.codex。
 *     项目级 .codex/config.toml 是 trusted-projects-only 的逐项目配置 → UMH 绝不写。
 *
 * TOML 编辑策略（本单最难点，工单明示）：Node 生态无保注释的 TOML 往返；smol-toml
 * （C01 固定 1.9.0，DEPENDENCIES-PIN 记载用途即"客户端配置适配器"）的 stringify
 * 会丢注释且把子对象提升为根级表（实测 `[env]`）→ **绝不整文档 stringify 覆盖**。
 * 采用"经过测试的段落编辑 + 解析校验"三重防线：
 *   1. 文本级段落操作：安装 = 在文件末尾**追加** `[mcp_servers.umh]` 段（已有全部
 *      字节逐字保留——测试断言 after.startsWith(before)）；卸载 = 仅切除该段（含其
 *      子表，按表头行边界；吞掉安装时留下的分隔空行）；
 *   2. 每次编辑后 smol-toml.parse 校验语法有效；
 *   3. 语义 diff：parse(before) 去掉 owned 段 与 parse(after) 去掉 owned 段
 *      deepEqual（无关表/键/注释的解析等价性），owned 段值 deepEqual 预期。
 * 任何一步不满足 → fail-closed（绝不发布）。owned 段值不同（含用户 enabled=false）
 * → OWNED_POINTER_OCCUPIED 拒绝；文件非有效 TOML → 手工片段（changes 恒空）。
 *
 * 序列化：自写确定性序列化器 —— 基本字符串（双引号，官方示例形态；`\` `"` 与控制
 * 字符按 TOML 规则转义，中文/空格原样）、args 单行数组、env inline 表（空 = `{}`）。
 * 段内零注释（注释会让卸载的"段内无用户内容"守卫无法区分归属）。
 *
 * apply = parseConfigPlan fail-closed → 拒其它端计划 → applyConfigPlan（A01 事务：
 * 锁内 beforeHash → 备份 → 原子发布 → published 语义）→ saveReceipt。受信绑定文件
 * 绝不由 adapter 写入（对齐 A02-A04 先例：绑定只能由 connect CLI 受信编排落盘 ——
 * 项目绑定不可被客户端请求更改）。
 *
 * remove（TOML 版三方比对，adapter 端实现 —— A01 removeOwnedConfig 对非 JSON 格式
 * 诚实拒绝 MANUAL_REMOVAL_REQUIRED）：值级 before/after/current 三方比对 + 卸载前
 * 段文本与 canonical 序列化逐字比对守卫（行尾注释/段内注释/增删键/重排格式等
 * "解析值不变但文本有别"的用户内容一律 CONFIG_CHANGED 不触碰 —— A05 修复轮
 * Important-1，取代原仅覆盖整行注释的守卫）；切除动作复用 A01
 * applyConfigPlan（临时反转计划：锁/beforeHash 复核/备份/原子发布全部继承，计划
 * 本身即刻丢弃、不落回执）；UMH 新建且逐字节未动的文件 → 整文件移除（A01 语义，
 * 先留适配器本地备份，字节另可从回执 afterBytes 追回）。手工回执（changes=[]）→
 * removed:false + 清理指引（A03 修复先例：声称自动移除成功即为误报）。
 *
 * validate 三项诚实：connection/policy=UNVERIFIED（真实客户端证据归 A09）、
 * sharedReadWrite=NOT_RUN（agent 未实际调用工具前不得判 PASS/FAIL）；cli/desktop
 * 两变体分别可验证。
 *
 * 规则注入（policy-block 端内接线，A01 Minor#8）：记忆策略只进 owned 的
 * 「UMH MEMORY POLICY v1」标记块，落点工作区指令文件（本工作区 AGENTS.md；官方
 * 页面未记录指令文件约定 → recipe.policyLocation 如实标注；用户手工粘贴，UMH 不
 * 自动写 —— markdown 非 ConfigChange 支持格式）。existing AGENTS 内容不替换。
 */
import type { ClientAdapter, ConfigPlan, Detection, LaunchSpec } from '../contracts.js';
/**
 * Codex 的 owned 段：user 级 config.toml 的 `[mcp_servers.umh]` 表
 * （工单明示 pointer=`mcp_servers.umh`；官方 stdio 字段 command/args/env）。
 */
export declare const CODEX_OWNED_POINTER = "/mcp_servers/umh";
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点工作区指令文件）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export declare const CODEX_POLICY_INSTRUCTIONS: readonly string[];
/**
 * Codex 配方（= docs/clients/recipes/codex.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[manual]=手工路径（配置缺失/非有效 TOML）、[auto]=自动
 * 路径附加指引。两节共享于 cli|desktop 双变体（官方单一配置面）。占位符
 * {{command}}/{{args}}/{{env}}/{{policy}} 由 preview 用 plan.launch 与模块策略插值。
 * 步骤按 2026-10-03 官方文档核实措辞（desktop 设置流/`/mcp`/enabled 子键/共享配置句）。
 */
export declare const CODEX_RECIPE: {
    readonly variant: "cli|desktop";
    readonly version: "0.160.0";
    readonly officialSource: "https://developers.openai.com/codex/mcp/ (308 -> https://learn.chatgpt.com/docs/extend/mcp?surface=cli); config: https://learn.chatgpt.com/docs/config-file/config-basic; https://learn.chatgpt.com/docs/config-file/config-reference; https://learn.chatgpt.com/docs/config-file/config-advanced";
    readonly verifiedAt: "2026-10-03";
    readonly configLocation: "user-level ~/.codex/config.toml (official: 'Codex stores its local state under CODEX_HOME (defaults to ~/.codex)'; a non-absolute CODEX_HOME is ignored by UMH); official MCP docs: 'The ChatGPT desktop app, Codex CLI, and IDE extension share this configuration'; project-level .codex/config.toml is trusted-projects-only and per-project — UMH never edits it";
    readonly format: "toml";
    readonly ownedPointer: "/mcp_servers/umh — a [mcp_servers.umh] TOML table with command/args/env (env in the official inline form, e.g. env = {}); auto-edit appends or removes only this section via tested section editing + parse validation (never whole-file stringify; unrelated tables, keys and comments are preserved byte-for-byte)";
    readonly policyLocation: "workspace agent-instruction file (AGENTS.md in this workspace; the official pages reviewed for this recipe do not document an instruction-file convention — verify against your own setup) — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers";
    readonly manualSteps: readonly ["[manual] UMH did not auto-edit the Codex TOML config (no safely auto-editable ~/.codex/config.toml was found — it may be missing, live under a custom CODEX_HOME, or not be valid TOML; UMH will not risk losing your settings or comments). Desktop route: ChatGPT desktop app → Settings → MCP servers → Add server, name umh-memory, type STDIO, provide the command below, Save, then Restart (official setup flow). CLI route: codex mcp add umh-memory -- <command...>, or hand-edit config.toml as below.", "[manual] Fill the entry exactly: command = {{command}}; args = {{args}}; env = {{env}}. In TOML this is one table: [mcp_servers.umh] with command/args/env keys (env uses the official inline form, e.g. env = {}). UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.", "[manual] Scope: prefer the user-level ~/.codex/config.toml — the official docs state 'The ChatGPT desktop app, Codex CLI, and IDE extension share this configuration', so one entry covers all of them. Project-level .codex/config.toml is loaded for trusted projects only and is per-project; UMH never edits it.", "[manual] Inject the memory policy: paste the following block into the workspace agent-instruction file you keep (AGENTS.md in this workspace; the official pages reviewed for this recipe do not document an instruction-file convention — verify against your own setup), keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[manual] Restart or start a new Codex session and confirm the umh-memory server shows connected (type /mcp in the TUI; the desktop app lists connected MCP servers in its settings).", "[manual] Verify: ask Codex to run memory_status; do not report onboarding success until the agent actually calls the tool.", "[auto] UMH appended the [mcp_servers.umh] section to the user-level ~/.codex/config.toml via tested section editing (a pre-edit backup and a rollback receipt were recorded under the UMH home; unrelated tables, keys and comments were preserved byte-for-byte). The official docs state 'The ChatGPT desktop app, Codex CLI, and IDE extension share this configuration', so one entry covers all of them.", "[auto] Confirm the umh-memory entry is enabled in your client: the official config reference documents an `enabled` sub-key for [mcp_servers.*] tables, and UMH never writes one — if you disabled umh yourself, UMH's overwrite guard refuses to clobber your choice on re-install.", "[auto] Inject the memory policy: paste the following block into the workspace agent-instruction file you keep (AGENTS.md in this workspace; the official pages reviewed for this recipe do not document an instruction-file convention — verify against your own setup), keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[auto] Restart or start a new Codex session, confirm the server shows connected (/mcp in the TUI or the desktop MCP list), then ask the agent to run memory_status; do not report onboarding success until the agent actually calls the tool."];
    readonly schemaValidated: true;
};
/**
 * 构造 Codex 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 codexAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export declare function makeCodexAdapter(options?: {
    umhHome?: string;
}): ClientAdapter;
/** Codex 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeCodexAdapter 绑定）。 */
export declare const codexAdapter: ClientAdapter;
export type { ClientAdapter, ConfigPlan, Detection, LaunchSpec };
