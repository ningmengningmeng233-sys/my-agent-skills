/**
 * packages/core/write.ts — 候选提交、管理裁决与对账（C06）。
 *
 * 保存通路（设计 §7）：schema 校验 → secret 拦截 → 长期价值检查 → 来源/内容
 * 去重 → 冲突检查 → 原生候选队列 → 管理审核 → 写入 → 读回确认。
 *
 * 工单 C06 冻结顺序（实现先校验→secret→权限→作用域→exact duplicate→native
 * propose）：validateCandidate（含 secret，先于任何引擎调用——C05 carry 硬性
 * 要求）→ reader 权限闸 → 作用域检查 → 回执锁内指纹/引擎重复/同源冲突 →
 * mdcg_propose（唯一写路径；候选无直写面）。operationId、memoryId 服务生成，
 * 不让模型指定；sourceId 只作带 client/binding 的指纹材料。
 *
 * 提交语义（冻结状态表）：pending_review=候选未生效；duplicate=未新增、返回
 * 既有 memoryId 并以 reasonCode 说明其是否已生效（duplicate / duplicate_committed）；
 * conflict=等待裁决、既有记忆未被覆盖；rejected=未保存候选或正文；unknown
 * committed=null 不提供成功断言、需对账、禁止自动重试；committed=true 仅在
 * 原生 committed 且 get 读回可见时成立。
 *
 * 审核（冻结代码块逐字）：admin 闸 → engine.review → `committed!==true` 报
 * pendingOutcome；否则 get 读回，缺失报 unknownOutcome（committed=null）。
 * pendingOutcome/unknownOutcome 是本文件内部 helper，不跨模块导出。accept 必须
 * 核对读回 ID/哈希；supersede 按总控裁决实现为 UMH 审计协调：取代内容走正常
 * 提案裁决链（对既有提案执行原生 accept，绝不绕审核、绝不 mdcg_forget），
 * supersedes 关系持久于回执层，引擎旧节点永不改动（"不进入正常召回"由 C09
 * 读层实现）。
 *
 * 对账（冻结代码块语义）：findOperation 'committed'+get 读回 → committed；
 * 'pending' → pending_review；'absent' → rejected/OPERATION_ABSENT。reconcile
 * 绝不重发内容、绝不另发随机 operationId（幂等重放同 operationId 亦然）。
 *
 * 回执存储解析：服务/测试经 useReceiptStoreFactory 注入 hint→store 工厂
 * （C07 注册物理作用域映射）；未注入时用默认工厂（UMH_HOME 或默认家目录，
 * 要求绑定携带物理 projectId）。多作用域经 storeCache 按 hint 缓存实例。
 */
import { createHash, randomBytes } from 'node:crypto';
import { homedir } from 'node:os';
import { join } from 'node:path';
import process from 'node:process';
import { UmhError, } from '../contracts/index.js';
import { validateCandidate } from './validation.js';
import { openReceiptStore, } from './receipt.js';
let storeFactory = null;
/** 注册 hint→store 工厂（C07 服务接线 / 测试沙盒注入）；传 null 恢复默认。 */
export function useReceiptStoreFactory(factory) {
    storeFactory = factory;
}
function defaultUmhHome() {
    const fromEnv = process.env.UMH_HOME;
    return typeof fromEnv === 'string' && fromEnv.trim() !== ''
        ? fromEnv
        : join(homedir(), '.universal-memory-hub');
}
function hintToScope(hint) {
    if (hint.kind === 'global')
        return { kind: 'global' };
    if (typeof hint.projectId !== 'string' || hint.projectId === '') {
        throw new UmhError('INVALID_ARGUMENT', 'project scope hint requires a projectId');
    }
    return { kind: 'project', projectId: hint.projectId };
}
async function storeFor(hint) {
    // 不在 write.ts 缓存实例：openReceiptStore 是无句柄构造，实例生命周期归工厂
    // 所有（测试重启模拟关店重开、C07 服务按作用域持有时都天然正确）。
    const factory = storeFactory ?? (async (h) => openReceiptStore(defaultUmhHome(), hintToScope(h)));
    return factory(hint);
}
/**
 * C09 读层消费：按作用域解析回执存储（与写路径共用同一工厂注册）。
 * 读层只用其台账方法（list / listSupersedeRelations）构建 active 排除集。
 */
