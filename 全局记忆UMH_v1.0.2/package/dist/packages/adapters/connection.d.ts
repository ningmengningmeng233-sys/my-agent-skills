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
import { UmhError } from '../contracts/index.js';
import type { Binding, ClientId } from '../contracts/index.js';
import type { ConfigPlan } from './contracts.js';
export type ConnectionState = 'prepared' | 'config_applied' | 'binding_published' | 'manual_pending' | 'configured' | 'needs_reconcile' | 'removed';
export type ConnectionRecord = {
    version: 1;
    connectionId: string;
    clientId: ClientId;
    variant: string;
    umhHome: string;
    bindingId: string;
    bindingHash: string;
    planId: string;
    planHash: string;
    receiptId?: string;
    receiptPath?: string;
    selectedConfigPath?: string;
    planPath?: string;
    bindingPath?: string;
    managerConfirmedAt?: string;
    state: ConnectionState;
    error?: string;
    updatedAt: string;
};
export type ConnectionResult = {
    connectionId: string;
    state: ConnectionState;
    configured: boolean;
    nativeVerified: false;
    bindingRemoved?: boolean;
    bindingPublished?: boolean;
    conflicts: string[];
    nextActions: string[];
};
/** hash 化 bindingId：`client-variant-`＋SHA256(norm root/workspace/目标) 前 24hex。 */
export declare function deriveBindingId(input: {
    clientId: ClientId;
    variant: string;
    workspace: string;
    target: string;
    umhHome: string;
}): string;
/**
 * 从 bindingId 反推 variant（apply 侧兼容旧 sidecar：record 缺失时重建意图用）。
 * hash 后缀固定 `-<24hex>`；legacy `client-variant` 无后缀。
 */
export declare function variantOfBindingId(clientId: ClientId, bindingId: string): string;
export declare function loadConnectionRecord(umhHome: string, connectionId: string): Promise<ConnectionRecord>;
export declare function prepareConnection(input: {
    plan: ConfigPlan;
    binding: Binding;
    variant: string;
    selectedConfigPath?: string;
    umhHome?: string;
    planPath?: string;
    bindingPath?: string;
}): Promise<ConnectionRecord>;
export declare function applyConnection(recordId: string, opts: {
    umhHome: string;
}): Promise<ConnectionResult>;
export declare function reconcileConnection(recordId: string, opts: {
    umhHome: string;
}): Promise<ConnectionResult>;
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
export declare function trustManualConnection(recordId: string, opts: {
    umhHome: string;
    confirmReviewed?: boolean;
}): Promise<ConnectionResult>;
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
export declare class DisconnectError extends UmhError {
    readonly connectionState: ConnectionState;
    constructor(code: string, message: string, state: ConnectionState);
}
export declare function disconnectConnection(recordId: string, opts: {
    umhHome: string;
    confirmManualRemoval?: boolean;
}): Promise<ConnectionResult>;
export declare function manualCleanupNextActions(record: ConnectionRecord): string[];
export declare function loadConnection(umhHome: string, connectionId: string): Promise<ConnectionRecord>;
