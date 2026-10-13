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
import { readFile, stat } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import process from 'node:process';
import { UmhError } from '../../contracts/index.js';
import { makeManualPlan, makeOwnedEntryPlan, parseConfigPlan } from '../config-plan.js';
import { buildPolicyBlock } from '../policy-block.js';
import { applyConfigPlan, loadReceiptById, removeOwnedConfig, saveReceipt, umhHomeOfPlan } from '../transaction.js';
/** Qoder CLI 的 owned 段：user 级 settings.json → mcpServers 命名空间下的 umh-memory。 */
export const QODER_OWNED_POINTER = '/mcpServers/umh-memory';
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点 AGENTS.md）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export const QODER_POLICY_INSTRUCTIONS = [
    '在回答前可调用 memory_status/memory_recall/memory_search/memory_get 读取项目共享记忆；读取失败不得阻塞回答。',
    '涉及用户偏好、项目决策、可复用事实时调用 memory_remember 提交记忆；一次只提交一条、正文自含。',
    '绝不把凭据、密钥或认证材料写入记忆正文。',
];
/**
 * Qoder 配方（= docs/clients/recipes/qoder.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[cli]=CLI 手工路径、[cli-auto]=CLI 自动路径附加指引、
 * [ide]=IDE 手工路径。占位符 {{command}}/{{args}}/{{env}}/{{policy}} 由 preview 用
 * plan.launch 与模块策略块插值。步骤按 2026-10-03 官方文档核实措辞。
 */
