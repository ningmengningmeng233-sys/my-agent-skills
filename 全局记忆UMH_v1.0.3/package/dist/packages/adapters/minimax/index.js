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
import { readFile, readdir, stat } from 'node:fs/promises';
import { mkdir } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join } from 'node:path';
import process from 'node:process';
import { UmhError } from '../../contracts/index.js';
import { makeManualPlan, makeOwnedEntryPlan, parseConfigPlan } from '../config-plan.js';
import { buildPolicyBlock } from '../policy-block.js';
import { applyConfigPlan, loadReceiptById, removeOwnedConfig, saveReceipt, umhHomeOfPlan } from '../transaction.js';
/**
 * MiniMax CLI 的 owned 段：选中 dataDir 下 `mcp.json` 的 `mcpServers` 对象内 `umh` 条目
 * （源码写路径唯一：`join(dataDir, 'mcp.json')`；stdio `{command,args,env}`，无 url 即
 * 按 stdio 读取；`type` 可省）。
 */
export const MINIMAX_OWNED_POINTER = '/mcpServers/umh';
/** profile 级 mcp.json 文件名（显式选择守卫用）。 */
const MCP_JSON_BASENAME = 'mcp.json';
/** 读兼容遗留别名段（`<dataDir>/mcp/mcp.json` 的中间目录名；写目标永不是它）。 */
const LEGACY_ALIAS_SEGMENT = 'mcp';
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点项目 AGENTS.md）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export const MINIMAX_POLICY_INSTRUCTIONS = [
    '在回答前可调用 memory_status/memory_recall/memory_search/memory_get 读取项目共享记忆；读取失败不得阻塞回答。',
    '涉及用户偏好、项目决策、可复用事实时调用 memory_remember 提交记忆；一次只提交一条、正文自含。',
    '绝不把凭据、密钥或认证材料写入记忆正文。',
];
/**
 * MiniMax 配方（= docs/clients/recipes/minimax.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[cli]=CLI 手工路径（未选 root/坏 JSON/别名/缺失）、
 * [cli-auto]=CLI 自动路径附加指引、[desktop]=桌面手工路径（恒 UNVERIFIED）。
 * 占位符 {{command}}/{{args}}/{{env}}/{{policy}} 由 preview 用 plan.launch 与模块策略插值。
 * 步骤按 2026-10-03 官方源码/文档核实措辞（dataDir 优先级/profile mcp.json/项目 .mcp.json/
 * /mcp 核验流/桌面源码边界）。
 */
