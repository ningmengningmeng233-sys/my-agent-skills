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
import { type Binding, type MemoryEngine, type RememberInput, type ReviewDecision, type ScopeRef, type WriteOutcome } from '../contracts/index.js';
import { type ReceiptStore } from './receipt.js';
export type ScopeHint = {
    kind: 'global' | 'project';
    projectId?: string;
};
export type ReceiptStoreFactory = (hint: ScopeHint) => Promise<ReceiptStore>;
/** 注册 hint→store 工厂（C07 服务接线 / 测试沙盒注入）；传 null 恢复默认。 */
export declare function useReceiptStoreFactory(factory: ReceiptStoreFactory | null): void;
/**
 * C09 读层消费：按作用域解析回执存储（与写路径共用同一工厂注册）。
 * 读层只用其台账方法（list / listSupersedeRelations）构建 active 排除集。
 */
export declare function receiptStoreFor(scope: ScopeRef): Promise<ReceiptStore>;
/**
 * 规范化正文（工单冻结口径）：NFC → CRLF→LF → trim 尾空白（行尾 + 文末）。
 * 引擎始终保存原始正文；只有哈希/指纹使用规范化形态。
 */
export declare function normalizeBody(text: string): string;
/** 内容载荷哈希（去重指纹；域分离前缀防跨域碰撞）。 */
export declare function computePayloadHash(content: string): string;
/**
 * 提交候选。顺序（工单冻结）：校验+secret → 权限 → 作用域 → 回执锁内
 * 幂等重放/指纹重复/引擎重复/同源冲突 → native propose → 回执落盘。
 * @param trusted 可选可信上下文：服务经 prepareOperation 预生成的 operationId
 *                （掉线前已知道对账 ID）；unit 未传时内部生成，绝不接受模型 ID。
 */
export declare function submitCandidate(input: RememberInput, binding: Binding, engine: MemoryEngine, trusted?: {
    operationId: string;
}): Promise<WriteOutcome>;
/**
 * 管理裁决。要求 admin 绑定（FORBIDDEN 闸，冻结代码块）；accept/supersede 必须
 * 原生 committed===true 且 get 读回可见并核对 ID/哈希；reject 不产生提交。
 */
export declare function reviewCandidate(input: ReviewDecision, binding: Binding, engine: MemoryEngine): Promise<WriteOutcome>;
/**
 * 按 operationId 对账（冻结语义）：引擎为真源，回执跟随引擎现状转移；
 * 绝不重发内容、绝不另发随机 operationId。
 */
export declare function reconcile(operationId: string, binding: Binding, engine: MemoryEngine): Promise<WriteOutcome>;
