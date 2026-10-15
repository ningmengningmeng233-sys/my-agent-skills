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
import { readFile, stat } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import process from 'node:process';
import { UmhError } from '../../contracts/index.js';
import { makeManualPlan, makeOwnedEntryPlan, parseConfigPlan } from '../config-plan.js';
import { buildPolicyBlock } from '../policy-block.js';
import { applyConfigPlan, loadReceiptById, removeOwnedConfig, saveReceipt, umhHomeOfPlan } from '../transaction.js';
/**
 * ZCode 的 owned 段：user 级原生 config.json → 嵌套 mcp.servers 命名空间下的
 * umh-memory（官方示例逐字核实：{"mcp":{"servers":{"<name>":{command,args,env}}}}）。
 */
export const ZCODE_OWNED_POINTER = '/mcp/servers/umh-memory';
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点工作区指令文件）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export const ZCODE_POLICY_INSTRUCTIONS = [
    '在回答前可调用 memory_status/memory_recall/memory_search/memory_get 读取项目共享记忆；读取失败不得阻塞回答。',
    '涉及用户偏好、项目决策、可复用事实时调用 memory_remember 提交记忆；一次只提交一条、正文自含。',
    '绝不把凭据、密钥或认证材料写入记忆正文。',
];
/**
 * ZCode 配方（= docs/clients/recipes/zcode.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[manual]=手工路径（配置缺失/非严格 JSON）、[auto]=自动
 * 路径附加指引。两节共享于 desktop|cli 双形态（官方单一配置面）。占位符
 * {{command}}/{{args}}/{{env}}/{{policy}} 由 preview 用 plan.launch 与模块策略插值。
 * 步骤按 2026-10-03 官方文档核实措辞。
 */