export const MINIMAX_RECIPE = {
    variant: 'cli|desktop',
    version: '0.6.2',
    officialSource: 'https://github.com/MiniMax-AI/minimax-code; profile MCP: https://github.com/MiniMax-AI/minimax-code/blob/main/packages/local-runtime-v2/docs/project-mcp.md; project MCP: https://github.com/MiniMax-AI/minimax-code/blob/main/docs/examples.md#5-connect-an-authenticated-project-mcp-server; data dirs: https://github.com/MiniMax-AI/minimax-code/blob/main/docs/installation.md#accounts-and-data',
    verifiedAt: '2026-10-03',
    configLocation: 'cli: <dataDir>/mcp.json where <dataDir> resolves as non-empty MINIMAX_DATA_DIR, else non-empty MAVIS_DATA_DIR, else ~/.minimax (or ~/.minimax-<profile> when a profile is selected) — frozen from packages/config/src/data-dir.ts + packages/tui/src/runtime/data-dir.ts + docs/installation.md; read-compatible legacy alias <dataDir>/mcp/mcp.json is never an auto-edit target; project-level .mcp.json in the session workspace is read-only shared runtime state (UMH never writes it); desktop: no verified profile configuration entry (the published source covers TUI/CLI/ACP only, not the desktop app) — desktop is manual-only',
    format: 'json|ui',
    ownedPointer: "/mcpServers/umh — a stdio server named 'umh' ({command,args,env}; an entry without a url is read as a stdio server) inside mcpServers of <dataDir>/mcp.json; config.yaml (custom_provider/minimax_api credentials) and sessions/ are never detection targets and are never read or written",
    policyLocation: "project AGENTS.md (MiniMax workspace instruction file: packages/local-runtime/src/project/instructions.ts PROJECT_INSTRUCTIONS_FILENAME = 'AGENTS.md') — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers",
    manualSteps: [
        '[cli] Choose the data directory explicitly first: default ~/.minimax, or ~/.minimax-<profile> when a profile is selected, or a redirected root via non-empty MINIMAX_DATA_DIR (preferred) / MAVIS_DATA_DIR. When several of these hold a config, UMH refuses to guess and reports PROFILE_REQUIRED — re-run detection with the explicit mcp.json path of the data directory you want to connect.',
        '[cli] UMH did not auto-edit a MiniMax mcp.json file (no safely auto-editable strict-JSON file was found for the selected data directory — it may be missing, live under a custom MINIMAX_DATA_DIR/MAVIS_DATA_DIR, use the legacy mcp/mcp.json alias, or not be strict JSON, and UMH will not risk losing your fields). Hand-edit the selected <dataDir>/mcp.json as described next; UMH never edits <dataDir>/config.yaml (provider credentials) or sessions.',
        '[cli] The entry to add is exactly: command = {{command}}; args = {{args}}; env = {{env}}. It is a stdio server named umh under the top-level mcpServers object of the selected <dataDir>/mcp.json. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.',
        '[cli] Inject the memory policy into the project AGENTS.md (the MiniMax workspace instruction file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}',
        '[cli] Restart MCode (or run /mcp reload) and confirm the umh entry: /mcp lists it as configured, then available after tool discovery or a call. Project-level .mcp.json in the workspace is a separate read-only shared layer — UMH never writes it.',
        '[cli] Verify: ask MiniMax to run memory_status; do not report onboarding success until the agent actually calls the tool.',
        '[cli-auto] UMH wrote the umh stdio entry into the explicitly selected <dataDir>/mcp.json (a pre-edit backup and a rollback receipt were recorded under the UMH home). Provider configuration (config.yaml), sessions and every other field were preserved untouched; UMH never reads sessions or credentials.',
        '[cli-auto] Restart MCode (or run /mcp reload) and confirm the umh entry in /mcp: configured before connection, available after tool discovery or a call.',
        '[cli-auto] Inject the memory policy into the project AGENTS.md (the MiniMax workspace instruction file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}',
        '[cli-auto] Verify: ask MiniMax to run memory_status; do not report onboarding success until the agent actually calls the tool.',
        '[desktop] The MiniMax desktop configuration entry is UNVERIFIED: the published source covers the terminal TUI, headless CLI and ACP but does not include the desktop application\'s source, so UMH cannot verify which profile file (if any) the desktop reads. UMH therefore never auto-writes for the desktop — a CLI success is never desktop evidence.',
        '[desktop] Confirm in the desktop app itself where it manages MCP servers (official UI / verified configuration only — do not assume it reads the CLI <dataDir>/mcp.json). Add a stdio server named umh with exactly: command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file.',
        '[desktop] The project-level .mcp.json in the session workspace is shared runtime state consumed by Desktop, TUI, mcode exec and mcode acp alike — UMH never writes it and never reverse-engineers any desktop database; no cloud memory service is contacted.',
        '[desktop] Inject the memory policy into the project AGENTS.md (the MiniMax workspace instruction file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}',
        '[desktop] Verify in the desktop app: check the umh entry in its MCP panel, then ask the agent to run memory_status; do not report onboarding success until the agent actually calls the tool.',
    ],
    schemaValidated: true,
};
const RECEIPT_ID_PATTERN = /^rcpt_[0-9a-f]{32}$/;
const VARIANTS = new Set(['cli', 'desktop']);
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
 * 存在且为严格 JSON 对象 → true；坏 JSON/非对象 → false（手工片段）。
 */
