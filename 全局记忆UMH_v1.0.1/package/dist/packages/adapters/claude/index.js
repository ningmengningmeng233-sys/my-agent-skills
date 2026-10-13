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
import { readFile, stat } from 'node:fs/promises';
import { mkdir } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join } from 'node:path';
import process from 'node:process';
import { UmhError } from '../../contracts/index.js';
import { makeManualPlan, makeOwnedEntryPlan, parseConfigPlan } from '../config-plan.js';
import { buildPolicyBlock } from '../policy-block.js';
import { applyConfigPlan, loadReceiptById, removeOwnedConfig, saveReceipt, umhHomeOfPlan } from '../transaction.js';
/**
 * Claude 的 owned 段：user 作用域 = ~/.claude.json 顶层 mcpServers 下的 umh 条目；
 * project 作用域 = <project>/.mcp.json 的 mcpServers 下的 umh 条目
 * （工单示例测试字面 `mcpServers.umh`；官方 stdio 条目字段 command/args/env，
 * 无 url 的条目按 stdio 读取 —— "type":"stdio" 可省）。
 */
export const CLAUDE_OWNED_POINTER = '/mcpServers/umh';
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点项目 CLAUDE.md）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export const CLAUDE_POLICY_INSTRUCTIONS = [
    '在回答前可调用 memory_status/memory_recall/memory_search/memory_get 读取项目共享记忆；读取失败不得阻塞回答。',
    '涉及用户偏好、项目决策、可复用事实时调用 memory_remember 提交记忆；一次只提交一条、正文自含。',
    '绝不把凭据、密钥或认证材料写入记忆正文。',
];
/**
 * Claude 配方（= docs/clients/recipes/claude.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[manual]=手工路径（未选作用域/local 作用域/非严格 JSON）、
 * [auto]=自动路径附加指引。占位符 {{command}}/{{args}}/{{env}}/{{policy}} 由 preview
 * 用 plan.launch 与模块策略插值。步骤按 2026-10-03 官方文档核实措辞（三作用域表/
 * project 批准提示/CLAUDE.md 记忆文件）。
 */