export function receiptStoreFor(scope) {
    return storeFor(scope.kind === 'global'
        ? { kind: 'global' }
        : { kind: 'project', projectId: scope.projectId });
}
// ---------------------------------------------------------------------------
// 形状/指纹/ID
// ---------------------------------------------------------------------------
const REMEMBER_KEYS = new Set([
    'type',
    'scope',
    'title',
    'content',
    'evidence',
    'sourceId',
]);
const REVIEW_KEYS = new Set(['proposalId', 'decision', 'reason', 'supersedes']);
const ROLES = new Set(['reader', 'recorder', 'admin']);
const DECISIONS = new Set(['accept', 'reject', 'supersede']);
function requireObject(value, what) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        throw new UmhError('INVALID_ARGUMENT', `${what} must be an object`);
    }
    return value;
}
function requireNonEmptyString(value, what) {
    if (typeof value !== 'string' || value.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', `${what} must be a non-empty string`);
    }
    return value;
}
/**
 * 解析级拒绝：模型不得经候选/裁决载荷携带 operation/approval/role 类字段
 * 提权或自带 ID（C02 wire 层已拒 snake_case 变体；此处为内部 camelCase 面的第二道闸）。
 */
function assertRememberInputShape(input) {
    const obj = requireObject(input, 'remember input');
    for (const key of Object.keys(obj)) {
        if (!REMEMBER_KEYS.has(key)) {
            throw new UmhError('INVALID_ARGUMENT', `unknown field on remember input: ${key} (model-supplied operation/approval fields are not accepted)`);
        }
    }
}
function assertReviewDecisionShape(input) {
    const obj = requireObject(input, 'review decision');
    for (const key of Object.keys(obj)) {
        if (!REVIEW_KEYS.has(key)) {
            throw new UmhError('INVALID_ARGUMENT', `unknown field on review decision: ${key} (model-supplied approval fields are not accepted)`);
        }
    }
    requireNonEmptyString(obj['proposalId'], 'review decision proposalId');
    requireNonEmptyString(obj['reason'], 'review decision reason');
    if (!DECISIONS.has(String(obj['decision']))) {
        throw new UmhError('INVALID_ARGUMENT', 'review decision must be accept|reject|supersede');
    }
    if (obj['supersedes'] !== undefined) {
        if (!Array.isArray(obj['supersedes']) || obj['supersedes'].length === 0) {
            throw new UmhError('INVALID_ARGUMENT', 'supersedes must be a non-empty array of memory ids');
        }
        for (const id of obj['supersedes']) {
            if (typeof id !== 'string' || id === '') {
                throw new UmhError('INVALID_ARGUMENT', 'supersedes entries must be non-empty strings');
            }
        }
    }
}
function assertBindingShape(binding) {
    const obj = requireObject(binding, 'binding');
    requireNonEmptyString(obj['clientId'], 'binding clientId');
    requireNonEmptyString(obj['bindingId'], 'binding bindingId');
    if (!ROLES.has(String(obj['role']))) {
        throw new UmhError('INVALID_ARGUMENT', 'binding role must be reader|recorder|admin');
    }
}
/**
 * 规范化正文（工单冻结口径）：NFC → CRLF→LF → trim 尾空白（行尾 + 文末）。
 * 引擎始终保存原始正文；只有哈希/指纹使用规范化形态。
 */
