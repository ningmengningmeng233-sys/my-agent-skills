/**
 * packages/gateway/rpc-client — Gateway 侧私有 RPC 客户端（C08）。
 *
 * 设计 §5 + C07 handoff（binding）：Gateway 是 RPC client，走 token-only 路径；
 * 绝不持有 admin nonce（review/reconcile/stop/listPending 私有管理通路与
 * Gateway 无关）。Gateway 只用两种 token-only 调用：
 *   - GET /health（服务可达性探测；unavailable 时可诊断错误）
 *   - POST /rpc/dispatch {binding,tool,args,operationId?}
 *   - POST /rpc/prepareOperation {bindingId}（写前预签发 operationId）
 *
 * 写路径幂等（工单 C08 + C06 语义）：memory_remember 先经 prepareOperation
 * 取得服务签发的 operationId，再作为可信 operationId 随 dispatch 发送 ——
 * 响应丢失后可按 ID 对账。RPC client 绝不在超时后自动 retry 写（unknown/
 * timeout 直接 surface 给调用方，由调用方按 operationId 对账）。
 *
 * 失败证据保全（C02 评审 carry，同 C07 launchService）：HTTP 层失败（连接
 * 拒绝/超时/非 JSON）抛出的 Error 携带 url 路径、耗时与底层原因 —— 先于
 * harness t.after 清理抛出，证据不可被销毁。
 */
import { type Binding } from '../contracts/index.js';
export declare const GATEWAY_RPC_TIMEOUT_MS = 20000;
export declare const GATEWAY_HEALTH_TIMEOUT_MS = 5000;
export interface GatewayRpcOptions {
    url: string;
    token: string;
    timeoutMs?: number;
}
/**
 * 网关 RPC 句柄：dispatch / prepareOperation / health。
 * 业务拒绝（200 {ok:false,error}）抛 UmhError(code,message) —— 调用方按 code
 * 区分业务 rejected；协议错误（4xx/5xx {error}）抛带 HTTP status 的 UmhError ——
 * 两类严格区分（工单 C08 要求）。
 */
export declare class GatewayRpcClient {
    private readonly url;
    private readonly token;
    private readonly timeoutMs;
    constructor(opts: GatewayRpcOptions);
    /** 服务可达性探测（token-only GET /health）。unavailable → 可诊断错误。 */
    checkHealth(): Promise<void>;
    /** 写前预签发 operationId（token-only POST /rpc/prepareOperation）。 */
    prepareOperation(bindingId: string): Promise<{
        operationId: string;
    }>;
    /**
     * 分发工具调用（token-only POST /rpc/dispatch）。绝不自动 retry：
     * 超时/传输失败直接抛 RPC_TIMEOUT / RPC_UNAVAILABLE，调用方按
     * operationId 对账（C06 语义）。
     */
    dispatch(req: {
        binding: Binding;
        tool: string;
        args: unknown;
        operationId?: string;
    }): Promise<unknown>;
}
