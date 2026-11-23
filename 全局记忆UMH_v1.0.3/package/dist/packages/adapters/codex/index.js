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
import { createHash, randomBytes } from 'node:crypto';
import { lstat, mkdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join } from 'node:path';
import process from 'node:process';
import { parse as tomlParse } from 'smol-toml';
import { UmhError } from '../../contracts/index.js';
import { deepEqual, getPointer, makeManualPlan, parseConfigPlan, sha256Hex } from '../config-plan.js';
import { buildPolicyBlock } from '../policy-block.js';
import { applyConfigPlan, loadReceiptById, saveReceipt, umhHomeOfPlan } from '../transaction.js';
/**
 * Codex 的 owned 段：user 级 config.toml 的 `[mcp_servers.umh]` 表
 * （工单明示 pointer=`mcp_servers.umh`；官方 stdio 字段 command/args/env）。
 */
export const CODEX_OWNED_POINTER = '/mcp_servers/umh';
/** owned 段表头（TOML 文本级定位用）。 */
const OWNED_TABLE_HEADER = '[mcp_servers.umh]';
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点工作区指令文件）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export const CODEX_POLICY_INSTRUCTIONS = [
    '在回答前可调用 memory_status/memory_recall/memory_search/memory_get 读取项目共享记忆；读取失败不得阻塞回答。',
    '涉及用户偏好、项目决策、可复用事实时调用 memory_remember 提交记忆；一次只提交一条、正文自含。',
    '绝不把凭据、密钥或认证材料写入记忆正文。',
];
/**
 * Codex 配方（= docs/clients/recipes/codex.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[manual]=手工路径（配置缺失/非有效 TOML）、[auto]=自动
 * 路径附加指引。两节共享于 cli|desktop 双变体（官方单一配置面）。占位符
 * {{command}}/{{args}}/{{env}}/{{policy}} 由 preview 用 plan.launch 与模块策略插值。
 * 步骤按 2026-10-03 官方文档核实措辞（desktop 设置流/`/mcp`/enabled 子键/共享配置句）。
 */