export function normalizeBody(text) {
    return text
        .normalize('NFC')
        .replace(/\r\n?/g, '\n')
        .replace(/[ \t]+$/gm, '')
        .replace(/\s+$/, '');
}
/** 内容载荷哈希（去重指纹；域分离前缀防跨域碰撞）。 */
export function computePayloadHash(content) {
    return createHash('sha256').update(`umh-payload-v1\u0000${normalizeBody(content)}`, 'utf8').digest('hex');
}
/** 来源指纹 = clientId + bindingId + sourceId 绑定（sourceId 仅作指纹材料）。 */
function sourceFingerprintOf(binding, sourceId) {
    return createHash('sha256')
        .update(`umh-source-v1\u0000${binding.clientId}\u0000${binding.bindingId}\u0000${sourceId}`, 'utf8')
        .digest('hex');
}
function generateOperationId() {
    return `op_${randomBytes(16).toString('hex')}`;
}
function generateMemoryId() {
    return `mem_${randomBytes(16).toString('hex')}`;
}
function requireTrustedOperationId(trusted) {
    const value = requireObject(trusted, 'trusted context')['operationId'];
    return requireNonEmptyString(value, 'trusted.operationId must be a service-generated string');
}
function nowIso() {
    return new Date().toISOString();
}
function scopeHint(input, binding) {
    if (input.scope === 'global')
        return { kind: 'global' };
    return { kind: 'project', projectId: String(binding.projectId ?? '') };
}
// ---------------------------------------------------------------------------
// submitCandidate
// ---------------------------------------------------------------------------
/**
 * 提交候选。顺序（工单冻结）：校验+secret → 权限 → 作用域 → 回执锁内
 * 幂等重放/指纹重复/引擎重复/同源冲突 → native propose → 回执落盘。
 * @param trusted 可选可信上下文：服务经 prepareOperation 预生成的 operationId
 *                （掉线前已知道对账 ID）；unit 未传时内部生成，绝不接受模型 ID。
 */
