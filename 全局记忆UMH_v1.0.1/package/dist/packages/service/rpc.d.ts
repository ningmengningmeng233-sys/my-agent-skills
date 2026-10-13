/**
 * packages/service/rpc — 私有 loopback RPC（C07）。
 *
 * 设计 §5：中央服务绑定 loopback，并验证 Gateway 身份。对内 RPC 不是对外网络
 * 服务；v1 不开放 LAN。引擎 worker 的 stdin/stdout 不直接交给客户端。
 *
 * 合同（工单 C07 + 总控裁决）：
 *   - 绑定严格 127.0.0.1（非法 bind addr 拒绝启动，INVALID_BIND_ADDRESS）。
 *   - RPC auth：Bearer token（<umhHome>/security/rpc/token，文件私有——Windows
 *     ACL 以当前用户 SID 校验 verify readable-only-by-owner，失败即拒绝启动；
 *     缺失/错误 token → 401 UNAUTHORIZED 协议错误）。校验用 sha256+timingSafeEqual
 *     （恒时比较，不泄漏长度/前缀）。
 *   - body 上限 256KiB（contracts.RPC_INGRESS_BODY_MAX_BYTES）→ 413 BODY_TOO_LARGE。
 *   - 畸形请求（非 JSON / 信封形状错）→ 400 PROTOCOL_ERROR，与业务 rejected
 *     （200 {ok:false,error}）严格区分。
 *   - auth 不接收模型角色覆盖：binding 是 Gateway 受信配置（信封承载），不是
 *     工具入参；模型在 args 里携带 role/approved 类字段在 wire 解析层拒绝。
 *   - 每 binding 限 scope/tool：模型工具面仅五个公共工具（memory_status/recall/
 *     search/get/remember），reader 无写，管理工具（review/reconcile/stop）不在
 *     dispatch 面。私有管理通路认证分级（C07 评审修正，精确口径）：
 *       · token+实例 nonce 双凭据（写操作/裁决类）：POST /admin/review、
 *         /admin/reconcile、/admin/listPending、/admin/stop；
 *       · token-only：GET /admin/workers（只读 worker registry）、GET /health、
 *         POST /rpc/dispatch、POST /rpc/prepareOperation。
 *     本地管理员可读 runtime/ 记录取得 nonce；Gateway 不持有 nonce。
 *   - C09 前读工具注入 fake readService（ReadService 接缝），不实现真实召回。
 *
 * 线路形状：POST /rpc/dispatch {binding,tool,args,operationId?} →
 *   200 {ok:true,result} | {ok:false,error:{code,message}}；4xx 为协议错误。
 */
import { type Binding, type MemoryRecord, type QueryScope, type RecallInput, type RecallResult, type ReviewDecision, type ScopeRef } from '../contracts/index.js';
import type { EnginePool, PoolWorkerInfo } from './pool.js';
export declare const SERVICE_PROTOCOL_VERSION = 1;
export declare const DEFAULT_BIND_HOST = "127.0.0.1";
/** v1 默认模型工具面（设计 §7 表；管理工具不进入）。 */
export declare const MODEL_TOOLS: ReadonlySet<string>;
/** 读服务接缝：服务运行时缺省注入真实实现（core/read.ts）；测试注入 fake（断言请求到达与作用域解析）。 */
export interface ReadService {
    recall(binding: Binding, input: RecallInput, scopes: ScopeRef[]): Promise<RecallResult>;
    search(binding: Binding, query: string, scope: QueryScope, scopes: ScopeRef[], limit: number): Promise<MemoryRecord[]>;
    get(binding: Binding, memoryId: string, scopes: ScopeRef[]): Promise<MemoryRecord | null>;
}
/** rpc token 文件路径：<securityDir>/rpc/token（security 属设计 §6 布局：令牌/密钥）。 */
export declare function rpcTokenPath(securityDir: string): string;
/**
 * 加载或创建 RPC token：已存在 → 校验私有性后复用（服务重启不轮换，Gateway
 * 绑定跨重启有效）；不存在 → 生成 32 字节 hex 并收紧 ACL 后独立校验。
 */
export declare function loadOrCreateRpcToken(securityDir: string): Promise<{
    token: string;
    path: string;
}>;
export interface DispatchRequest {
    binding: Binding;
    tool: string;
    args: unknown;
    operationId?: string;
}
export interface Dispatcher {
    dispatch(req: DispatchRequest): Promise<unknown>;
    prepareOperation(bindingId: string): Promise<{
        operationId: string;
    }>;
}
export declare function createDispatcher(opts: {
    pool: EnginePool;
    readService?: ReadService;
}): Dispatcher;
export interface AdminSurface {
    review(scope: ScopeRef, decision: ReviewDecision): Promise<unknown>;
    reconcile(scope: ScopeRef, operationId: string): Promise<unknown>;
    listPending(scope: ScopeRef): Promise<unknown>;
    workers(): Promise<PoolWorkerInfo[]>;
}
export interface RpcServerOptions {
    token: string;
    /**
     * 实例 nonce：写操作/裁决类 admin 端点（POST review/reconcile/listPending/
     * stop）要求请求携带（本地管理员读 runtime 记录）。GET /admin/workers 是
     * token-only 只读端点，不要求 nonce。
     */
    nonce: string;
    host?: string;
    dispatcher: Dispatcher;
    admin: AdminSurface;
    /** /admin/stop（nonce 核验通过）后触发（服务优雅停机）。 */
    requestStop: () => void;
}
export interface RpcServerHandle {
    port: number;
    url: string;
    close(): Promise<void>;
}
export declare function startRpcServer(opts: RpcServerOptions): Promise<RpcServerHandle>;
export declare function rpcPost(url: string, path: string, token: string, body: unknown, timeoutMs?: number): Promise<{
    status: number;
    json: Record<string, unknown>;
}>;
export declare function rpcGet(url: string, path: string, token: string, timeoutMs?: number): Promise<{
    status: number;
    json: Record<string, unknown>;
}>;
/**
 * 私有管理通路客户端（ServiceHandle.admin / CLI stop/review 消费）。
 * 认证分级：review/reconcile/listPending/stop 走 POST /admin/*（token + 实例
 * nonce 双凭据）；workers() 走 GET /admin/workers（token-only 只读，不带 nonce）。
 */
export declare class ServiceAdminClient {
    private readonly url;
    private readonly token;
    private readonly nonce;
    constructor(url: string, token: string, nonce: string);
    review(scope: ScopeRef, decision: ReviewDecision): Promise<unknown>;
    reconcile(scope: ScopeRef, operationId: string): Promise<unknown>;
    listPending(scope: ScopeRef): Promise<unknown>;
    workers(): Promise<PoolWorkerInfo[]>;
    stop(): Promise<void>;
}
