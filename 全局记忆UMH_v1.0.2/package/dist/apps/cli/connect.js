/**
 * apps/cli/connect — connect / disconnect 命令层（R01：真实 adapter 路由）。
 *
 * 命令（R01 冻结）：
 *   umh connect <clientId> --variant <v> --workspace <dir> --dry-run --out <plan.json>
 *       [--umh-home <path>] [--config-path <absolute path>]
 *   umh connect apply <plan.json>
 *   umh disconnect --receipt <receipt.json> [--umh-home <path>]
 *
 * R01 路由（补充工单 L108-114 代码块）：
 *   const adapter = resolveAdapter(clientId, { umhHome });
 *   const detection = await adapter.detect({ home: userHome(), variant,
 *     ...(configPath ? { configPath } : {}) });
 *   const plan = await adapter.preview({ detection, binding, launch });
 *
 * 信任属性（C08 裁决 + A 阶段全局约束）：
 *   - projectId 只由本命令经 C03 resolveProject(--workspace) 解析（受信管理面），
 *     绝不接受客户端/请求参数传入的绑定内容。
 *   - apply 步骤把完整 Binding（含已解析物理 projectId）经
 *     writeTrustedBindingFile 写入 `<umhHome>/config/bindings/<bindingId>.json`
 *     （C08 Gateway 读取侧契约：文件名= bindingId、防遍历校验、缺文件 fail-closed）。
 *   - 服务 token 绝不出现在 plan/receipt/绑定文件/env/argv/output（makeLaunchSpec
 *     的 env 恒为空；token 由 Gateway 从私有文件读取）。
 *   - --dry-run 零副作用（除用户显式 --out 请求的 plan 文件与 sidecar）。
 *
 * 落盘布局（--out 为 plan.json 时，均在本目录）：
 *   plan.json            ConfigPlan（受保护文件，mode 0o600）
 *   plan.binding.json    受信绑定 sidecar（apply 时消费；连接内容以 connect 解析为准）
 *   plan.receipt.json    apply 产出的 Receipt（disconnect --receipt 的输入）
 *
 * R01 去猜测（补充工单 L108）：
 *   - 去掉生产 detectGeneric（`<home>/.<clientId>/mcp.json` 探针）与
 *     `.<client>/mcp.json` 猜路径；configPath 只来自显式 --config-path（必须绝对
 *     路径，相对路径拒绝），不传即 adapter 发现模式；
 *   - 未知 variant 由 adapter 拒绝（本层不代判变体合法性）；
 *   - 未知数据库/内部应用数据一律不读（CLI 只读显式 configPath 与 adapter 内部
 *     的官方文档化候选）。
 */
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { isAbsolute, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import process from 'node:process';
import { UmhError } from '../../packages/contracts/index.js';
import { resolveAdapter } from '../../packages/adapters/registry.js';
import { applyConnection, deriveBindingId, disconnectConnection, loadConnectionRecord, manualCleanupNextActions, prepareConnection, reconcileConnection, trustManualConnection, variantOfBindingId, } from '../../packages/adapters/connection.js';
import { resolveProject } from '../../packages/scope/identity.js';
import { makeLaunchSpec } from '../../packages/adapters/launch.js';
import { parseConfigPlan } from '../../packages/adapters/config-plan.js';
import { writeProtectedJson, loadReceiptById, loadReceiptFile, } from '../../packages/adapters/transaction.js';
import { UsageError } from './lifecycle.js';
export const CLIENT_IDS = new Set([
    'cherry', 'qoder', 'zcode', 'codex', 'claude', 'minimax', 'dsh', 'generic',
]);
const VARIANT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
function argValue(argv, name) {
    const idx = argv.indexOf(name);
    if (idx < 0 || idx + 1 >= argv.length)
        return undefined;
    return argv[idx + 1];
}
function out(payload) {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
}
function diag(message) {
    process.stderr.write(`${message}\n`);
}
function umhHomeDefault(argv) {
    const flag = argValue(argv, '--umh-home');
    if (flag !== undefined && flag.trim() !== '')
        return flag;
    if (process.env.UMH_HOME !== undefined && process.env.UMH_HOME.trim() !== '')
        return process.env.UMH_HOME;
    return join(homedir(), '.universal-memory-hub');
}
/** 仅取显式 --umh-home（disconnect 的绑定清理绝不猜默认家目录）。 */
function flagUmhHome(argv) {
    const flag = argValue(argv, '--umh-home');
    return flag !== undefined && flag.trim() !== '' ? flag : null;
}
/** gateway 入口的绝对路径（与本模块同为 dist 产物，相对位置固定）。 */
function gatewayJsPath() {
    return fileURLToPath(new URL('../../apps/gateway/main.js', import.meta.url));
}
function requireSafeVariant(variant) {
    if (variant === undefined || !VARIANT_PATTERN.test(variant)) {
        throw new UsageError('--variant <v> is required (letters/digits/._-, 1-64 chars)');
    }
    return variant;
}
function userHome() {
    return process.env.USERPROFILE ?? process.env.HOME ?? homedir();
}
/** 显式 --config-path：必须绝对路径（相对路径拒绝）；缺省 = adapter 发现模式。 */
function explicitConfigPath(rest) {
    const flag = argValue(rest, '--config-path');
    if (flag === undefined)
        return undefined;
    if (flag.trim() === '' || !isAbsolute(flag)) {
        throw new UsageError('--config-path must be an absolute path');
    }
    return flag;
}
/** 严格参数解析：已知 flag 白名单之外一律拒绝（防拼写错误静默通过）。 */
function requireKnownPreviewFlags(rest) {
    const known = new Set([
        '--variant', '--workspace', '--dry-run', '--out', '--umh-home', '--config-path',
    ]);
    for (let i = 0; i < rest.length; i++) {
        const token = rest[i];
        if (!token.startsWith('--'))
            continue;
        if (!known.has(token)) {
            throw new UsageError(`unknown flag for connect preview: ${token}`);
        }
        if (token === '--dry-run')
            continue;
        const value = rest[i + 1];
        if (value === undefined || value.startsWith('--')) {
            throw new UsageError(`flag ${token} requires a value`);
        }
        i++;
    }
}
/** umh connect <clientId> --variant <v> --workspace <dir> --dry-run --out <plan.json> [--config-path <abs>] */
export async function runConnectRoute(argv) {
    const [clientId, ...rest] = argv;
    if (clientId === undefined || !CLIENT_IDS.has(clientId)) {
        throw new UsageError(`connect requires a clientId: ${[...CLIENT_IDS].join('|')}`);
    }
    requireKnownPreviewFlags(rest);
    const variant = requireSafeVariant(argValue(rest, '--variant'));
    const workspace = argValue(rest, '--workspace');
    if (workspace === undefined || workspace.trim() === '') {
        throw new UsageError('--workspace <dir> is required');
    }
    if (!rest.includes('--dry-run')) {
        throw new UsageError('A01 connect supports --dry-run only; apply afterwards with: umh connect apply <plan.json>');
    }
    const outPath = argValue(rest, '--out');
    if (outPath === undefined || outPath.trim() === '') {
        throw new UsageError('--out <plan.json> is required');
    }
    if (!isAbsolute(outPath)) {
        throw new UsageError('--out must be an absolute path');
    }
    const configPath = explicitConfigPath(rest);
    const umhHome = umhHomeDefault(rest);
    // R01 真实 adapter 路由（补充工单 L110-114 代码块）：resolve → detect → preview。
    // detect 消费 {home, variant, configPath}；preview 消费原冻结对象 {detection, binding, launch}。
    const adapter = resolveAdapter(clientId, { umhHome });
    const detection = await adapter.detect({
        home: userHome(),
        variant,
        ...(configPath !== undefined ? { configPath } : {}),
    });
    // 受信管理面解析：projectId 只来自 C03 resolveProject(--workspace)。
    // bindingId 为 hash 化唯一 ID（R02）：client-variant- +
    // SHA256(norm UMH root/workspace/配置目标) 前 24hex；同一 projectId 可共享
    // worker，不同 workspace 绝不共享连接 ID。
    const resolution = await resolveProject(workspace);
    const configTarget = configPath ?? detection.configPaths[0] ?? resolution.canonicalWorkspace;
    const bindingId = deriveBindingId({
        clientId: clientId,
        variant,
        workspace: resolution.canonicalWorkspace,
        target: configTarget,
        umhHome,
    });
    const binding = {
        clientId: clientId,
        role: 'recorder',
        bindingId,
        projectId: resolution.projectId,
        workspace: resolution.canonicalWorkspace,
    };
    const launch = makeLaunchSpec(binding, {
        nodeExecutable: process.execPath,
        gatewayJs: gatewayJsPath(),
        umhHome,
    });
    const plan = await adapter.preview({ detection, binding, launch });
    const sidecarPath = sidecarOf(outPath, '.binding.json');
    const receiptPath = sidecarOf(outPath, '.receipt.json');
    // prepared 意图持久（R02）：prepareConnection 只校验形状/hash 并落盘
    // `<umhHome>/runtime/connections/<connectionId>.json`，不写客户端、不发布
    // binding。record 记录 plan/binding 的 sidecar 引用与 hash。
    const record = await prepareConnection({
        plan,
        binding,
        variant,
        ...(configPath !== undefined ? { selectedConfigPath: configPath } : {}),
        umhHome,
        planPath: outPath,
        bindingPath: sidecarPath,
    });
    const connectionPath = joinConnectionPath(umhHome, record.connectionId);
    await writeProtectedJson(outPath, plan);
    await writeProtectedJson(sidecarPath, binding);
    out({
        status: 'planned',
        planId: plan.id,
        planPath: outPath,
        bindingPath: sidecarPath,
        receiptPath,
        bindingId: binding.bindingId,
        connectionId: record.connectionId,
        connectionPath,
        changes: plan.changes.length,
        manualInstructions: plan.manualInstructions.length,
        detectionState: detection.state,
    });
    return 0;
}
function sidecarOf(planPath, suffix) {
    return planPath.replace(/\.json$/i, '') + suffix;
}
/** 受保护 connection record 的落盘路径（与 connection.ts connectionFile 同源）。 */
function joinConnectionPath(umhHome, connectionId) {
    return join(umhHome, 'runtime', 'connections', `${connectionId}.json`);
}
/** `--fail-after <stage>` 只认合成值 config|binding（测试合成故障；生产无公开 fault hook）。 */
function parseApplyArgv(argv) {
    let planPath;
    let failAfter = null;
    for (let i = 0; i < argv.length; i++) {
        const token = argv[i];
        if (token === '--fail-after') {
            const value = argv[i + 1];
            if (value !== 'config' && value !== 'binding') {
                throw new UsageError('--fail-after must be config|binding');
            }
            failAfter = value;
            i++;
        }
        else if (token.startsWith('--')) {
            throw new UsageError(`unknown flag for connect apply: ${token}`);
        }
        else if (planPath === undefined) {
            planPath = token;
        }
        else {
            throw new UsageError('connect apply accepts a single <plan.json>');
        }
    }
    if (planPath === undefined || planPath.trim() === '') {
        throw new UsageError('connect apply requires <plan.json>');
    }
    return { planPath, failAfter };
}
/** umh connect apply <plan.json> [--fail-after <config|binding>] */
export async function runConnectApplyRoute(argv) {
    const { planPath, failAfter } = parseApplyArgv(argv);
    let planRaw;
    try {
        planRaw = JSON.parse(await readFile(planPath, 'utf8'));
    }
    catch (err) {
        throw new UmhError('PLAN_INVALID', `plan file is not readable JSON (${planPath}): ${err.message}`);
    }
    const plan = parseConfigPlan(planRaw);
    const sidecarPath = sidecarOf(planPath, '.binding.json');
    let bindingRaw;
    try {
        bindingRaw = JSON.parse(await readFile(sidecarPath, 'utf8'));
    }
    catch (err) {
        throw new UmhError('PLAN_BINDING_MISSING', `trusted binding sidecar is missing (${sidecarPath}); re-run umh connect --dry-run to regenerate it (${err.message})`);
    }
    // R02 编排（补充工单 L140-147 代码块与顺序）：
    //   prepared 意图持久 → adapter.apply 一次 → loadReceiptById →
    //   record config_applied → 发布 binding → record 最终状态。
    // binding 只由 CLI 受信 workspace 解析的 sidecar 提供；record 与 plan/binding
    // hash 吻合后行动（篡改拒绝）。legacy sidecar（无 record）先重建 prepared 意图。
    const args = plan.launch.args;
    const homeIdx = args.indexOf('--umh-home');
    if (homeIdx < 0 || homeIdx + 1 >= args.length) {
        throw new UmhError('PLAN_INVALID', 'plan.launch.args must carry --umh-home (makeLaunchSpec contract)');
    }
    const umhHome = args[homeIdx + 1];
    if (failAfter !== null) {
        process.env.UMH_R02_FAIL_AFTER = failAfter;
    }
    let record;
    try {
        record = await loadConnectionRecord(umhHome, plan.bindingId);
    }
    catch (err) {
        if (!(err instanceof UmhError) || err.code !== 'CONNECTION_UNKNOWN')
            throw err;
        // legacy sidecar（R01 及更早 preview 产物）：重建 prepared 意图，不覆盖已有绑定。
        const variant = variantOfBindingId(plan.clientId, plan.bindingId);
        record = await prepareConnection({
            plan,
            binding: bindingRaw,
            variant,
            umhHome,
            planPath,
            bindingPath: sidecarPath,
        });
    }
    // 显式 root 与 record 不符即 CONNECTION_ROOT_MISMATCH（load 后 requireSameRoot 抛）。
    if (record.planPath !== planPath || record.bindingPath !== sidecarPath) {
        throw new UmhError('PLAN_BINDING_MISSING', `trusted sidecar location differs from the recorded one (plan=${planPath}, binding=${sidecarPath}); re-run umh connect --dry-run to regenerate it`);
    }
    const beforeApply = await loadConnectionRecord(umhHome, record.connectionId);
    const hadReceipt = beforeApply.receiptId !== undefined;
    const result = await applyConnection(record.connectionId, { umhHome });
    const settled = await loadConnectionRecord(umhHome, record.connectionId);
    const receiptSidecar = sidecarOf(planPath, '.receipt.json');
    // <plan>.receipt.json 沿 R01 布局保留：有 receiptId 即写该次回执快照（幂等重放
    // 不作为"本次发布"证据，applied 另行判定）；manual/失败路径 receiptPath=null。
    let receiptOutPath = null;
    if (settled.receiptId !== undefined) {
        const stored = await loadReceiptById(umhHome, settled.receiptId);
        await writeProtectedJson(receiptSidecar, stored);
        receiptOutPath = receiptSidecar;
    }
    const bindingPath = join(umhHome, 'config', 'bindings', `${record.bindingId}.json`);
    // applied = 本次 adapter.apply 是否确有发布（A01 published 语义）：
    //   - 手工计划恒 false；
    //   - 本次调用前已有 receiptId（此前已调过 apply，含恢复分支）→ false；
    //   - 本次新得 receiptId（prepared → config_applied → …）→ true。
    // 失败一律走 UmhError 非 0（state=needs_reconcile），绝不报 applied:true 假成功。
    const applied = plan.changes.length > 0 && !hadReceipt && settled.receiptId !== undefined;
    delete process.env.UMH_R02_FAIL_AFTER;
    out({
        status: result.state === 'needs_reconcile' ? 'needs_reconcile' : result.state === 'configured' ? 'applied' : result.state,
        receiptId: settled.receiptId ?? null,
        receiptPath: receiptOutPath,
        bindingId: record.bindingId,
        bindingPath,
        connectionId: record.connectionId,
        connectionPath: joinConnectionPath(umhHome, record.connectionId),
        state: result.state,
        configured: result.configured,
        nativeVerified: false,
        applied,
        conflicts: result.conflicts,
        nextActions: result.nextActions,
    });
    return 0;
}
/** umh connect reconcile --connection <id> --umh-home <root> */
export async function runConnectReconcileRoute(argv) {
    const connectionId = argValue(argv, '--connection');
    if (connectionId === undefined || connectionId.trim() === '') {
        throw new UsageError('connect reconcile requires --connection <id>');
    }
    const umhHome = argValue(argv, '--umh-home');
    if (umhHome === undefined || umhHome.trim() === '') {
        throw new UsageError('connect reconcile requires --umh-home <root>');
    }
    for (const token of argv) {
        if (!token.startsWith('--'))
            continue;
        if (token !== '--connection' && token !== '--umh-home') {
            throw new UsageError(`unknown flag for connect reconcile: ${token}`);
        }
    }
    const idValue = argv[argv.indexOf('--connection') + 1];
    const homeValue = argv[argv.indexOf('--umh-home') + 1];
    if (idValue === undefined || idValue.startsWith('--') || homeValue === undefined || homeValue.startsWith('--')) {
        throw new UsageError('connect reconcile requires --connection <id> --umh-home <root>');
    }
    const result = await reconcileConnection(idValue, { umhHome: homeValue });
    out({
        status: result.state,
        connectionId: result.connectionId,
        connectionPath: joinConnectionPath(homeValue, result.connectionId),
        state: result.state,
        configured: result.configured,
        nativeVerified: false,
        conflicts: result.conflicts,
        nextActions: result.nextActions,
    });
    return 0;
}
/**
 * umh connect trust --connection <id> --umh-home <root> --confirm-reviewed
 *
 * 手工连接信任链（补充设计 2026-10-06 §2.2）：管理者显式确认已审阅计划与手工
 * 配置范围后，经既有受信管理面（writeTrustedBindingFile）发布该连接的受信绑定。
 * 语义与拒绝面见 trustManualConnection 合同；本层只做参数防御与 MANUAL_CONFIRM_REQUIRED
 * 的富化输出（nextActions + nativeVerified=false，绝不伪称模型/native 验证）。
 */
export async function runConnectTrustRoute(argv) {
    const connectionId = argValue(argv, '--connection');
    if (connectionId === undefined || connectionId.trim() === '') {
        throw new UsageError('connect trust requires --connection <id>');
    }
    const umhHome = argValue(argv, '--umh-home');
    if (umhHome === undefined || umhHome.trim() === '') {
        throw new UsageError('connect trust requires --umh-home <root>');
    }
    const confirmReviewed = argv.includes('--confirm-reviewed');
    for (const token of argv) {
        if (!token.startsWith('--'))
            continue;
        if (token !== '--connection' && token !== '--umh-home' && token !== '--confirm-reviewed') {
            throw new UsageError(`unknown flag for connect trust: ${token}`);
        }
    }
    // 防御：值位是另一个 flag（如 --connection --umh-home）时按缺失处理。
    if (connectionId.startsWith('--') || umhHome.startsWith('--')) {
        throw new UsageError('connect trust requires --connection <id> --umh-home <root>');
    }
    try {
        const result = await trustManualConnection(connectionId, { umhHome, confirmReviewed });
        out({
            status: result.state,
            connectionId: result.connectionId,
            connectionPath: joinConnectionPath(umhHome, result.connectionId),
            state: result.state,
            configured: result.configured,
            bindingPublished: result.bindingPublished ?? false,
            nativeVerified: false,
            conflicts: result.conflicts,
            nextActions: result.nextActions,
        });
        // needs_reconcile（合成中断/恢复中间态）按失败语义非零退出——状态已如实落盘。
        return result.state === 'needs_reconcile' ? 1 : 0;
    }
    catch (err) {
        // MANUAL_CONFIRM_REQUIRED 富化：确认指引与 nativeVerified=false 同源输出。
        if (err instanceof UmhError && err.code === 'MANUAL_CONFIRM_REQUIRED') {
            const record = await loadConnectionRecord(umhHome, connectionId).catch(() => null);
            out({
                error: {
                    code: err.code,
                    message: err.message,
                    connectionId,
                    nativeVerified: false,
                    nextActions: [
                        'review the plan (plan.json manualInstructions) and the hand-configuration scope with the manager',
                        `re-run: umh connect trust --connection ${connectionId} --umh-home <root> --confirm-reviewed`,
                        'publishing the binding is NOT native PASS; real tool-call verification happens in the field drill',
                    ],
                    ...(record !== null && record.state === 'manual_pending' ? { state: record.state } : {}),
                },
            });
            diag(`umh connect trust failed: [${err.code}] ${err.message}`);
            return 1;
        }
        throw err;
    }
}
/** 从回执 changes 的 afterOwned（{command,args,env}）推导 umhHome（disconnect 的尽力路径）。 */
function umhHomeFromReceipt(receipt) {
    for (const change of receipt.changes) {
        const after = change.afterOwned;
        if (after !== null && typeof after === 'object' && !Array.isArray(after)) {
            const args = after.args;
            if (Array.isArray(args)) {
                const idx = args.indexOf('--umh-home');
                if (idx >= 0 && idx + 1 < args.length && typeof args[idx + 1] === 'string') {
                    return args[idx + 1];
                }
            }
        }
    }
    return null;
}
/**
 * umh disconnect --connection <id> --umh-home <root> [--confirm-manual-removal]
 * umh disconnect --receipt <receipt.json> [--umh-home <path>]（legacy 兼容）
 *
 * R03 路由（补充工单 L170-176 代码块与顺序）：
 *   - root 从显式参数或 record 取，两者冲突拒绝（CONNECTION_ROOT_MISMATCH，
 *     由 disconnectConnection 内 requireSameRoot 抛）；
 *   - legacy 仅安全读取 receipt launch 推断（umhHomeFromReceipt），手工无 root
 *     拒绝（CONNECTION_ROOT_REQUIRED），绝不猜默认家目录；
 *   - 自动先 adapter.remove，配置冲突保留 binding；成功后检查 bindingHash 相符再删；
 *   - 手工返回 manual_cleanup_required 与步骤，无确认非 0；只有明确
 *     --confirm-manual-removal 后可清本连接 binding，仍不伪称模型/native 验证；
 *   - delete 失败返回 needs_reconcile 且非 0（connection.ts 内处理，本层只透传）。
 */
export async function runDisconnectRoute(argv) {
    const connectionId = argValue(argv, '--connection');
    const receiptPath = argValue(argv, '--receipt');
    const confirmManualRemoval = argv.includes('--confirm-manual-removal');
    for (const token of argv) {
        if (!token.startsWith('--'))
            continue;
        if (token !== '--connection' && token !== '--receipt' && token !== '--umh-home' && token !== '--confirm-manual-removal') {
            throw new UsageError(`unknown flag for disconnect: ${token}`);
        }
    }
    if (connectionId !== undefined && receiptPath !== undefined) {
        throw new UsageError('disconnect accepts either --connection or --receipt, not both');
    }
    if (confirmManualRemoval && connectionId === undefined) {
        throw new UsageError('--confirm-manual-removal requires --connection <id>');
    }
    // --connection 主路径：显式 root 必填（与 record 比对，不符即 MISMATCH）。
    if (connectionId !== undefined) {
        if (connectionId.trim() === '')
            throw new UsageError('disconnect requires --connection <id>');
        const umhHome = argValue(argv, '--umh-home');
        if (umhHome === undefined || umhHome.trim() === '') {
            throw new UsageError('disconnect --connection requires --umh-home <root>');
        }
        try {
            const result = await disconnectConnection(connectionId, { umhHome, confirmManualRemoval });
            out({
                status: result.state,
                connectionId: result.connectionId,
                connectionPath: joinConnectionPath(umhHome, result.connectionId),
                state: result.state,
                configured: result.configured,
                nativeVerified: false,
                bindingRemoved: result.bindingRemoved ?? false,
                conflicts: result.conflicts,
                nextActions: result.nextActions,
            });
            return 0;
        }
        catch (err) {
            // MANUAL_CLEANUP_REQUIRED 富化：步骤与 nativeVerified=false 同源输出
            // （绝不伪称模型/native 验证），其余 UmhError 由 main.ts 统一报非 0。
            if (err instanceof UmhError && err.code === 'MANUAL_CLEANUP_REQUIRED') {
                const record = await loadConnectionRecord(umhHome, connectionId).catch(() => null);
                const nextActions = record !== null ? manualCleanupNextActions(record) : [];
                out({
                    error: {
                        code: err.code,
                        message: err.message,
                        connectionId,
                        nativeVerified: false,
                        nextActions,
                    },
                });
                diag(`umh disconnect failed: [${err.code}] ${err.message}`);
                return 1;
            }
            throw err;
        }
    }
    // legacy --receipt 兼容：不能猜 home。root 只取显式 --umh-home 或回执 launch
    // 推导；两者皆无（手工回执无 changes）→ CONNECTION_ROOT_REQUIRED 响亮拒绝。
    if (receiptPath === undefined || receiptPath.trim() === '') {
        throw new UsageError('disconnect requires --connection <id> or --receipt <receipt.json>');
    }
    const receipt = await loadReceiptFile(receiptPath);
    const explicitHome = flagUmhHome(argv);
    const inferredHome = umhHomeFromReceipt(receipt);
    if (explicitHome !== null && inferredHome !== null && explicitHome !== inferredHome) {
        throw new UmhError('CONNECTION_ROOT_MISMATCH', 'explicit --umh-home does not match the UMH home recorded in the receipt; re-run with the recorded root');
    }
    const umhHome = explicitHome ?? inferredHome;
    if (umhHome === null) {
        // Minor-N1：手工 receipt 无 changes、无可信 root —— 旧行为静默跳过清理报
        // removed；现必须响亮拒绝（CONNECTION_ROOT_REQUIRED）并给出下一动作。
        out({
            error: {
                code: 'CONNECTION_ROOT_REQUIRED',
                message: 'provide a verified UMH root: re-run with an explicit --umh-home (a manual receipt carries no trusted root)',
                bindingId: receipt.bindingId,
                nativeVerified: false,
                nextActions: [
                    `re-run: umh disconnect --receipt ${receiptPath} --umh-home <root>`,
                    'for a prepared connection prefer: umh disconnect --connection <id> --umh-home <root>',
                ],
            },
        });
        diag('umh disconnect failed: [CONNECTION_ROOT_REQUIRED] provide a verified UMH root');
        return 1;
    }
    // legacy 有根：按可信 record client/root 选 factory 调 adapter.remove
    // （不直接 removeOwnedConfig —— 端别 TOML/YAML 三方比对在 adapter 侧）。
    // 先找该 bindingId 归属的 connection record（hash 化 ID 即 connectionId）。
    let record = null;
    try {
        record = await loadConnectionRecord(umhHome, receipt.bindingId);
    }
    catch (err) {
        if (!(err instanceof UmhError) || err.code !== 'CONNECTION_UNKNOWN')
            throw err;
    }
    if (record !== null) {
        // 有 record 即走统一 disconnectConnection（显式 root 已比对候选；
        // record 内 requireSameRoot 复核；手工仍需确认门）。
        try {
            const result = await disconnectConnection(record.connectionId, { umhHome, confirmManualRemoval: false });
            out({
                status: result.state,
                removed: result.state === 'removed',
                connectionId: result.connectionId,
                connectionPath: joinConnectionPath(umhHome, result.connectionId),
                state: result.state,
                configured: result.configured,
                nativeVerified: false,
                bindingId: receipt.bindingId,
                bindingRemoved: result.bindingRemoved ?? false,
                conflicts: result.conflicts,
                nextActions: result.nextActions,
            });
            return 0;
        }
        catch (err) {
            if (err instanceof UmhError && err.code === 'MANUAL_CLEANUP_REQUIRED') {
                const nextActions = manualCleanupNextActions(record);
                out({
                    error: {
                        code: err.code,
                        message: err.message,
                        connectionId: record.connectionId,
                        nativeVerified: false,
                        nextActions,
                    },
                });
                diag(`umh disconnect failed: [${err.code}] ${err.message}`);
                return 1;
            }
            throw err;
        }
    }
    // 无 record 的 legacy 回执（R01 及更早产物）：按回执 client 选 factory 调
    // adapter.remove；binding 文件归属用回执 bindingId 定位；delete 失败绝不吞错。
    const { deleteTrustedBindingFile } = await import('../../packages/adapters/transaction.js');
    const adapter = resolveAdapter(receipt.clientId, { umhHome });
    let removal;
    try {
        removal = await adapter.remove({ receiptId: receipt.id });
    }
    catch (err) {
        throw err;
    }
    if (!removal.removed) {
        out({
            status: 'conflict',
            removed: false,
            conflicts: removal.conflicts,
            bindingId: receipt.bindingId,
            bindingRemoved: false,
            nativeVerified: false,
            nextActions: [`resolve the conflicts above, then re-run: umh disconnect --receipt ${receiptPath} --umh-home ${umhHome}`],
        });
        return 1;
    }
    let bindingRemoved = false;
    try {
        bindingRemoved = await deleteTrustedBindingFile(umhHome, receipt.bindingId);
    }
    catch (err) {
        // delete 失败不得 catch(()=>false) 后整体报 removed；needs_reconcile 且非 0。
        out({
            error: {
                code: 'BINDING_DELETE_FAILED',
                message: err instanceof UmhError ? `[${err.code}] ${err.message}` : String(err?.message ?? err),
                state: 'needs_reconcile',
                bindingId: receipt.bindingId,
                nativeVerified: false,
                nextActions: [`inspect <umhHome>/config/bindings/${receipt.bindingId}.json, then re-run: umh disconnect --receipt ${receiptPath} --umh-home ${umhHome}`],
            },
        });
        diag(`umh disconnect failed: [BINDING_DELETE_FAILED] binding file could not be deleted`);
        return 1;
    }
    out({
        status: 'removed',
        removed: true,
        conflicts: [],
        bindingId: receipt.bindingId,
        bindingRemoved,
        nativeVerified: false,
        nextActions: [],
    });
    return 0;
}
