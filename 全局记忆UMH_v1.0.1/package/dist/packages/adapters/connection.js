/**
 * packages/adapters/connection — 连接编排与安装对账（R02）。
 *
 * 合同（补充工单 R02 L119-149 + 补充设计 §5）：
 *   deriveBindingId({clientId,variant,workspace,target,umhHome}):string —
 *     `client-variant-` + SHA256(norm UMH root/workspace/配置目标) 前 24hex；
 *     不同 workspace 不共享连接 ID，同一 projectId 可共享 worker。
 *   prepareConnection({plan,binding,variant,selectedConfigPath?,umhHome?,
 *     planPath?,bindingPath?}):Promise<ConnectionRecord> — hash 吻合后持久化
 *     prepared 意图（manual 计划即 manual_pending），不写客户端/不发布 binding。
 *   applyConnection(recordId,{umhHome}):Promise<ConnectionResult> —
 *     prepared 意图持久 → adapter.apply 一次 → loadReceiptById →
 *     record config_applied → 发布 binding → record 最终状态。
 *   reconcileConnection(recordId,{umhHome}):Promise<ConnectionResult> —
 *     按 record 与配置/binding 实际 hash 对账，不盲目重发 adapter.apply
 *     （已有 receiptId 时绝不再次调用 apply）。
 *
 * ConnectionRecord（补充设计 §5 逐字 + planPath/bindingPath 引用字段，无正文；
 *   2026-10-06 补充：managerConfirmedAt? —— 手工连接信任链（connect trust）
 *   的管理者确认时间戳，幂等重入不覆盖）：
 *   {version:1;connectionId;clientId;variant;umhHome;bindingId;bindingHash;
 *    planId;planHash;receiptId?;receiptPath?;selectedConfigPath?;
 *    planPath?;bindingPath?;managerConfirmedAt?;state;error?;updatedAt}
 * state ∈ prepared|config_applied|binding_published|manual_pending|
 *          configured|needs_reconcile|removed。
 * 落盘 `<umhHome>/runtime/connections/<connectionId>.json`（受保护 0o600，
 * 原子写）；不存记忆正文或 secret（只存引用与 hash；LaunchSpec env 恒空，
 * token 由 Gateway 从私有文件读取，结构上不可能进入 record）。
 *
 * 纪律：
 *   - binding 内容只来自 CLI 受信 workspace 解析的 sidecar（本模块只校验形状/
 *     物理 projectId 与 hash 吻合，绝不从客户端参数构造绑定内容）；
 *   - record 级严格锁串行（`<umhHome>/config/conn-locks/<id>.lock`，C01 同型
 *     O_EXCL+有界重试+超龄让位）；配置自身仍用 A01 锁，固定先 connection
 *     后 config 的锁顺序；
 *   - 目标 owned 配置占用即冲突（OWNED_POINTER_OCCUPIED 由 adapter/事务层抛出，
 *     本层记 needs_reconcile 后原样抛出，不覆盖）；
 *   - 失败记 needs_reconcile + 明确 error，不吞异常；
 *   - 只有 hash 与 ownership 证明正确才恢复或撤销；
 *   - registry 复用（resolveAdapter 拿同一 adapter）；
 *   - 合成故障只认进程 env `UMH_R02_FAIL_AFTER=config|binding`（测试合成场景用；
 *     CLI 不设该 env 即无注入面，生产无公开 fault hook）。
 */
import { createHash, randomBytes } from 'node:crypto';
import { mkdir, open, readFile, rename, rm, stat, writeFile } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join } from 'node:path';
import process from 'node:process';
import { UmhError } from '../contracts/index.js';
import { normalizeWorkspacePath } from '../scope/identity.js';
import { parseConfigPlan, sha256Hex } from './config-plan.js';
import { isValidBindingId } from './contracts.js';
import { deleteTrustedBindingFile, loadReceiptById, umhHomeOfPlan, writeTrustedBindingFile, } from './transaction.js';
import { resolveAdapter } from './registry.js';
const CLIENT_IDS = new Set([
    'cherry', 'qoder', 'zcode', 'codex', 'claude', 'minimax', 'dsh', 'generic',
]);
const VARIANT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
/** hash 化 bindingId：`client-variant-`＋SHA256(norm root/workspace/目标) 前 24hex。 */
export function deriveBindingId(input) {
    const { clientId, variant, workspace, target, umhHome } = input ?? {};
    if (!CLIENT_IDS.has(String(clientId))) {
        throw new UmhError('UNKNOWN_CLIENT', `unknown client: ${String(clientId)}`);
    }
    if (typeof variant !== 'string' || !VARIANT_PATTERN.test(variant)) {
        throw new UmhError('INVALID_ARGUMENT', 'variant must be letters/digits/._-, 1-64 chars');
    }
    for (const [field, value] of [['workspace', workspace], ['target', target], ['umhHome', umhHome]]) {
        if (typeof value !== 'string' || value.trim() === '') {
            throw new UmhError('INVALID_ARGUMENT', `${field} must be a non-empty string`);
        }
    }
    const norm = [
        normalizeWorkspacePath(umhHome),
        normalizeWorkspacePath(workspace),
        normalizeWorkspacePath(target),
    ].join('\n');
    const hash = sha256Hex(norm).slice(0, 24);
    const bindingId = `${clientId}-${variant}-${hash}`;
    if (!isValidBindingId(bindingId)) {
        throw new UmhError('INVALID_BINDING_ID', `derived binding id is not a safe file name: ${bindingId}`);
    }
    return bindingId;
}
/**
 * 从 bindingId 反推 variant（apply 侧兼容旧 sidecar：record 缺失时重建意图用）。
 * hash 后缀固定 `-<24hex>`；legacy `client-variant` 无后缀。
 */
