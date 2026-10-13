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
import type { ScopeRef } from '../contracts/index.js';
export type ReceiptState = 'pending' | 'committed' | 'rejected' | 'conflict' | 'unknown' | 'superseded';
export declare const RECEIPT_STATES: readonly ReceiptState[];
/**
 * 回执记录：字段集恰好为工单冻结的八字段，不含任何候选正文。
 * `scope` 为 kind（'global'|'project'）；项目区分由作用域绑定文件承载。
 */
export type MemoryReceipt = {
    operationId: string;
    bindingId: string;
    scope: 'global' | 'project';
    memoryId?: string;
    proposalId?: string;
    payloadHash: string;
    state: ReceiptState;
    updatedAt: string;
};
/** supersedes 关系台账条目（oldId→newId+reason+时间戳；仅引用元数据，非第二正文）。 */
export type SupersedeRelation = {
    oldMemoryId: string;
    newMemoryId: string;
    operationId: string;
    reason: string;
    createdAt: string;
};
/**
 * 取代意向（评审修复轮 1）：在 native accept 之前持久化；取代流程在"提交已落盘、
 * 关系未写盘"窗口崩溃时，由 reconcile 按本意向补全（幂等）。仅引用元数据。
 */
export type SupersedeIntent = {
    proposalId: string;
    operationId: string;
    oldMemoryIds: string[];
    payloadHash: string;
    reason: string;
    createdAt: string;
};
export interface ReceiptTx {
    findByOperationId(operationId: string): Promise<MemoryReceipt | null>;
    findByProposalId(proposalId: string): Promise<MemoryReceipt | null>;
    findByFingerprint(scope: 'global' | 'project', payloadHash: string): Promise<MemoryReceipt | null>;
    findSourceConflict(sourceFingerprint: string, payloadHash: string): Promise<MemoryReceipt | null>;
    upsert(receipt: MemoryReceipt, sourceFingerprint?: string): Promise<void>;
    markSuperseded(memoryIds: readonly string[]): Promise<void>;
    addSupersedeRelation(relation: SupersedeRelation): Promise<void>;
    listSupersedeRelations(): Promise<SupersedeRelation[]>;
    addSupersedeIntent(intent: SupersedeIntent): Promise<void>;
    findSupersedeIntentByOperationId(operationId: string): Promise<SupersedeIntent | null>;
    removeSupersedeIntent(proposalId: string): Promise<void>;
    list(): Promise<MemoryReceipt[]>;
}
export interface ReceiptStore {
    readonly scope: ScopeRef;
    /** 跨进程排他临界区；`tx` 暴露免锁原语供组合使用（不得在 fn 内再调用 store 级方法）。 */
    tx<T>(fn: (tx: ReceiptTx) => Promise<T> | T): Promise<T>;
    list(): Promise<MemoryReceipt[]>;
    findByOperationId(operationId: string): Promise<MemoryReceipt | null>;
    findByProposalId(proposalId: string): Promise<MemoryReceipt | null>;
    findByFingerprint(scope: 'global' | 'project', payloadHash: string): Promise<MemoryReceipt | null>;
    findSourceConflict(sourceFingerprint: string, payloadHash: string): Promise<MemoryReceipt | null>;
    upsert(receipt: MemoryReceipt, sourceFingerprint?: string): Promise<void>;
    markSuperseded(memoryIds: readonly string[]): Promise<void>;
    addSupersedeRelation(relation: SupersedeRelation): Promise<void>;
    listSupersedeRelations(): Promise<SupersedeRelation[]>;
    addSupersedeIntent(intent: SupersedeIntent): Promise<void>;
    findSupersedeIntentByOperationId(operationId: string): Promise<SupersedeIntent | null>;
    removeSupersedeIntent(proposalId: string): Promise<void>;
    close(): Promise<void>;
}
/**
 * 打开作用域绑定的回执存储。project 作用域要求物理合法 projectId（C03 冻结形态）；
 * 绑定层逻辑 ID → 物理 ID 的映射由服务层（C07）完成。
 */
export declare function openReceiptStore(umhHome: string, scope: ScopeRef, opts?: {
    lockTimeoutMs?: number;
}): Promise<ReceiptStore>;