export const ZCODE_RECIPE = {
    variant: 'desktop|cli',
    version: '3.14.4',
    officialSource: 'https://www.zcode.network/en/docs/mcp-services/; cn: https://www.zcode.network/cn/docs/mcp-services/',
    verifiedAt: '2026-10-03',
    configLocation: "user-level native ~/.zcode/cli/config.json → mcp.servers (official scope 'User (all workspaces)'; hand-editing these files is officially supported 'to manage servers in bulk'; workspace scope <project>/.zcode/config.json is per-project and UMH never edits it; same-scope .agents/mcp.json compat files are fallback-only and are never written by UMH or the ZCode panel)",
    format: 'json',
    ownedPointer: "/mcp/servers/umh-memory inside ~/.zcode/cli/config.json — note the nested native structure mcp → servers → 'umh-memory' (not the flat mcpServers key); auto-edit only for strict JSON, anything else gets a manual fragment instead",
    policyLocation: "workspace agent-instruction file (AGENTS.md in this workspace; the official MCP docs do not document an instruction-file convention — verify against your own setup) — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers",
    manualSteps: [
        '[manual] UMH did not auto-edit the ZCode user-level native config (no safely auto-editable strict-JSON ~/.zcode/cli/config.json was found — it may be missing or not strict JSON, and UMH will not risk losing fields or comments). Prefer the panel route: Settings → MCP Servers → New MCP Server, scope User (all workspaces), name umh-memory, type stdio.',
        "[manual] Fill the form exactly: command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config. Hand-editing is officially supported ('to manage servers in bulk'): alternatively add the entry under mcp.servers in ~/.zcode/cli/config.json (the workspace scope <project>/.zcode/config.json is per-project; UMH never edits it).",
        '[manual] Enable the tools for the target agent: click Add, then confirm the umh-memory entry is enabled in the MCP Servers list — a disabled server is stored as "enable": false in its config object, and a server without the field counts as enabled. All configured servers are available to ZCode Agent; there is no per-assistant binding step.',
        '[manual] Start a new session: configured servers connect at session start (the official docs state workspace servers connect automatically at session start); confirm the umh-memory entry shows enabled.',
        "[manual] Inject the memory policy: paste the following block into the workspace agent-instruction file you keep (AGENTS.md in this workspace; the official MCP docs do not document an instruction-file convention — verify against your own setup), keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}",
        '[manual] Verify from a ZCode session: ask the agent to run memory_status; do not report onboarding success until the agent actually calls the tool.',
        '[auto] UMH wrote the umh-memory entry into the user-level native config ~/.zcode/cli/config.json under mcp.servers (a pre-edit backup and a rollback receipt were recorded under the UMH home). Open Settings → MCP Servers and confirm the entry is enabled in the list: a disabled server is stored as "enable": false in its config object, and a server without the field counts as enabled.',
        "[auto] Inject the memory policy: paste the following block into the workspace agent-instruction file you keep (AGENTS.md in this workspace; the official MCP docs do not document an instruction-file convention — verify against your own setup), keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}",
        '[auto] Start a new session and verify: ask the agent to run memory_status; do not report onboarding success until the agent actually calls the tool.',
    ],
    schemaValidated: true,
};
const RECEIPT_ID_PATTERN = /^rcpt_[0-9a-f]{32}$/;
const VARIANTS = new Set(['desktop', 'cli']);
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
        throw new UmhError('CONFIG_PATH_INVALID', `zcode config path is not readable: ${path}`);
    }
    return isStrictJsonObject(bytes.toString('utf8'));
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
    const policyBlock = buildPolicyBlock(ZCODE_POLICY_INSTRUCTIONS);
    return ZCODE_RECIPE.manualSteps
        .filter((step) => step.startsWith(prefix))
        .map((step) => interpolateStep(step.slice(prefix.length), launch, policyBlock));
}
function requireZcodeDetection(detection) {
    if (detection === null || typeof detection !== 'object' || detection.clientId !== 'zcode') {
        throw new UmhError('INVALID_ARGUMENT', 'zcode adapter requires a detection for clientId zcode');
    }
}
function requireVariant(variant) {
    // variant 决定写入路径（desktop/cli 共享同一文档化配置面）：未知变体响亮拒绝，
    // 绝不静默降级（静默手工会掩盖笔误，A03 先例）。
    if (!VARIANTS.has(variant)) {
        throw new UmhError('INVALID_ARGUMENT', `zcode variant must be 'desktop' or 'cli': ${String(variant)}`);
    }
}
function requireReceiptId(input) {
    const receiptId = input?.receiptId;
    if (typeof receiptId !== 'string' || !RECEIPT_ID_PATTERN.test(receiptId)) {
        throw new UmhError('RECEIPT_INVALID', `zcode adapter requires a rcpt_<32hex> receipt id: ${String(receiptId)}`);
    }
    return receiptId;
}
// ---------------------------------------------------------------------------
// detect（只读：stat + 严格 JSON 判读；绝不写盘、绝不读聊天/记忆内容）
// ---------------------------------------------------------------------------
/** 官方文档化的 user 级原生配置（"User (all workspaces)"）；desktop/cli 共享。 */
function userNativeConfigPath(home) {
    return join(home, '.zcode', 'cli', 'config.json');
}
/**
 * 发现模式只列官方已记录的候选（唯一文档化 user 级原生配置）；installed 只认该
 * 候选（桌面应用数据目录官方未文档化为安装证据，不探测 —— 避免其它工具的同名
 * 目录误报，A03 先例）。显式 opts.configPath = 独占选择（每目标独立 plan/apply）。
 */