async function autoEditable(path) {
    let bytes;
    try {
        bytes = await readFile(path);
    }
    catch (err) {
        if (err.code === 'ENOENT')
            return true;
        throw new UmhError('CONFIG_PATH_INVALID', `minimax config path is not readable: ${path}`);
    }
    return isStrictJsonObject(bytes.toString('utf8'));
}
/** 非空环境变量（源码 `.trim()` 语义：空串/空白视为未设置）。 */
function nonEmptyEnv(name) {
    const value = process.env[name];
    if (typeof value !== 'string')
        return null;
    const trimmed = value.trim();
    return trimmed === '' ? null : trimmed;
}
/** 默认 dataDir：`~/.minimax`（profile 选中时 `~/.minimax-<profile>`）。 */
function defaultDataDir(home, profile) {
    return profile === null ? join(home, '.minimax') : join(home, `.minimax-${profile}`);
}
/** 显式 profile（`--profile` 旗标无 argv 上下文时读 PROFILE 环境变量；git auto 形态不消费）。 */
function explicitProfile() {
    const value = process.env.PROFILE;
    if (typeof value !== 'string')
        return null;
    const trimmed = value.trim();
    return trimmed === '' ? null : trimmed;
}
/**
 * dataDir 解析（冻结优先级，源码逐字）：非空 MINIMAX_DATA_DIR → 非空 MAVIS_DATA_DIR →
 * 默认（default/profile）。返回 { dir, redirected }（redirected = 前两者的赢家）。
 */
function resolveDataDir(home) {
    const minimax = nonEmptyEnv('MINIMAX_DATA_DIR');
    if (minimax !== null)
        return { dir: minimax, redirected: minimax };
    const mavis = nonEmptyEnv('MAVIS_DATA_DIR');
    if (mavis !== null)
        return { dir: mavis, redirected: mavis };
    return { dir: defaultDataDir(home, explicitProfile()), redirected: null };
}
/** 候选 dataDir 全集（default + 家目录下已存在的 `.minimax-*` profile 根），用于发现模式的多 root 探测。 */
async function candidateDataDirs(home) {
    const dirs = [defaultDataDir(home, null)];
    const profile = explicitProfile();
    // PROFILE 环境变量显式点名时，该 profile 根即候选之一（与 default 并列 → 多候选拒绝）。
    if (profile !== null)
        dirs.push(defaultDataDir(home, profile));
    // 家目录下已存在的 `.minimax-<profile>` 根：逐个 stat（只读存在性，不读内容）。
    try {
        for (const entry of await readdir(home, { withFileTypes: true })) {
            if (!entry.isDirectory())
                continue;
            if (!isProfileDataDirName(entry.name))
                continue;
            const candidate = join(home, entry.name);
            if (!dirs.includes(candidate))
                dirs.push(candidate);
        }
    }
    catch {
        /* 家目录不可列举 → 仅默认候选 */
    }
    return dirs;
}
/** 已知 profile 根形态（`~/.minimax-<profile>`）：用于显式路径守卫。 */
function isProfileDataDirName(name) {
    if (process.platform === 'win32')
        return /^\.minimax-.+/i.test(name);
    return /^\.minimax-.+/.test(name);
}
/** 显式 configPath 是否为遗留别名 `<root>/mcp/mcp.json`（读兼容，永不自动写）。 */
function isLegacyAliasPath(configPath) {
    return basename(configPath) === MCP_JSON_BASENAME && basename(dirname(configPath)) === LEGACY_ALIAS_SEGMENT;
}
/**
 * preview 侧的显式路径形态判定（detect 只做独占受理，不做形态守卫 ——
 * qoder/codex/claude 同型先例：detect 绝不 PROFILE_REQUIRED）：
 *   - 非 `mcp.json` 形态 → rejected（preview 抛 PROFILE_REQUIRED，绝不猜）；
 *   - 遗留别名 `<root>/mcp/mcp.json` → legacy-alias（读兼容，永不自动写 → preview 降级手工）；
 *   - 其余 `mcp.json` 形态 → canonical（父目录即被选 data root）。
 */