export const CODEX_RECIPE = {
    variant: 'cli|desktop',
    version: '0.160.0',
    officialSource: 'https://developers.openai.com/codex/mcp/ (308 -> https://learn.chatgpt.com/docs/extend/mcp?surface=cli); config: https://learn.chatgpt.com/docs/config-file/config-basic; https://learn.chatgpt.com/docs/config-file/config-reference; https://learn.chatgpt.com/docs/config-file/config-advanced',
    verifiedAt: '2026-10-03',
    configLocation: "user-level ~/.codex/config.toml (official: 'Codex stores its local state under CODEX_HOME (defaults to ~/.codex)'; a non-absolute CODEX_HOME is ignored by UMH); official MCP docs: 'The ChatGPT desktop app, Codex CLI, and IDE extension share this configuration'; project-level .codex/config.toml is trusted-projects-only and per-project — UMH never edits it",
    format: 'toml',
    ownedPointer: '/mcp_servers/umh — a [mcp_servers.umh] TOML table with command/args/env (env in the official inline form, e.g. env = {}); auto-edit appends or removes only this section via tested section editing + parse validation (never whole-file stringify; unrelated tables, keys and comments are preserved byte-for-byte)',
    policyLocation: "workspace agent-instruction file (AGENTS.md in this workspace; the official pages reviewed for this recipe do not document an instruction-file convention — verify against your own setup) — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers",
    manualSteps: [
        '[manual] UMH did not auto-edit the Codex TOML config (no safely auto-editable ~/.codex/config.toml was found — it may be missing, live under a custom CODEX_HOME, or not be valid TOML; UMH will not risk losing your settings or comments). Desktop route: ChatGPT desktop app → Settings → MCP servers → Add server, name umh-memory, type STDIO, provide the command below, Save, then Restart (official setup flow). CLI route: codex mcp add umh-memory -- <command...>, or hand-edit config.toml as below.',
        '[manual] Fill the entry exactly: command = {{command}}; args = {{args}}; env = {{env}}. In TOML this is one table: [mcp_servers.umh] with command/args/env keys (env uses the official inline form, e.g. env = {}). UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.',
        "[manual] Scope: prefer the user-level ~/.codex/config.toml — the official docs state 'The ChatGPT desktop app, Codex CLI, and IDE extension share this configuration', so one entry covers all of them. Project-level .codex/config.toml is loaded for trusted projects only and is per-project; UMH never edits it.",
        '[manual] Inject the memory policy: paste the following block into the workspace agent-instruction file you keep (AGENTS.md in this workspace; the official pages reviewed for this recipe do not document an instruction-file convention — verify against your own setup), keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}',
        '[manual] Restart or start a new Codex session and confirm the umh-memory server shows connected (type /mcp in the TUI; the desktop app lists connected MCP servers in its settings).',
        '[manual] Verify: ask Codex to run memory_status; do not report onboarding success until the agent actually calls the tool.',
        '[auto] UMH appended the [mcp_servers.umh] section to the user-level ~/.codex/config.toml via tested section editing (a pre-edit backup and a rollback receipt were recorded under the UMH home; unrelated tables, keys and comments were preserved byte-for-byte). The official docs state \'The ChatGPT desktop app, Codex CLI, and IDE extension share this configuration\', so one entry covers all of them.',
        "[auto] Confirm the umh-memory entry is enabled in your client: the official config reference documents an `enabled` sub-key for [mcp_servers.*] tables, and UMH never writes one — if you disabled umh yourself, UMH's overwrite guard refuses to clobber your choice on re-install.",
        '[auto] Inject the memory policy: paste the following block into the workspace agent-instruction file you keep (AGENTS.md in this workspace; the official pages reviewed for this recipe do not document an instruction-file convention — verify against your own setup), keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}',
        '[auto] Restart or start a new Codex session, confirm the server shows connected (/mcp in the TUI or the desktop MCP list), then ask the agent to run memory_status; do not report onboarding success until the agent actually calls the tool.',
    ],
    schemaValidated: true,
};
const RECEIPT_ID_PATTERN = /^rcpt_[0-9a-f]{32}$/;
const VARIANTS = new Set(['cli', 'desktop']);
// ---------------------------------------------------------------------------
// TOML 段落编辑原语（文本级；每次编辑后 parse 校验 + 语义 diff，见模块头）
// ---------------------------------------------------------------------------
/** TOML 基本字符串（双引号，官方示例形态）：`\` `"` 与控制字符转义；中文/空格原样。 */
function tomlBasicString(value) {
    let out = '"';
    for (const ch of value) {
        if (ch === '\\')
            out += '\\\\';
        else if (ch === '"')
            out += '\\"';
        else if (ch === '\b')
            out += '\\b';
        else if (ch === '\t')
            out += '\\t';
        else if (ch === '\n')
            out += '\\n';
        else if (ch === '\f')
            out += '\\f';
        else if (ch === '\r')
            out += '\\r';
        else {
            const code = ch.codePointAt(0);
            if (code < 0x20 || code === 0x7f)
                out += `\\u${code.toString(16).padStart(4, '0')}`;
            else
                out += ch;
        }
    }
    return `${out}"`;
}
/** launch 对象在客户端配置中的落地形态（与 LaunchSpec 同源，字段一一对应）。 */
function launchEntryOf(launch) {
    return { command: launch.command, args: [...launch.args], env: { ...launch.env } };
}
/** canonical owned 段（段内零注释；键序 command/args/env 对齐官方参考表）。 */
function canonicalOwnedSection(entry) {
    const envItems = Object.keys(entry.env).map((k) => `${tomlBasicString(k)} = ${tomlBasicString(entry.env[k])}`);
    const lines = [
        OWNED_TABLE_HEADER,
        `command = ${tomlBasicString(entry.command)}`,
        `args = [${entry.args.map(tomlBasicString).join(', ')}]`,
        `env = {${envItems.length === 0 ? '' : ` ${envItems.join(', ')} `}}`,
    ];
    return `${lines.join('\n')}\n`;
}
/** 逐行扫描（保留原始字节偏移，绝不归一化行尾 —— 无关区域字节级保真）。 */
function scanLines(raw) {
    const lines = [];
    let i = 0;
    while (i < raw.length) {
        let end = raw.indexOf('\n', i);
        end = end === -1 ? raw.length : end + 1;
        let textEnd = end;
        if (textEnd > i && raw[textEnd - 1] === '\n')
            textEnd -= 1;
        if (textEnd > i && raw[textEnd - 1] === '\r')
            textEnd -= 1;
        lines.push({ start: i, end, text: raw.slice(i, textEnd) });
        i = end;
    }
    return lines;
}
/** `[mcp_servers.umh]` 表头行（容忍括号内空白与引号包裹的段名）。 */
const OWNED_HEADER = /^\s*\[\s*mcp_servers\s*\.\s*(?:"umh"|'umh'|umh)\s*\]\s*$/;
/** 任意表头行（[x] / [[x]]），捕获键路径。 */
const ANY_HEADER = /^\s*\[\[?\s*([^\]]*?)\s*\]?\]\s*$/;
/**
 * 定位 owned 段的字节区间 [start, end)：从表头行起，到下一个"非 owned 子表"表头行
 * 止（`mcp_servers.umh.*` 子表属于本段）。找不到表头 → null。
 */