async function detectShared(home, variant, configPath) {
    const documentedPath = userNativeConfigPath(home);
    if (typeof configPath === 'string' && configPath.trim() !== '') {
        const selected = configPath;
        const exists = await pathExists(selected);
        return {
            clientId: 'zcode',
            variant,
            version: null,
            installed: exists || (await pathExists(documentedPath)),
            configPaths: [selected],
            format: 'json',
            state: exists && (await autoEditable(selected)) ? 'verified' : 'unverified',
        };
    }
    const configPaths = (await pathExists(documentedPath)) ? [documentedPath] : [];
    const state = configPaths.length === 1 && (await autoEditable(configPaths[0])) ? 'verified' : 'unverified';
    return { clientId: 'zcode', variant, version: null, installed: configPaths.length > 0, configPaths, format: 'json', state };
}
// ---------------------------------------------------------------------------
// adapter 工厂（zcodeAdapter = 缺省单例；remove 需要绑定 umhHome 时用工厂）
// ---------------------------------------------------------------------------
/** 缺省 umhHome 解析：UMH_HOME 环境变量（connect CLI 同款约定；绝对路径才接受）。 */
function umhHomeFromEnv() {
    const value = process.env.UMH_HOME;
    return typeof value === 'string' && value.trim() !== '' && isAbsolute(value) ? value : null;
}
/**
 * 构造 ZCode 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 zcodeAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export function makeZcodeAdapter(options) {
    const boundHome = typeof options?.umhHome === 'string' && options.umhHome.trim() !== '' ? options.umhHome : null;
    async function resolveRemoveHome() {
        const home = boundHome ?? umhHomeFromEnv();
        if (home === null) {
            throw new UmhError('INVALID_ARGUMENT', 'zcode adapter remove needs a UMH home: construct via makeZcodeAdapter({ umhHome }) or set UMH_HOME (never guessed)');
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
            requireZcodeDetection(input?.detection);
            const { detection, binding, launch } = input;
            requireVariant(detection.variant);
            const paths = Array.isArray(detection.configPaths) ? detection.configPaths : [];
            // 多候选：绝不隐式选第一项（伪造/外来检出同拒 —— UMH 只认 detect 的独占选择）。
            if (paths.length > 1) {
                throw new UmhError('PROFILE_REQUIRED', `multiple ZCode config candidates (${paths.join('; ')}); UMH never picks one automatically — ` +
                    're-run detect with the explicit configPath of the file to connect ' +
                    '(the only documented auto-edit target is the user-level ~/.zcode/cli/config.json)');
            }
            if (paths.length === 0) {
                // 无检出：给已核实的面板/手工编辑步骤（创建或编辑 user 级原生配置）。
                return makeManualPlan('zcode', binding, launch, stepsFor('manual', launch));
            }
            const path = paths[0];
            if (!(await autoEditable(path))) {
                // 注释/非严格 JSON：JSON 往返无法证明注释/字段保留 → 手工片段，绝不破坏性 stringify。
                return makeManualPlan('zcode', binding, launch, stepsFor('manual', launch));
            }
            // 严格 JSON：A01 owned 段计划（只动 /mcp/servers/umh-memory，第三方内容绝不过写）。
            const plan = await makeOwnedEntryPlan({ ...detection, configPaths: [path], format: 'json' }, ZCODE_OWNED_POINTER, launch, binding);
            // 自动路径附加指引（enable 确认 + 策略块 + 新会话/核验）——逐字来自 recipe。
            return { ...plan, manualInstructions: stepsFor('auto', launch) };
        },
        async apply(plan) {
            const parsed = parseConfigPlan(plan);
            if (parsed.clientId !== 'zcode') {
                throw new UmhError('PLAN_INVALID', `zcode apply refuses plans for another client: ${parsed.clientId}`);
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
            // 诚实三态：适配层未做真实连通性探针（A09 才取得真实 ZCode 证据）；
            // agent 未实际调用工具前 sharedReadWrite 恒为 NOT_RUN（工单明示）。
            return { connection: 'UNVERIFIED', sharedReadWrite: 'NOT_RUN', policy: 'UNVERIFIED' };
        },
        async remove(input) {
            const receiptId = requireReceiptId(input);
            const receipt = await loadReceiptById(await resolveRemoveHome(), receiptId);
            // 手工计划回执（配置缺失/非严格 JSON 的手工路径，changes 恒空）：UMH 从未写过
            // 任何客户端配置字节，没有可自动移除的磁盘足迹 —— removed:false + 手工清理
            // 指引（cherry 先例：声称自动移除成功即为误报），绝不谎报 removed:true 零动作。
            if (receipt.changes.length === 0) {
                return {
                    removed: false,
                    conflicts: [
                        'zcode manual onboarding: this receipt recorded no UMH-written config changes, so there is nothing to auto-remove (removed stays false; claiming automated success would be a false report).',
                        'Manual cleanup: delete the hand-added umh-memory server entry (Settings → MCP Servers list, or the umh-memory object under mcp.servers in the user-level ~/.zcode/cli/config.json).',
                        "Manual cleanup: delete the whole UMH MEMORY POLICY block (BEGIN to END markers) from the workspace agent-instruction file (AGENTS.md in this workspace).",
                        'If the trusted binding file was created via umh connect apply, revoke it with: umh disconnect --receipt <receipt.json> (it lives in <umhHome>/config/bindings/ and has no ZCode-side counterpart).',
                    ],
                };
            }
            // 三方比对（A01 事务层）：用户后改 owned 段 → conflict 不触碰；无关编辑幸存。
            return removeOwnedConfig(receipt);
        },
    };
}
/** ZCode 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeZcodeAdapter 绑定）。 */
export const zcodeAdapter = makeZcodeAdapter();