function classifyPreviewPath(configPath) {
    if (!isAbsolute(configPath))
        return { kind: 'rejected' };
    if (basename(configPath) !== MCP_JSON_BASENAME)
        return { kind: 'rejected' };
    if (isLegacyAliasPath(configPath))
        return { kind: 'legacy-alias' };
    return { kind: 'canonical' };
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
    const policyBlock = buildPolicyBlock(MINIMAX_POLICY_INSTRUCTIONS);
    return MINIMAX_RECIPE.manualSteps
        .filter((step) => step.startsWith(prefix))
        .map((step) => interpolateStep(step.slice(prefix.length), launch, policyBlock));
}
function requireMinimaxDetection(detection) {
    if (detection === null || typeof detection !== 'object' || detection.clientId !== 'minimax') {
        throw new UmhError('INVALID_ARGUMENT', 'minimax adapter requires a detection for clientId minimax');
    }
}
function requireVariant(variant) {
    // variant 决定写入路径（cli 自动 / desktop 手工）：未知变体响亮拒绝，绝不静默降级。
    if (!VARIANTS.has(variant)) {
        throw new UmhError('INVALID_ARGUMENT', `minimax variant must be 'cli' or 'desktop': ${String(variant)}`);
    }
}
function requireReceiptId(input) {
    const receiptId = input?.receiptId;
    if (typeof receiptId !== 'string' || !RECEIPT_ID_PATTERN.test(receiptId)) {
        throw new UmhError('RECEIPT_INVALID', `minimax adapter requires a rcpt_<32hex> receipt id: ${String(receiptId)}`);
    }
    return receiptId;
}
// ---------------------------------------------------------------------------
// detect（只读：stat + 严格 JSON 判读；绝不写盘、不读 sessions/config.yaml 内容）
// ---------------------------------------------------------------------------
/** 某 dataDir 的 mcp.json 候选存在性（只 stat，不读内容 —— 安装证据层面）。 */
async function mcpJsonExists(dataDir) {
    return pathExists(join(dataDir, MCP_JSON_BASENAME));
}
async function detectCli(home, configPath) {
    if (typeof configPath === 'string' && configPath.trim() !== '') {
        // 显式选择：独占该路径（qoder/codex/claude 同型先例：detect 只做存在性/严格 JSON
        // 判读，不做路径形态守卫；不明形态交由 preview 抛 PROFILE_REQUIRED，绝不猜）。
        // 遗留别名 `<root>/mcp/mcp.json` 在显式选择下同样受理为一次选择（读兼容），
        // 但 preview 恒降级手工（写目标唯一，见 preview 别名分支）。
        const selected = configPath;
        const exists = await pathExists(selected);
        let verified = false;
        if (exists) {
            try {
                verified = isStrictJsonObject(await readFile(selected, 'utf8'));
            }
            catch {
                verified = false;
            }
        }
        return {
            clientId: 'minimax',
            variant: 'cli',
            version: null,
            installed: exists || (await mcpJsonExists(resolveDataDir(home).dir)),
            configPaths: [selected],
            format: 'json',
            state: verified ? 'verified' : 'unverified',
        };
    }
    // 发现模式：重定向赢家独占（排他优先级本身）→ 否则列出存在的候选；
    // 多候选交由 preview 抛 PROFILE_REQUIRED 强制显式选择。
    const { dir, redirected } = resolveDataDir(home);
    if (redirected !== null) {
        const selected = join(redirected, MCP_JSON_BASENAME);
        const exists = await pathExists(selected);
        let verified = false;
        if (exists) {
            try {
                verified = isStrictJsonObject(await readFile(selected, 'utf8'));
            }
            catch {
                verified = false;
            }
        }
        return {
            clientId: 'minimax',
            variant: 'cli',
            version: null,
            installed: exists,
            configPaths: [selected],
            format: 'json',
            state: verified ? 'verified' : 'unverified',
        };
    }
    const found = [];
    // default 根 + 家目录下已存在的 profile 根（两者并存 = 多候选 → preview 拒绝）。
    for (const candidate of await candidateDataDirs(home)) {
        if (await mcpJsonExists(candidate))
            found.push(join(candidate, MCP_JSON_BASENAME));
    }
    // 遗留别名 `<dataDir>/mcp/mcp.json` 仅在 canonical 缺席时计入发现（且 preview 恒手工）。
    for (const candidate of await candidateDataDirs(home)) {
        if (found.some((p) => dirname(p) === candidate))
            continue;
        const alias = join(candidate, LEGACY_ALIAS_SEGMENT, MCP_JSON_BASENAME);
        if (await pathExists(alias))
            found.push(alias);
    }
    const installed = found.length > 0;
    let state = 'unverified';
    if (found.length === 1) {
        try {
            state = isStrictJsonObject(await readFile(found[0], 'utf8')) ? 'verified' : 'unverified';
        }
        catch {
            state = 'unverified';
        }
    }
    return { clientId: 'minimax', variant: 'cli', version: null, installed, configPaths: found, format: 'json', state };
}
/**
 * 桌面检出（恒 UNVERIFIED 手工）：已出版源码只覆盖 TUI/CLI/ACP，不含桌面应用源码，
 * 桌面 profile 配置入口不可核实。detect 绝不借 CLI 目录推断 —— configPaths 恒 []、
 * format 'ui'、installed false（无已核实桌面安装探针），version 恒 null。
 */
