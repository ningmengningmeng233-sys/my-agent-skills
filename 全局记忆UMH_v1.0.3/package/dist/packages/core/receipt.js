/**
 * packages/core/receipt.ts — 操作回执存储（C06）。
 *
 * 工单 C06 合同：receipt 持久仅 `operationId,bindingId,scope,memoryId,proposalId,
 * payloadHash,state,updatedAt` 八字段，不含正文；跨进程锁 + 原子写；锁内提供
 * findReceiptByFingerprint（先查 pending/committed/conflict/superseded 引用）与
 * 来源指纹索引（同 sourceId 不同内容 → 冲突，不当 duplicate——控制器裁决）；
 * supersedes 关系（oldId→newId+reason+时间戳）持久于本层（supersede 补充探针
 * NOT_PROVEN 后的总控裁决：取代是 UMH 审计协调，引擎旧节点永不改动）。
 *
 * 存储布局：每个作用域一份文件 `<umhHome>/receipts/<global|project-<projectId>>/
 * receipts.json`——回执的 `scope` 字段为冻结的 kind（'global'|'project'），项目
 * 区分由作用域绑定文件承载（与 C03 物理根隔离同构，杜绝跨项目指纹误撞）。
 * 文件内除 receipts 外仅存 hash/引用索引（sourceIndex、supersedeRelations），
 * 不保存第二份候选正文（设计 §7：Canonical Record，引擎是真源）。
 *
 * 锁纪律：`tx()` 以排他锁包裹临界区（去重判定 → 引擎 propose → 回执写必须
 * 原子于其他进程视角）；锁文件 O_EXCL 创建（'wx'）+ 有界重试 + 超龄让位
 * （mtime > 60s 视为陈旧让位，不按 PID kill——计划 §4：锁身份无法验证 fail
 * closed）。原子写 = 同目录临时文件 + rename（Windows MoveFileEx REPLACE）。
 * 损坏文件 fail closed（RECEIPT_CORRUPT），绝不静默重建。
 */
