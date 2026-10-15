import type { ScopeRef } from './contracts.js';
export type ScopeAdmin = {
    reconcile(scope: ScopeRef, operationId: string): Promise<unknown>;
    listPending(scope: ScopeRef): Promise<unknown>;
    review?(scope: ScopeRef, decision: unknown): Promise<unknown>;
};
/**
 * rebuildScope：只碰 C01 indexWhitelist（_index.json，复用
 * MdCgEngine.rebuildIndex 删 _index 后重扫——不另写），不删
 * hippocampus/deletions/trash/security。admin 须带 umhHome（服务句柄侧
 * 由 maintenance 路由补齐；测试经 (svc.admin as umhHome) 显式给）。
 */
export declare function rebuildScope(scope: ScopeRef, admin: unknown): Promise<void>;
/**
 * 只读探针：native mdcg_forget/mdcg_restore 存在性（C04 注释称未列入 C01
 * operationsHeader——O02 以只读探针先验证原生工具存在性）。
 * 返回 {forget: boolean, restore: boolean}；证不出则调用方走回执/台账层
 * 关系标记＋读层排除（不另建正文真源；C06 supersede 先例同理）。
 *
 * 存在性判定（C1 修复：返回计算值，旧硬编码 {true,true} 已删）：
 * 工具名可被路由（ok 或业务 error）即存在。
 * - 成功响应：ok:true，或 ok:false + 非空 error 字符串（not_found 等业务错误
 *   计存在——工具真实应答了“缺失”，不是“无此工具”）。
 * - 抛错：仅 mapNativeError 映射的上游工具错误计存在（ENGINE_NATIVE_ERROR /
 *   ACCESS_DENIED / QUEUE_LOCK_TIMEOUT）；ENGINE_TRANSPORT_ERROR /
 *   ENGINE_CLOSED / ENGINE_BAD_RESPONSE（未知工具非 JSON 等）及其他一律计不
 *   存在。传输失败绝不能计为“工具存在”（旧 `!== 'ENGINE_BAD_RESPONSE'` 把传输
 *   错误误判为存在，已修正）。
 */
export declare function probeErrorMeansExists(err: unknown): boolean;
export declare function probeNativeForgetRestore(umhHome: string, scope: ScopeRef): Promise<{
    forget: boolean;
    restore: boolean;
}>;
/**
 * forgetScoped：优先走 native mdcg_forget（只读探针证存在——本仓已证）；
 * admin 须带 umhHome。id 为空即拒绝；原生 not_found 即 FORGET_NOT_FOUND。
 */
export declare function forgetScoped(scope: ScopeRef, id: string, admin: unknown): Promise<void>;
/**
 * restoreScoped：优先走 native mdcg_restore（force=true——forget 即进删除清单，
 * 非 force 被拒绝面；恢复语义以 force 为准，见探针结论）。
 */
export declare function restoreScoped(scope: ScopeRef, id: string, admin: unknown): Promise<void>;
