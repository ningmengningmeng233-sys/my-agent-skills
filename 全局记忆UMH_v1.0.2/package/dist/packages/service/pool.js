/**
 * packages/service/pool — 作用域 worker 调度（C07）。
 *
 * 设计 §5：每个作用域由中央服务持有一个 worker，初版顺序调度该 worker 的操作，
 * 避免多个 Gateway 独立启动写者导致状态竞争；不同作用域可独立调度。
 *
 * 合同（工单 C07）：
 *   - 每 root 懒启动 worker（get(scope) 首次请求时创建；global 在 boot readiness
 *     由服务先行初始化——owner 测试的初始 worker 计数为 1）。
 *   - 同一 root 的请求 FIFO 串行、绝不重叠（不同 root 互不阻塞）。
 *   - 过载与超时是两种不同错误（POOL_OVERLOAD vs POOL_TIMEOUT）。
 *   - worker 崩溃后拒绝该 root 的后续排队（WORKER_CRASHED，明确错误）；
 *     未知写结局按 C06 语义转入对账状态（回执已持久化，绝不自动重试）。
 *   - stop() 关闭全部 worker 并清空 registry。
 *
 * 角色边界（C04 carry）：pool 内部 worker 一律 role=admin（服务特权 worker，
 * 可 propose 可 review）；客户侧 Binding 的角色权限在 dispatch 边界复核，
 * pool 内部 role=admin 不等于客户获得 admin。
 */
