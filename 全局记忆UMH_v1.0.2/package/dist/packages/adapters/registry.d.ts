/**
 * packages/adapters/registry — 客户端适配器注册表（R01）。
 *
 * contract（补充工单 R01 L92 接口）：
 *   resolveAdapter(clientId, {umhHome}) → ClientAdapter。
 *   - qoder/zcode/codex/claude/minimax/dsh 调用已存在的 make*Adapter，传 {umhHome}；
 *   - cherry/generic 用已有单例 cherryAdapter/genericAdapter；
 *   - 穷举八 client，不 fallback；未知 client → UNKNOWN_CLIENT（绝不猜 adapter）；
 *   - 未知 variant 由各 adapter.detect 自行拒绝（INVALID_ARGUMENT），registry 不代判。
 *
 * R02 将在本文件旁新增 packages/adapters/connection.ts（deriveBindingId /
 * prepareConnection / applyConnection / reconcileConnection / disconnectConnection）；
 * 本文件只做 adapter 路由，不做 binding/connection 状态。
 */
import type { ClientId } from '../contracts/index.js';
import type { ClientAdapter } from './contracts.js';
/** 八端穷举（ClientId 全集；未知一律 UNKNOWN_CLIENT，绝不 fallback）。 */
export declare const CLIENT_IDS: ReadonlyArray<ClientId>;
/**
 * 按 clientId 解析到其专属 adapter。
 * options.umhHome 透传给各 make*Adapter（绑定 remove 的回执查找点）；
 * cherry/generic 为纯 UI/手工单例，无 umhHome 工厂参数，直接返回已有单例。
 */
export declare function resolveAdapter(clientId: ClientId, opts: {
    umhHome: string;
}): ClientAdapter;