export async function submitCandidate(input, binding, engine, trusted) {
    assertRememberInputShape(input);
    assertBindingShape(binding);
    const operationId = trusted === undefined ? generateOperationId() : requireTrustedOperationId(trusted);
    // 1) 校验 + secret 拦截（C05 carry：先于任何引擎调用；结果不带原文）
    const verdict = validateCandidate(input);
    if (!verdict.allowed) {
        return { status: 'rejected', committed: false, operationId, reasonCode: verdict.reasonCode };
    }
    // 2) 权限：reader 只读
    if (binding.role === 'reader') {
        throw new UmhError('ROLE_FORBIDDEN', 'reader binding cannot submit candidates');
    }
    // 3) 作用域：project 候选要求绑定携带 projectId（逻辑→物理映射由服务层完成）
    if (input.scope === 'project' && typeof binding.projectId !== 'string') {
        throw new UmhError('INVALID_ARGUMENT', 'project-scope candidate requires binding.projectId');
    }
    const hint = scopeHint(input, binding);
    const payloadHash = computePayloadHash(input.content);
    const sourceFingerprint = typeof input.sourceId === 'string' && input.sourceId !== ''
        ? sourceFingerprintOf(binding, input.sourceId)
        : null;
    const store = await storeFor(hint);
    return store.tx(async (tx) => {
        // 3a) 幂等重放：同 operationId 重试不新发提案（禁止另发随机 ID 重试）
        const existing = await tx.findByOperationId(operationId);
        if (existing !== null)
            return replayOutcome(existing);
        // 3b) 内容指纹：pending/committed/conflict/superseded/unknown 回执先行。
        //     评审修复轮 1：unknown（写入中断）命中时不得再发提案——surface 原操作，
        //     调用方据此 reconcile（unknown→pending/committed/rejected）解除阻塞。
        const fingerprintHit = await tx.findByFingerprint(input.scope, payloadHash);
        if (fingerprintHit !== null) {
            if (fingerprintHit.state === 'unknown') {
                return {
                    status: 'unknown',
                    committed: null,
                    operationId: fingerprintHit.operationId,
                    reasonCode: 'WRITE_INTERRUPTED',
                };
            }
            return duplicateOutcome(fingerprintHit, operationId);
        }
        // 3c) 引擎 active 重复（回执缺席时的第二道闸，如外部/历史数据）
        const engineDuplicate = await engine.findDuplicate(input);
        if (engineDuplicate !== null) {
            return {
                status: 'duplicate',
                committed: false,
                operationId,
                memoryId: engineDuplicate.id,
                reasonCode: 'duplicate_committed',
            };
        }
        // 3d) 同 sourceId 不同内容 → 冲突（进审核等待裁决，不当 duplicate；
        //     既有记忆未被覆盖）
        const conflict = sourceFingerprint !== null && (await tx.findSourceConflict(sourceFingerprint, payloadHash)) !== null;
        // 4) 原生候选队列（唯一写路径；无审核直写面）
        const memoryId = generateMemoryId();
        try {
            const native = await engine.propose(input, { memoryId, operationId, binding });
            const receipt = {
                operationId,
                bindingId: binding.bindingId,
                scope: input.scope,
                memoryId,
                proposalId: native.proposalId,
                payloadHash,
                state: conflict ? 'conflict' : 'pending',
                updatedAt: nowIso(),
            };
            await tx.upsert(receipt, sourceFingerprint ?? undefined);
            return conflict
                ? {
                    status: 'conflict',
                    committed: false,
                    operationId,
                    memoryId,
                    proposalId: native.proposalId,
                    reasonCode: 'SOURCE_CONFLICT',
                }
                : { status: 'pending_review', committed: false, operationId, memoryId, proposalId: native.proposalId };
        }
        catch (err) {
            if (err instanceof UmhError)
                throw err; // 引擎确定性拒绝如实上抛，不伪装成 unknown
            // 连接中断/超时：未知回执持久化，等待对账；绝不在此自动重试
            const receipt = {
                operationId,
                bindingId: binding.bindingId,
                scope: input.scope,
                payloadHash,
                state: 'unknown',
                updatedAt: nowIso(),
            };
            await tx.upsert(receipt, sourceFingerprint ?? undefined);
            return { status: 'unknown', committed: null, operationId, reasonCode: 'WRITE_INTERRUPTED' };
        }
    });
}
function replayOutcome(receipt) {
    switch (receipt.state) {
        case 'pending':
            return {
                status: 'pending_review',
                committed: false,
                operationId: receipt.operationId,
                memoryId: receipt.memoryId,
                proposalId: receipt.proposalId,
            };
        case 'conflict':
            return {
                status: 'conflict',
                committed: false,
                operationId: receipt.operationId,
                memoryId: receipt.memoryId,
                proposalId: receipt.proposalId,
                reasonCode: 'SOURCE_CONFLICT',
            };
        case 'committed':
            return {
                status: 'committed',
                committed: true,
                operationId: receipt.operationId,
                memoryId: receipt.memoryId,
                proposalId: receipt.proposalId,
            };
        case 'superseded':
            return {
                status: 'committed',
                committed: true,
                operationId: receipt.operationId,
                memoryId: receipt.memoryId,
                proposalId: receipt.proposalId,
                reasonCode: 'RECEIPT_SUPERSEDED',
            };
        case 'rejected':
            return {
                status: 'rejected',
                committed: false,
                operationId: receipt.operationId,
                reasonCode: 'REJECTED_PREVIOUSLY',
            };
        case 'unknown':
            return {
                status: 'unknown',
                committed: null,
                operationId: receipt.operationId,
                reasonCode: 'WRITE_INTERRUPTED',
            };
    }
}
function duplicateOutcome(prior, operationId) {
    const alreadyLive = prior.state === 'committed' || prior.state === 'superseded';
    return {
        status: 'duplicate',
        committed: false,
        operationId,
        memoryId: prior.memoryId,
        proposalId: alreadyLive ? undefined : prior.proposalId,
        reasonCode: alreadyLive ? 'duplicate_committed' : 'duplicate',
    };
}
// ---------------------------------------------------------------------------
// reviewCandidate
// ---------------------------------------------------------------------------
/**
 * 管理裁决。要求 admin 绑定（FORBIDDEN 闸，冻结代码块）；accept/supersede 必须
 * 原生 committed===true 且 get 读回可见并核对 ID/哈希；reject 不产生提交。
 */
