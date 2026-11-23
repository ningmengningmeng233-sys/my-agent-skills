/**
 * packages/gateway/server — 标准 stdio MCP Gateway（C08）。
 *
 * 设计 §7：普通客户端默认看到五个工具；管理工具不进入默认模型工具面。
 * 总控裁决（binding）：MCP inputSchema 宽松透传，拒绝单点在 service 侧 C02
 * parser —— Gateway 不做二次收紧，避免双重真相源。
 *
 * 冻结公共工具表（工单 C08 原文）：registration 仅按此表，管理入口不能通过
 * RPC 通配自动导出。
 */
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import { UmhError } from '../contracts/index.js';
export const PUBLIC_TOOLS = [
    'memory_status',
    'memory_recall',
    'memory_search',
    'memory_get',
    'memory_remember',
];
const PUBLIC_TOOL_SET = new Set(PUBLIC_TOOLS);
/** 显式可选 schema：字段名/类型与 docs/api.md §3 及 service parser 一致；
 *  全部 .optional() + .passthrough() —— 未知字段与缺失必填字段仍直达
 *  service 拒绝点（service 仍是业务唯一真相源）；显式字段仅为客户端提示，
 *  其类型不符由 MCP SDK 按 JSON Schema 常规校验拦截（-32602），业务枚举/
 *  范围/长度校验仍在 service（C02 parser）。 */
const TOOL_SCHEMAS = Object.freeze({
    memory_status: z.object({}).passthrough(),
    memory_recall: z.object({
        query: z.string().optional(),
        scope: z.string().optional(),
        budget_tokens: z.number().optional(),
    }).passthrough(),
    memory_search: z.object({
        query: z.string().optional(),
        scope: z.string().optional(),
        limit: z.number().optional(),
    }).passthrough(),
    memory_get: z.object({
        memory_id: z.string().optional(),
    }).passthrough(),
    memory_remember: z.object({
        type: z.string().optional(),
        scope: z.string().optional(),
        title: z.string().optional(),
        content: z.string().optional(),
        evidence: z.string().optional(),
        source_id: z.string().optional(),
    }).passthrough(),
});
const TOOL_DESCRIPTIONS = {
    memory_status: 'Service health, own binding scope, engine status and available capabilities. No arguments.',
    memory_recall: 'Recall confirmed memories. Args: query, scope=auto|global|project, budget_tokens.',
    memory_search: 'Search within allowed scopes. Args: query, scope, limit.',
    memory_get: 'Fetch one memory by id after permission check. Args: memory_id.',
    memory_remember: 'Submit a candidate memory (pending_review, never auto-committed). Args: type, scope, title, content, evidence, source_id?.',
};
// ---------------------------------------------------------------------------
// 公共 wire 序列化：内部 camelCase → 公共 snake_case（C02 parser 的反向）
// ---------------------------------------------------------------------------
function serializeRecord(value) {
    if (Array.isArray(value))
        return value.map(serializeRecord);
    if (value !== null && typeof value === 'object') {
        const src = value;
        const out = {};
        for (const [key, entry] of Object.entries(src)) {
            const mapped = key === 'schemaVersion' ? 'schema_version'
                : key === 'budgetTokens' ? 'budget_tokens'
                    : key === 'tokensUsed' ? 'tokens_used'
                        : key === 'sourceId' ? 'source_id'
                            : key === 'operationId' ? 'operation_id'
                                : key === 'memoryId' ? 'memory_id'
                                    : key === 'proposalId' ? 'proposal_id'
                                        : key === 'reasonCode' ? 'reason_code'
                                            : key === 'projectId' ? 'project_id'
                                                : key;
            out[mapped] = serializeRecord(entry);
        }
        return out;
    }
    return value;
}
function isPlainObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
}
/**
 * memory_status 去枚举整形（C07 评审 carry Minor#1）：service 的 status 载荷
 * 含全量 pool registry（跨项目 projectId 可枚举）。非 admin binding 只保留
 * 设计 §7 memory_status 表承诺的四项：服务健康、自身绑定作用域、引擎状态与
 * 可用能力 —— 其它项目的标识符一律剥离。admin binding 保留完整载荷（本地
 * 管理面经受信配置持有 admin binding，模型工具面默认非 admin）。
 */
