/**
 * packages/adapters/claude — Claude Code 适配器（A06）：cli 单变体，scope 显式选择。
 *
 * 核心语义（工单 A06 + A 阶段全局约束；官方文档 2026-10-03 核实，见
 * .lemon/evidence/A06/claude-docs-research.md）：
 *
 * 官方三作用域（code.claude.com/docs/en/mcp 作用域表，逐字）：
 *   - user：全项目生效，"stored in ~/.claude.json"（顶层 mcpServers 对象；官方原文
 *     "the same stdio server name at the top level of ~/.claude.json (user scope)"）；
 *   - project："Current project only / Yes, via version control / .mcp.json in project
 *     root"；"Claude Code prompts for approval in interactive sessions before using
 *     project-scoped servers from .mcp.json files"；
 *   - local："Local scope is the default"，存于 ~/.claude.json 内
 *     projects["<项目路径>"].mcpServers（官方 local 示例逐字）。
 *
 * scope 显式选择（工单冻结，A03 PROFILE_REQUIRED 同型；错误名 CONFIG_SCOPE_REQUIRED）：
 *   - detect 无显式 configPath（发现模式）→ 只报告安装证据（状态文件存在性，
 *     CLAUDE_CONFIG_DIR 迁址感知），configPaths 恒 [] —— **绝不点名作用域**（一个
 *     `~/.claude.json` 同时承载 user 与 local 两个作用域，"存在一个文件"不构成任何
 *     作用域选择）；preview 对空/多候选 configPaths 抛 CONFIG_SCOPE_REQUIRED；
 *   - 显式选择 = detect(opts.configPath)，路径形态唯一命名作用域：basename
 *     `.mcp.json` → project；basename `.claude.json`（含迁址）→ user；其它形态 =
 *     不明作用域 → CONFIG_SCOPE_REQUIRED（绝不猜）；
 *   - local 作用域 = 手工路径：项目键是 Claude Code 按其运行时 cwd 自写的键，精确
 *     归一化形态（大小写/分隔符/盘符）在本机无 CLI 条件下 UNVERIFIED，猜键写入的最坏
 *     失败形态是"条目落进 Claude 不读的键 → 静默失联"（违背"未核实格式禁止猜写"）。
 *     manualSteps 给官方 `claude mcp add --scope local` 指引（Claude 自写自己的键）。
 *     user 与 local 共用一个文件 → 显式指向该状态文件时确定性解析为 user 作用域，
 *     不存在选择歧义。工单 GREEN 面亦只要求 user/project fixture 隔离。
 *
 * 自动写入面（工单"官方 CLI/已核实 owned 配置"）：user/project 两个官方文档化 JSON
 * 文件，owned pointer = `/mcpServers/umh`（工单示例测试字面 mcpServers.umh；A04 已立
 * owned pointer 可安全偏离 A01 缺省值的先例，A05 codex 同款工单字面 pointer）。
 * **adapter 绝不调 claude CLI 写配置**（官方 add/add-json 无 dry-run，预览与备份由
 * A01 事务层负责 —— 工单明示分工）；官方 CLI 命令仅作为 manualSteps 备选。
 *
 * hooks/env/key 零触碰（工单冻结项，Review Focus"未知字段和用户后改内容不丢"）：
 *   - settings 文件（~/.claude/settings.json、.claude/settings.json、
 *     .claude/settings.local.json —— hooks/env/apiKeyHelper 所在）永不出现在
 *     detection.configPaths（结构性零触碰，专测钉死）；
 *   - 严格 JSON 才自动编辑：JSON 原生往返保留全部字段与键序（hooks/env/
 *     disabledMcpServers/enabledMcpServers/apiKeyHelper/未知字段 deepEqual 专测）；
 *     JSONC/损坏 → 手工片段（changes 恒空），绝不破坏性 stringify；
 *   - owned 段纪律（A01）：pointer 处已有非 UMH 内容 → OWNED_POINTER_OCCUPIED 拒绝；
 *   - **UMH 不注册任何 Claude Code hook**（工单明示禁止用 Stop Hook 绕 candidate 门）；
 *   - 原生 Auto Memory（~/.claude/projects/<project>/memory/，官方文档化默认开启）：
 *     零读零写零停用（结构上不在任何写入路径）。
 *
 * apply = parseConfigPlan fail-closed → 拒其它端计划 → 全新安装（beforeHash null）目标
 * 预创建缺失父目录（迁址后的 CLAUDE_CONFIG_DIR 可能不存在；A05 先例）→ applyConfigPlan
 * （A01 事务：锁内 beforeHash → 备份 → 原子发布 → published 语义）→ saveReceipt。
 * 受信绑定文件绝不由 adapter 写入（对齐 A02-A05 先例：绑定只能由 connect CLI 受信
 * 编排经 writeTrustedBindingFile 落盘 —— 项目绑定不可被客户端请求更改）。
 *
 * remove：JSON 端直接复用 A01 removeOwnedConfig 三方比对（用户后改 owned 段 →
 * CONFIG_CHANGED 不触碰；无关编辑幸存；UMH 新建且未动的文件 → 整文件移除）；手工
 * 回执（changes=[]）→ removed:false + 清理指引（A03 修复先例：声称自动移除成功即为
 * 误报）。
 *
 * validate 三项诚实：connection/policy=UNVERIFIED（真实客户端证据归 A09）、
 * sharedReadWrite=NOT_RUN（agent 未实际调用工具前不得判 PASS/FAIL）。
 *
 * 规则注入（policy-block 端内接线，A01 Minor#8）：记忆策略只进 owned 的
 * 「UMH MEMORY POLICY v1」标记块，落点项目 CLAUDE.md（官方项目记忆文件；AGENTS.md 为
 * 官方记录的回退），用户手工粘贴（markdown 非 ConfigChange 支持格式，自动写入无合同
 * 通道）。existing 内容不替换（mergePolicyBlock 幂等合同）。
 */