import { join } from 'node:path';
import { UmhError, } from '../contracts/index.js';
import { resolveEngineRoot } from '../scope/paths.js';
import { createMdCgEngine } from '../engine/mdcg.js';
/** 在途上限（每个作用域运行中+排队中的请求总数；超过即 POOL_OVERLOAD）。 */
export const DEFAULT_QUEUE_LIMIT = 32;
/** 单次操作超时（native 调用通常亚秒级；默认 30s 余量充足）。 */
export const DEFAULT_OP_TIMEOUT_MS = 30_000;
/** 崩溃类错误码：命中即把该作用域 worker 标记为 crashed（refuse further queuing）。 */
const CRASH_CODES = new Set([
    'ENGINE_TRANSPORT_ERROR',
    'ENGINE_CLOSED',
    'ENGINE_BOOT_FAILED',
    'ENGINE_BAD_RESPONSE',
    'QUEUE_LOCK_TIMEOUT',
]);
function validateScope(scope) {
    if (scope === null || typeof scope !== 'object' || Array.isArray(scope)) {
        throw new UmhError('INVALID_ARGUMENT', 'scope must be a ScopeRef object');
    }
    if (scope.kind !== 'global' && scope.kind !== 'project') {
        throw new UmhError('INVALID_ARGUMENT', `scope kind must be 'global' or 'project'`);
    }
    if (scope.kind === 'project' && (typeof scope.projectId !== 'string' || scope.projectId === '')) {
        throw new UmhError('INVALID_ARGUMENT', 'project scope requires a projectId');
    }
}
function isCrashError(err) {
    if (!(err instanceof UmhError))
        return true; // 非受控异常：worker 状态不可信
    return CRASH_CODES.has(err.code);
}
export class EnginePool {
    opts;
    entries = new Map();
    stopped = false;
    queueLimit;
    opTimeoutMs;
    constructor(opts) {
        this.opts = opts;
        this.queueLimit = opts.queueLimit ?? DEFAULT_QUEUE_LIMIT;
        this.opTimeoutMs = opts.opTimeoutMs ?? DEFAULT_OP_TIMEOUT_MS;
    }
    keyOf(scope) {
        validateScope(scope);
        return scope.kind === 'global' ? 'global' : `project:${scope.projectId}`;
    }
    /**
     * 取该作用域的唯一 worker（懒启动；同 key 复用同一实例——一个作用域实例
     * 独占一个 worker，绝不因并发首请求起两个）。
     */
    async get(scope) {
        if (this.stopped) {
            throw new UmhError('POOL_STOPPED', 'engine pool already stopped');
        }
        const key = this.keyOf(scope);
        let entry = this.entries.get(key);
        if (entry === undefined) {
            entry = { scope, key, state: 'active', engine: null, creating: null, chain: Promise.resolve(), pending: 0 };
            this.entries.set(key, entry);
        }
        if (entry.state === 'crashed') {
            throw new UmhError('WORKER_CRASHED', `worker for scope ${key} crashed; further queuing is refused until the service restarts (reconcile pending operations afterwards)`);
        }
        if (entry.engine !== null)
            return entry.engine;
        if (entry.creating === null) {
            const creating = this.opts.factory(scope).then((engine) => {
                entry.engine = engine;
                this.notifyChange();
                return engine;
            }, (err) => {
                // 启动失败不是崩溃：允许下次请求重试（boot 是幂等的目录/令牌流程）。
                this.entries.delete(key);
                this.notifyChange();
                throw err;
            });
            entry.creating = creating;
        }
        return entry.creating;
    }
    /**
     * 在该作用域的 worker 上串行执行 fn：FIFO、同 root 绝不重叠。
     * 过载（POOL_OVERLOAD）与超时（POOL_TIMEOUT）是两种不同错误。
     * queueLimit 语义：该作用域在途（运行中+排队中）请求总数上限。
     */
    async run(scope, fn) {
        const entry = await this.acquireEntry(scope);
        if (entry.pending >= this.queueLimit) {
            throw new UmhError('POOL_OVERLOAD', `scope ${entry.key} has ${entry.pending} in-flight requests (limit ${this.queueLimit}); retry later`);
        }
        entry.pending += 1;
        try {
            const engine = entry.engine;
            const job = entry.chain.then(() => fn(engine));
            // 链跟踪完整作业时长：即使超时先返回，原操作仍在链上占位，绝不重叠。
            entry.chain = job.then(() => undefined, () => undefined);
            return await this.withTimeout(job, entry);
        }
        catch (err) {
            if (isCrashError(err))
                this.markCrashed(entry);
            throw err;
        }
        finally {
            entry.pending -= 1;
        }
    }
    /** worker registry 快照（服务 status / 私有管理通路消费）。 */
    registry() {
        return [...this.entries.values()].map((e) => ({
            scope: e.scope,
            state: e.state,
            pending: e.pending,
        }));
    }
    /** 关闭全部 worker 并清空 registry（服务 stop 的组成步骤）。 */
    async stop() {
        if (this.stopped)
            return;
        this.stopped = true;
        const entries = [...this.entries.values()];
        this.entries.clear();
        for (const entry of entries) {
            const close = async () => {
                if (entry.creating !== null) {
                    try {
                        await (await entry.creating).close();
                    }
                    catch { /* 尽力关闭 */ }
                    return;
                }
                if (entry.engine !== null) {
                    try {
                        await entry.engine.close();
                    }
                    catch { /* 尽力关闭 */ }
                }
            };
            await close();
        }
        this.notifyChange();
    }
    // ------------------------------------------------------------------
    async acquireEntry(scope) {
        const key = this.keyOf(scope);
        // get() 负责懒启动与 crashed 拒绝；此处只取 entry。
        await this.get(scope);
        return this.entries.get(key);
    }
    withTimeout(job, entry) {
        let timer;
        const timeout = new Promise((_, reject) => {
            timer = setTimeout(() => {
                reject(new UmhError('POOL_TIMEOUT', `operation on scope ${entry.key} exceeded ${this.opTimeoutMs}ms; worker marked crashed, reconcile after restart`));
            }, this.opTimeoutMs);
        });
        // 超时后 worker 的 stdin/stdout 状态不可信：标记 crashed（后续拒绝排队）。
        timeout.catch(() => this.markCrashed(entry));
        return Promise.race([job, timeout]).finally(() => clearTimeout(timer));
    }
    markCrashed(entry) {
        if (entry.state !== 'crashed') {
            entry.state = 'crashed';
            this.notifyChange();
        }
    }
    notifyChange() {
        try {
            this.opts.onWorkersChanged?.();
        }
        catch { /* 状态快照写盘失败不阻断调度 */ }
    }
}
/**
 * 真实引擎工厂：root = resolveEngineRoot（物理合法性唯一强制点），
 * aux = <umhHome>/security/<global|project-<projectId>>，worker 角色 admin
 * （服务特权 worker；客户角色在 dispatch 边界复核）。
 */
export function createRealEngineFactory(umhHome, python, policyFile) {
    if (typeof umhHome !== 'string' || umhHome.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be a non-empty string');
    }
    if (typeof python !== 'string' || python === '') {
        throw new UmhError('INVALID_ARGUMENT', 'python must be a non-empty string');
    }
    if (typeof policyFile !== 'string' || policyFile === '') {
        throw new UmhError('INVALID_ARGUMENT', 'policyFile must be a non-empty string');
    }
    return async (scope) => {
        validateScope(scope);
        const root = resolveEngineRoot(umhHome, scope);
        const auxRoot = join(umhHome, 'security', scope.kind === 'global' ? 'global' : `project-${scope.projectId}`);
        return createMdCgEngine({ scope, root, auxRoot, python, policyFile, role: 'admin' });
    };
}