function findOwnedSection(raw) {
    const lines = scanLines(raw);
    let headerIdx = -1;
    for (let i = 0; i < lines.length; i++) {
        if (OWNED_HEADER.test(lines[i].text)) {
            headerIdx = i;
            break;
        }
    }
    if (headerIdx < 0)
        return null;
    let endIdx = lines.length;
    for (let i = headerIdx + 1; i < lines.length; i++) {
        const m = ANY_HEADER.exec(lines[i].text);
        if (!m)
            continue;
        const key = m[1].replace(/[\s"']/g, '');
        if (key.startsWith('mcp_servers.umh.'))
            continue; // owned 子表，仍在本段内
        endIdx = i;
        break;
    }
    const start = lines[headerIdx].start;
    const end = endIdx < lines.length ? lines[endIdx].start : raw.length;
    return { start, end };
}
/**
 * owned 段文本是否与 UMH 的 canonical 序列化逐字一致（行尾归一化后比对）。
 *
 * UMH 写入的段永远是 canonicalOwnedSection(entry) 的逐字拷贝（行尾随文件风格）。
 * 任何分歧 —— 行尾注释、段内注释、增删键、空行、重排格式 —— 都是用户内容混入或
 * 用户改动：注释/空白不进解析值，值级三方比对会判"未改动"，唯独段文本能证明它。
 * 该单一检查统一承担卸载侧的用户内容守卫（A05 修复轮 Important-1：取代原"段内
 * 无注释行"守卫，补上其行尾注释盲区，两种混入形态行为归一）→ 分歧即 CONFIG_CHANGED
 * 不触碰。env 键序取自回执 afterOwned 的 JSON 序（= 安装时写出序），重建文本与
 * 安装字节一致，不产生假冲突。
 */
function sectionTextIsCanonical(raw, section, entry) {
    const text = raw.slice(section.start, section.end).replace(/\r\n/g, '\n');
    return text === canonicalOwnedSection(entry);
}
/** 追加 owned 段：已有字节逐字保留；缺行尾补行尾 + 一个空行分隔；行尾跟随文件风格。 */
function insertOwnedSection(raw, section) {
    if (raw === '')
        return section;
    const eol = raw.includes('\r\n') ? '\r\n' : '\n';
    const base = raw.endsWith('\n') ? raw : raw + eol;
    return base + eol + section.replace(/\n/g, eol);
}
/** 切除 owned 段：仅移除该段字节（含段前紧邻的安装分隔空行）；其余字节原样。 */
function cutOwnedSection(raw, section) {
    const before = raw.slice(0, section.start);
    const lines = scanLines(before);
    let start = section.start;
    for (let i = lines.length - 1; i >= 0; i--) {
        if (lines[i].text.trim() !== '')
            break;
        start = lines[i].start;
        break; // 安装恰好留一个分隔空行：只吞这一个，用户自己的空行原地幸存（A05 修复轮 Minor）
    }
    return raw.slice(0, start) + raw.slice(section.end);
}
/** 解析 TOML 文档（必须为表）；失败 CONFIG_UNPARSEABLE。 */
function parseTomlDoc(text, path) {
    let doc;
    try {
        doc = tomlParse(text);
    }
    catch (err) {
        throw new UmhError('CONFIG_UNPARSEABLE', `codex config is not valid TOML (${path}): ${err.message}`);
    }
    if (doc === null || typeof doc !== 'object' || Array.isArray(doc)) {
        throw new UmhError('CONFIG_UNPARSEABLE', `codex config is not a TOML table: ${path}`);
    }
    return doc;
}
/** 文档副本，去掉 mcp_servers.umh（其余结构原样保留供语义 diff）。
 * 变空后的 mcp_servers 容器一并去掉：空表不承载用户数据，避免"卸载后文件不再提及
 * mcp_servers"与"显式 [mcp_servers] 空表头幸存"两种文本形态在语义 diff 上假冲突
 * （显式表头的字节幸存由切除边界保证，本函数只做值级语义比较）。 */
function withoutOwned(doc) {
    const clone = { ...doc };
    const ms = clone['mcp_servers'];
    if (ms !== null && typeof ms === 'object' && !Array.isArray(ms)) {
        const msClone = { ...ms };
        delete msClone['umh'];
        if (Object.keys(msClone).length === 0)
            delete clone['mcp_servers'];
        else
            clone['mcp_servers'] = msClone;
    }
    return clone;
}
/**
 * 编辑后不变量（fail-closed，任何一条不满足绝不发布）：
 *   1. after 仍是有效 TOML；
 *   2. owned 段值 = 预期（present）或不存在（absent）；
 *   3. 语义 diff：before/after 去掉 owned 段后 deepEqual（首次新建无 before，跳过 3）。
 */
function requireTomlEditInvariants(params) {
    const afterDoc = parseTomlDoc(params.afterRaw, params.path);
    const afterOwned = getPointer(afterDoc, CODEX_OWNED_POINTER) ?? null;
    if (params.mode === 'present' && !deepEqual(afterOwned, params.desiredOwned ?? null)) {
        throw new UmhError('PLAN_INVALID', `internal invariant violation: post-edit TOML does not carry the desired owned entry (${params.path})`);
    }
    if (params.mode === 'absent' && afterOwned !== null) {
        throw new UmhError('PLAN_INVALID', `internal invariant violation: post-edit TOML still carries the owned entry (${params.path})`);
    }
    if (params.beforeRaw !== null) {
        const beforeDoc = parseTomlDoc(params.beforeRaw, params.path);
        if (!deepEqual(withoutOwned(beforeDoc), withoutOwned(afterDoc))) {
            throw new UmhError('PLAN_INVALID', `internal invariant violation: post-edit TOML does not preserve unrelated content (${params.path})`);
        }
    }
}
// ---------------------------------------------------------------------------
// 小原语
// ---------------------------------------------------------------------------
function sha256Buffer(bytes) {
    return createHash('sha256').update(bytes).digest('hex');
}
async function pathExists(path) {
    try {
        await stat(path);
        return true;
    }
    catch {
        return false;
    }
}
/** 严格 TOML 表判定（解析成功即有效；TOML 根恒为表）。 */
function isValidToml(text) {
    try {
        parseTomlDoc(text, '<probe>');
        return true;
    }
    catch {
        return false;
    }
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
    const policyBlock = buildPolicyBlock(CODEX_POLICY_INSTRUCTIONS);
    return CODEX_RECIPE.manualSteps
        .filter((step) => step.startsWith(prefix))
        .map((step) => interpolateStep(step.slice(prefix.length), launch, policyBlock));
}
function requireCodexDetection(detection) {
    if (detection === null || typeof detection !== 'object' || detection.clientId !== 'codex') {
        throw new UmhError('INVALID_ARGUMENT', 'codex adapter requires a detection for clientId codex');
    }
}
function requireVariant(variant) {
    // variant 决定写入路径（cli/desktop 共享同一官方文档化配置面）：未知变体响亮拒绝，
    // 绝不静默降级（静默手工会掩盖笔误，A03/A04 先例）。
    if (!VARIANTS.has(variant)) {
        throw new UmhError('INVALID_ARGUMENT', `codex variant must be 'cli' or 'desktop': ${String(variant)}`);
    }
}
function requireReceiptId(input) {
    const receiptId = input?.receiptId;
    if (typeof receiptId !== 'string' || !RECEIPT_ID_PATTERN.test(receiptId)) {
        throw new UmhError('RECEIPT_INVALID', `codex adapter requires a rcpt_<32hex> receipt id: ${String(receiptId)}`);
    }
    return receiptId;
}
// ---------------------------------------------------------------------------
// detect（只读：stat + TOML 有效性判读；绝不写盘、绝不读聊天/记忆内容）
// ---------------------------------------------------------------------------
/**
 * 官方文档化的 user 级配置根：CODEX_HOME（绝对路径时）?? `<home>/.codex`
 * （官方："Codex stores its local state under CODEX_HOME (defaults to ~/.codex)"；
 * 非绝对路径忽略 —— 绝不按任意 cwd 解析相对路径）。
 */
function codexConfigPath(home) {
    const env = process.env.CODEX_HOME;
    const root = typeof env === 'string' && env.trim() !== '' && isAbsolute(env) ? env : join(home, '.codex');
    return join(root, 'config.toml');
}
/**
 * 发现模式只列官方已记录的 user 级候选（CODEX_HOME/config.toml）；installed 只认该
 * 候选（桌面 app 安装目录官方未文档化为安装证据，不探测 —— A03"裸目录不构成安装
 * 证据"纪律）。显式 opts.configPath = 独占选择（每目标独立 plan/apply）。
 */
async function detectShared(home, variant, configPath) {
    const documentedPath = codexConfigPath(home);
    if (typeof configPath === 'string' && configPath.trim() !== '') {
        const selected = configPath;
        const exists = await pathExists(selected);
        let verified = false;
        if (exists) {
            try {
                verified = isValidToml((await readFile(selected, 'utf8')));
            }
            catch {
                verified = false;
            }
        }
        return {
            clientId: 'codex',
            variant,
            version: null,
            installed: exists || (await pathExists(documentedPath)),
            configPaths: [selected],
            format: 'toml',
            state: verified ? 'verified' : 'unverified',
        };
    }
    const configPaths = (await pathExists(documentedPath)) ? [documentedPath] : [];
    let state = 'unverified';
    if (configPaths.length === 1) {
        try {
            state = isValidToml(await readFile(configPaths[0], 'utf8')) ? 'verified' : 'unverified';
        }
        catch {
            state = 'unverified';
        }
    }
    return { clientId: 'codex', variant, version: null, installed: configPaths.length > 0, configPaths, format: 'toml', state };
}
// ---------------------------------------------------------------------------
// preview（只读；TOML 段落编辑计划）
// ---------------------------------------------------------------------------
/**
 * 构造 owned 段变更：文件缺失 → 新建；有效 TOML 且 owned 段缺席 → 末尾追加；
 * owned 段值已等于预期 → 幂等（afterBytes = 当前字节）；值不同 →
 * OWNED_POINTER_OCCUPIED（绝不覆盖第三方/用户内容，含 enabled=false 自禁）；
 * 非有效 TOML → 调用方降级手工片段。
 */
async function planTomlChange(path, launch) {
    const desired = launchEntryOf(launch);
    let bytes;
    try {
        bytes = await readFile(path);
    }
    catch (err) {
        if (err.code === 'ENOENT') {
            // 全新文件：owned 段即全部内容（无无关内容可失）。
            const afterBytes = canonicalOwnedSection(desired);
            return {
                path,
                format: 'toml',
                beforeHash: null,
                afterHash: sha256Hex(afterBytes),
                ownedPointer: CODEX_OWNED_POINTER,
                beforeOwned: null,
                afterOwned: desired,
                afterBytes,
            };
        }
        throw new UmhError('CONFIG_PATH_INVALID', `codex config path is not readable: ${path}`);
    }
    const beforeRaw = bytes.toString('utf8');
    const beforeHash = sha256Buffer(bytes);
    const doc = parseTomlDoc(beforeRaw, path);
    const beforeOwned = getPointer(doc, CODEX_OWNED_POINTER) ?? null;
    let afterBytes;
    if (beforeOwned !== null) {
        if (!deepEqual(beforeOwned, desired)) {
            throw new UmhError('OWNED_POINTER_OCCUPIED', `${path} already has other content at ${CODEX_OWNED_POINTER}; refusing to overwrite (choose another pointer or use manual steps)`);
        }
        afterBytes = beforeRaw; // 幂等：目标状态已在场（apply 命中 afterHash 跳过）
    }
    else {
        if (findOwnedSection(beforeRaw) !== null) {
            // 表头在场但解析无值：非常规布局，宁可拒绝也不赌段落编辑。
            throw new UmhError('OWNED_POINTER_OCCUPIED', `${path} has a ${OWNED_TABLE_HEADER} header that does not parse into ${CODEX_OWNED_POINTER}; refusing to edit (use manual steps)`);
        }
        afterBytes = insertOwnedSection(beforeRaw, canonicalOwnedSection(desired));
    }
    requireTomlEditInvariants({
        path,
        beforeRaw,
        afterRaw: afterBytes,
        mode: 'present',
        desiredOwned: desired,
    });
    return {
        path,
        format: 'toml',
        beforeHash,
        afterHash: sha256Hex(afterBytes),
        ownedPointer: CODEX_OWNED_POINTER,
        beforeOwned,
        afterOwned: desired,
        afterBytes,
    };
}
// ---------------------------------------------------------------------------
// remove（TOML 版三方比对；切除动作复用 A01 applyConfigPlan 的锁/备份/原子发布）
// ---------------------------------------------------------------------------
/** 从回执 afterOwned（{command,args,env}）重建 LaunchSpec（形状不符 RECEIPT_INVALID）。 */
function launchFromOwned(entry) {
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) {
        throw new UmhError('RECEIPT_INVALID', 'receipt change afterOwned must be a launch entry object');
    }
    const e = entry;
    if (typeof e.command !== 'string' || e.command === '') {
        throw new UmhError('RECEIPT_INVALID', 'receipt launch entry command must be a non-empty string');
    }
    if (!Array.isArray(e.args) || e.args.some((a) => typeof a !== 'string')) {
        throw new UmhError('RECEIPT_INVALID', 'receipt launch entry args must be an array of strings');
    }
    if (e.env === null || typeof e.env !== 'object' || Array.isArray(e.env)) {
        throw new UmhError('RECEIPT_INVALID', 'receipt launch entry env must be a Record<string,string>');
    }
    const env = {};
    for (const [k, v] of Object.entries(e.env)) {
        if (typeof v !== 'string')
            throw new UmhError('RECEIPT_INVALID', `receipt launch entry env.${k} must be a string`);
        env[k] = v;
    }
    return { command: e.command, args: [...e.args], env };
}
/** 适配器本地备份（A01 backupOriginal 为私有原语；同目录同命名约定，mode 0o600）。 */
async function backupBytes(path, umhHome, bytes) {
    const dir = join(umhHome, 'backups', 'client-config');
    await mkdir(dir, { recursive: true });
    const stem = sha256Hex(path.split('\\').join('/')).slice(0, 8);
    const backupPath = join(dir, `${basename(path)}.${stem}.${new Date().toISOString().replace(/[:.]/g, '-')}.${randomBytes(4).toString('hex')}.bak`);
    await writeFile(backupPath, bytes, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
}
async function readRawOrNull(path) {
    try {
        return await readFile(path);
    }
    catch (err) {
        if (err.code === 'ENOENT')
            return null;
        throw new UmhError('CONFIG_PATH_INVALID', `codex config path is not readable: ${path}`);
    }
}
/** 从回执 afterOwned（{command,args,env}）提取 --umh-home（锁/备份/备份目录所需）。 */
function umhHomeOfLaunch(launch) {
    const idx = launch.args.indexOf('--umh-home');
    if (idx < 0 || idx + 1 >= launch.args.length || launch.args[idx + 1].trim() === '') {
        throw new UmhError('RECEIPT_INVALID', 'receipt launch entry must carry --umh-home; cannot locate the UMH home for locking and backup');
    }
    return launch.args[idx + 1];
}
/**
 * TOML 回执的三方比对卸载（值级 before/after/current + 段文本 canonical 逐字守卫，
 * A05 修复轮 Important-1）。
 * 切除经临时反转计划走 applyConfigPlan（锁内 beforeHash 复核 → 备份 → 原子发布），
 * 该临时计划即刻丢弃、绝不落回执 —— 卸载没有独立回执，原安装回执保持审计轨迹。
 * UMH 新建且逐字节未动的文件 → 整文件移除（先备份；字节另可从回执 afterBytes 追回）。
 */
async function removeTomlOwned(receipt) {
    const launch = launchFromOwned(receipt.changes[0]?.afterOwned);
    const umhHome = umhHomeOfLaunch(launch);
    const conflicts = [];
    const removalChanges = [];
    const fileRemovals = [];
    for (const change of receipt.changes) {
        if (change.format !== 'toml') {
            conflicts.push(`${change.path}: non-TOML change in a codex receipt (${change.format}); refusing (UNSUPPORTED_FORMAT)`);
            continue;
        }
        let info = null;
        try {
            info = await lstat(change.path);
        }
        catch (err) {
            if (err.code !== 'ENOENT') {
                conflicts.push(`${change.path}: config path is not inspectable (CONFIG_PATH_INVALID)`);
                continue;
            }
        }
        if (info !== null) {
            if (info.isSymbolicLink()) {
                conflicts.push(`${change.path}: config path is a symlink/junction; refusing (CONFIG_SYMLINK)`);
                continue;
            }
            if (!info.isFile()) {
                conflicts.push(`${change.path}: config path is not a regular file (CONFIG_PATH_INVALID)`);
                continue;
            }
        }
        const bytes = await readRawOrNull(change.path);
        if (bytes === null)
            continue; // 文件已不在 → owned 段必然已移除（幂等）
        const raw = bytes.toString('utf8');
        const currentHash = sha256Buffer(bytes);
        let doc;
        try {
            doc = parseTomlDoc(raw, change.path);
        }
        catch (err) {
            conflicts.push(`${change.path}: ${err.message} (CONFIG_UNPARSEABLE)`);
            continue;
        }
        const current = getPointer(doc, CODEX_OWNED_POINTER) ?? null;
        if (deepEqual(current, change.afterOwned)) {
            const section = findOwnedSection(raw);
            if (section === null) {
                conflicts.push(`${change.path}: owned value matches but the ${OWNED_TABLE_HEADER} section cannot be located textually; edit manually (CONFIG_CHANGED)`);
                continue;
            }
            // 卸载守卫（A05 修复轮 Important-1）：段文本 ≠ canonical 序列化（行尾归一化后）
            // = 用户内容混入（行尾/整行注释、增删键、空行、重排格式）→ conflict 不触碰。
            // current 与 afterOwned 已 deepEqual，故以回执 afterOwned 重建 canonical 文本
            // 即 canonicalOwnedSection(current)（env 键序 = 安装时写出序，无假冲突）。
            if (!sectionTextIsCanonical(raw, section, launchEntryOf(launchFromOwned(change.afterOwned)))) {
                conflicts.push(`${change.path}: the owned section text differs from the UMH-written canonical form (user comments or edits inside) — refusing to remove it (CONFIG_CHANGED)`);
                continue;
            }
            // UMH 新建且自安装后逐字节未动 → 整文件移除（A01 语义；先备份留证）。
            if (change.beforeHash === null && currentHash === change.afterHash) {
                fileRemovals.push({ path: change.path, bytes });
                continue;
            }
            const afterRaw = cutOwnedSection(raw, section);
            try {
                requireTomlEditInvariants({ path: change.path, beforeRaw: raw, afterRaw, mode: 'absent' });
            }
            catch (err) {
                conflicts.push(`${change.path}: ${err.message}`);
                continue;
            }
            removalChanges.push({
                path: change.path,
                format: 'toml',
                beforeHash: currentHash,
                afterHash: sha256Hex(afterRaw),
                ownedPointer: change.ownedPointer,
                beforeOwned: current,
                afterOwned: null,
                afterBytes: afterRaw,
            });
        }
        else if (deepEqual(current, change.beforeOwned)) {
            // 用户已还原为安装前状态：owned 段已不在，无需动作。
        }
        else {
            conflicts.push(`${change.path}: owned segment ${change.ownedPointer} was modified after installation (CONFIG_CHANGED)`);
        }
    }
    if (conflicts.length === 0 && removalChanges.length > 0) {
        // 临时反转计划：owned 段 absent 的"安装"。复用 A01 事务的全部纪律
        // （锁内 beforeHash 复核 / 备份 / 原子发布）；计划即刻丢弃，不落回执。
        const plan = {
            id: `plan_${randomBytes(12).toString('hex')}`,
            clientId: 'codex',
            bindingId: receipt.bindingId,
            launch,
            changes: removalChanges,
            manualInstructions: [],
            dryRun: true,
            recipeVersion: 1,
        };
        try {
            await applyConfigPlan(plan);
        }
        catch (err) {
            if (err instanceof UmhError) {
                return { removed: false, conflicts: [`${err.code}: ${err.message}`] };
            }
            throw err;
        }
    }
    if (conflicts.length === 0) {
        for (const removal of fileRemovals) {
            await backupBytes(removal.path, umhHome, removal.bytes);
            await rm(removal.path, { force: false });
        }
    }
    return { removed: conflicts.length === 0, conflicts };
}
// ---------------------------------------------------------------------------
// adapter 工厂（codexAdapter = 缺省单例；remove 需要绑定 umhHome 时用工厂）
// ---------------------------------------------------------------------------
/** 缺省 umhHome 解析：UMH_HOME 环境变量（connect CLI 同款约定；绝对路径才接受）。 */
function umhHomeFromEnv() {
    const value = process.env.UMH_HOME;
    return typeof value === 'string' && value.trim() !== '' && isAbsolute(value) ? value : null;
}
/**
 * 构造 Codex 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 codexAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export function makeCodexAdapter(options) {
    const boundHome = typeof options?.umhHome === 'string' && options.umhHome.trim() !== '' ? options.umhHome : null;
    async function resolveRemoveHome() {
        const home = boundHome ?? umhHomeFromEnv();
        if (home === null) {
            throw new UmhError('INVALID_ARGUMENT', 'codex adapter remove needs a UMH home: construct via makeCodexAdapter({ umhHome }) or set UMH_HOME (never guessed)');
        }
        return home;
    }
    return {
        async detect(opts) {
            if (opts === null || typeof opts !== 'object' || typeof opts.home !== 'string' || opts.home.trim() === '') {
                throw new UmhError('INVALID_ARGUMENT', 'detect requires a home directory');
            }
            requireVariant(opts.variant);
            return detectShared(opts.home, opts.variant, opts.configPath);
        },
        async preview(input) {
            requireCodexDetection(input?.detection);
            const { detection, binding, launch } = input;
            requireVariant(detection.variant);
            const paths = Array.isArray(detection.configPaths) ? detection.configPaths : [];
            // 伪造检出（格式与官方文档化 TOML 矛盾）不能推进写入路径。
            if (paths.length > 0 && detection.format !== 'toml') {
                throw new UmhError('INVALID_ARGUMENT', `codex detection must report format 'toml' (got '${String(detection.format)}'); a fabricated detection cannot drive TOML section edits`);
            }
            // 多候选：绝不隐式选第一项（伪造/外来检出同拒 —— UMH 只认 detect 的独占选择）。
            if (paths.length > 1) {
                throw new UmhError('PROFILE_REQUIRED', `multiple Codex config candidates (${paths.join('; ')}); UMH never picks one automatically — ` +
                    're-run detect with the explicit configPath of the file to connect ' +
                    '(the only documented auto-edit target is the user-level ~/.codex/config.toml)');
            }
            if (paths.length === 0) {
                // 无检出：给已核实的桌面/CLI/手工编辑步骤（创建或编辑 user 级 config.toml）。
                return makeManualPlan('codex', binding, launch, stepsFor('manual', launch));
            }
            const path = paths[0];
            let rawExists = true;
            try {
                await readFile(path);
            }
            catch (err) {
                if (err.code !== 'ENOENT') {
                    throw new UmhError('CONFIG_PATH_INVALID', `codex config path is not readable: ${path}`);
                }
                rawExists = false;
            }
            if (rawExists) {
                const raw = (await readFile(path)).toString('utf8');
                if (!isValidToml(raw)) {
                    // 非有效 TOML：段落编辑无法证明用户内容幸存 → 手工片段，绝不破坏性改写。
                    return makeManualPlan('codex', binding, launch, stepsFor('manual', launch));
                }
            }
            // 有效 TOML（或缺席即将新建）：段落编辑计划（只动 [mcp_servers.umh]，第三方
            // 表/顶部键/profiles/注释绝不过写；OWNED_POINTER_OCCUPIED 守卫在 planTomlChange）。
            const change = await planTomlChange(path, launch);
            const plan = {
                id: `plan_${randomBytes(12).toString('hex')}`,
                clientId: 'codex',
                bindingId: binding.bindingId,
                launch: { command: launch.command, args: [...launch.args], env: { ...launch.env } },
                changes: [change],
                manualInstructions: stepsFor('auto', launch),
                dryRun: true,
                recipeVersion: 1,
            };
            // 冻结形状自检（含 launch 形状、change 字段、绝对路径），fail-closed。
            return parseConfigPlan(plan);
        },
        async apply(plan) {
            const parsed = parseConfigPlan(plan);
            if (parsed.clientId !== 'codex') {
                throw new UmhError('PLAN_INVALID', `codex apply refuses plans for another client: ${parsed.clientId}`);
            }
            // 全新安装（beforeHash null）时目标文件与其父目录可能都不存在（codex 从未运行过）：
            // A01 publishAtomic 原子写在同目录落临时文件，这里先补建缺失的父目录（空目录
            // 创建是幂等的物化步骤，不触碰任何既有字节；目录存在时零操作）。
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
            // 诚实三态：适配层未做真实连通性探针（A09 才取得真实 Codex/desktop 证据，
            // cli/desktop 两变体分别采集）；agent 未实际调用工具前 sharedReadWrite 恒
            // NOT_RUN（工单明示）。
            return { connection: 'UNVERIFIED', sharedReadWrite: 'NOT_RUN', policy: 'UNVERIFIED' };
        },
        async remove(input) {
            const receiptId = requireReceiptId(input);
            const receipt = await loadReceiptById(await resolveRemoveHome(), receiptId);
            // 手工计划回执（配置缺失/非有效 TOML 的手工路径，changes 恒空）：UMH 从未写过
            // 任何客户端配置字节，没有可自动移除的磁盘足迹 —— removed:false + 手工清理
            // 指引（cherry 先例：声称自动移除成功即为误报），绝不谎报 removed:true 零动作。
            if (receipt.changes.length === 0) {
                return {
                    removed: false,
                    conflicts: [
                        'codex manual onboarding: this receipt recorded no UMH-written config changes, so there is nothing to auto-remove (removed stays false; claiming automated success would be a false report).',
                        'Manual cleanup: delete the hand-added [mcp_servers.umh] table from ~/.codex/config.toml (or remove the umh-memory entry in the ChatGPT desktop app under Settings → MCP servers).',
                        'Manual cleanup: delete the whole UMH MEMORY POLICY block (BEGIN to END markers) from the workspace agent-instruction file (AGENTS.md in this workspace).',
                        'If the trusted binding file was created via umh connect apply, revoke it with: umh disconnect --receipt <receipt.json> (it lives in <umhHome>/config/bindings/ and has no Codex-side counterpart).',
                    ],
                };
            }
            // TOML 版三方比对（值级 before/after/current + 段文本 canonical 逐字守卫）；
            // 切除复用 A01 事务纪律。用户后改 owned 段（含行尾注释等解析不可见编辑）→
            // conflict 不触碰；无关编辑幸存。
            return removeTomlOwned(receipt);
        },
    };
}
/** Codex 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeCodexAdapter 绑定）。 */
export const codexAdapter = makeCodexAdapter();
