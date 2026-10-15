/**
 * packages/ops/rebuild — O02 RED 桩（故意未实现：rebuildScope 恒抛）。
 *
 * GREEN 将实现：rebuild 只碰 C01 indexWhitelist（_index.json），不删
 * hippocampus/deletions/trash/security；调用 native forget/restore，
 * 不设公共 purge；管理 handle 扩展 forget/restore/listPending。
 */
import { UmhError } from '../contracts/index.js';
function requireScope(scope) {
    if (scope === null || typeof scope !== 'object' || Array.isArray(scope)) {
        throw new UmhError('INVALID_ARGUMENT', 'scope must be a ScopeRef object');
    }
    if (scope.kind === 'global')
        return;
    if (scope.kind === 'project') {
        if (typeof scope.projectId !== 'string' || !/^prj_[0-9a-f]{64}$/.test(scope.projectId)) {
            throw new UmhError('INVALID_PROJECT_ID', 'project id must be prj_ + 64 lowercase hex');
        }
        return;
    }
    throw new UmhError('INVALID_ARGUMENT', "scope kind must be 'global' or 'project'");
}
function enginePoolFor(umhHome) {
    return import('../service/pool.js').then(async ({ EnginePool, createRealEngineFactory }) => {
        const pool = new EnginePool({
            // policyFile 缺省走服务默认值（vendor/mdcg/data/policy.json 由服务侧解析）；
            // 此处显式拼接到仓库 vendor（测试/CLI 进程 cwd=仓库根）。
            factory: createRealEngineFactory(umhHome, 'python', './vendor/mdcg/data/policy.json'),
        });
        return { pool: pool };
    });
}
function umhHomeOfAdmin(admin) {
    const home = admin.umhHome;
    return typeof home === 'string' && home !== '' ? home : null;
}
/**
 * rebuildScope：只碰 C01 indexWhitelist（_index.json，复用
 * MdCgEngine.rebuildIndex 删 _index 后重扫——不另写），不删
 * hippocampus/deletions/trash/security。admin 须带 umhHome（服务句柄侧
 * 由 maintenance 路由补齐；测试经 (svc.admin as umhHome) 显式给）。
 */
export async function rebuildScope(scope, admin) {
    requireScope(scope);
    const umhHome = umhHomeOfAdmin(admin);
    if (umhHome === null) {
        throw new UmhError('INVALID_ARGUMENT', 'rebuildScope admin must carry umhHome (service handle scope)');
    }
    const { pool } = await enginePoolFor(umhHome);
    try {
        const engine = await pool.get(scope);
        await engine.rebuildIndex();
    }
    finally {
        await pool.stop();
    }
}
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
export function probeErrorMeansExists(err) {
    const code = err?.code;
    return code === 'ENGINE_NATIVE_ERROR' || code === 'ACCESS_DENIED' || code === 'QUEUE_LOCK_TIMEOUT';
}
function probeOkMeansExists(out) {
    return out['ok'] === true || (typeof out['error'] === 'string' && out['error'] !== '');
}
export async function probeNativeForgetRestore(umhHome, scope) {
    requireScope(scope);
    const { pool } = await enginePoolFor(umhHome);
    try {
        const engine = await pool.get(scope);
        const conn = engine.conn;
        if (!conn)
            return { forget: false, restore: false };
        let forget = false;
        let restore = false;
        try {
            forget = probeOkMeansExists(await conn.call('mdcg_forget', { node_id: '__o02_probe__', reason: 'probe' }));
        }
        catch (err) {
            forget = probeErrorMeansExists(err);
        }
        try {
            restore = probeOkMeansExists(await conn.call('mdcg_restore', { node_id: '__o02_probe__' }));
        }
        catch (err) {
            restore = probeErrorMeansExists(err);
        }
        return { forget, restore };
    }
    finally {
        await pool.stop();
    }
}
/**
 * forgetScoped：优先走 native mdcg_forget（只读探针证存在——本仓已证）；
 * admin 须带 umhHome。id 为空即拒绝；原生 not_found 即 FORGET_NOT_FOUND。
 */
export async function forgetScoped(scope, id, admin) {
    requireScope(scope);
    if (typeof id !== 'string' || id.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'forget id must be a non-empty string');
    }
    const umhHome = umhHomeOfAdmin(admin);
    if (umhHome === null) {
        throw new UmhError('INVALID_ARGUMENT', 'forgetScoped admin must carry umhHome (service handle scope)');
    }
    const { pool } = await enginePoolFor(umhHome);
    try {
        const engine = await pool.get(scope);
        const conn = engine.conn;
        if (!conn)
            throw new UmhError('ENGINE_UNAVAILABLE', 'forget path has no engine connection');
        const out = await conn.call('mdcg_forget', { node_id: id, reason: 'O02 scoped forget' });
        if (out['ok'] !== true) {
            const code = String(out['error'] ?? 'forget_failed');
            if (code === 'not_found')
                throw new UmhError('FORGET_NOT_FOUND', `forget target not found: ${id}`);
            throw new UmhError('ENGINE_NATIVE_ERROR', `mdcg_forget: ${code}`);
        }
    }
    finally {
        await pool.stop();
    }
}
/**
 * restoreScoped：优先走 native mdcg_restore（force=true——forget 即进删除清单，
 * 非 force 被拒绝面；恢复语义以 force 为准，见探针结论）。
 */
export async function restoreScoped(scope, id, admin) {
    requireScope(scope);
    if (typeof id !== 'string' || id.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'restore id must be a non-empty string');
    }
    const umhHome = umhHomeOfAdmin(admin);
    if (umhHome === null) {
        throw new UmhError('INVALID_ARGUMENT', 'restoreScoped admin must carry umhHome (service handle scope)');
    }
    const { pool } = await enginePoolFor(umhHome);
    try {
        const engine = await pool.get(scope);
        const conn = engine.conn;
        if (!conn)
            throw new UmhError('ENGINE_UNAVAILABLE', 'restore path has no engine connection');
        const out = await conn.call('mdcg_restore', { node_id: id, force: true });
        if (out['ok'] !== true) {
            const code = String(out['error'] ?? 'restore_failed');
            if (code === 'not_in_trash')
                throw new UmhError('RESTORE_NOT_FOUND', `restore target is not in trash: ${id}`);
            throw new UmhError('ENGINE_NATIVE_ERROR', `mdcg_restore: ${code}`);
        }
    }
    finally {
        await pool.stop();
    }
}