import { mkdir, open, readFile, rename, rm, stat, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import process from 'node:process';
import { UmhError } from '../contracts/index.js';
export const RECEIPT_STATES = Object.freeze([
    'pending',
    'committed',
    'rejected',
    'conflict',
    'unknown',
    'superseded',
]);
function emptyFile() {
    return { version: 1, receipts: [], sourceIndex: {}, supersedeRelations: [], supersedeIntents: [] };
}
const PROJECT_ID_PATTERN = /^prj_[0-9a-f]{64}$/;
const LOCK_TIMEOUT_MS_DEFAULT = 10_000;
const STALE_LOCK_MS = 60_000;
const RETRY_DELAY_MS = 25;
/**
 * 内容指纹参与去重的回执状态：等待裁决/已生效/冲突待裁/已被取代（记录仍 active）/
 * 未知（写入中断——评审修复轮 1：unknown 必须挡住同文再提交，否则中断后重提交会
 * 排出第二条同文提案，admin 可能双双接受）。
 */
const FINGERPRINT_STATES = new Set([
    'pending',
    'committed',
    'conflict',
    'superseded',
    'unknown',
]);
/** 来源冲突只看仍在等裁决或已生效的既有记录（已拒绝/已由 supersede 解决的不算）。 */
const SOURCE_CONFLICT_STATES = new Set([
    'pending',
    'committed',
    'conflict',
]);
function requireStr(value, field) {
    if (typeof value !== 'string' || value === '') {
        throw new UmhError('INVALID_ARGUMENT', `receipt field ${field} must be a non-empty string`);
    }
    return value;
}
/** 收敛为冻结八字段：多余键一律剔除，正文/额外负载永不落盘。 */
function pickReceipt(receipt) {
    if (receipt === null || typeof receipt !== 'object' || Array.isArray(receipt)) {
        throw new UmhError('INVALID_ARGUMENT', 'receipt must be an object');
    }
    if (receipt.scope !== 'global' && receipt.scope !== 'project') {
        throw new UmhError('INVALID_ARGUMENT', 'receipt field scope must be global|project');
    }
    if (!RECEIPT_STATES.includes(receipt.state)) {
        throw new UmhError('INVALID_ARGUMENT', `receipt field state must be one of: ${RECEIPT_STATES.join('|')}`);
    }
    const out = {
        operationId: requireStr(receipt.operationId, 'operationId'),
        bindingId: requireStr(receipt.bindingId, 'bindingId'),
        scope: receipt.scope,
        payloadHash: requireStr(receipt.payloadHash, 'payloadHash'),
        state: receipt.state,
        updatedAt: requireStr(receipt.updatedAt, 'updatedAt'),
    };
    if (typeof receipt.memoryId === 'string' && receipt.memoryId !== '')
        out.memoryId = receipt.memoryId;
    if (typeof receipt.proposalId === 'string' && receipt.proposalId !== '')
        out.proposalId = receipt.proposalId;
    return out;
}
function validateScope(scope) {
    if (scope === null || typeof scope !== 'object' || Array.isArray(scope)) {
        throw new UmhError('INVALID_ARGUMENT', 'scope must be a ScopeRef object');
    }
    if (scope.kind === 'global')
        return;
    if (scope.kind === 'project') {
        if (typeof scope.projectId !== 'string' || !PROJECT_ID_PATTERN.test(scope.projectId)) {
            throw new UmhError('INVALID_PROJECT_ID', `project id must be 'prj_' followed by 64 lowercase hex characters`);
        }
        return;
    }
    throw new UmhError('INVALID_ARGUMENT', `scope kind must be 'global' or 'project'`);
}
function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}
class FileReceiptStore {
    scope;
    dir;
    file;
    lockFile;
    lockTimeoutMs;
    closed = false;
    constructor(umhHome, scope, lockTimeoutMs) {
        this.scope = scope;
        this.dir = join(umhHome, 'receipts', scope.kind === 'global' ? 'global' : `project-${scope.projectId}`);
        this.file = join(this.dir, 'receipts.json');
        this.lockFile = join(this.dir, 'receipts.lock');
        this.lockTimeoutMs = lockTimeoutMs;
    }
    async tx(fn) {
        if (this.closed)
            throw new UmhError('RECEIPT_STORE_CLOSED', 'receipt store already closed');
        await mkdir(this.dir, { recursive: true });
        const release = await this.acquireLock();
        try {
            return await fn(this.makeTx());
        }
        finally {
            await release();
        }
    }
    list() {
        return this.tx((t) => t.list());
    }
    findByOperationId(operationId) {
        return this.tx((t) => t.findByOperationId(operationId));
    }
    findByProposalId(proposalId) {
        return this.tx((t) => t.findByProposalId(proposalId));
    }
    findByFingerprint(scope, payloadHash) {
        return this.tx((t) => t.findByFingerprint(scope, payloadHash));
    }
    findSourceConflict(sourceFingerprint, payloadHash) {
        return this.tx((t) => t.findSourceConflict(sourceFingerprint, payloadHash));
    }
    upsert(receipt, sourceFingerprint) {
        return this.tx((t) => t.upsert(receipt, sourceFingerprint));
    }
    markSuperseded(memoryIds) {
        return this.tx((t) => t.markSuperseded(memoryIds));
    }
    addSupersedeRelation(relation) {
        return this.tx((t) => t.addSupersedeRelation(relation));
    }
    listSupersedeRelations() {
        return this.tx((t) => t.listSupersedeRelations());
    }
    addSupersedeIntent(intent) {
        return this.tx((t) => t.addSupersedeIntent(intent));
    }
    findSupersedeIntentByOperationId(operationId) {
        return this.tx((t) => t.findSupersedeIntentByOperationId(operationId));
    }
    removeSupersedeIntent(proposalId) {
        return this.tx((t) => t.removeSupersedeIntent(proposalId));
    }
    async close() {
        this.closed = true;
    }
    // ------------------------------------------------------------------
    // 内部：持久化与锁
    // ------------------------------------------------------------------
    assertOpen() {
        if (this.closed)
            throw new UmhError('RECEIPT_STORE_CLOSED', 'receipt store already closed');
    }
    async read() {
        let text;
        try {
            text = await readFile(this.file, 'utf8');
        }
        catch (err) {
            if (err.code === 'ENOENT')
                return emptyFile();
            throw new UmhError('RECEIPT_IO_ERROR', `receipt store unreadable: ${err.message}`);
        }
        let parsed;
        try {
            parsed = JSON.parse(text);
        }
        catch {
            throw new UmhError('RECEIPT_CORRUPT', 'receipt store file is not valid JSON');
        }
        if (parsed === null || typeof parsed !== 'object' || parsed.version !== 1) {
            throw new UmhError('RECEIPT_CORRUPT', 'receipt store file has an unsupported shape');
        }
        const file = parsed;
        if (!Array.isArray(file.receipts)) {
            throw new UmhError('RECEIPT_CORRUPT', 'receipt store file is missing receipts');
        }
        return {
            version: 1,
            receipts: file.receipts,
            sourceIndex: file.sourceIndex ?? {},
            supersedeRelations: file.supersedeRelations ?? [],
            supersedeIntents: file.supersedeIntents ?? [],
        };
    }
    async write(file) {
        const tmp = join(this.dir, `.receipts-${randomBytes(6).toString('hex')}.tmp`);
        try {
            await writeFile(tmp, JSON.stringify(file), 'utf8');
            // 同目录 rename：Windows 走 MoveFileEx REPLACE_EXISTING，读取方只见完整文件。
            await rename(tmp, this.file);
        }
        catch (err) {
            await rm(tmp, { force: true }).catch(() => { });
            throw new UmhError('RECEIPT_IO_ERROR', `receipt store write failed: ${err.message}`);
        }
    }
    makeTx() {
        const store = this;
        return {
            async findByOperationId(operationId) {
                return (await store.read()).receipts.find((r) => r.operationId === operationId) ?? null;
            },
            async findByProposalId(proposalId) {
                return (await store.read()).receipts.find((r) => r.proposalId === proposalId) ?? null;
            },
            async findByFingerprint(scope, payloadHash) {
                const file = await store.read();
                return (file.receipts.find((r) => r.scope === scope && r.payloadHash === payloadHash && FINGERPRINT_STATES.has(r.state)) ?? null);
            },
            async findSourceConflict(sourceFingerprint, payloadHash) {
                const file = await store.read();
                const entries = file.sourceIndex[sourceFingerprint] ?? [];
                for (const entry of entries) {
                    const receipt = file.receipts.find((r) => r.operationId === entry.operationId);
                    if (receipt === undefined || !SOURCE_CONFLICT_STATES.has(receipt.state))
                        continue;
                    if (receipt.payloadHash !== payloadHash)
                        return receipt; // 同源不同正文 → 冲突
                }
                return null;
            },
            async upsert(receipt, sourceFingerprint) {
                const file = await store.read();
                const clean = pickReceipt(receipt);
                const idx = file.receipts.findIndex((r) => r.operationId === clean.operationId);
                if (idx >= 0)
                    file.receipts[idx] = clean;
                else
                    file.receipts.push(clean);
                if (sourceFingerprint !== undefined && sourceFingerprint !== '') {
                    const list = file.sourceIndex[sourceFingerprint] ?? [];
                    const entry = { operationId: clean.operationId, payloadHash: clean.payloadHash };
                    const eIdx = list.findIndex((e) => e.operationId === clean.operationId);
                    if (eIdx >= 0)
                        list[eIdx] = entry;
                    else
                        list.push(entry);
                    file.sourceIndex[sourceFingerprint] = list;
                }
                await store.write(file);
            },
            async markSuperseded(memoryIds) {
                const file = await store.read();
                const set = new Set(memoryIds);
                const now = new Date().toISOString();
                let changed = false;
                for (const r of file.receipts) {
                    if (r.memoryId !== undefined && set.has(r.memoryId) && r.state !== 'superseded') {
                        r.state = 'superseded';
                        r.updatedAt = now;
                        changed = true;
                    }
                }
                if (changed)
                    await store.write(file);
            },
            async addSupersedeRelation(relation) {
                const file = await store.read();
                const duplicate = file.supersedeRelations.some((r) => r.oldMemoryId === relation.oldMemoryId && r.newMemoryId === relation.newMemoryId);
                if (duplicate)
                    return; // 幂等：同一取代关系只记一次
                file.supersedeRelations.push({
                    oldMemoryId: requireStr(relation.oldMemoryId, 'oldMemoryId'),
                    newMemoryId: requireStr(relation.newMemoryId, 'newMemoryId'),
                    operationId: typeof relation.operationId === 'string' ? relation.operationId : '',
                    reason: typeof relation.reason === 'string' ? relation.reason : '',
                    createdAt: requireStr(relation.createdAt, 'createdAt'),
                });
                await store.write(file);
            },
            async listSupersedeRelations() {
                return (await store.read()).supersedeRelations;
            },
            async addSupersedeIntent(intent) {
                const file = await store.read();
                const clean = {
                    proposalId: requireStr(intent.proposalId, 'proposalId'),
                    operationId: typeof intent.operationId === 'string' ? intent.operationId : '',
                    oldMemoryIds: Array.isArray(intent.oldMemoryIds)
                        ? intent.oldMemoryIds.filter((id) => typeof id === 'string' && id !== '')
                        : [],
                    payloadHash: typeof intent.payloadHash === 'string' ? intent.payloadHash : '',
                    reason: typeof intent.reason === 'string' ? intent.reason : '',
                    createdAt: requireStr(intent.createdAt, 'createdAt'),
                };
                if (clean.oldMemoryIds.length === 0) {
                    throw new UmhError('INVALID_ARGUMENT', 'supersede intent requires oldMemoryIds');
                }
                // 同提案只保留最新意向（admin 重发取代时覆盖）。
                const idx = file.supersedeIntents.findIndex((i) => i.proposalId === clean.proposalId);
                if (idx >= 0)
                    file.supersedeIntents[idx] = clean;
                else
                    file.supersedeIntents.push(clean);
                await store.write(file);
            },
            async findSupersedeIntentByOperationId(operationId) {
                const file = await store.read();
                return (file.supersedeIntents.find((i) => i.operationId === operationId && i.operationId !== '') ?? null);
            },
            async removeSupersedeIntent(proposalId) {
                const file = await store.read();
                const next = file.supersedeIntents.filter((i) => i.proposalId !== proposalId);
                if (next.length === file.supersedeIntents.length)
                    return; // 幂等：无意向则无写盘
                file.supersedeIntents = next;
                await store.write(file);
            },
            async list() {
                return (await store.read()).receipts;
            },
        };
    }
    /** 跨进程排他锁：O_EXCL 创建 + 有界重试 + 超龄让位；超时 fail closed。 */
    async acquireLock() {
        const deadline = Date.now() + this.lockTimeoutMs;
        for (;;) {
            this.assertOpen();
            try {
                const handle = await open(this.lockFile, 'wx');
                try {
                    await handle.writeFile(`${process.pid} ${new Date().toISOString()}\n`, 'utf8');
                }
                finally {
                    await handle.close().catch(() => { });
                }
                let released = false;
                return async () => {
                    if (released)
                        return;
                    released = true;
                    await rm(this.lockFile, { force: true }).catch(() => { });
                };
            }
            catch (err) {
                if (err.code !== 'EEXIST') {
                    throw new UmhError('RECEIPT_IO_ERROR', `receipt lock acquire failed: ${err.message}`);
                }
                if (await this.lockIsStale()) {
                    // 陈旧锁让位（mtime 超龄；不按 PID kill —— 计划 §4 纪律）
                    await rm(this.lockFile, { force: true }).catch(() => { });
                    continue;
                }
                if (Date.now() >= deadline) {
                    throw new UmhError('RECEIPT_LOCK_TIMEOUT', 'receipt store lock could not be acquired in time');
                }
                await delay(RETRY_DELAY_MS + Math.floor(Math.random() * 25));
            }
        }
    }
    async lockIsStale() {
        try {
            const info = await stat(this.lockFile);
            return Date.now() - info.mtimeMs > STALE_LOCK_MS;
        }
        catch {
            return false;
        }
    }
}
/**
 * 打开作用域绑定的回执存储。project 作用域要求物理合法 projectId（C03 冻结形态）；
 * 绑定层逻辑 ID → 物理 ID 的映射由服务层（C07）完成。
 */
export async function openReceiptStore(umhHome, scope, opts) {
    if (typeof umhHome !== 'string' || umhHome.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be a non-empty string');
    }
    validateScope(scope);
    return new FileReceiptStore(umhHome, scope, opts?.lockTimeoutMs ?? LOCK_TIMEOUT_MS_DEFAULT);
}