export async function reviewCandidate(input, binding, engine) {
    assertReviewDecisionShape(input);
    // 冻结代码块：admin 闸
    if (binding.role !== 'admin')
        throw new UmhError('FORBIDDEN', 'review requires admin');
    assertBindingShape(binding);
    const store = await storeFor(storeHintOfBinding(binding));
    const receiptBefore = await store.findByProposalId(input.proposalId);
    // operationId 解析：回执优先；其次审核队列的原生 umh:op 标签（断线恢复路径）。
    const operationId = receiptBefore?.operationId ?? (await resolveOperationIdFromQueue(engine, input.proposalId)) ?? '';
    if (input.decision === 'supersede') {
        return supersedeFlow(input, binding, engine, store, receiptBefore, operationId);
    }
    const native = await engine.review(input);
    if (input.decision === 'reject') {
        // reject 不产生提交；队列由原生裁决移除，回执如实转 rejected。
        await updateReceipt(store, receiptBefore, operationId, { state: 'rejected', proposalId: input.proposalId });
        return { status: 'rejected', committed: false, operationId, proposalId: input.proposalId };
    }
    // accept —— 冻结代码块：committed!==true 永远不等于已记住
    if (native.committed !== true)
        return pendingOutcome(native, operationId);
    if (typeof native.memoryId !== 'string' || native.memoryId === '') {
        throw new UmhError('ENGINE_BAD_RESPONSE', 'native review committed without a memoryId');
    }
    const readback = await engine.get(native.memoryId);
    if (!readback)
        return unknownOutcome(native.memoryId, operationId);
    assertCommitIntegrity(native, readback, receiptBefore);
    await updateReceipt(store, receiptBefore, operationId, {
        state: 'committed',
        memoryId: native.memoryId,
        proposalId: input.proposalId,
    });
    return {
        status: 'committed',
        committed: true,
        operationId,
        memoryId: native.memoryId,
        proposalId: input.proposalId,
    };
}
/** supersede（总控裁决）：取代内容走正常候选裁决链 + 回执层持久 supersedes 关系。 */
async function supersedeFlow(input, _binding, engine, store, receiptBefore, operationId) {
    const olds = input.supersedes ?? [];
    if (olds.length === 0) {
        throw new UmhError('INVALID_ARGUMENT', 'supersede decision requires at least one supersedes target');
    }
    // 取代目标必须存在且可取回；取代永不改动旧节点（历史保留，fail-closed）。
    for (const oldId of olds) {
        const old = await engine.get(oldId);
        if (old === null) {
            throw new UmhError('MEMORY_NOT_FOUND', `supersede target is not retrievable: ${oldId}`);
        }
    }
    // 取代意向先于 native accept 持久化（评审修复轮 1：提交与关系写盘之间的崩溃
    // 窗口由 reconcile 按本意向幂等补全——工单"事务不足时用审计过的协调状态机，
    // 并验证中断恢复"）。
    const intent = {
        proposalId: input.proposalId,
        operationId,
        oldMemoryIds: [...olds],
        payloadHash: receiptBefore?.payloadHash ?? '',
        reason: input.reason,
        createdAt: nowIso(),
    };
    await store.tx((tx) => tx.addSupersedeIntent(intent));
    // 取代内容经正常提案裁决（对既有提案执行原生 accept；绝不直写、绝不 mdcg_forget）
    const native = await engine.review({ proposalId: input.proposalId, decision: 'accept', reason: input.reason });
    if (native.committed !== true) {
        // 提案仍在队列等待裁决：意向随之撤销（admin 重发时会重新持久化）。
        await store.tx((tx) => tx.removeSupersedeIntent(input.proposalId));
        return pendingOutcome(native, operationId);
    }
    if (typeof native.memoryId !== 'string' || native.memoryId === '') {
        throw new UmhError('ENGINE_BAD_RESPONSE', 'native review committed without a memoryId');
    }
    const readback = await engine.get(native.memoryId);
    if (!readback)
        return unknownOutcome(native.memoryId, operationId);
    assertCommitIntegrity(native, readback, receiptBefore);
    const newId = native.memoryId;
    const timestamp = nowIso();
    await store.tx(async (tx) => {
        for (const oldId of olds) {
            const relation = {
                oldMemoryId: oldId,
                newMemoryId: newId,
                operationId,
                reason: input.reason,
                createdAt: timestamp,
            };
            await tx.addSupersedeRelation(relation);
        }
        await tx.markSuperseded(olds); // 仅回执层标记；引擎旧节点不动
        const target = receiptBefore ?? (operationId !== '' ? await tx.findByOperationId(operationId) : null);
        if (target !== null) {
            await tx.upsert({
                ...target,
                state: 'committed',
                memoryId: newId,
                proposalId: input.proposalId,
                updatedAt: timestamp,
            });
        }
        await tx.removeSupersedeIntent(input.proposalId); // 关系已落盘：意向闭环
    });
    return { status: 'committed', committed: true, operationId, memoryId: newId, proposalId: input.proposalId };
}
// ---------------------------------------------------------------------------
// reconcile
// ---------------------------------------------------------------------------
/**
 * 按 operationId 对账（冻结语义）：引擎为真源，回执跟随引擎现状转移；
 * 绝不重发内容、绝不另发随机 operationId。
 */