export function variantOfBindingId(clientId, bindingId) {
    const prefix = `${clientId}-`;
    if (typeof bindingId !== 'string' || !bindingId.startsWith(prefix)) {
        throw new UmhError('PLAN_BINDING_MISMATCH', 'plan bindingId does not belong to its client');
    }
    const rest = bindingId.slice(prefix.length);
    const hashed = /-([0-9a-f]{24})$/.exec(rest);
    const variant = hashed ? rest.slice(0, -25) : rest;
    if (!VARIANT_PATTERN.test(variant)) {
        throw new UmhError('PLAN_BINDING_MISMATCH', `cannot recover a safe variant from binding id: ${bindingId}`);
    }
    return variant;
}
// ---------------------------------------------------------------------------
// record 持久化与锁
// ---------------------------------------------------------------------------
function connectionFile(umhHome, connectionId) {
    return join(umhHome, 'runtime', 'connections', `${connectionId}.json`);
}
function stableStringify(value) {
    return JSON.stringify(value);
}
function bindingHashOf(binding) {
    return sha256Hex(stableStringify(binding));
}
function planHashOf(plan) {
    return sha256Hex(stableStringify(plan));
}
const ALLOWED_RECORD_FIELDS = new Set([
    'version', 'connectionId', 'clientId', 'bindingId', 'variant', 'umhHome',
    'bindingHash', 'planId', 'planHash', 'receiptId', 'receiptPath',
    'selectedConfigPath', 'planPath', 'bindingPath', 'managerConfirmedAt',
    'state', 'error', 'updatedAt',
]);
const STATES = new Set([
    'prepared', 'config_applied', 'binding_published', 'manual_pending',
    'configured', 'needs_reconcile', 'removed',
]);
function parseConnectionRecord(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        throw new UmhError('CONNECTION_INVALID', 'connection record must be a JSON object');
    }
    const r = value;
    for (const key of Object.keys(r)) {
        if (!ALLOWED_RECORD_FIELDS.has(key)) {
            throw new UmhError('CONNECTION_INVALID', `unknown connection record field: ${key}`);
        }
    }
    if (r['version'] !== 1)
        throw new UmhError('CONNECTION_INVALID', 'connection record version must be 1');
    if (!isValidBindingId(r['connectionId']))
        throw new UmhError('CONNECTION_INVALID', 'connection record has a bad connectionId');
    if (!CLIENT_IDS.has(String(r['clientId'])))
        throw new UmhError('CONNECTION_INVALID', 'connection record has an unknown clientId');
    if (typeof r['variant'] !== 'string' || r['variant'].trim() === '') {
        throw new UmhError('CONNECTION_INVALID', 'connection record variant must be a non-empty string');
    }
    if (typeof r['umhHome'] !== 'string' || r['umhHome'].trim() === '' || !isAbsolute(r['umhHome'])) {
        throw new UmhError('CONNECTION_INVALID', 'connection record umhHome must be an absolute path');
    }
    if (!isValidBindingId(r['bindingId']))
        throw new UmhError('CONNECTION_INVALID', 'connection record has a bad bindingId');
    if (typeof r['bindingHash'] !== 'string' || !/^[0-9a-f]{64}$/.test(r['bindingHash'])) {
        throw new UmhError('CONNECTION_INVALID', 'connection record bindingHash must be sha256 hex');
    }
    if (typeof r['planId'] !== 'string' || r['planId'] === '') {
        throw new UmhError('CONNECTION_INVALID', 'connection record planId must be a non-empty string');
    }
    if (typeof r['planHash'] !== 'string' || !/^[0-9a-f]{64}$/.test(r['planHash'])) {
        throw new UmhError('CONNECTION_INVALID', 'connection record planHash must be sha256 hex');
    }
    if (!STATES.has(String(r['state'])))
        throw new UmhError('CONNECTION_INVALID', 'connection record has an unknown state');
    for (const field of ['receiptId', 'receiptPath', 'selectedConfigPath', 'planPath', 'bindingPath', 'managerConfirmedAt', 'error']) {
        if (r[field] !== undefined && typeof r[field] !== 'string') {
            throw new UmhError('CONNECTION_INVALID', `connection record ${field} must be a string`);
        }
    }
    if (typeof r['updatedAt'] !== 'string' || r['updatedAt'] === '') {
        throw new UmhError('CONNECTION_INVALID', 'connection record updatedAt must be a non-empty string');
    }
    return r;
}
async function writeRecordFile(umhHome, record) {
    const file = connectionFile(umhHome, record.connectionId);
    await mkdir(dirname(file), { recursive: true });
    const tmp = join(dirname(file), `.${basename(file)}.${randomBytes(6).toString('hex')}.tmp`);
    try {
        await writeFile(tmp, `${JSON.stringify(record, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
        await rename(tmp, file);
    }
    catch (err) {
        await rm(tmp, { force: true }).catch(() => { });
        throw new UmhError('CONNECTION_PERSIST_FAILED', `connection record write failed (${file}): ${err.message}`);
    }
    return file;
}
export async function loadConnectionRecord(umhHome, connectionId) {
    if (typeof umhHome !== 'string' || umhHome.trim() === '' || !isAbsolute(umhHome)) {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be an absolute path');
    }
    if (!isValidBindingId(connectionId)) {
        throw new UmhError('CONNECTION_UNKNOWN', `unknown connection: ${String(connectionId)}`);
    }
    let bytes;
    try {
        bytes = await readFile(connectionFile(umhHome, connectionId));
    }
    catch (err) {
        if (err.code === 'ENOENT') {
            throw new UmhError('CONNECTION_UNKNOWN', `unknown connection: ${connectionId}`);
        }
        throw new UmhError('CONNECTION_INVALID', `connection record is not readable: ${err.message}`);
    }
    let parsed;
    try {
        parsed = JSON.parse(bytes.toString('utf8'));
    }
    catch (err) {
        throw new UmhError('CONNECTION_INVALID', `connection record is not valid JSON: ${err.message}`);
    }
    return parseConnectionRecord(parsed);
}
/** record 级严格锁（C01 同型 O_EXCL + 有界重试 + 超龄让位）；调用方固定先 connection 后 config。 */
async function withConnectionLock(umhHome, connectionId, fn) {
    if (typeof umhHome !== 'string' || umhHome.trim() === '' || !isAbsolute(umhHome)) {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be an absolute path');
    }
    const dir = join(umhHome, 'config', 'conn-locks');
    await mkdir(dir, { recursive: true });
    const lockFile = join(dir, `${sha256Hex(normalizeWorkspacePath(String(connectionId)))}.lock`);
    const deadline = Date.now() + 10_000;
    for (;;) {
        try {
            const handle = await open(lockFile, 'wx');
            try {
                await handle.writeFile(`${process.pid} ${new Date().toISOString()}\n`, 'utf8');
            }
            finally {
                await handle.close().catch(() => { });
            }
            break;
        }
        catch (err) {
            if (err.code !== 'EEXIST') {
                throw new UmhError('CONNECTION_LOCK_ERROR', `connection lock acquire failed: ${err.message}`);
            }
            let stale = false;
            try {
                stale = Date.now() - (await stat(lockFile)).mtimeMs > 60_000;
            }
            catch {
                stale = false;
            }
            if (stale) {
                await rm(lockFile, { force: true }).catch(() => { });
                continue;
            }
            if (Date.now() >= deadline) {
                throw new UmhError('CONNECTION_LOCK_TIMEOUT', 'connection lock could not be acquired in time');
            }
            await new Promise((resolve) => setTimeout(resolve, 25 + Math.floor(Math.random() * 25)));
        }
    }
    try {
        return await fn();
    }
    finally {
        await rm(lockFile, { force: true }).catch(() => { });
    }
}
function requireSameRoot(record, umhHome) {
    if (normalizeWorkspacePath(record.umhHome) !== normalizeWorkspacePath(umhHome)) {
        throw new UmhError('CONNECTION_ROOT_MISMATCH', `connection ${record.connectionId} belongs to another UMH home; re-run with the recorded --umh-home`);
    }
}
/**
 * 显式 --config-path 作用域钉死（F01 R5 缺陷修复：reconcile 曾把残留计划意图写到
 * 用户级文件，恢复哈希不等于修复）。
 * preview 记录 selectedConfigPath 后，apply/reconcile/disconnect 消费侧必须断言
 * plan.changes 的每条 path 都归一化等于该显式选择；漂移（残留/窜改/错配）即
 * PLAN_CHANGED fail-closed，绝不按 plan 静默写盘，更不回落到其它配置面。
 * 无显式选择（字段缺席）时守卫空转 —— 发现模式本就由 adapter 官方候选决定目标。
 */
function requireSelectedScope(record, plan) {
    if (typeof record.selectedConfigPath !== 'string' || record.selectedConfigPath === '')
        return;
    const selected = normalizeWorkspacePath(record.selectedConfigPath);
    for (const change of plan.changes) {
        if (normalizeWorkspacePath(change.path) !== selected) {
            throw new UmhError('PLAN_CHANGED', `connection ${record.connectionId} pins an explicit config target, but the plan now points elsewhere; re-run preview with the explicit --config-path`);
        }
    }
}
async function readJsonFile(path, code) {
    let bytes;
    try {
        bytes = await readFile(path);
    }
    catch (err) {
        throw new UmhError(code, `sidecar file is not readable (${path}): ${err.message}`);
    }
    try {
        return JSON.parse(bytes.toString('utf8'));
    }
    catch (err) {
        throw new UmhError(code, `sidecar file is not valid JSON (${path}): ${err.message}`);
    }
}
/** 合成故障注入点（只认测试合成 env，生产无公开 fault hook）。 */
function injectedFault() {
    const value = process.env.UMH_R02_FAIL_AFTER;
    if (value === 'config' || value === 'binding')
        return value;
    return null;
}
// ---------------------------------------------------------------------------
// prepare
// ---------------------------------------------------------------------------
export async function prepareConnection(input) {
    const plan = parseConfigPlan(input?.plan);
    const binding = input?.binding;
    if (binding === null || typeof binding !== 'object' || Array.isArray(binding)) {
        throw new UmhError('BINDING_INVALID', 'binding must be a JSON object');
    }
    if (binding.bindingId !== plan.bindingId) {
        throw new UmhError('PLAN_BINDING_MISMATCH', 'plan.bindingId and binding.bindingId must agree before preparing');
    }
    const umhHome = input?.umhHome ?? umhHomeOfPlan(plan);
    if (typeof umhHome !== 'string' || umhHome.trim() === '' || !isAbsolute(umhHome)) {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be an absolute path');
    }
    if (typeof input?.variant !== 'string' || !VARIANT_PATTERN.test(input.variant)) {
        throw new UmhError('INVALID_ARGUMENT', 'variant must be letters/digits/._-, 1-64 chars');
    }
    // record 只接受与 plan/binding 内容一致的意图（hash 引用；篡改在消费侧拒绝）。
    const record = {
        version: 1,
        connectionId: plan.bindingId,
        clientId: plan.clientId,
        variant: input.variant,
        umhHome,
        bindingId: binding.bindingId,
        bindingHash: bindingHashOf(binding),
        planId: plan.id,
        planHash: planHashOf(plan),
        updatedAt: new Date().toISOString(),
        state: plan.changes.length === 0 ? 'manual_pending' : 'prepared',
    };
    if (typeof input?.selectedConfigPath === 'string' && input.selectedConfigPath !== '') {
        record.selectedConfigPath = input.selectedConfigPath;
    }
    // 显式选择与 plan 错配尽早 fail-closed（评审 Should-1）：adapter 生成的 plan
    // 若与显式 --config-path 不一致，preview 落盘时即拒绝，不拖到 apply 消费侧。
    requireSelectedScope(record, plan);
    if (typeof input?.planPath === 'string' && input.planPath !== '')
        record.planPath = input.planPath;
    if (typeof input?.bindingPath === 'string' && input.bindingPath !== '')
        record.bindingPath = input.bindingPath;
    await withConnectionLock(umhHome, record.connectionId, async () => {
        await writeRecordFile(umhHome, record);
    });
    return record;
}
// ---------------------------------------------------------------------------
// apply
// ---------------------------------------------------------------------------
async function readBindingFile(umhHome, bindingId) {
    try {
        const raw = await readFile(join(umhHome, 'config', 'bindings', `${bindingId}.json`), 'utf8');
        return JSON.parse(raw);
    }
    catch (err) {
        if (err.code === 'ENOENT')
            return null;
        throw err;
    }
}
/**
 * disconnect 侧已发布 binding 读取（R03 私有；apply/reconcile 共用的
 * readBindingFile 保持 R02 语义零改动）。
 * EISDIR/EPERM 等读失败与坏 JSON 一律判删除失败面（调用方记 needs_reconcile）。
 */
async function readInstalledBindingForDisconnect(umhHome, bindingId) {
    let raw;
    try {
        raw = await readFile(join(umhHome, 'config', 'bindings', `${bindingId}.json`), 'utf8');
    }
    catch (err) {
        if (err.code === 'ENOENT')
            return null;
        throw new UmhError('BINDING_UNREADABLE', `installed binding is not readable: ${String(err?.message ?? err)}`);
    }
    try {
        return JSON.parse(raw);
    }
    catch (err) {
        throw new UmhError('BINDING_INVALID', `installed binding is not valid JSON: ${err.message}`);
    }
}
/** disconnect 侧 binding 读失败是否必须走删除失败面（EISDIR/EPERM 等）。 */
function isBindingReadFailure(err) {
    if (err instanceof UmhError)
        return true;
    const code = err?.code;
    return typeof code === 'string' && code !== 'ENOENT';
}
function toResult(record, extra) {
    const configured = record.state === 'configured';
    return {
        connectionId: record.connectionId,
        state: record.state,
        configured,
        nativeVerified: false,
        conflicts: [],
        nextActions: configured ? [] : [`run: umh connect reconcile --connection ${record.connectionId} --umh-home <root>`],
        ...extra,
    };
}
export async function applyConnection(recordId, opts) {
    const umhHome = opts?.umhHome;
    return withConnectionLock(umhHome, recordId, async () => {
        const record = await loadConnectionRecord(umhHome, recordId);
        requireSameRoot(record, umhHome);
        // sidecar 引用与 hash 吻合后才行动（篡改拒绝）。
        if (record.planPath === undefined || record.bindingPath === undefined) {
            throw new UmhError('CONNECTION_INVALID', 'connection record misses sidecar references; re-run preview to regenerate it');
        }
        const planRaw = await readJsonFile(record.planPath, 'PLAN_INVALID');
        const bindingRaw = await readJsonFile(record.bindingPath, 'PLAN_BINDING_MISSING');
        const plan = parseConfigPlan(planRaw);
        if (plan.id !== record.planId || planHashOf(plan) !== record.planHash) {
            throw new UmhError('PLAN_CHANGED', 'plan file changed since preview; re-run preview to regenerate it');
        }
        requireSelectedScope(record, plan);
        if (bindingRaw.bindingId !== record.bindingId || bindingHashOf(bindingRaw) !== record.bindingHash) {
            throw new UmhError('BINDING_CHANGED', 'binding sidecar changed since preview; re-run preview to regenerate it');
        }
        // 幂等：已终态且 sidecar 未动 → 不再调用 adapter.apply。
        // binding_published（手工信任链）也在内：重放 apply 不得把已信任连接降级。
        if (record.state === 'configured' || record.state === 'manual_pending' || record.state === 'binding_published' || record.state === 'removed') {
            return toResult(record);
        }
        // manual 计划：configured=false、state=manual_pending、nativeVerified=false。
        if (plan.changes.length === 0) {
            record.state = 'manual_pending';
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            // 下一步是显式确认发布绑定（trust）——reconcile 对 manual 连接只是空转。
            return {
                ...toResult(record),
                nextActions: [
                    `run: umh connect trust --connection ${record.connectionId} --umh-home ${record.umhHome} --confirm-reviewed`,
                    'publishing the binding is NOT native PASS; real tool-call verification happens in the field drill',
                ],
            };
        }
        // 端别 adapter.apply（registry 复用同一 adapter；已有 receiptId 即此前已调过，
        // 恢复路径绝不再次调用 apply —— persistReceiptReference 完成后掉线也不重发）。
        if (record.receiptId === undefined) {
            const adapter = resolveAdapter(plan.clientId, { umhHome });
            let receipt;
            try {
                const applied = await adapter.apply(plan);
                receipt = await loadReceiptById(umhHome, applied.receiptId);
            }
            catch (err) {
                // 失败 → needs_reconcile + 明确 error，不吞异常；binding 绝不发布。
                record.state = 'needs_reconcile';
                record.error = err instanceof UmhError ? `[${err.code}] ${err.message}` : String(err?.message ?? err);
                record.updatedAt = new Date().toISOString();
                await writeRecordFile(umhHome, record);
                throw err;
            }
            // persistReceiptReference：record 记 receiptId（对账引用）。
            record.receiptId = receipt.id;
            record.state = 'config_applied';
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            // 合成中断点：persistReceiptReference 完成后掉线 → 恢复走 reconcile，不重发 apply。
            if (injectedFault() === 'config') {
                record.state = 'needs_reconcile';
                record.error = 'UMH_R02_INJECTED_FAULT: crashed after config_applied, before binding publish';
                record.updatedAt = new Date().toISOString();
                await writeRecordFile(umhHome, record);
                return toResult(record);
            }
        }
        else {
            // 恢复分支：receipt 引用必须仍可加载，否则 needs_reconcile 等人工介入。
            try {
                await loadReceiptById(umhHome, record.receiptId);
            }
            catch (err) {
                record.state = 'needs_reconcile';
                record.error = err instanceof UmhError ? `[${err.code}] ${err.message}` : String(err?.message ?? err);
                record.updatedAt = new Date().toISOString();
                await writeRecordFile(umhHome, record);
                throw err;
            }
        }
        // 受信 binding 发布（已有内容一致 → 幂等；已有不同内容 → 冲突，不覆盖；
        // 已有 legacy 绑定（同 id 同内容）保留）。
        const existing = await readBindingFile(umhHome, record.bindingId);
        if (existing !== null && stableStringify(existing) !== stableStringify(bindingRaw)) {
            record.state = 'needs_reconcile';
            record.error = 'BINDING_CONFLICT: a different binding already occupies this connection id; refusing to overwrite';
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            throw new UmhError('BINDING_CONFLICT', record.error);
        }
        await writeTrustedBindingFile(umhHome, bindingRaw);
        if (injectedFault() === 'binding') {
            record.state = 'needs_reconcile';
            record.error = 'UMH_R02_INJECTED_FAULT: crashed after binding publish, before final state';
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            return toResult(record);
        }
        record.state = 'configured';
        delete record.error;
        record.updatedAt = new Date().toISOString();
        await writeRecordFile(umhHome, record);
        return toResult(record);
    });
}
// ---------------------------------------------------------------------------
// reconcile
// ---------------------------------------------------------------------------
async function fileHashOrNull(path) {
    try {
        const bytes = await readFile(path);
        return createHash('sha256').update(bytes).digest('hex');
    }
    catch (err) {
        if (err.code === 'ENOENT')
            return null;
        throw new UmhError('CONFIG_PATH_INVALID', `config path is not readable: ${path}`);
    }
}
export async function reconcileConnection(recordId, opts) {
    const umhHome = opts?.umhHome;
    return withConnectionLock(umhHome, recordId, async () => {
        const record = await loadConnectionRecord(umhHome, recordId);
        requireSameRoot(record, umhHome);
        // removed 是终态：对账不复活、不回退、不重发布；重接入必须走全新
        // preview→apply（→trust）。早退在 sidecar 校验之前——终态记录不要求
        // sidecar 仍在场。
        if (record.state === 'removed') {
            return {
                ...toResult(record),
                nextActions: [
                    're-onboard with a fresh preview: umh connect <client> --variant <v> --workspace <dir> --dry-run, then connect apply, then connect trust --confirm-reviewed',
                ],
            };
        }
        if (record.planPath === undefined || record.bindingPath === undefined) {
            throw new UmhError('CONNECTION_INVALID', 'connection record misses sidecar references; re-run preview to regenerate it');
        }
        const planRaw = await readJsonFile(record.planPath, 'PLAN_INVALID');
        const bindingRaw = await readJsonFile(record.bindingPath, 'PLAN_BINDING_MISSING');
        const plan = parseConfigPlan(planRaw);
        if (plan.id !== record.planId || planHashOf(plan) !== record.planHash) {
            throw new UmhError('PLAN_CHANGED', 'plan file changed since preview; re-run preview to regenerate it');
        }
        requireSelectedScope(record, plan);
        if (bindingRaw.bindingId !== record.bindingId || bindingHashOf(bindingRaw) !== record.bindingHash) {
            throw new UmhError('BINDING_CHANGED', 'binding sidecar changed since preview; re-run preview to regenerate it');
        }
        if (plan.changes.length === 0) {
            // manual：信任发布后对账不得降级——binding_published/configured 原样保持
            // （补充设计 2026-10-06 §2.4；现场曾实测 removed→reconcile 复活缺陷）。
            if (record.state === 'binding_published' || record.state === 'configured') {
                return toResult(record);
            }
            // 恢复到 manual_pending 时清除历史 error（如掉线注入残留），如实反映当前态。
            delete record.error;
            record.state = 'manual_pending';
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            return toResult(record);
        }
        if (record.state === 'configured') {
            return toResult(record);
        }
        // ownership + hash 证明：每个 change 的当前字节 == afterHash（目标状态在场），
        // 且 binding 文件与 sidecar 一致（或可安全发布）。
        let configMatches = true;
        for (const change of plan.changes) {
            const current = await fileHashOrNull(change.path);
            if (current !== change.afterHash) {
                configMatches = false;
                break;
            }
        }
        const published = await readBindingFile(umhHome, record.bindingId);
        const bindingMatches = published !== null && stableStringify(published) === stableStringify(bindingRaw);
        if (configMatches && bindingMatches) {
            record.state = 'configured';
            delete record.error;
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            return toResult(record);
        }
        if (configMatches && !bindingMatches) {
            // 配置已写、binding 未发布 → 只有 ownership 证明正确才发布（已有异物即冲突）。
            if (published !== null) {
                record.state = 'needs_reconcile';
                record.error = 'BINDING_CONFLICT: a different binding already occupies this connection id; refusing to overwrite';
                record.updatedAt = new Date().toISOString();
                await writeRecordFile(umhHome, record);
                throw new UmhError('BINDING_CONFLICT', record.error);
            }
            await writeTrustedBindingFile(umhHome, bindingRaw);
            record.state = 'configured';
            delete record.error;
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            return toResult(record);
        }
        // 配置不符目标：有 receipt（apply 已调过）→ 绝不重发 apply，等人工/重 preview。
        if (record.receiptId !== undefined) {
            record.state = 'needs_reconcile';
            record.error = record.error ?? 'CONFIG_MISMATCH: installed config no longer matches the prepared plan; re-run preview for a fresh plan';
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            throw new UmhError('CONFIG_MISMATCH', record.error);
        }
        // 无 receipt（apply 从未成功）→ 允许走一次 adapter.apply 再发布。
        const adapter = resolveAdapter(plan.clientId, { umhHome });
        let receipt;
        try {
            const applied = await adapter.apply(plan);
            receipt = await loadReceiptById(umhHome, applied.receiptId);
        }
        catch (err) {
            record.state = 'needs_reconcile';
            record.error = err instanceof UmhError ? `[${err.code}] ${err.message}` : String(err?.message ?? err);
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            throw err;
        }
        record.receiptId = receipt.id;
        record.state = 'config_applied';
        record.updatedAt = new Date().toISOString();
        await writeRecordFile(umhHome, record);
        await writeTrustedBindingFile(umhHome, bindingRaw);
        record.state = 'configured';
        delete record.error;
        record.updatedAt = new Date().toISOString();
        await writeRecordFile(umhHome, record);
        return toResult(record);
    });
}
// ---------------------------------------------------------------------------
// trust（手工连接信任链：显式确认 + 受信绑定发布，补充设计 2026-10-06 §2.2）
// ---------------------------------------------------------------------------
/**
 * trustManualConnection(recordId,{umhHome,confirmReviewed}) — 手工连接信任链的
 * 唯一发布通道（方向 B）。
 *
 * 合同：
 *   - 仅受理 manual 连接（plan.changes 空）；自动连接拒绝（CONNECTION_INVALID，
 *     走 apply/reconcile）；
 *   - 前置与 apply/reconcile 同源同强度：root 一致（CONNECTION_ROOT_MISMATCH）、
 *     sidecar 引用在、plan/binding hash 吻合（篡改即 PLAN_CHANGED/BINDING_CHANGED）；
 *   - 管理者未确认（confirmReviewed!==true）→ MANUAL_CONFIRM_REQUIRED 非零，零副作用；
 *   - removed 终态拒绝（CONNECTION_REMOVED）——重接入必须走全新 preview→apply→trust；
 *   - 冲突：受信位已有不同内容 → BINDING_CONFLICT + needs_reconcile，绝不覆盖；
 *   - 发布只经 writeTrustedBindingFile（A01 管理面）；成功 → state=binding_published
 *     + managerConfirmedAt（幂等重入不覆盖既有时间戳）+ 清 error；
 *   - 幂等：binding_published/configured 且绑定在场一致 → 原样返回；
 *   - UMH_R02_FAIL_AFTER=binding：发布后、状态落盘前掉线语义 → needs_reconcile
 *     + error（绑定已在场）；重跑 trust 走幂等路径恢复——中断可对账；
 *   - 发布失败 → needs_reconcile + 原样抛出，不吞；
 *   - 诚实口径：bindingPublished=true 只表示「管理者已确认并发布受信绑定」；
 *     configured 恒 false（manual 不产 configured）、nativeVerified 恒 false——
 *     发布绑定不等于现场 PASS，真实客户端工具调用验证属 F01 现场证据链。
 */
export async function trustManualConnection(recordId, opts) {
    const umhHome = opts?.umhHome;
    return withConnectionLock(umhHome, recordId, async () => {
        const record = await loadConnectionRecord(umhHome, recordId);
        requireSameRoot(record, umhHome);
        // removed 是终态：绝不在旧连接上发布信任（重接入走全新 preview→apply→trust）。
        if (record.state === 'removed') {
            throw new UmhError('CONNECTION_REMOVED', `connection ${record.connectionId} has been removed; re-onboard with a fresh preview + apply + trust`);
        }
        if (record.planPath === undefined || record.bindingPath === undefined) {
            throw new UmhError('CONNECTION_INVALID', 'connection record misses sidecar references; re-run preview to regenerate it');
        }
        const planRaw = await readJsonFile(record.planPath, 'PLAN_INVALID');
        const bindingRaw = await readJsonFile(record.bindingPath, 'PLAN_BINDING_MISSING');
        const plan = parseConfigPlan(planRaw);
        if (plan.id !== record.planId || planHashOf(plan) !== record.planHash) {
            throw new UmhError('PLAN_CHANGED', 'plan file changed since preview; re-run preview to regenerate it');
        }
        if (bindingRaw.bindingId !== record.bindingId || bindingHashOf(bindingRaw) !== record.bindingHash) {
            throw new UmhError('BINDING_CHANGED', 'binding sidecar changed since preview; re-run preview to regenerate it');
        }
        if (plan.changes.length !== 0) {
            throw new UmhError('CONNECTION_INVALID', 'connect trust only applies to manual connections (automatic connections use apply/reconcile)');
        }
        const publishedResult = () => ({
            ...toResult(record),
            bindingPublished: true,
            nextActions: [
                'hand-configure the client per the plan manual instructions (the binding is already trusted)',
                'real client tool-call verification happens in the field drill; publishing the binding is NOT native PASS',
            ],
        });
        // 幂等：已发布/已配置且绑定在场一致 → 原样返回（不重写、不覆盖确认时间戳）。
        if (record.state === 'binding_published' || record.state === 'configured') {
            const existing = await readBindingFile(umhHome, record.bindingId);
            if (existing !== null && stableStringify(existing) === stableStringify(bindingRaw)) {
                return publishedResult();
            }
            // 状态已发布但绑定文件缺失/被删 → 走发布路径补齐（可对账恢复）。
        }
        if (opts?.confirmReviewed !== true) {
            throw new UmhError('MANUAL_CONFIRM_REQUIRED', `manual connection ${record.connectionId} needs explicit manager confirmation: review the plan and the hand-configuration scope, then re-run with --confirm-reviewed`);
        }
        // 冲突：受信位已有不同内容 → 绝不覆盖（异物保留，needs_reconcile 等人工）。
        const existing = await readBindingFile(umhHome, record.bindingId);
        if (existing !== null && stableStringify(existing) !== stableStringify(bindingRaw)) {
            record.state = 'needs_reconcile';
            record.error = 'BINDING_CONFLICT: a different binding already occupies this connection id; refusing to overwrite';
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            throw new UmhError('BINDING_CONFLICT', record.error);
        }
        await writeTrustedBindingFile(umhHome, bindingRaw);
        if (injectedFault() === 'binding') {
            // 发布后、状态落盘前掉线语义：绑定已在场，重跑 trust 走幂等路径恢复。
            record.state = 'needs_reconcile';
            record.error = 'UMH_R02_INJECTED_FAULT: crashed after binding publish, before final state';
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            return toResult(record);
        }
        record.state = 'binding_published';
        if (record.managerConfirmedAt === undefined)
            record.managerConfirmedAt = new Date().toISOString();
        delete record.error;
        record.updatedAt = new Date().toISOString();
        await writeRecordFile(umhHome, record);
        return publishedResult();
    });
}
// ---------------------------------------------------------------------------
// disconnect
// ---------------------------------------------------------------------------
async function readInstalledBindingFile(umhHome, bindingId) {
    return readInstalledBindingForDisconnect(umhHome, bindingId);
}
/**
 * disconnectConnection(recordId,{umhHome,confirmManualRemoval}) — R03 端别撤销。
 *
 * 合同（补充工单 R03 L169-176 + 补充设计 §6）：
 *   - root 从显式参数或 record 取，两者冲突拒绝（CONNECTION_ROOT_MISMATCH）；
 *   - 自动端先调 adapter.remove（按可信 record client 和 root 选 factory），
 *     配置冲突（removed=false）即保留 binding，记 needs_reconcile 后非 0 抛出；
 *   - remove 成功后，只有已发布 binding 与 sidecar hash 相符才删文件；
 *     篡改/异物不删（bindingConflict → needs_reconcile）；
 *   - delete 失败不得 catch(()=>false) 后整体报 removed —— 直接记
 *     needs_reconcile 后非 0 抛出；
 *   - 手工（record manual_pending 或 plan changes 空）：无确认即
 *     MANUAL_CLEANUP_REQUIRED（含步骤与 nextActions，nativeVerified 恒 false，
 *     不伪称模型/native 验证）；只有明确 confirmManualRemoval 后才清本连接
 *     binding，仍不碰客户端配置、不伪称验证；
 *   - 成功终态 state=removed，record 持久；configured 恒 false、
 *     nativeVerified 恒 false。
 */
/**
 * disconnect 失败附带 record state（CLI 的 {error,state} 同源输出用；
 * main.ts 按 connectionState 透出；不进 record 文件，不属冻结字段）。
 */
export class DisconnectError extends UmhError {
    connectionState;
    constructor(code, message, state) {
        super(code, message);
        this.name = 'DisconnectError';
        this.connectionState = state;
    }
}
export async function disconnectConnection(recordId, opts) {
    const umhHome = opts?.umhHome;
    const confirmManualRemoval = opts?.confirmManualRemoval === true;
    return withConnectionLock(umhHome, recordId, async () => {
        const record = await loadConnectionRecord(umhHome, recordId);
        // 显式 root 与 record 不符即 CONNECTION_ROOT_MISMATCH（load 后 requireSameRoot）。
        requireSameRoot(record, umhHome);
        if (record.planPath === undefined || record.bindingPath === undefined) {
            throw new UmhError('CONNECTION_INVALID', 'connection record misses sidecar references; re-run preview to regenerate it');
        }
        // 幂等：已移除即直接返回（不再触碰配置/binding）。
        if (record.state === 'removed') {
            return { ...toResult(record), bindingRemoved: false };
        }
        const bindingRaw = await readJsonFile(record.bindingPath, 'PLAN_BINDING_MISSING');
        if (bindingRaw.bindingId !== record.bindingId || bindingHashOf(bindingRaw) !== record.bindingHash) {
            throw new UmhError('BINDING_CHANGED', 'binding sidecar changed since preview; re-run preview to regenerate it');
        }
        // 手工连接：plan changes 空（或 record manual_pending）→ 无确认即响亮指引。
        let planChanges = 0;
        try {
            const planRaw = await readJsonFile(record.planPath, 'PLAN_INVALID');
            const plan = parseConfigPlan(planRaw);
            requireSelectedScope(record, plan);
            planChanges = plan.changes.length;
        }
        catch (err) {
            // plan sidecar 缺失/被篡改同样 fail-closed（不猜测手工与否）。
            if (err instanceof UmhError && (err.code === 'PLAN_INVALID' || err.code === 'PLAN_CHANGED'))
                throw err;
            throw new UmhError('PLAN_INVALID', `plan sidecar is not readable (${record.planPath})`);
        }
        const isManual = record.state === 'manual_pending' || planChanges === 0;
        if (isManual && !confirmManualRemoval) {
            const steps = manualCleanupSteps(record);
            throw new UmhError('MANUAL_CLEANUP_REQUIRED', `manual connection ${record.connectionId} needs hand cleanup first: ${steps[0]}`);
        }
        if (isManual) {
            // 已确认手工清理：只清本连接 binding（不碰客户端配置、不调 adapter.remove
            // —— 手工端本就没有 UMH 自动写盘足迹，仍不伪称模型/native 验证）。
            try {
                const existed = await removeInstalledBindingOnly(umhHome, record.bindingId);
                record.state = 'removed';
                record.updatedAt = new Date().toISOString();
                await writeRecordFile(umhHome, record);
                return {
                    connectionId: record.connectionId,
                    state: record.state,
                    configured: false,
                    nativeVerified: false,
                    bindingRemoved: existed,
                    conflicts: [],
                    nextActions: [],
                };
            }
            catch (err) {
                // delete 失败 → needs_reconcile 且非 0（绝不吞错报 removed）。
                record.state = 'needs_reconcile';
                record.error = err instanceof UmhError ? `[${err.code}] ${err.message}` : String(err?.message ?? err);
                record.updatedAt = new Date().toISOString();
                await writeRecordFile(umhHome, record);
                throw new DisconnectError('BINDING_DELETE_FAILED', record.error, record.state);
            }
        }
        // 自动端：按可信 record client 和 root 选 factory，先 adapter.remove。
        if (record.receiptId === undefined) {
            record.state = 'needs_reconcile';
            record.error = 'RECEIPT_MISSING: connection has no installed receipt to remove; re-run preview for a fresh plan';
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            throw new DisconnectError('RECEIPT_MISSING', record.error, record.state);
        }
        const adapter = resolveAdapter(record.clientId, { umhHome });
        let removal;
        try {
            removal = await adapter.remove({ receiptId: record.receiptId });
        }
        catch (err) {
            record.state = 'needs_reconcile';
            record.error = err instanceof UmhError ? `[${err.code}] ${err.message}` : String(err?.message ?? err);
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            throw err;
        }
        if (!removal.removed) {
            // 配置冲突（用户后改 owned 段等）→ 保留 binding，记 needs_reconcile 后非 0。
            record.state = 'needs_reconcile';
            record.error = removal.conflicts.length > 0
                ? removal.conflicts.join('; ')
                : 'CONFIG_CONFLICT: client config differs from the installed state; resolve manually, then disconnect again';
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            throw new DisconnectError('CONFIG_CONFLICT', record.error, record.state);
        }
        // remove 成功：已发布 binding 必须仍与 sidecar hash 相符才删；异物不删。
        // 已发布 binding 不可读（目录占据/权限/坏字节）同样是删除失败面 —— 记
        // needs_reconcile 后非 0，绝不裸错直抛（无 JSON）或吞错报 removed。
        let installed;
        try {
            installed = await readInstalledBindingFile(umhHome, record.bindingId);
        }
        catch (err) {
            if (!isBindingReadFailure(err))
                throw err;
            record.state = 'needs_reconcile';
            record.error = err instanceof UmhError ? `[${err.code}] ${err.message}` : `BINDING_UNREADABLE: installed binding is not readable: ${String(err?.message ?? err)}`;
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            throw new DisconnectError('BINDING_DELETE_FAILED', record.error, record.state);
        }
        if (installed !== null && stableStringify(installed) !== stableStringify(bindingRaw)) {
            record.state = 'needs_reconcile';
            record.error = 'BINDING_CONFLICT: installed binding differs from this connection; refusing to delete another connection\'s binding';
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            throw new DisconnectError('BINDING_CONFLICT', record.error, record.state);
        }
        try {
            const bindingRemoved = installed === null ? false : await removeInstalledBindingOnly(umhHome, record.bindingId);
            record.state = 'removed';
            delete record.error;
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            return {
                connectionId: record.connectionId,
                state: record.state,
                configured: false,
                nativeVerified: false,
                bindingRemoved,
                conflicts: [],
                nextActions: [],
            };
        }
        catch (err) {
            // delete 失败不得 catch(()=>false) 后整体报 removed —— needs_reconcile 且非 0。
            record.state = 'needs_reconcile';
            record.error = err instanceof UmhError ? `[${err.code}] ${err.message}` : String(err?.message ?? err);
            record.updatedAt = new Date().toISOString();
            await writeRecordFile(umhHome, record);
            throw new DisconnectError('BINDING_DELETE_FAILED', record.error, record.state);
        }
    });
}
/** 手工清理步骤（只描述本连接归属动作；绝不伪称自动完成/模型验证）。 */
function manualCleanupSteps(record) {
    return [
        `remove the hand-added UMH entry for client ${record.clientId} (variant ${record.variant}) from the client app by hand`,
        `then re-run: umh disconnect --connection ${record.connectionId} --umh-home <root> --confirm-manual-removal (clears only this connection's binding file)`,
    ];
}
function nextActionsOf(record) {
    return manualCleanupSteps(record);
}
/** 仅删本连接 binding 文件；读/删路径异常直接抛出（调用方记 needs_reconcile）。 */
async function removeInstalledBindingOnly(umhHome, bindingId) {
    return deleteTrustedBindingFile(umhHome, bindingId);
}
// MANUAL_CLEANUP_REQUIRED 需要把步骤带给 CLI（UmhError 只 carry code/message，
// CLI 侧按 code 重建 nextActions；此处导出步骤构造供 CLI/测试同源使用）。
export function manualCleanupNextActions(record) {
    return nextActionsOf(record);
}
export async function loadConnection(umhHome, connectionId) {
    return loadConnectionRecord(umhHome, connectionId);
}
