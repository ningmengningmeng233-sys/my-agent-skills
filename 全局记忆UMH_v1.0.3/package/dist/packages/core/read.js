/**
 * packages/core/read.ts — 作用域读取、active 过滤与共享 token 预算（C09）。
 *
 * 职责（工单 C09 + 累积裁决）：
 *   - recall/search/get 三入口在引擎池之上按绑定解析可读作用域
 *     （resolveReadScopes，同一纯函数；绝不信任模型输入的 scope 越权），
 *     逐作用域调用引擎，保留单库排序、分组输出（project 组在前）。
 *   - active 过滤（supersede 补充探针 NOT_PROVEN 后的有界裁决）：引擎内
 *     prefilter 不可行（原生召回不按标签过滤），读层排除 superseded/pending/
 *     rejected/conflict/unknown 记录 —— 排除集来自 C06 回执台账
 *     （listSupersedeRelations + 非 committed 回执的 memoryId），不造第二真源、
 *     不改 vendor。superseded 记录先耗引擎预算再被排除属有界偏差，O01 终验。
 *   - 预算（上游引擎计数/裁剪口径，禁止字符÷4 估算）：双作用域时 global
 *     25%（Math.floor）/ project 其余（75% 口径，工单冻结骨架）；单作用域全给
 *     该侧。未耗完的一侧可再分配：project 剩余并入 global 授权；global 剩余且
 *     project 已耗尽其分配时对 project 二次召回并按 native id 去重（上游排序
 *     无偏移机制，二次召回可能重复装包，去重后仅输出增量）。tokensUsed 为
 *     各次引擎上报值之和，恒不超预算；零预算的一侧绝不以非法 budget 调引擎。
 *   - 公共引用：记录返回客户端前统一转 handle（contracts.encodeHandle，
 *     全局 g:<nativeId> / 项目 p:<projectId>:<nativeId>），Engine 内部仍用
 *     nativeId；记录必须标 scope（按被查物理根权威标注，不信任记录自报）与
 *     来源。nativeId 不可编码（含冒号/路径分隔符的外来节点）剔除并告警，
 *     不让单条脏数据打断整个召回。
 *   - get 不信任 handle 授权：解码后按绑定重新验证作用域；伪造/格式非法/
 *     他项目/不存在一律 NOT_FOUND（返回 null），不触碰他库引擎、不透露其它
 *     项目存在性。
 */
import { BUDGET_TOKENS_MAX, SEARCH_LIMIT_MAX, UmhError, } from '../contracts/index.js';
import { decodeHandle, encodeHandle } from '../contracts/index.js';
import { resolveReadScopes } from '../scope/binding.js';
import { receiptStoreFor } from './write.js';
function requireBinding(binding) {
    if (binding === null || typeof binding !== 'object' || Array.isArray(binding)) {
        throw new UmhError('INVALID_ARGUMENT', 'binding must be a binding object');
    }
}
function scopeLabel(scope) {
    return scope.kind === 'global' ? 'global' : `project ${scope.projectId}`;
}
// ---------------------------------------------------------------------------
// active 排除集（C06 回执台账消费）
// ---------------------------------------------------------------------------
/**
 * 读层排除集：supersede 台账的全部 oldId（取代关系是权威）+ 非 committed 回执
 * 的 memoryId（pending/rejected/conflict/unknown 候选与中断不得出现在公共读面；
 * 台账不可读时 fail closed 上抛，绝不静默放行未审内容）。
 */
async function exclusionIdsFor(scope) {
    const store = await receiptStoreFor(scope);
    const ids = new Set();
    const relations = await store.listSupersedeRelations();
    for (const relation of relations)
        ids.add(relation.oldMemoryId);
    const receipts = await store.list();
    for (const receipt of receipts) {
        if (receipt.memoryId !== undefined && receipt.state !== 'committed')
            ids.add(receipt.memoryId);
    }
    return ids;
}
// ---------------------------------------------------------------------------
// 公共引用成形：scope 权威标注 + handle 编码
// ---------------------------------------------------------------------------
/**
 * 记录出引擎后的公共形态：scope 按被查物理根权威标注（引擎真源按根隔离，
 * 记录自报不作为授权依据），id 转公共 handle。nativeId 不可编码（外来节点含
 * 冒号/路径分隔符）→ null，调用方剔除，不让单条脏数据打断整个召回。
 */
