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
import { UmhError } from '../contracts/index.js';
import { cherryAdapter } from './cherry/index.js';
import { makeQoderAdapter } from './qoder/index.js';
import { makeZcodeAdapter } from './zcode/index.js';
import { makeCodexAdapter } from './codex/index.js';
import { makeClaudeAdapter } from './claude/index.js';
import { makeMinimaxAdapter } from './minimax/index.js';
import { makeDshAdapter } from './dsh/index.js';
import { genericAdapter } from './generic/index.js';
/** 八端穷举（ClientId 全集；未知一律 UNKNOWN_CLIENT，绝不 fallback）。 */
export const CLIENT_IDS = [
    'cherry',
    'qoder',
    'zcode',
    'codex',
    'claude',
    'minimax',
    'dsh',
    'generic',
];
/**
 * 按 clientId 解析到其专属 adapter。
 * options.umhHome 透传给各 make*Adapter（绑定 remove 的回执查找点）；
 * cherry/generic 为纯 UI/手工单例，无 umhHome 工厂参数，直接返回已有单例。
 */
export function resolveAdapter(clientId, opts) {
    if (!CLIENT_IDS.includes(clientId)) {
        throw new UmhError('UNKNOWN_CLIENT', `unknown client: ${String(clientId)}`);
    }
    const umhHome = opts?.umhHome;
    switch (clientId) {
        case 'qoder':
            return makeQoderAdapter({ umhHome });
        case 'zcode':
            return makeZcodeAdapter({ umhHome });
        case 'codex':
            return makeCodexAdapter({ umhHome });
        case 'claude':
            return makeClaudeAdapter({ umhHome });
        case 'minimax':
            return makeMinimaxAdapter({ umhHome });
        case 'dsh':
            return makeDshAdapter({ umhHome });
        case 'cherry':
            return cherryAdapter;
        case 'generic':
            return genericAdapter;
    }
}