import type { ClientAdapter, ConfigPlan, Detection, LaunchSpec } from '../contracts.js';
/**
 * Claude 的 owned 段：user 作用域 = ~/.claude.json 顶层 mcpServers 下的 umh 条目；
 * project 作用域 = <project>/.mcp.json 的 mcpServers 下的 umh 条目
 * （工单示例测试字面 `mcpServers.umh`；官方 stdio 条目字段 command/args/env，
 * 无 url 的条目按 stdio 读取 —— "type":"stdio" 可省）。
 */
export declare const CLAUDE_OWNED_POINTER = "/mcpServers/umh";
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点项目 CLAUDE.md）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export declare const CLAUDE_POLICY_INSTRUCTIONS: readonly string[];
/**
 * Claude 配方（= docs/clients/recipes/claude.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[manual]=手工路径（未选作用域/local 作用域/非严格 JSON）、
 * [auto]=自动路径附加指引。占位符 {{command}}/{{args}}/{{env}}/{{policy}} 由 preview
 * 用 plan.launch 与模块策略插值。步骤按 2026-10-03 官方文档核实措辞（三作用域表/
 * project 批准提示/CLAUDE.md 记忆文件）。
 */
export declare const CLAUDE_RECIPE: {
    readonly variant: "cli";
    readonly version: "2.1.288";
    readonly officialSource: "https://code.claude.com/docs/en/mcp; settings/hooks/env: https://code.claude.com/docs/en/settings; memory (CLAUDE.md): https://code.claude.com/docs/en/memory";
    readonly verifiedAt: "2026-10-03";
    readonly configLocation: "user scope: top-level mcpServers in ~/.claude.json (official scope table: 'User | All your projects | No | ~/.claude.json'; home-directory files can be relocated with CLAUDE_CONFIG_DIR); project scope: mcpServers in .mcp.json at the project root ('Yes, via version control'; Claude Code prompts for approval before using project-scoped servers); local scope: the per-project mcpServers under projects[\"<project path>\"] in ~/.claude.json ('Local scope is the default') — UMH auto-edits only strict JSON and only the user/project files; the local scope is served by manual steps because the per-project key form cannot be verified without the CLI";
    readonly format: "json";
    readonly ownedPointer: "/mcpServers/umh — a stdio server named 'umh' ({command,args,env}; an entry without a url is read as a stdio server per the official docs) inside the top-level mcpServers object of ~/.claude.json (user scope) or of <project>/.mcp.json (project scope); hooks/env/apiKeyHelper/settings files and the disabled/enabled server lists are never touched, and UMH registers no Claude Code hooks";
    readonly policyLocation: "project CLAUDE.md (official project memory file; AGENTS.md is read as a fallback when no CLAUDE.md exists) — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers";
    readonly manualSteps: readonly ["[manual] Choose the scope explicitly first: user = all your projects (top-level mcpServers in ~/.claude.json), project = shared with the team via version control (.mcp.json in the project root; Claude Code asks for approval in interactive sessions before using project-scoped servers), local = current project only (the per-project mcpServers inside ~/.claude.json). UMH never picks a scope for you — a detection without an explicit scope file is refused with CONFIG_SCOPE_REQUIRED.", "[manual] UMH did not auto-edit a Claude configuration file (no safely auto-editable strict-JSON file was found for the selected scope — it may be missing or not strict JSON, and UMH will not risk losing your fields). Official CLI route: claude mcp add --scope <user|project|local> umh -- <command> <args...> using the exact command and args below; or hand-edit the scope file as described next.", "[manual] The entry to add is exactly: command = {{command}}; args = {{args}}; env = {{env}}. user scope: a stdio server named umh under the top-level mcpServers object of ~/.claude.json (with CLAUDE_CONFIG_DIR set, the relocated state file); project scope: under mcpServers in <project>/.mcp.json; local scope: under the mcpServers of the matching project path inside the projects object of ~/.claude.json. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.", "[manual] Inject the memory policy into the project CLAUDE.md (the official project memory file; Claude Code reads AGENTS.md as a fallback when no CLAUDE.md exists): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[manual] Start a new Claude Code session (servers load at session start) and confirm the umh entry: claude mcp list or /mcp in the REPL; project-scope entries from .mcp.json require interactive approval on first use.", "[manual] Verify: ask Claude Code to run memory_status; do not report onboarding success until the agent actually calls the tool.", "[auto] UMH wrote the umh stdio entry into the explicitly selected scope file (user: top-level mcpServers in ~/.claude.json, or project: mcpServers in <project>/.mcp.json) — a pre-edit backup and a rollback receipt were recorded under the UMH home. Hooks, env maps, the disabled/enabled server lists and every other field were preserved untouched; UMH registers no Claude Code hooks and never touches settings files.", "[auto] Start a new Claude Code session and confirm the umh entry: claude mcp list or /mcp in the REPL (a project-scope .mcp.json entry asks for interactive approval on first use; check the file into version control to share it with the team).", "[auto] Inject the memory policy into the project CLAUDE.md (the official project memory file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}", "[auto] Verify: ask Claude Code to run memory_status; do not report onboarding success until the agent actually calls the tool."];
    readonly schemaValidated: true;
};
/**
 * 构造 Claude 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 claudeAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export declare function makeClaudeAdapter(options?: {
    umhHome?: string;
}): ClientAdapter;
/** Claude 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeClaudeAdapter 绑定）。 */
export declare const claudeAdapter: ClientAdapter;
export type { ClientAdapter, ConfigPlan, Detection, LaunchSpec };
