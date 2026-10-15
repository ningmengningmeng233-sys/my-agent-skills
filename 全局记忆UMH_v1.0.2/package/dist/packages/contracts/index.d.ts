/**
 * packages/contracts — 唯一跨组件类型与公共 wire 解析（C02）。
 *
 * 内部字段一律 camelCase；公共 MCP wire 字段为 snake_case：
 *   budget_tokens / source_id / operation_id / memory_id / proposal_id /
 *   reason_code / tokens_used / schema_version / project_id
 * C02 parser 在本模块把 wire 入参严格校验后转成内部 camelCase；
 * C08 serializer 在 Gateway 边界反向转换。
 *
 * 类型合同冻结自计划 v1「文件结构与共用合同」，禁止邻接工单改名。
 */
/** 跨组件统一错误：`code` 用于程序分支，`message` 用于人类阅读。 */
export declare class UmhError extends Error {
    readonly code: string;
    constructor(code: string, message: string);
}
export type ClientId = 'cherry' | 'qoder' | 'zcode' | 'codex' | 'claude' | 'minimax' | 'dsh' | 'generic';
export type Role = 'reader' | 'recorder' | 'admin';
export type QueryScope = 'auto' | 'global' | 'project';
export type ScopeRef = {
    kind: 'global';
} | {
    kind: 'project';
    projectId: string;
};
export type Binding = {
    clientId: ClientId;
    role: Role;
    bindingId: string;
    projectId?: string;
    workspace?: string;
};
export type MemoryType = 'preference' | 'fact' | 'project' | 'decision' | 'procedure' | 'lesson' | 'pitfall' | 'constraint' | 'environment' | 'architecture' | 'reference';
export type RememberInput = {
    type: MemoryType;
    scope: 'global' | 'project';
    title: string;
    content: string;
    evidence: string;
    sourceId?: string;
};
export type WriteOutcome = {
    status: 'pending_review' | 'duplicate' | 'conflict' | 'rejected' | 'committed' | 'unknown';
    committed: boolean | null;
    operationId: string;
    memoryId?: string;
    proposalId?: string;
    reasonCode?: string;
};
export type RecallInput = {
    query: string;
    scope: QueryScope;
    budgetTokens: number;
};
export type MemoryRecord = {
    schemaVersion: 1;
    id: string;
    type: MemoryType;
    scope: ScopeRef;
    title: string;
    content: string;
    status: 'active' | 'superseded';
    confidence: 'confirmed' | 'observed' | 'inferred' | 'uncertain';
    tags: string[];
    source: {
        clientId: ClientId;
        sourceId?: string;
    };
    provenance: {
        createdAt: string;
        approvedAt?: string;
    };
    relations: {
        supersedes: string[];
        conflictsWith: string[];
        derivedFrom: string[];
    };
};
export type RecallResult = {
    memories: MemoryRecord[];
    tokensUsed: number;
    budgetTokens: number;
    truncated: boolean;
    warnings: string[];
};
export type EngineWriteResult = {
    committed: boolean;
    memoryId?: string;
    proposalId?: string;
    reasonCode?: string;
};
export type ReviewDecision = {
    proposalId: string;
    decision: 'accept' | 'reject' | 'supersede';
    reason: string;
    supersedes?: string[];
};
/** 单作用域引擎实例；不接收模型任意 project_id。 */
export interface MemoryEngine {
    health(): Promise<{
        available: boolean;
    }>;
    propose(input: RememberInput, meta: {
        memoryId: string;
        operationId: string;
        binding: Binding;
    }): Promise<EngineWriteResult>;
    review(input: ReviewDecision): Promise<EngineWriteResult>;
    findOperation(operationId: string): Promise<{
        state: 'absent' | 'pending' | 'committed';
        memoryId?: string;
        proposalId?: string;
    }>;
    get(id: string): Promise<MemoryRecord | null>;
    findDuplicate(input: RememberInput): Promise<MemoryRecord | null>;
    recall(input: RecallInput): Promise<RecallResult>;
    search(query: string, limit: number): Promise<MemoryRecord[]>;
    listPending(): Promise<{
        proposalId: string;
        operationId: string;
    }[]>;
    forget(id: string): Promise<void>;
    restore(id: string): Promise<void>;
    rebuildIndex(): Promise<void>;
    close(): Promise<void>;
}
/** title 上限：200 Unicode 字符（按 code point 计数）。 */
export declare const TITLE_MAX_CHARS = 200;
/** content 上限：16384 UTF-8 bytes。 */
export declare const CONTENT_MAX_BYTES = 16384;
/** evidence 上限：4096 UTF-8 bytes。 */
export declare const EVIDENCE_MAX_BYTES = 4096;
/** query 上限：4096 UTF-8 bytes。 */
export declare const QUERY_MAX_BYTES = 4096;
/** budget_tokens 上限：1～8192 整数。 */
export declare const BUDGET_TOKENS_MAX = 8192;
/** memory_search 的 limit 上限：1～100 整数。 */
export declare const SEARCH_LIMIT_MAX = 100;
/** RPC ingress body 上限：256KiB（后续 service/gateway 任务使用）。 */
export declare const RPC_INGRESS_BODY_MAX_BYTES: number;
/** 解析 memory_remember wire 入参；未知字段/非对象/数组/session 一律 INVALID_ARGUMENT。 */
export declare function parseRemember(value: unknown): RememberInput;
/** 解析 memory_recall wire 入参；budget_tokens 1～8192 整数，query 1～4096 bytes。 */
export declare function parseRecall(value: unknown): RecallInput;
/** 校验 memory_search 的 limit：1～SEARCH_LIMIT_MAX 整数。 */
export declare function validateSearchLimit(value: unknown): number;
/**
 * 编码公共引用 handle：全局 `g:<nativeId>`，项目 `p:<projectId>:<nativeId>`。
 * nativeId 为空或含冒号/路径分隔符 → INVALID_ARGUMENT（handle 是客户端可构造
 * 的公共形态，必须保持无歧义可解码；UMH 写侧生成的 `mem_` id 天然满足）。
 */
export declare function encodeHandle(scope: ScopeRef, nativeId: string): string;
/**
 * 解码 handle（纯语法解析，不做授权——授权由 get 按绑定重新验证）。
 * 形状不符（未知前缀 / 缺段 / nativeId 含冒号或路径分隔符 / 空段）→ null，
 * 调用方（core read get）一律按 NOT_FOUND 语义处理，不区分"格式非法"与
 * "不存在"，避免泄露任何可探测性差异。
 */
export declare function decodeHandle(handle: string): {
    scope: ScopeRef;
    nativeId: string;
} | null;
