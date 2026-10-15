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
import { type Binding } from '../contracts/index.js';
import type { GatewayRpcClient } from './rpc-client.js';
export declare const PUBLIC_TOOLS: readonly ["memory_status", "memory_recall", "memory_search", "memory_get", "memory_remember"];
export type PublicToolName = (typeof PUBLIC_TOOLS)[number];
export interface GatewayServerOptions {
    binding: Binding;
    rpc: GatewayRpcClient;
}
/**
 * memory_status 去枚举整形（C07 评审 carry Minor#1）：service 的 status 载荷
 * 含全量 pool registry（跨项目 projectId 可枚举）。非 admin binding 只保留
 * 设计 §7 memory_status 表承诺的四项：服务健康、自身绑定作用域、引擎状态与
 * 可用能力 —— 其它项目的标识符一律剥离。admin binding 保留完整载荷（本地
 * 管理面经受信配置持有 admin binding，模型工具面默认非 admin）。
 */
export declare function shapeStatusForBinding(result: unknown, binding: Binding): unknown;
/** dispatch 结果 → 公共 wire 载荷（status 按 binding 整形，其余直转字段名）。 */
export declare function serializeResult(tool: string, result: unknown, binding: Binding): unknown;
export interface GatewayHandle {
    server: McpServer;
    close(): Promise<void>;
}
export declare function startGateway(opts: GatewayServerOptions): Promise<GatewayHandle>;