function toPublicRecord(record, scope) {
    let handle;
    try {
        handle = encodeHandle(scope, record.id);
    }
    catch {
        return null;
    }
    return { ...record, id: handle, scope };
}
async function recallScope(pool, scope, query, queryScope, budgetTokens, skipNative) {
    const run = {
        memories: [],
        tokensUsed: 0,
        truncated: false,
        warnings: [],
        seenNative: new Set(skipNative ?? []),
    };
    if (!Number.isInteger(budgetTokens) || budgetTokens < 1) {
        return run; // 零预算侧不调用引擎（引擎合同 budgetTokens ≥ 1）
    }
    const engine = await pool.get(scope);
    const result = await engine.recall({ query, scope: queryScope, budgetTokens });
    run.tokensUsed = typeof result.tokensUsed === 'number' && result.tokensUsed >= 0 ? result.tokensUsed : 0;
    run.truncated = result.truncated === true;
    if (Array.isArray(result.warnings))
        run.warnings.push(...result.warnings.map(String));
    const excluded = await exclusionIdsFor(scope);
    let dropped = 0;
    for (const record of Array.isArray(result.memories) ? result.memories : []) {
        if (record === null || typeof record !== 'object') {
            dropped += 1;
            continue;
        }
        if (record.status === 'superseded' || excluded.has(record.id)) {
            dropped += 1;
            run.seenNative.add(record.id);
            continue;
        }
        if (run.seenNative.has(record.id))
            continue; // 同库重复（二次召回重复装包）：静默去重
        const publicRecord = toPublicRecord(record, scope);
        if (publicRecord === null) {
            dropped += 1;
            run.seenNative.add(record.id);
            continue;
        }
        run.seenNative.add(record.id);
        run.memories.push(publicRecord);
    }
    if (dropped > 0) {
        run.warnings.push(`excluded ${dropped} non-active record(s) in ${scopeLabel(scope)} scope`);
    }
    return run;
}
function requireRecallInput(input) {
    if (input === null || typeof input !== 'object') {
        throw new UmhError('INVALID_ARGUMENT', 'recall input must be an object');
    }
    if (typeof input.query !== 'string' || input.query.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'field query must be a non-empty string');
    }
    if (!Number.isInteger(input.budgetTokens) || input.budgetTokens < 1 || input.budgetTokens > BUDGET_TOKENS_MAX) {
        throw new UmhError('INVALID_ARGUMENT', `field budgetTokens must be an integer between 1 and ${BUDGET_TOKENS_MAX}`);
    }
}
export async function recall(input, binding, pool) {
    requireBinding(binding);
    requireRecallInput(input);
    const scopes = resolveReadScopes(binding, input.scope);
    // 单作用域（未绑定 auto / 显式 global / 显式 project）：全预算给该侧。
    if (scopes.length === 1) {
        const run = await recallScope(pool, scopes[0], input.query, input.scope, input.budgetTokens);
        return {
            memories: run.memories,
            tokensUsed: run.tokensUsed,
            budgetTokens: input.budgetTokens,
            truncated: run.truncated,
            warnings: run.warnings,
        };
    }
    // 双作用域（auto + 已绑定项目）恒为 [project, global]（resolveReadScopes 冻结形态）。
    const projectScope = scopes[0];
    const globalScope = scopes[1];
    if (projectScope.kind !== 'project' || globalScope.kind !== 'global') {
        throw new UmhError('INVALID_ARGUMENT', 'unexpected read scope combination for recall');
    }
    // 工单冻结骨架：globalBudget = Math.floor(budget*0.25)；projectBudget = budget - globalBudget。
    const globalAlloc = Math.floor(input.budgetTokens * 0.25);
    const projectAlloc = input.budgetTokens - globalAlloc;
    // pass 1：project 优先（auto 语义 project 在前；更具体的作用域先占预算）。
    const projectRun = await recallScope(pool, projectScope, input.query, input.scope, projectAlloc);
    // 再分配（正向）：project 未耗完的份额并入 global 授权；总量仍 ≤ budget。
    const projectLeftover = Math.max(0, projectAlloc - projectRun.tokensUsed);
    const globalGranted = globalAlloc + projectLeftover;
    const globalRun = await recallScope(pool, globalScope, input.query, input.scope, globalGranted);
    // 再分配（反向）：global 未耗完且 project 已耗尽其分配 → 对 project 二次召回，
    // 按 native id 去重（上游无偏移机制，重复装包去重后只输出增量；tokensUsed
    // 按引擎上报累计，总量恒 ≤ budget —— 有界，O01 关注项）。
    const globalLeftover = Math.max(0, globalGranted - globalRun.tokensUsed);
    let extraRun = null;
    if (globalLeftover > 0 && projectRun.tokensUsed >= projectAlloc) {
        extraRun = await recallScope(pool, projectScope, input.query, input.scope, globalLeftover, projectRun.seenNative);
    }
    const memories = [...projectRun.memories, ...globalRun.memories, ...(extraRun?.memories ?? [])];
    return {
        memories,
        tokensUsed: projectRun.tokensUsed + globalRun.tokensUsed + (extraRun?.tokensUsed ?? 0),
        budgetTokens: input.budgetTokens,
        truncated: projectRun.truncated || globalRun.truncated || (extraRun?.truncated ?? false),
        warnings: [...projectRun.warnings, ...globalRun.warnings, ...(extraRun?.warnings ?? [])],
    };
}
// ---------------------------------------------------------------------------
// search：逐作用域检索，保留单库排序，分组输出
// ---------------------------------------------------------------------------
export async function search(query, scope, limit, binding, pool) {
    requireBinding(binding);
    if (typeof query !== 'string' || query.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'field query must be a non-empty string');
    }
    if (!Number.isInteger(limit) || limit < 1 || limit > SEARCH_LIMIT_MAX) {
        throw new UmhError('INVALID_ARGUMENT', `field limit must be an integer between 1 and ${SEARCH_LIMIT_MAX}`);
    }
    const scopes = resolveReadScopes(binding, scope);
    const results = [];
    for (const scopeRef of scopes) {
        // limit 按单库语义透传（保留单库排序；分组输出，不做跨库混排/截断）。
        const engine = await pool.get(scopeRef);
        const found = await engine.search(query, limit);
        const excluded = await exclusionIdsFor(scopeRef);
        const seen = new Set();
        for (const record of Array.isArray(found) ? found : []) {
            if (record === null || typeof record !== 'object')
                continue;
            if (record.status === 'superseded' || excluded.has(record.id) || seen.has(record.id))
                continue;
            const publicRecord = toPublicRecord(record, scopeRef);
            if (publicRecord === null)
                continue;
            seen.add(record.id);
            results.push(publicRecord);
        }
    }
    return results;
}
// ---------------------------------------------------------------------------
// get：handle 解码 + 绑定重新验证 scope（绝不信任 handle 自身授权）
// ---------------------------------------------------------------------------
export async function get(memoryId, binding, pool) {
    requireBinding(binding);
    if (typeof memoryId !== 'string' || memoryId === '')
        return null;
    // handle 是客户端可构造的公共形态：解码只给语法，授权必须按绑定重新验证。
    const decoded = decodeHandle(memoryId);
    if (decoded === null)
        return null; // 伪造/裸 nativeId/格式非法 → NOT_FOUND 语义
    if (decoded.scope.kind === 'project' && binding.projectId !== decoded.scope.projectId) {
        return null; // 他项目或未绑定：一律 NOT_FOUND，绝不触碰他库引擎（不泄露存在性）
    }
    const engine = await pool.get(decoded.scope);
    const record = await engine.get(decoded.nativeId);
    if (record === null || record.status === 'superseded')
        return null;
    const excluded = await exclusionIdsFor(decoded.scope);
    if (excluded.has(decoded.nativeId))
        return null;
    return toPublicRecord(record, decoded.scope);
}
// ---------------------------------------------------------------------------
// ReadService 薄适配（rpc.ts 注入缝）
// ---------------------------------------------------------------------------
/**
 * rpc.ts ReadService 的薄适配：service 边界已完成 wire 解析与
 * resolveReadScopes 鉴权解析；本适配持有池引用，按冻结的 core 签名从绑定重新
 * 派生作用域（同一纯函数，结果一致）。返回结构化类型与 rpc.ts ReadService
 * 兼容，core 层不反向依赖 service 层。
 */
export function createReadService(pool) {
    return {
        recall: (binding, input) => recall(input, binding, pool),
        search: (binding, query, scope, _scopes, limit) => search(query, scope, limit, binding, pool),
        get: (binding, memoryId) => get(memoryId, binding, pool),
    };
}
