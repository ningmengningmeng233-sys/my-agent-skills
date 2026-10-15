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
// ---------------------------------------------------------------------------
// 错误类型
// ---------------------------------------------------------------------------
/** 跨组件统一错误：`code` 用于程序分支，`message` 用于人类阅读。 */
export class UmhError extends Error {
    code;
    constructor(code, message) {
        super(message);
        this.name = 'UmhError';
        this.code = code;
    }
}
// ---------------------------------------------------------------------------
// 共享上限常量（计划 v1 C02 最小实现清单）
// ---------------------------------------------------------------------------
/** title 上限：200 Unicode 字符（按 code point 计数）。 */
export const TITLE_MAX_CHARS = 200;
/** content 上限：16384 UTF-8 bytes。 */
export const CONTENT_MAX_BYTES = 16384;
/** evidence 上限：4096 UTF-8 bytes。 */
export const EVIDENCE_MAX_BYTES = 4096;
/** query 上限：4096 UTF-8 bytes。 */
export const QUERY_MAX_BYTES = 4096;
/** budget_tokens 上限：1～8192 整数。 */
export const BUDGET_TOKENS_MAX = 8192;
/** memory_search 的 limit 上限：1～100 整数。 */
export const SEARCH_LIMIT_MAX = 100;
/** RPC ingress body 上限：256KiB（后续 service/gateway 任务使用）。 */
export const RPC_INGRESS_BODY_MAX_BYTES = 256 * 1024;
// ---------------------------------------------------------------------------
// wire 解析：严格字段白名单 -> 内部 camelCase
// ---------------------------------------------------------------------------
const MEMORY_TYPES = new Set([
    'preference',
    'fact',
    'project',
    'decision',
    'procedure',
    'lesson',
    'pitfall',
    'constraint',
    'environment',
    'architecture',
    'reference',
]);
/** memory_remember wire 字段白名单（公共 wire 用 snake_case）。 */
const REMEMBER_WIRE_FIELDS = new Set([
    'type',
    'scope',
    'title',
    'content',
    'evidence',
    'source_id',
]);
/** memory_recall wire 字段白名单。 */
const RECALL_WIRE_FIELDS = new Set(['query', 'scope', 'budget_tokens']);
const WRITE_SCOPES = new Set(['global', 'project']);
const READ_SCOPES = new Set(['auto', 'global', 'project']);
/**
 * 计算 UTF-8 编码字节数（与 TextEncoder 输出长度一致，含孤立代理项替换为 U+FFFD 的情形）。
 * 纯 ES2022 实现，避免依赖平台全局对象。
 */
function utf8ByteLength(value) {
    let bytes = 0;
    for (let i = 0; i < value.length; i++) {
        const code = value.codePointAt(i);
        if (code > 0xffff)
            i++; // 代理对整体消费
        if (code < 0x80)
            bytes += 1;
        else if (code < 0x800)
            bytes += 2;
        else if (code < 0x10000)
            bytes += 3;
        else
            bytes += 4;
    }
    return bytes;
}
function requirePlainObject(value, what) {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        throw new UmhError('INVALID_ARGUMENT', `${what} must be a JSON object`);
    }
    return value;
}
function rejectUnknownFields(value, allowed) {
    for (const key of Object.keys(value)) {
        if (!allowed.has(key)) {
            throw new UmhError('INVALID_ARGUMENT', `unknown field: ${key}`);
        }
    }
}
function requireEnum(value, field, allowed) {
    if (typeof value !== 'string' || !allowed.has(value)) {
        throw new UmhError('INVALID_ARGUMENT', `field ${field} must be one of: ${[...allowed].join('|')}`);
    }
    return value;
}
function requireBoundedString(value, field, maxBytes) {
    const raw = value[field];
    if (typeof raw !== 'string') {
        throw new UmhError('INVALID_ARGUMENT', `field ${field} must be a string`);
    }
    const bytes = utf8ByteLength(raw);
    if (bytes < 1) {
        throw new UmhError('INVALID_ARGUMENT', `field ${field} must not be empty`);
    }
    if (bytes > maxBytes) {
        throw new UmhError('INVALID_ARGUMENT', `field ${field} exceeds ${maxBytes} UTF-8 bytes (got ${bytes})`);
    }
    return raw;
}
function requireTitle(value) {
    const raw = value['title'];
    if (typeof raw !== 'string') {
        throw new UmhError('INVALID_ARGUMENT', 'field title must be a string');
    }
    const chars = [...raw].length;
    if (chars < 1 || chars > TITLE_MAX_CHARS) {
        throw new UmhError('INVALID_ARGUMENT', `field title must be 1-${TITLE_MAX_CHARS} Unicode characters (got ${chars})`);
    }
    return raw;
}
function requireInt(value, field, min, max) {
    const raw = value[field];
    if (typeof raw !== 'number' || !Number.isInteger(raw) || raw < min || raw > max) {
        throw new UmhError('INVALID_ARGUMENT', `field ${field} must be an integer between ${min} and ${max}`);
    }
    return raw;
}
/** 解析 memory_remember wire 入参；未知字段/非对象/数组/session 一律 INVALID_ARGUMENT。 */
export function parseRemember(value) {
    const wire = requirePlainObject(value, 'memory_remember input');
    rejectUnknownFields(wire, REMEMBER_WIRE_FIELDS);
    const type = requireEnum(wire['type'], 'type', MEMORY_TYPES);
    const scope = requireEnum(wire['scope'], 'scope', WRITE_SCOPES);
    const title = requireTitle(wire);
    const content = requireBoundedString(wire, 'content', CONTENT_MAX_BYTES);
    const evidence = requireBoundedString(wire, 'evidence', EVIDENCE_MAX_BYTES);
    const input = { type, scope, title, content, evidence };
    const sourceId = wire['source_id'];
    if (sourceId !== undefined) {
        if (typeof sourceId !== 'string') {
            throw new UmhError('INVALID_ARGUMENT', 'field source_id must be a string');
        }
        input.sourceId = sourceId;
    }
    return input;
}
/** 解析 memory_recall wire 入参；budget_tokens 1～8192 整数，query 1～4096 bytes。 */
export function parseRecall(value) {
    const wire = requirePlainObject(value, 'memory_recall input');
    rejectUnknownFields(wire, RECALL_WIRE_FIELDS);
    const query = requireBoundedString(wire, 'query', QUERY_MAX_BYTES);
    const scope = requireEnum(wire['scope'], 'scope', READ_SCOPES);
    const budgetTokens = requireInt(wire, 'budget_tokens', 1, BUDGET_TOKENS_MAX);
    return { query, scope, budgetTokens };
}
/** 校验 memory_search 的 limit：1～SEARCH_LIMIT_MAX 整数。 */
export function validateSearchLimit(value) {
    if (typeof value !== 'number' || !Number.isInteger(value) || value < 1 || value > SEARCH_LIMIT_MAX) {
        throw new UmhError('INVALID_ARGUMENT', `field limit must be an integer between 1 and ${SEARCH_LIMIT_MAX}`);
    }
    return value;
}
// ---------------------------------------------------------------------------
// 公共引用 handle（C09）：Engine 内部仍用 nativeId；跨边界公共引用统一转 handle
// ---------------------------------------------------------------------------
/**
 * handle 中 nativeId 的非法字符：`:` 是 handle 的结构分隔符；`/`、`\` 是路径
 * 分隔符（防遍历与防歧义）。项目段 projectId 的合法性由使用方按绑定重新验证
 * （decode 只做语法解析，绝不承载授权）。
 */