async function detectDesktop() {
    return {
        clientId: 'minimax',
        variant: 'desktop',
        version: null,
        installed: false,
        configPaths: [],
        format: 'ui',
        state: 'unverified',
    };
}
// ---------------------------------------------------------------------------
// adapter 工厂（minimaxAdapter = 缺省单例；remove 需要绑定 umhHome 时用工厂）
// ---------------------------------------------------------------------------
/** 缺省 umhHome 解析：UMH_HOME 环境变量（connect CLI 同款约定；绝对路径才接受）。 */
function umhHomeFromEnv() {
    const value = process.env.UMH_HOME;
    return typeof value === 'string' && value.trim() !== '' && isAbsolute(value) ? value : null;
}
/**
 * 构造 MiniMax 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 minimaxAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export function makeMinimaxAdapter(options) {
    const boundHome = typeof options?.umhHome === 'string' && options.umhHome.trim() !== '' ? options.umhHome : null;
    async function resolveRemoveHome() {
        const home = boundHome ?? umhHomeFromEnv();
        if (home === null) {
            throw new UmhError('INVALID_ARGUMENT', 'minimax adapter remove needs a UMH home: construct via makeMinimaxAdapter({ umhHome }) or set UMH_HOME (never guessed)');
        }
        return home;
    }
    return {
        async detect(opts) {
            if (opts === null || typeof opts !== 'object' || typeof opts.home !== 'string' || opts.home.trim() === '') {
                throw new UmhError('INVALID_ARGUMENT', 'detect requires a home directory');
            }
            requireVariant(opts.variant);
            if (opts.variant === 'desktop')
                return detectDesktop();
            return detectCli(opts.home, opts.configPath);
        },
        async preview(input) {
            requireMinimaxDetection(input?.detection);
            const { detection, binding, launch } = input;
            requireVariant(detection.variant);
            // 桌面恒手工：与检出声称的格式/验证态无关（CLI 成功永不构成桌面证据 ——
            // 伪造 verified JSON 检出套 desktop 变体也一样手工）。
            if (detection.variant === 'desktop') {
                return makeManualPlan('minimax', binding, launch, stepsFor('desktop', launch));
            }
            const paths = Array.isArray(detection.configPaths) ? detection.configPaths : [];
            // 伪造检出（格式与官方文档化 JSON 矛盾）不能推进写入路径。
            if (paths.length > 0 && detection.format !== 'json') {
                throw new UmhError('INVALID_ARGUMENT', `minimax detection must report format 'json' (got '${String(detection.format)}'); a fabricated detection cannot drive JSON owned-pointer edits`);
            }
            // 多候选：绝不隐式选第一项（default/profile/redirected 必须人来定）。
            if (paths.length > 1) {
                throw new UmhError('PROFILE_REQUIRED', `multiple MiniMax data roots detected (${paths.join('; ')}); UMH never picks one automatically — ` +
                    're-run detect with the explicit mcp.json path of the data directory to connect ' +
                    '(default ~/.minimax, ~/.minimax-<profile> for a selected profile, or a MINIMAX_DATA_DIR/MAVIS_DATA_DIR redirect)');
            }
            if (paths.length === 0) {
                // 无检出（全新机器且无重定向）：给已核实的手工建库步骤（选 root 后创建 mcp.json）。
                return makeManualPlan('minimax', binding, launch, stepsFor('cli', launch));
            }
            const path = paths[0];
            // 显式路径形态判定（detect 只做独占受理，不做守卫 —— 拒绝点统一在 preview，
            // qoder/codex/claude 同型先例：detect 绝不 PROFILE_REQUIRED）。
            const verdict = classifyPreviewPath(path);
            if (verdict.kind === 'rejected') {
                throw new UmhError('PROFILE_REQUIRED', `the selected config path does not name a MiniMax data-directory mcp.json ('<dataDir>/mcp.json'): ${path}`);
            }
            // 遗留别名 `<dataDir>/mcp/mcp.json`：读兼容但写目标唯一 → 手工片段，绝不写别名。
            if (verdict.kind === 'legacy-alias') {
                return makeManualPlan('minimax', binding, launch, stepsFor('cli', launch));
            }
            let rawExists = true;
            try {
                await readFile(path);
            }
            catch (err) {
                if (err.code !== 'ENOENT') {
                    throw new UmhError('CONFIG_PATH_INVALID', `minimax config path is not readable: ${path}`);
                }
                rawExists = false;
            }
            if (rawExists && !(await autoEditable(path))) {
                // 坏 JSON/非对象：JSON 往返无法证明用户内容幸存 → 手工片段，绝不破坏性改写。
                return makeManualPlan('minimax', binding, launch, stepsFor('cli', launch));
            }
            // 严格 JSON（或缺席即将新建）：A01 owned 段计划（只动 /mcpServers/umh，
            // provider/sessions/第三方字段绝不过写；OWNED_POINTER_OCCUPIED 守卫继承 A01）。
            const plan = await makeOwnedEntryPlan({ ...detection, configPaths: [path], format: 'json' }, MINIMAX_OWNED_POINTER, launch, binding);
            // 自动路径附加指引（重启//mcp 核验/策略块）——规则只进 owned policy 块，逐字来自 recipe。
            return { ...plan, manualInstructions: stepsFor('cli-auto', launch) };
        },
        async apply(plan) {
            const parsed = parseConfigPlan(plan);
            if (parsed.clientId !== 'minimax') {
                throw new UmhError('PLAN_INVALID', `minimax apply refuses plans for another client: ${parsed.clientId}`);
            }
            // 全新安装（beforeHash null）时目标文件与其父目录可能都不存在（重定向根/dataDir
            // 从未创建）：A01 publishAtomic 的同目录临时文件写需要父目录在场；空目录创建是
            // 幂等物化步骤，不触碰任何既有字节（A05/A06 先例）。
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
            // 诚实三态：适配层未做真实连通性探针（A09 才取得真实 MiniMax 证据，
            // cli/desktop 两变体分别采集）；agent 未实际调用工具前 sharedReadWrite 恒
            // NOT_RUN（工单明示）。
            return { connection: 'UNVERIFIED', sharedReadWrite: 'NOT_RUN', policy: 'UNVERIFIED' };
        },
        async remove(input) {
            const receiptId = requireReceiptId(input);
            const receipt = await loadReceiptById(await resolveRemoveHome(), receiptId);
            // 手工计划回执（desktop / 别名 / 坏 JSON / 未选 root 的手工路径，changes 恒空）：
            // UMH 从未写过任何客户端配置字节，没有可自动移除的磁盘足迹 —— removed:false +
            // 手工清理指引（cherry 先例：声称自动移除成功即为误报），绝不谎报 removed:true。
            if (receipt.changes.length === 0) {
                return {
                    removed: false,
                    conflicts: [
                        'minimax manual onboarding: this receipt recorded no UMH-written config changes, so there is nothing to auto-remove (removed stays false; claiming automated success would be a false report).',
                        'Manual cleanup: delete the hand-added umh server entry from the MiniMax MCP configuration you edited (CLI: the umh entry under mcpServers in the selected <dataDir>/mcp.json; desktop: the umh entry in the desktop MCP panel). Project-level .mcp.json in the workspace is read-only shared state — UMH never writes it, so it needs no UMH cleanup.',
                        'Manual cleanup: delete the whole UMH MEMORY POLICY block (BEGIN to END markers) from the project AGENTS.md (the MiniMax workspace instruction file).',
                        'If the trusted binding file was created via umh connect apply, revoke it with: umh disconnect --receipt <receipt.json> (it lives in <umhHome>/config/bindings/ and has no MiniMax-side counterpart).',
                    ],
                };
            }
            // 三方比对（A01 事务层，JSON 原生支持）：用户后改 owned 段 → conflict 不触碰；
            // 无关编辑幸存；UMH 新建且未动的文件 → 整文件移除。
            return removeOwnedConfig(receipt);
        },
    };
}
/** MiniMax 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeMinimaxAdapter 绑定）。 */
export const minimaxAdapter = makeMinimaxAdapter();