export const CLAUDE_RECIPE = {
    variant: 'cli',
    version: '2.1.288',
    officialSource: 'https://code.claude.com/docs/en/mcp; settings/hooks/env: https://code.claude.com/docs/en/settings; memory (CLAUDE.md): https://code.claude.com/docs/en/memory',
    verifiedAt: '2026-10-03',
    configLocation: "user scope: top-level mcpServers in ~/.claude.json (official scope table: 'User | All your projects | No | ~/.claude.json'; home-directory files can be relocated with CLAUDE_CONFIG_DIR); project scope: mcpServers in .mcp.json at the project root ('Yes, via version control'; Claude Code prompts for approval before using project-scoped servers); local scope: the per-project mcpServers under projects[\"<project path>\"] in ~/.claude.json ('Local scope is the default') — UMH auto-edits only strict JSON and only the user/project files; the local scope is served by manual steps because the per-project key form cannot be verified without the CLI",
    format: 'json',
    ownedPointer: "/mcpServers/umh — a stdio server named 'umh' ({command,args,env}; an entry without a url is read as a stdio server per the official docs) inside the top-level mcpServers object of ~/.claude.json (user scope) or of <project>/.mcp.json (project scope); hooks/env/apiKeyHelper/settings files and the disabled/enabled server lists are never touched, and UMH registers no Claude Code hooks",
    policyLocation: "project CLAUDE.md (official project memory file; AGENTS.md is read as a fallback when no CLAUDE.md exists) — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers",
    manualSteps: [
        '[manual] Choose the scope explicitly first: user = all your projects (top-level mcpServers in ~/.claude.json), project = shared with the team via version control (.mcp.json in the project root; Claude Code asks for approval in interactive sessions before using project-scoped servers), local = current project only (the per-project mcpServers inside ~/.claude.json). UMH never picks a scope for you — a detection without an explicit scope file is refused with CONFIG_SCOPE_REQUIRED.',
        '[manual] UMH did not auto-edit a Claude configuration file (no safely auto-editable strict-JSON file was found for the selected scope — it may be missing or not strict JSON, and UMH will not risk losing your fields). Official CLI route: claude mcp add --scope <user|project|local> umh -- <command> <args...> using the exact command and args below; or hand-edit the scope file as described next.',
        '[manual] The entry to add is exactly: command = {{command}}; args = {{args}}; env = {{env}}. user scope: a stdio server named umh under the top-level mcpServers object of ~/.claude.json (with CLAUDE_CONFIG_DIR set, the relocated state file); project scope: under mcpServers in <project>/.mcp.json; local scope: under the mcpServers of the matching project path inside the projects object of ~/.claude.json. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.',
        '[manual] Inject the memory policy into the project CLAUDE.md (the official project memory file; Claude Code reads AGENTS.md as a fallback when no CLAUDE.md exists): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}',
        '[manual] Start a new Claude Code session (servers load at session start) and confirm the umh entry: claude mcp list or /mcp in the REPL; project-scope entries from .mcp.json require interactive approval on first use.',
        '[manual] Verify: ask Claude Code to run memory_status; do not report onboarding success until the agent actually calls the tool.',
        '[auto] UMH wrote the umh stdio entry into the explicitly selected scope file (user: top-level mcpServers in ~/.claude.json, or project: mcpServers in <project>/.mcp.json) — a pre-edit backup and a rollback receipt were recorded under the UMH home. Hooks, env maps, the disabled/enabled server lists and every other field were preserved untouched; UMH registers no Claude Code hooks and never touches settings files.',
        '[auto] Start a new Claude Code session and confirm the umh entry: claude mcp list or /mcp in the REPL (a project-scope .mcp.json entry asks for interactive approval on first use; check the file into version control to share it with the team).',
        '[auto] Inject the memory policy into the project CLAUDE.md (the official project memory file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}',
        '[auto] Verify: ask Claude Code to run memory_status; do not report onboarding success until the agent actually calls the tool.',
    ],
    schemaValidated: true,
};
const RECEIPT_ID_PATTERN = /^rcpt_[0-9a-f]{32}$/;
// ---------------------------------------------------------------------------
// 小原语
// ---------------------------------------------------------------------------
async function pathExists(path) {
    try {
        await stat(path);
        return true;
    }
    catch {
        return false;
    }
}
/** 严格 JSON 对象判定（注释/数组/标量都算"非严格对象"）。 */
function isStrictJsonObject(text) {
    try {
        const parsed = JSON.parse(text);
        return parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed);
    }
    catch {
        return false;
    }
}
/**
 * 自动编辑资格：文件缺失（即将新建，A01 事务支持）→ true；
 * 存在且为严格 JSON 对象 → true；JSONC/解析失败 → false（手工片段）。
 */
async function autoEditable(path) {
    let bytes;
    try {
        bytes = await readFile(path);
    }
    catch (err) {
        if (err.code === 'ENOENT')
            return true;
        throw new UmhError('CONFIG_PATH_INVALID', `claude config path is not readable: ${path}`);
    }
    return isStrictJsonObject(bytes.toString('utf8'));
}
/** Windows 文件系统大小写不敏感的 basename 归一键。 */
function basenameKey(path) {
    const name = basename(path);
    return process.platform === 'win32' ? name.toLowerCase() : name;
}
/**
 * 显式 configPath 的作用域解析（工单"不明 scope 返回 CONFIG_SCOPE_REQUIRED"）：
 * basename `.mcp.json` → project；basename `.claude.json`（含 CLAUDE_CONFIG_DIR 迁址
 * 形态）→ user；其它形态 → CONFIG_SCOPE_REQUIRED（绝不猜）。local 作用域与 user 共用
 * 同一状态文件且无 auto 路径（见模块头），故状态文件形态确定性地解析为 user 作用域。
 */
