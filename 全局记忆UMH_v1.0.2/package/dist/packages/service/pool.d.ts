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
import { type MemoryEngine, type ScopeRef } from '../contracts/index.js';
/** 在途上限（每个作用域运行中+排队中的请求总数；超过即 POOL_OVERLOAD）。 */
export declare const DEFAULT_QUEUE_LIMIT = 32;
/** 单次操作超时（native 调用通常亚秒级；默认 30s 余量充足）。 */
export declare const DEFAULT_OP_TIMEOUT_MS = 30000;
export interface EnginePoolOptions {
    /** 作用域 → 引擎工厂（默认真实 md_cg worker；测试注入 fake）。 */
    factory: (scope: ScopeRef) => Promise<MemoryEngine>;
    /** 每作用域等待队列上限（默认 32）。 */
    queueLimit?: number;
    /** 单次操作超时毫秒（默认 30000；测试注入小值）。 */
    opTimeoutMs?: number;
    /** worker registry 变化回调（服务用它重写 runtime/status.json）。 */
    onWorkersChanged?: () => void;
}
export type PoolWorkerState = 'active' | 'crashed';
export interface PoolWorkerInfo {
    scope: ScopeRef;
    state: PoolWorkerState;
    pending: number;
}
export declare class EnginePool {
    private readonly opts;
    private entries;
    private stopped;
    private readonly queueLimit;
    private readonly opTimeoutMs;
    constructor(opts: EnginePoolOptions);
    keyOf(scope: ScopeRef): string;
    /**
     * 取该作用域的唯一 worker（懒启动；同 key 复用同一实例——一个作用域实例
     * 独占一个 worker，绝不因并发首请求起两个）。
     */
    get(scope: ScopeRef): Promise<MemoryEngine>;
    /**
     * 在该作用域的 worker 上串行执行 fn：FIFO、同 root 绝不重叠。
     * 过载（POOL_OVERLOAD）与超时（POOL_TIMEOUT）是两种不同错误。
     * queueLimit 语义：该作用域在途（运行中+排队中）请求总数上限。
     */
    run<T>(scope: ScopeRef, fn: (engine: MemoryEngine) => Promise<T>): Promise<T>;
    /** worker registry 快照（服务 status / 私有管理通路消费）。 */
    registry(): PoolWorkerInfo[];
    /** 关闭全部 worker 并清空 registry（服务 stop 的组成步骤）。 */
    stop(): Promise<void>;
    private acquireEntry;
    private withTimeout;
    private markCrashed;
    private notifyChange;
}
/**
 * 真实引擎工厂：root = resolveEngineRoot（物理合法性唯一强制点），
 * aux = <umhHome>/security/<global|project-<projectId>>，worker 角色 admin
 * （服务特权 worker；客户角色在 dispatch 边界复核）。
 */
export declare function createRealEngineFactory(umhHome: string, python: string, policyFile: string): (scope: ScopeRef) => Promise<MemoryEngine>;