const HANDLE_NATIVE_ID_FORBIDDEN = /[:/\\]/;
/**
 * 编码公共引用 handle：全局 `g:<nativeId>`，项目 `p:<projectId>:<nativeId>`。
 * nativeId 为空或含冒号/路径分隔符 → INVALID_ARGUMENT（handle 是客户端可构造
 * 的公共形态，必须保持无歧义可解码；UMH 写侧生成的 `mem_` id 天然满足）。
 */
export function encodeHandle(scope, nativeId) {
    if (typeof nativeId !== 'string' || nativeId === '') {
        throw new UmhError('INVALID_ARGUMENT', 'nativeId must be a non-empty string');
    }
    if (HANDLE_NATIVE_ID_FORBIDDEN.test(nativeId)) {
        throw new UmhError('INVALID_ARGUMENT', 'nativeId must not contain ":" or path separators');
    }
    if (scope === null || typeof scope !== 'object' || Array.isArray(scope)) {
        throw new UmhError('INVALID_ARGUMENT', 'scope must be a ScopeRef object');
    }
    if (scope.kind === 'global')
        return `g:${nativeId}`;
    if (scope.kind === 'project') {
        if (typeof scope.projectId !== 'string' || scope.projectId === '') {
            throw new UmhError('INVALID_ARGUMENT', 'project scope handle requires a projectId');
        }
        return `p:${scope.projectId}:${nativeId}`;
    }
    throw new UmhError('INVALID_ARGUMENT', `scope kind must be 'global' or 'project'`);
}
/**
 * 解码 handle（纯语法解析，不做授权——授权由 get 按绑定重新验证）。
 * 形状不符（未知前缀 / 缺段 / nativeId 含冒号或路径分隔符 / 空段）→ null，
 * 调用方（core read get）一律按 NOT_FOUND 语义处理，不区分"格式非法"与
 * "不存在"，避免泄露任何可探测性差异。
 */
export function decodeHandle(handle) {
    if (typeof handle !== 'string' || handle === '')
        return null;
    if (handle.startsWith('g:')) {
        const nativeId = handle.slice(2);
        if (nativeId === '' || HANDLE_NATIVE_ID_FORBIDDEN.test(nativeId))
            return null;
        return { scope: { kind: 'global' }, nativeId };
    }
    if (handle.startsWith('p:')) {
        const rest = handle.slice(2);
        const sep = rest.indexOf(':');
        if (sep <= 0)
            return null;
        const projectId = rest.slice(0, sep);
        const nativeId = rest.slice(sep + 1);
        if (projectId === '' || nativeId === '' || HANDLE_NATIVE_ID_FORBIDDEN.test(nativeId))
            return null;
        return { scope: { kind: 'project', projectId }, nativeId };
    }
    return null;
}