export async function reconcile(operationId, binding, engine) {
    requireNonEmptyString(operationId, 'operationId');
    assertBindingShape(binding);
    if (binding.role === 'reader') {
        throw new UmhError('ROLE_FORBIDDEN', 'reader binding has no write operations to reconcile');
    }
    const store = await storeFor(storeHintOfBinding(binding));
    const receipt = await store.findByOperationId(operationId);
    if (receipt !== null && receipt.state === 'committed') {
        // 提交时已读回验证；此处复核引擎现状，防回执/引擎分叉。
        const native = await engine.findOperation(operationId);
        if (native.state === 'committed' && typeof native.memoryId === 'string') {
            const visible = await engine.get(native.memoryId);
            if (visible !== null) {
                await completeSupersedeIntent(store, operationId, native.memoryId);
                return {
                    status: 'committed',
                    committed: true,
                    operationId,
                    memoryId: native.memoryId,
                    proposalId: receipt.proposalId,
                };
            }
        }
        return {
            status: 'unknown',
            committed: null,
            operationId,
            memoryId: receipt.memoryId,
            reasonCode: 'RECEIPT_ENGINE_DIVERGENCE',
        };
    }
    const native = await engine.findOperation(operationId);
    if (native.state === 'committed') {
        const memoryId = typeof native.memoryId === 'string' ? native.memoryId : '';
        const readback = memoryId !== '' ? await engine.get(memoryId) : null;
        if (readback === null) {
            await markReceipt(store, receipt, operationId, 'unknown');
            return { status: 'unknown', committed: null, operationId, reasonCode: 'COMMIT_NOT_VISIBLE' };
        }
        // 回执转移为 committed（缺席时按读回记录回填；payloadHash 保留原值优先）
        await store.upsert({
            operationId,
            bindingId: receipt?.bindingId ?? binding.bindingId,
            scope: readback.scope.kind,
            memoryId,
            proposalId: native.proposalId ?? receipt?.proposalId,
            payloadHash: receipt?.payloadHash ?? computePayloadHash(readback.content),
            state: 'committed',
            updatedAt: nowIso(),
        });
        // 悬挂的取代意向在此补全（评审修复轮 1：取代中断恢复）。
        await completeSupersedeIntent(store, operationId, memoryId);
        return {
            status: 'committed',
            committed: true,
            operationId,
            memoryId,
            proposalId: native.proposalId ?? receipt?.proposalId,
        };
    }
    if (native.state === 'pending') {
        await markReceipt(store, receipt, operationId, 'pending', native.proposalId);
        return {
            status: 'pending_review',
            committed: false,
            operationId,
            memoryId: native.memoryId,
            proposalId: native.proposalId,
        };
    }
    // absent：提案从未落地或已被拒绝——未保存即如实报告，未知操作不落回执；
    // 悬挂的取代意向随之撤销（无提交即无可补全的关系）。
    if (receipt !== null)
        await markReceipt(store, receipt, operationId, 'rejected');
    await removeSupersedeIntentIfAny(store, operationId);
    return { status: 'rejected', committed: false, operationId, reasonCode: 'OPERATION_ABSENT' };
}
// ---------------------------------------------------------------------------
// 内部 helper
// ---------------------------------------------------------------------------
function storeHintOfBinding(binding) {
    return typeof binding.projectId === 'string' && binding.projectId !== ''
        ? { kind: 'project', projectId: binding.projectId }
        : { kind: 'global' };
}
async function resolveOperationIdFromQueue(engine, proposalId) {
    try {
        if (typeof engine.listPending !== 'function')
            return null;
        const pending = await engine.listPending();
        const hit = pending.find((p) => p.proposalId === proposalId);
        return hit !== undefined && typeof hit.operationId === 'string' && hit.operationId !== ''
            ? hit.operationId
            : null;
    }
    catch {
        return null; // 队列不可读时不阻断裁决（operationId 以空串上报）
    }
}
/** 冻结块内部 helper：native ok 无 committed → 待审结果（不跨模块导出）。 */
function pendingOutcome(native, operationId) {
    return {
        status: 'pending_review',
        committed: false,
        operationId,
        memoryId: native.memoryId,
        proposalId: native.proposalId,
        reasonCode: native.reasonCode,
    };
}
/** 冻结块内部 helper：声称已提交但读回不可见 → 未知（committed=null；不跨模块导出）。 */
function unknownOutcome(memoryId, operationId) {
    return {
        status: 'unknown',
        committed: null,
        operationId,
        memoryId,
        reasonCode: 'COMMIT_NOT_VISIBLE',
    };
}
/** admin accept 核对：native committed + get 读回的 ID/哈希（工单冻结要求）。 */
function assertCommitIntegrity(native, readback, receipt) {
    if (typeof native.memoryId === 'string' && readback.id !== native.memoryId) {
        throw new UmhError('COMMIT_ID_MISMATCH', 'read-back id does not match the committed id');
    }
    if (receipt !== null && receipt.payloadHash !== '') {
        const observed = computePayloadHash(readback.content);
        if (observed !== receipt.payloadHash) {
            throw new UmhError('COMMIT_HASH_MISMATCH', 'committed content hash does not match the reviewed candidate');
        }
    }
}
async function updateReceipt(store, receiptBefore, operationId, patch) {
    const target = receiptBefore ?? (operationId !== '' ? await store.findByOperationId(operationId) : null);
    if (target === null)
        return; // 非 UMH 路由的提案：无回执可更新
    await store.upsert({
        ...target,
        state: patch.state,
        memoryId: patch.memoryId ?? target.memoryId,
        proposalId: patch.proposalId ?? target.proposalId,
        updatedAt: nowIso(),
    });
}
async function markReceipt(store, receipt, operationId, state, proposalId) {
    const target = receipt ?? (await store.findByOperationId(operationId));
    if (target === null)
        return;
    await store.upsert({
        ...target,
        state,
        proposalId: proposalId ?? target.proposalId,
        updatedAt: nowIso(),
    });
}
/**
 * 评审修复轮 1：按操作 ID 补全悬挂的取代意向（幂等——关系写盘按
 * oldMemoryId+newMemoryId 去重，markSuperseded 幂等，意向补全后移除）。
 */
async function completeSupersedeIntent(store, operationId, memoryId) {
    await store.tx(async (tx) => {
        const intent = await tx.findSupersedeIntentByOperationId(operationId);
        if (intent === null)
            return;
        for (const oldId of intent.oldMemoryIds) {
            const relation = {
                oldMemoryId: oldId,
                newMemoryId: memoryId,
                operationId: intent.operationId,
                reason: intent.reason,
                createdAt: intent.createdAt,
            };
            await tx.addSupersedeRelation(relation);
        }
        await tx.markSuperseded(intent.oldMemoryIds);
        await tx.removeSupersedeIntent(intent.proposalId);
    });
}
/** 提案确认从未提交（absent）时清理悬挂意向，防止陈旧意向无限滞留。 */
async function removeSupersedeIntentIfAny(store, operationId) {
    const intent = await store.findSupersedeIntentByOperationId(operationId);
    if (intent !== null)
        await store.removeSupersedeIntent(intent.proposalId);
}