export const QODER_RECIPE = {
    variant: 'cli|ide',
    version: '0.4.3',
    officialSource: 'https://docs.qoder.com/cli/mcp-reference; https://docs.qoder.com/cli/settings-reference; https://docs.qoder.com/cli/settings; ide: https://docs.qoder.com/user-guide/chat/model-context-protocol',
    verifiedAt: '2026-10-03',
    configLocation: "cli: user-level ~/.qoder/settings.json → mcpServers (official: 'Available to all projects'; the user config dir can be relocated with QODER_CONFIG_DIR; JSONC // comments are officially supported; project-level settings/.mcp.json require approval before use). ide: settings UI only (Ctrl+Shift+, → MCP → My Servers); no user-editable config file is documented, UMH never writes it.",
    format: 'json|ui',
    ownedPointer: "cli: /mcpServers/umh-memory inside ~/.qoder/settings.json (auto-edit only for strict JSON; JSONC/commented files get a manual fragment instead); ide: ui://mcp (the server entry named 'umh-memory' in IDE Settings → MCP → My Servers)",
    policyLocation: "project AGENTS.md (Qoder CLI context.fileName default) — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers",
    manualSteps: [
        '[cli] Decide the target profile explicitly first (CN or international edition, several accounts, or a relocated QODER_CONFIG_DIR): when several Qoder config roots exist, UMH refuses to guess and reports PROFILE_REQUIRED — re-run detection with the explicit settings.json path of the profile you want to connect.',
        '[cli] UMH did not auto-edit a Qoder settings file (no safely auto-editable strict-JSON settings file was found — it may be missing or contain JSONC comments, and UMH will not risk losing comments or fields). Open the user-level settings file at ~/.qoder/settings.json (create it if missing; this scope is \'Available to all projects\'; project-level files require per-project approval).',
        '[cli] Under the top-level mcpServers object add a stdio server named umh-memory with exactly: command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.',
        '[cli] Restart Qoder CLI (mcpServers changes require restart), then run /mcp to check the server is listed and /mcp reload after later edits; the UMH tools include memory_status.',
        "[cli] Inject the memory policy into the project AGENTS.md (Qoder's default memory file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}",
        '[cli] Verify from a Qoder session: ask it to run memory_status; do not report onboarding success until the CLI actually calls the tool.',
        '[cli-auto] UMH wrote the umh-memory entry into the user-level settings file (a pre-edit backup and a rollback receipt were recorded under the UMH home). Restart Qoder CLI (mcpServers changes require restart) and run /mcp to confirm the server; the UMH tools include memory_status.',
        "[cli-auto] Inject the memory policy into the project AGENTS.md (Qoder's default memory file): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}",
        '[cli-auto] Verify from a Qoder session: ask it to run memory_status; do not report onboarding success until the CLI actually calls the tool.',
        '[ide] The Qoder IDE form is verified only as a settings UI; no user-editable config file is documented, so UMH never writes IDE configuration. Open IDE Settings (Ctrl+Shift+, on Windows) and navigate to MCP in the left-side navigation pane.',
        '[ide] On the My Servers tab click + Add; in the JSON file that appears add a server named umh-memory with transport type STDIO and exactly: command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file.',
        '[ide] Close the file and click Save when prompted; a link icon means the connection is successful. Tool calls prompt for confirmation (Ctrl+Enter) before running.',
        '[ide] Inject the memory policy block (see docs/clients/qoder.md) into your instruction file and verify: ask the IDE agent to run memory_status; do not report onboarding success until it actually calls the tool.',
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
/** 严格 JSON 对象判定（JSONC 注释/数组/标量都算"非严格对象"）。 */
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
        throw new UmhError('CONFIG_PATH_INVALID', `qoder settings path is not readable: ${path}`);
    }
    return isStrictJsonObject(bytes.toString('utf8'));
}
/** 同一路径的大小写不敏感去重键（Windows 文件系统不区分大小写）。 */
function pathKey(path) {
    return process.platform === 'win32' ? path.toLowerCase() : path;
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
    const policyBlock = buildPolicyBlock(QODER_POLICY_INSTRUCTIONS);
    return QODER_RECIPE.manualSteps
        .filter((step) => step.startsWith(prefix))
        .map((step) => interpolateStep(step.slice(prefix.length), launch, policyBlock));
}
function requireQoderDetection(detection) {
    if (detection === null || typeof detection !== 'object' || detection.clientId !== 'qoder') {
        throw new UmhError('INVALID_ARGUMENT', 'qoder adapter requires a detection for clientId qoder');
    }
}
function requireReceiptId(input) {
    const receiptId = input?.receiptId;
    if (typeof receiptId !== 'string' || !RECEIPT_ID_PATTERN.test(receiptId)) {
        throw new UmhError('RECEIPT_INVALID', `qoder adapter requires a rcpt_<32hex> receipt id: ${String(receiptId)}`);
    }
    return receiptId;
}
// umhHomeOfPlan 自 transaction.ts 导入（apply 的回执落盘点与事务同源，A03 修复轮 1 DRY）。
// ---------------------------------------------------------------------------
// detect（只读：stat + 严格 JSON 判读；绝不写盘、绝不读聊天/记忆内容）
// ---------------------------------------------------------------------------
/** CLI：默认 user 级 settings.json（官方记录的固定位置）。 */
function defaultCliSettingsPath(home) {
    return join(home, '.qoder', 'settings.json');
}
async function detectCli(home, configPath) {
    const defaultPath = defaultCliSettingsPath(home);
    if (typeof configPath === 'string' && configPath.trim() !== '') {
        // 显式选择：独占该 profile（绝不把其它候选混进来 —— 多候选歧义在 preview 拒绝）。
        const selected = configPath;
        const exists = await pathExists(selected);
        return {
            clientId: 'qoder',
            variant: 'cli',
            version: null,
            installed: exists || (await pathExists(defaultPath)),
            configPaths: [selected],
            format: 'json',
            state: exists && (await autoEditable(selected)) ? 'verified' : 'unverified',
        };
    }
    // 发现模式：只列官方已记录的候选根（默认 ~/.qoder + QODER_CONFIG_DIR 迁址根），
    // 只报存在者；绝不隐式选择 —— 多候选交由 preview 抛 PROFILE_REQUIRED 强制显式选择。
    const candidates = [];
    const envDir = process.env.QODER_CONFIG_DIR;
    if (typeof envDir === 'string' && envDir.trim() !== '') {
        const relocated = join(envDir.trim(), 'settings.json');
        if (await pathExists(relocated))
            candidates.push(relocated);
    }
    if (await pathExists(defaultPath))
        candidates.push(defaultPath);
    const seen = new Set();
    const configPaths = candidates.filter((candidate) => {
        const key = pathKey(candidate);
        if (seen.has(key))
            return false;
        seen.add(key);
        return true;
    });
    // installed 只认 settings.json 候选（裸 `.qoder` 目录里可能有其它工具的文件，
    // 例如 A01 generic fixture 的 mcp.json —— 目录存在不构成安装证据）。
    const installed = configPaths.length > 0;
    const state = configPaths.length === 1 && (await autoEditable(configPaths[0])) ? 'verified' : 'unverified';
    return { clientId: 'qoder', variant: 'cli', version: null, installed, configPaths, format: 'json', state };
}
/** IDE：只探测安装痕迹（无一属于"可编辑配置文件"）；路径/格式官方未记录 → 恒不宣称。 */
function ideProbePaths(home) {
    switch (process.platform) {
        case 'win32':
            return [join(home, 'AppData', 'Local', 'Qoder'), join(home, 'AppData', 'Roaming', 'Qoder')];
        case 'darwin':
            return ['/Applications/Qoder.app'];
        default:
            return [join(home, '.config', 'Qoder')];
    }
}
async function detectIde(home, variant) {
    let installed = false;
    for (const candidate of ideProbePaths(home)) {
        if (await pathExists(candidate)) {
            installed = true;
            break;
        }
    }
    return { clientId: 'qoder', variant, version: null, installed, configPaths: [], format: 'ui', state: 'unverified' };
}
// ---------------------------------------------------------------------------
// adapter 工厂（qoderAdapter = 缺省单例；remove 需要绑定 umhHome 时用工厂）
// ---------------------------------------------------------------------------
/** 缺省 umhHome 解析：UMH_HOME 环境变量（connect CLI 同款约定；绝对路径才接受）。 */
function umhHomeFromEnv() {
    const value = process.env.UMH_HOME;
    return typeof value === 'string' && value.trim() !== '' && isAbsolute(value) ? value : null;
}
/**
 * 构造 Qoder 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 qoderAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export function makeQoderAdapter(options) {
    const boundHome = typeof options?.umhHome === 'string' && options.umhHome.trim() !== '' ? options.umhHome : null;
    async function resolveRemoveHome() {
        const home = boundHome ?? umhHomeFromEnv();
        if (home === null) {
            throw new UmhError('INVALID_ARGUMENT', 'qoder adapter remove needs a UMH home: construct via makeQoderAdapter({ umhHome }) or set UMH_HOME (never guessed)');
        }
        return home;
    }
    return {
        async detect(opts) {
            if (opts === null || typeof opts !== 'object' || typeof opts.home !== 'string' || opts.home.trim() === '') {
                throw new UmhError('INVALID_ARGUMENT', 'detect requires a home directory');
            }
            if (opts.variant === 'cli')
                return detectCli(opts.home, opts.configPath);
            if (opts.variant === 'ide')
                return detectIde(opts.home, opts.variant);
            // variant 决定写入路径（cli 自动 / ide 手工）：未知变体响亮拒绝，绝不静默降级。
            throw new UmhError('INVALID_ARGUMENT', `qoder variant must be 'cli' or 'ide': ${String(opts.variant)}`);
        },
        async preview(input) {
            requireQoderDetection(input?.detection);
            const { detection, binding, launch } = input;
            // IDE 形态未核实（官方只有 settings UI，配置文件路径无文档）→ 恒手工计划。
            if (detection.variant === 'ide') {
                return makeManualPlan('qoder', binding, launch, stepsFor('ide', launch));
            }
            if (detection.variant !== 'cli') {
                throw new UmhError('INVALID_ARGUMENT', `qoder variant must be 'cli' or 'ide': ${String(detection.variant)}`);
            }
            const paths = Array.isArray(detection.configPaths) ? detection.configPaths : [];
            // 多 profile：绝不隐式选第一项（Review Focus：CN/国际与多账号要明确选择）。
            if (paths.length > 1) {
                throw new UmhError('PROFILE_REQUIRED', `multiple Qoder CLI profiles detected (${paths.join('; ')}); UMH never picks one automatically — ` +
                    're-run detect with the explicit configPath of the profile to connect ' +
                    '(CN/international edition, multiple accounts, or a relocated QODER_CONFIG_DIR)');
            }
            if (paths.length === 0) {
                // 无检出：给已核实的建库/手工步骤（创建或编辑 user 级 settings.json）。
                return makeManualPlan('qoder', binding, launch, stepsFor('cli', launch));
            }
            const path = paths[0];
            if (!(await autoEditable(path))) {
                // JSONC 注释/非严格 JSON：JSON 往返无法证明注释保留 → 手工片段，绝不破坏性 stringify。
                return makeManualPlan('qoder', binding, launch, stepsFor('cli', launch));
            }
            // 严格 JSON：A01 owned 段计划（只动 /mcpServers/umh-memory，第三方内容绝不过写）。
            const plan = await makeOwnedEntryPlan({ ...detection, configPaths: [path], format: 'json' }, QODER_OWNED_POINTER, launch, binding);
            // 自动路径附加指引（策略块 + 重启/核验）——规则只进 owned policy 块，逐字来自 recipe。
            return { ...plan, manualInstructions: stepsFor('cli-auto', launch) };
        },
        async apply(plan) {
            const parsed = parseConfigPlan(plan);
            if (parsed.clientId !== 'qoder') {
                throw new UmhError('PLAN_INVALID', `qoder apply refuses plans for another client: ${parsed.clientId}`);
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
            // 诚实三态：适配层未做真实连通性探针（A09 才取得真实 Qoder 证据）；
            // Qoder 未实际调用工具前 sharedReadWrite 恒为 NOT_RUN（工单明示）。
            return { connection: 'UNVERIFIED', sharedReadWrite: 'NOT_RUN', policy: 'UNVERIFIED' };
        },
        async remove(input) {
            const receiptId = requireReceiptId(input);
            const receipt = await loadReceiptById(await resolveRemoveHome(), receiptId);
            // 手工计划回执（IDE / JSONC 降级路径，changes 恒空）：UMH 从未写过任何客户端
            // 配置字节，没有可自动移除的磁盘足迹 —— removed:false + 手工清理指引（cherry
            // 先例：声称自动移除成功即为误报），绝不谎报 removed:true 零动作。
            if (receipt.changes.length === 0) {
                return {
                    removed: false,
                    conflicts: [
                        'qoder manual onboarding: this receipt recorded no UMH-written config changes, so there is nothing to auto-remove (removed stays false; claiming automated success would be a false report).',
                        'Manual cleanup: delete the hand-added umh-memory server entry (CLI: the umh-memory block under mcpServers in the user-level ~/.qoder/settings.json; IDE: Settings → MCP → My Servers → umh-memory) and restart Qoder afterwards.',
                        "Manual cleanup: delete the whole UMH MEMORY POLICY block (BEGIN to END markers) from the project AGENTS.md (Qoder's default memory file).",
                        'If the trusted binding file was created via umh connect apply, revoke it with: umh disconnect --receipt <receipt.json> (it lives in <umhHome>/config/bindings/ and has no Qoder-side counterpart).',
                    ],
                };
            }
            // 三方比对（A01 事务层）：用户后改 owned 段 → conflict 不触碰；无关编辑幸存。
            return removeOwnedConfig(receipt);
        },
    };
}
/** Qoder 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeQoderAdapter 绑定）。 */
export const qoderAdapter = makeQoderAdapter();