function scopeOfConfigPath(path) {
    const name = basenameKey(path);
    if (name === '.mcp.json')
        return 'project';
    if (name === '.claude.json')
        return 'user';
    throw new UmhError('CONFIG_SCOPE_REQUIRED', `the explicit config path does not name a Claude scope ('.claude.json' = user scope, '.mcp.json' = project scope): ${path}`);
}
/**
 * 官方文档化的状态文件：`~/.claude.json`（user/local 作用域共用载体；CLAUDE_CONFIG_DIR
 * 迁址："Claude Code then stores your settings, session history, and plugins there
 * instead" → $CLAUDE_CONFIG_DIR/.claude.json；非绝对路径忽略，绝不按任意 cwd 解析）。
 * 仅用于 installed 证据 —— 自动写入目标恒为显式 configPath。
 */
function stateFilePath(home) {
    const env = process.env.CLAUDE_CONFIG_DIR;
    const root = typeof env === 'string' && env.trim() !== '' && isAbsolute(env) ? env : home;
    return join(root, '.claude.json');
}
/** recipe 步骤模板插值：GUI/手工复制字段与 plan.launch 严格同源（A01 头部合同）。 */
function interpolateStep(step, launch, policyBlock) {
    return step
        .replaceAll('{{command}}', launch.command)
        .replaceAll('{{args}}', JSON.stringify(launch.args))
        .replaceAll('{{env}}', JSON.stringify(launch.env))
        .replaceAll('{{policy}}', policyBlock);
}
/** 取指定节标签的 recipe 步骤（剥标签、插值）。 */
function stepsFor(tag, launch) {
    const prefix = `[${tag}] `;
    const policyBlock = buildPolicyBlock(CLAUDE_POLICY_INSTRUCTIONS);
    return CLAUDE_RECIPE.manualSteps
        .filter((step) => step.startsWith(prefix))
        .map((step) => interpolateStep(step.slice(prefix.length), launch, policyBlock));
}
function requireClaudeDetection(detection) {
    if (detection === null || typeof detection !== 'object' || detection.clientId !== 'claude') {
        throw new UmhError('INVALID_ARGUMENT', 'claude adapter requires a detection for clientId claude');
    }
}
function requireReceiptId(input) {
    const receiptId = input?.receiptId;
    if (typeof receiptId !== 'string' || !RECEIPT_ID_PATTERN.test(receiptId)) {
        throw new UmhError('RECEIPT_INVALID', `claude adapter requires a rcpt_<32hex> receipt id: ${String(receiptId)}`);
    }
    return receiptId;
}
// ---------------------------------------------------------------------------
// detect（只读：stat + 严格 JSON 判读；绝不写盘、绝不读聊天/记忆/凭据内容）
// ---------------------------------------------------------------------------
async function detectCli(home, configPath) {
    if (typeof configPath === 'string' && configPath.trim() !== '') {
        // 显式选择：路径形态唯一命名作用域（不明形态 → CONFIG_SCOPE_REQUIRED，绝不猜）。
        scopeOfConfigPath(configPath);
        const exists = await pathExists(configPath);
        let verified = false;
        if (exists) {
            try {
                verified = isStrictJsonObject(await readFile(configPath, 'utf8'));
            }
            catch {
                verified = false;
            }
        }
        return {
            clientId: 'claude',
            variant: 'cli',
            version: null,
            // 安装证据：被选文件在场，或官方状态文件在场（project 作用域的 .mcp.json 可以
            // 尚未创建而 Claude Code 已安装）。
            installed: exists || (await pathExists(stateFilePath(home))),
            configPaths: [configPath],
            format: 'json',
            state: verified ? 'verified' : 'unverified',
        };
    }
    // 发现模式：只报告安装证据（官方状态文件存在性，CLAUDE_CONFIG_DIR 迁址感知），
    // configPaths 恒 [] —— 绝不点名作用域（一个 ~/.claude.json 承载 user+local 两个
    // 作用域，"存在一个文件"不构成选择）；preview 对空/多候选抛 CONFIG_SCOPE_REQUIRED。
    const installed = await pathExists(stateFilePath(home));
    return {
        clientId: 'claude',
        variant: 'cli',
        version: null,
        installed,
        configPaths: [],
        format: 'json',
        state: 'unverified',
    };
}
// ---------------------------------------------------------------------------
// adapter 工厂（claudeAdapter = 缺省单例；remove 需要绑定 umhHome 时用工厂）
// ---------------------------------------------------------------------------
/** 缺省 umhHome 解析：UMH_HOME 环境变量（connect CLI 同款约定；绝对路径才接受）。 */
function umhHomeFromEnv() {
    const value = process.env.UMH_HOME;
    return typeof value === 'string' && value.trim() !== '' && isAbsolute(value) ? value : null;
}
/**
 * 构造 Claude 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 claudeAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export function makeClaudeAdapter(options) {
    const boundHome = typeof options?.umhHome === 'string' && options.umhHome.trim() !== '' ? options.umhHome : null;
    async function resolveRemoveHome() {
        const home = boundHome ?? umhHomeFromEnv();
        if (home === null) {
            throw new UmhError('INVALID_ARGUMENT', 'claude adapter remove needs a UMH home: construct via makeClaudeAdapter({ umhHome }) or set UMH_HOME (never guessed)');
        }
        return home;
    }
    return {
        async detect(opts) {
            if (opts === null || typeof opts !== 'object' || typeof opts.home !== 'string' || opts.home.trim() === '') {
                throw new UmhError('INVALID_ARGUMENT', 'detect requires a home directory');
            }
            // 工单冻结 variant=cli（Claude Code 即 CLI 形态；Claude Desktop 是另一应用，非本
            // 工单范围）：未知变体响亮拒绝，绝不静默降级（A03-A05 先例）。
            if (opts.variant !== 'cli') {
                throw new UmhError('INVALID_ARGUMENT', `claude variant must be 'cli': ${String(opts.variant)}`);
            }
            return detectCli(opts.home, opts.configPath);
        },
        async preview(input) {
            requireClaudeDetection(input?.detection);
            const { detection, binding, launch } = input;
            if (detection.variant !== 'cli') {
                throw new UmhError('INVALID_ARGUMENT', `claude variant must be 'cli': ${String(detection.variant)}`);
            }
            const paths = Array.isArray(detection.configPaths) ? detection.configPaths : [];
            // scope 显式选择（工单冻结）：空（发现模式未选）/多候选（外来检出）→
            // CONFIG_SCOPE_REQUIRED —— 绝不全局默认（user/project/local 必须人来定）。
            if (paths.length !== 1) {
                throw new UmhError('CONFIG_SCOPE_REQUIRED', `claude connect needs an explicitly selected scope file (${paths.length} candidates given): ` +
                    "run detect with configPath = the state file (~/.claude.json, user scope) or <project>/.mcp.json (project scope); " +
                    'the local scope is served by manual steps (claude mcp add --scope local). UMH never defaults to a global scope.');
            }
            const path = paths[0];
            const scope = scopeOfConfigPath(path);
            void scope; // 作用域只决定文档语义（文件形态一致）；写入面由显式路径唯一确定。
            // 伪造检出（格式与官方文档化 JSON 矛盾）不能推进写入路径。
            if (detection.format !== 'json') {
                throw new UmhError('INVALID_ARGUMENT', `claude detection must report format 'json' (got '${String(detection.format)}'); a fabricated detection cannot drive JSON owned-pointer edits`);
            }
            if (!(await autoEditable(path))) {
                // 非严格 JSON（注释/损坏）：JSON 往返无法证明用户内容幸存 → 手工片段，
                // 绝不破坏性 stringify。
                return makeManualPlan('claude', binding, launch, stepsFor('manual', launch));
            }
            // 严格 JSON（或缺席即将新建）：A01 owned 段计划（只动 /mcpServers/umh，
            // hooks/env/键序/第三方字段绝不过写；OWNED_POINTER_OCCUPIED 守卫继承 A01）。
            const plan = await makeOwnedEntryPlan({ ...detection, configPaths: [path], format: 'json' }, CLAUDE_OWNED_POINTER, launch, binding);
            // 自动路径附加指引（重启/批准/策略块/核验）——规则只进 owned policy 块，逐字来自 recipe。
            return { ...plan, manualInstructions: stepsFor('auto', launch) };
        },
        async apply(plan) {
            const parsed = parseConfigPlan(plan);
            if (parsed.clientId !== 'claude') {
                throw new UmhError('PLAN_INVALID', `claude apply refuses plans for another client: ${parsed.clientId}`);
            }
            // 全新安装（beforeHash null）时目标父目录可能不存在（如迁址后的 CLAUDE_CONFIG_DIR
            // 目录）：A01 publishAtomic 的同目录临时文件写需要父目录在场；空目录创建是幂等
            // 物化步骤，不触碰任何既有字节（A05 先例）。
            for (const change of parsed.changes) {
                await stat(change.path).catch((err) => {
                    if (err.code === 'ENOENT')
                        return mkdir(dirname(change.path), { recursive: true });
                    return undefined; // 其它 stat 异常交由 applyConfigPlan 的既有检查响亮失败
                });
            }
            // applyConfigPlan（A01 事务）内部自带 --umh-home 契约；这里同源提取用于回执落盘。
            const umhHome = umhHomeOfPlan(parsed);
            const receipt = await applyConfigPlan(parsed);
            await saveReceipt(umhHome, receipt);
            // applied =「本次发布过内容」（A01 published 语义：幂等跳过不计；手工计划恒 false）。
            return { receiptId: receipt.id, applied: receipt.published };
        },
        async validate(input) {
            requireReceiptId(input);
            // 诚实三态：适配层未做真实连通性探针（A09 才取得真实 Claude Code 证据）；
            // agent 未实际调用工具前 sharedReadWrite 恒为 NOT_RUN（工单明示）。
            return { connection: 'UNVERIFIED', sharedReadWrite: 'NOT_RUN', policy: 'UNVERIFIED' };
        },
        async remove(input) {
            const receiptId = requireReceiptId(input);
            const receipt = await loadReceiptById(await resolveRemoveHome(), receiptId);
            // 手工计划回执（未选作用域/local/非严格 JSON 的手工路径，changes 恒空）：UMH 从未
            // 写过任何客户端配置字节，没有可自动移除的磁盘足迹 —— removed:false + 手工清理
            // 指引（cherry 先例：声称自动移除成功即为误报），绝不谎报 removed:true 零动作。
            if (receipt.changes.length === 0) {
                return {
                    removed: false,
                    conflicts: [
                        'claude manual onboarding: this receipt recorded no UMH-written config changes, so there is nothing to auto-remove (removed stays false; claiming automated success would be a false report).',
                        'Manual cleanup: delete the hand-added umh server entry from the scope file you chose (user scope: the top-level mcpServers object of ~/.claude.json; project scope: mcpServers in <project>/.mcp.json; local scope: the entry under the project path inside the projects object of ~/.claude.json, or claude mcp remove umh).',
                        'Manual cleanup: delete the whole UMH MEMORY POLICY block (BEGIN to END markers) from the project CLAUDE.md (or your AGENTS.md fallback).',
                        'If the trusted binding file was created via umh connect apply, revoke it with: umh disconnect --receipt <receipt.json> (it lives in <umhHome>/config/bindings/ and has no Claude-side counterpart).',
                    ],
                };
            }
            // 三方比对（A01 事务层，JSON 原生支持）：用户后改 owned 段 → conflict 不触碰；
            // 无关编辑（含 hooks/env）幸存；UMH 新建且未动的文件 → 整文件移除。
            return removeOwnedConfig(receipt);
        },
    };
}
/** Claude 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeClaudeAdapter 绑定）。 */
export const claudeAdapter = makeClaudeAdapter();
