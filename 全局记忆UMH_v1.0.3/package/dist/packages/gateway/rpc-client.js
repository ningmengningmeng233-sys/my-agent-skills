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
import { request } from 'node:http';
import { URL } from 'node:url';
import { UmhError } from '../contracts/index.js';
export const GATEWAY_RPC_TIMEOUT_MS = 20_000;
export const GATEWAY_HEALTH_TIMEOUT_MS = 5_000;
function postJson(url, path, token, body, timeoutMs) {
    const startedAt = Date.now();
    const target = new URL(path, url);
    const payload = JSON.stringify(body);
    return new Promise((resolve, reject) => {
        const fail = (code, detail) => {
            const err = new UmhError(code, `gateway rpc ${path} failed after ${Date.now() - startedAt}ms: ${detail} (url=${target.host}${target.pathname})`);
            reject(err);
        };
        let req;
        try {
            req = request({
                hostname: target.hostname,
                port: target.port,
                path: `${target.pathname}${target.search}`,
                method: 'POST',
                headers: {
                    authorization: `Bearer ${token}`,
                    'content-type': 'application/json',
                    'content-length': Buffer.byteLength(payload),
                },
                timeout: timeoutMs,
            }, (res) => {
                const chunks = [];
                res.on('data', (c) => chunks.push(c));
                res.on('end', () => {
                    const text = Buffer.concat(chunks).toString('utf8');
                    let json;
                    try {
                        json = JSON.parse(text);
                    }
                    catch {
                        fail('RPC_BAD_RESPONSE', `non-JSON body (status ${res.statusCode}): ${text.slice(0, 200)}`);
                        return;
                    }
                    resolve({ status: res.statusCode ?? 0, json });
                });
                res.on('error', (err) => fail('RPC_TRANSPORT_ERROR', `response stream failed: ${err.message}`));
            });
        }
        catch (err) {
            fail('RPC_TRANSPORT_ERROR', `request setup failed: ${err.message}`);
            return;
        }
        req.on('timeout', () => {
            req.destroy();
            fail('RPC_TIMEOUT', `timed out after ${timeoutMs}ms (no auto-retry; reconcile by operationId)`);
        });
        req.on('error', (err) => fail('RPC_UNAVAILABLE', err.message));
        req.write(payload);
        req.end();
    });
}
function getJson(url, path, token, timeoutMs) {
    const startedAt = Date.now();
    const target = new URL(path, url);
    return new Promise((resolve, reject) => {
        const fail = (code, detail) => {
            reject(new UmhError(code, `gateway rpc ${path} failed after ${Date.now() - startedAt}ms: ${detail} (url=${target.host}${target.pathname})`));
        };
        let req;
        try {
            req = request({
                hostname: target.hostname,
                port: target.port,
                path: `${target.pathname}${target.search}`,
                method: 'GET',
                headers: { authorization: `Bearer ${token}` },
                timeout: timeoutMs,
            }, (res) => {
                const chunks = [];
                res.on('data', (c) => chunks.push(c));
                res.on('end', () => {
                    const text = Buffer.concat(chunks).toString('utf8');
                    try {
                        resolve({ status: res.statusCode ?? 0, json: JSON.parse(text) });
                    }
                    catch {
                        fail('RPC_BAD_RESPONSE', `non-JSON body (status ${res.statusCode}): ${text.slice(0, 200)}`);
                    }
                });
                res.on('error', (err) => fail('RPC_TRANSPORT_ERROR', `response stream failed: ${err.message}`));
            });
        }
        catch (err) {
            fail('RPC_TRANSPORT_ERROR', `request setup failed: ${err.message}`);
            return;
        }
        req.on('timeout', () => {
            req.destroy();
            fail('RPC_TIMEOUT', `timed out after ${timeoutMs}ms`);
        });
        req.on('error', (err) => fail('RPC_UNAVAILABLE', err.message));
        req.end();
    });
}
/**
 * 网关 RPC 句柄：dispatch / prepareOperation / health。
 * 业务拒绝（200 {ok:false,error}）抛 UmhError(code,message) —— 调用方按 code
 * 区分业务 rejected；协议错误（4xx/5xx {error}）抛带 HTTP status 的 UmhError ——
 * 两类严格区分（工单 C08 要求）。
 */
export class GatewayRpcClient {
    url;
    token;
    timeoutMs;
    constructor(opts) {
        if (typeof opts.url !== 'string' || opts.url.trim() === '') {
            throw new UmhError('INVALID_ARGUMENT', 'gateway rpc url must be a non-empty string');
        }
        if (typeof opts.token !== 'string' || opts.token === '') {
            throw new UmhError('INVALID_ARGUMENT', 'gateway rpc token must be a non-empty string');
        }
        this.url = opts.url;
        this.token = opts.token;
        this.timeoutMs = opts.timeoutMs ?? GATEWAY_RPC_TIMEOUT_MS;
    }
    /** 服务可达性探测（token-only GET /health）。unavailable → 可诊断错误。 */
    async checkHealth() {
        let res;
        try {
            res = await getJson(this.url, '/health', this.token, GATEWAY_HEALTH_TIMEOUT_MS);
        }
        catch (err) {
            throw new UmhError('SERVICE_UNAVAILABLE', `central service is unreachable: ${err.message}`);
        }
        if (res.status !== 200) {
            throw new UmhError('SERVICE_UNAVAILABLE', `central service health check returned HTTP ${res.status}`);
        }
    }
    /** 写前预签发 operationId（token-only POST /rpc/prepareOperation）。 */
    async prepareOperation(bindingId) {
        const res = await postJson(this.url, '/rpc/prepareOperation', this.token, { bindingId }, this.timeoutMs);
        return unwrapBusiness(res, '/rpc/prepareOperation');
    }
    /**
     * 分发工具调用（token-only POST /rpc/dispatch）。绝不自动 retry：
     * 超时/传输失败直接抛 RPC_TIMEOUT / RPC_UNAVAILABLE，调用方按
     * operationId 对账（C06 语义）。
     */
    async dispatch(req) {
        const res = await postJson(this.url, '/rpc/dispatch', this.token, {
            binding: req.binding,
            tool: req.tool,
            args: req.args ?? {},
            ...(req.operationId !== undefined ? { operationId: req.operationId } : {}),
        }, this.timeoutMs);
        return unwrapBusiness(res, '/rpc/dispatch');
    }
}
function unwrapBusiness(res, path) {
    const json = res.json;
    if (res.status === 200 && json['ok'] === true)
        return json['result'];
    if (res.status === 200) {
        const err = json['error'];
        throw new UmhError(String(err?.code ?? 'RPC_ERROR'), String(err?.message ?? `${path} rejected the call`));
    }
    const err = json['error'];
    throw new UmhError(String(err?.code ?? 'RPC_PROTOCOL_ERROR'), `${path} protocol error (HTTP ${res.status}): ${String(err?.message ?? 'unknown')}`);
}