export function shapeStatusForBinding(result, binding) {
    if (!isPlainObject(result))
        return serializeRecord(result);
    if (binding.role === 'admin')
        return serializeRecord(result);
    const workers = Array.isArray(result['workers']) ? result['workers'] : [];
    const ownProjectId = binding.projectId;
    const scrubbedWorkers = workers.map((w) => {
        if (!isPlainObject(w))
            return serializeRecord(w);
        const scope = w['scope'];
        let scopeOut = serializeRecord(scope);
        if (isPlainObject(scope) && scope['kind'] === 'project') {
            const pid = scope['projectId'];
            // 自身项目条目保留 kind；其它项目条目折叠为计数位（不转发标识符）。
            scopeOut = typeof pid === 'string' && pid === ownProjectId
                ? { kind: 'project', project_id: pid }
                : { kind: 'project' };
        }
        const entry = {};
        if (typeof w['state'] === 'string')
            entry['state'] = w['state'];
        if (typeof w['pending'] === 'number')
            entry['pending'] = w['pending'];
        entry['scope'] = scopeOut;
        return entry;
    });
    const engineAvailable = workers.some((w) => isPlainObject(w) && w['state'] === 'active');
    return {
        service: { available: true },
        binding: serializeRecord(result['binding'] ?? {
            clientId: binding.clientId,
            role: binding.role,
            bindingId: binding.bindingId,
            ...(binding.projectId !== undefined ? { projectId: binding.projectId } : {}),
        }),
        engineAvailable,
        readServiceWired: result['readServiceWired'] ?? false,
        workers: scrubbedWorkers,
    };
}
/** dispatch 结果 → 公共 wire 载荷（status 按 binding 整形，其余直转字段名）。 */
export function serializeResult(tool, result, binding) {
    if (tool === 'memory_status')
        return shapeStatusForBinding(result, binding);
    return serializeRecord(result);
}
function toolError(message) {
    return { content: [{ type: 'text', text: message }], isError: true };
}
function describeCallError(tool, err) {
    const code = err?.code;
    const message = err?.message ?? String(err);
    // 业务 rejected 与协议/传输错误都以 isError 工具结果返回（协议 error 与
    // 业务 rejected 的区分在 wire 上：本 Gateway 只注册五工具，未知工具名由
    // SDK 抛 JSON-RPC InvalidParams 协议错误，不经过这里）。
    return typeof code === 'string' && code !== '' ? `[${code}] ${message}` : message;
}
export async function startGateway(opts) {
    const { binding, rpc } = opts;
    // 启动 fail-fast：service 不可达时拒绝启动（expired/unknown binding 由
    // main.ts 在绑定文件+服务侧双重核验；此处核验传输可达性）。
    await rpc.checkHealth();
    const server = new McpServer({ name: 'umh-gateway', version: '1.0.0' });
    for (const name of PUBLIC_TOOLS) {
        server.registerTool(name, { description: TOOL_DESCRIPTIONS[name], inputSchema: TOOL_SCHEMAS[name] }, async (args) => {
            try {
                const wireArgs = (args ?? {});
                if (name === 'memory_remember') {
                    // 写路径幂等：先预签发 operationId，再作为可信 operationId 分发；
                    // 超时不 retry —— surface unknown/timeout，由调用方按 ID 对账。
                    const { operationId } = await rpc.prepareOperation(binding.bindingId);
                    const result = await rpc.dispatch({ binding, tool: name, args: wireArgs, operationId });
                    return { content: [{ type: 'text', text: JSON.stringify(serializeResult(name, result, binding)) }] };
                }
                const result = await rpc.dispatch({ binding, tool: name, args: wireArgs });
                return { content: [{ type: 'text', text: JSON.stringify(serializeResult(name, result, binding)) }] };
            }
            catch (err) {
                if (err instanceof UmhError && (err.code === 'RPC_TIMEOUT' || err.code === 'RPC_UNAVAILABLE' || err.code === 'SERVICE_UNAVAILABLE')) {
                    return toolError(`[SERVICE_UNAVAILABLE] central service is unreachable: ${err.message}`);
                }
                return toolError(describeCallError(name, err));
            }
        });
    }
    // 防御性断言：注册表恰为五公共工具（管理入口不得经 RPC 通配导出）。
    const registered = Object.keys(server._registeredTools ?? {});
    for (const name of registered) {
        if (!PUBLIC_TOOL_SET.has(name)) {
            throw new UmhError('GATEWAY_TOOL_LEAK', `non-public tool registered: ${name}`);
        }
    }
    const transport = new StdioServerTransport();
    await server.connect(transport);
    let closed = false;
    return {
        server,
        async close() {
            if (closed)
                return;
            closed = true;
            // EOF hygiene：只释放自己的连接，绝不停全局 service。
            try {
                await transport.close();
            }
            catch { /* 尽力释放 */ }
            try {
                await server.close();
            }
            catch { /* 尽力释放 */ }
        },
    };
}
