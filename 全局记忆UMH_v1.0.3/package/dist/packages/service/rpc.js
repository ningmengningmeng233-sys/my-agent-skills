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
import { spawnSync } from 'node:child_process';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { mkdir, readFile, stat, writeFile, chmod } from 'node:fs/promises';
import { createServer } from 'node:http';
import { join } from 'node:path';
import process from 'node:process';
import { RPC_INGRESS_BODY_MAX_BYTES, UmhError, } from '../contracts/index.js';
import { parseRecall, parseRemember, validateSearchLimit } from '../contracts/index.js';
import { resolveReadScopes, resolveWriteScope } from '../scope/binding.js';
import { submitCandidate } from '../core/write.js';
export const SERVICE_PROTOCOL_VERSION = 1;
export const DEFAULT_BIND_HOST = '127.0.0.1';
/** v1 默认模型工具面（设计 §7 表；管理工具不进入）。 */
export const MODEL_TOOLS = new Set([
    'memory_status',
    'memory_recall',
    'memory_search',
    'memory_get',
    'memory_remember',
]);
/** reader 绑定可用的工具（只读面）。 */
const READER_TOOLS = new Set([
    'memory_status',
    'memory_recall',
    'memory_search',
    'memory_get',
]);
const CLIENT_IDS = new Set([
    'cherry', 'qoder', 'zcode', 'codex', 'claude', 'minimax', 'dsh', 'generic',
]);
const ROLES = new Set(['reader', 'recorder', 'admin']);
// ---------------------------------------------------------------------------
// RPC token（文件私有；Windows ACL 按当前用户 SID 校验，失败即拒绝启动）
// ---------------------------------------------------------------------------
const RPC_TOKEN_DIR = 'rpc';
const RPC_TOKEN_FILE = 'token';
const IS_WIN = process.platform === 'win32';
/** rpc token 文件路径：<securityDir>/rpc/token（security 属设计 §6 布局：令牌/密钥）。 */
export function rpcTokenPath(securityDir) {
    return join(securityDir, RPC_TOKEN_DIR, RPC_TOKEN_FILE);
}
function psQuotePath(path) {
    return `'${path.replace(/'/g, "''")}'`;
}
function runPowerShell(script) {
    const out = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script], { encoding: 'utf8', timeout: 30_000, windowsHide: true, shell: false });
    if (out.error)
        return { ok: false, stdout: out.stdout ?? '', stderr: String(out.error.message) };
    return { ok: out.status === 0, stdout: out.stdout ?? '', stderr: out.stderr ?? '' };
}
/**
 * 收紧并校验 token 文件 ACL（Windows）：去继承、清既有 ACE、仅保留当前用户
 * SID 的 FullControl；随后独立读回 ACL 校验（SID 精确匹配 + 无继承），任一
 * 不满足 → RPC_TOKEN_NOT_PRIVATE（fail closed，拒绝启动）。
 * POSIX：chmod 0600 + mode 位校验。
 *
 * SID 校验（总控裁决）：IdentityReference 一律 translate 成 SID 字符串后与
 * 当前用户 SID 精确比较 —— 不受本地化组名/账户名形态影响。
 */
const ACL_HARDEN_SCRIPT_TEMPLATE = [
    "$ErrorActionPreference='Stop'",
    '$p=<PATH>',
    '$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User',
    '$acl=Get-Acl -LiteralPath $p',
    '[void]$acl.SetAccessRuleProtection($true,$false)',
    'foreach($r in @($acl.Access)){ [void]$acl.RemoveAccessRule($r) }',
    "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow')",
    '[void]$acl.AddAccessRule($rule)',
    'Set-Acl -LiteralPath $p -AclObject $acl',
    '$v=Get-Acl -LiteralPath $p',
    '$sids=@()',
    'foreach($r in $v.Access){ try { $sids+=($r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier])).Value } catch { $sids+=([string]$r.IdentityReference) } }',
    '[Console]::Out.Write((ConvertTo-Json ([ordered]@{me=$sid.Value;sids=$sids;protected=$v.AreAccessRulesProtected}) -Compress))',
].join('\n');
const ACL_VERIFY_SCRIPT_TEMPLATE = [
    "$ErrorActionPreference='Stop'",
    '$p=<PATH>',
    '$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User',
    '$v=Get-Acl -LiteralPath $p',
    '$sids=@()',
    'foreach($r in $v.Access){ try { $sids+=($r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier])).Value } catch { $sids+=([string]$r.IdentityReference) } }',
    '[Console]::Out.Write((ConvertTo-Json ([ordered]@{me=$sid.Value;sids=$sids;protected=$v.AreAccessRulesProtected}) -Compress))',
].join('\n');
function parseAclReport(stdout) {
    let parsed;
    try {
        parsed = JSON.parse(stdout.trim().split('\n').pop() ?? '');
    }
    catch {
        throw new UmhError('RPC_TOKEN_NOT_PRIVATE', 'token file ACL could not be verified (fail closed)');
    }
    const report = parsed;
    if (typeof report['me'] !== 'string' ||
        !Array.isArray(report['sids']) ||
        typeof report['protected'] !== 'boolean') {
        throw new UmhError('RPC_TOKEN_NOT_PRIVATE', 'token file ACL report is malformed (fail closed)');
    }
    return {
        me: report['me'],
        sids: report['sids'].map((s) => String(s)),
        protected: report['protected'],
    };
}
/** ACL 校验：无继承 + 允许主体恰好是当前用户 SID（verify readable-only-by-owner）。 */
function verifyAclReport(report) {
    if (!report.protected || report.sids.length === 0) {
        throw new UmhError('RPC_TOKEN_NOT_PRIVATE', 'token file ACL allows other principals or inherits (fail closed)');
    }
    const unique = [...new Set(report.sids)];
    if (unique.length !== 1 || unique[0] !== report.me) {
        throw new UmhError('RPC_TOKEN_NOT_PRIVATE', `token file is not readable-only-by-owner (ACE subjects: ${unique.join(', ')})`);
    }
}
function hardenAndVerifyTokenFileWin(file) {
    const script = ACL_HARDEN_SCRIPT_TEMPLATE.replace('<PATH>', psQuotePath(file));
    const out = runPowerShell(script);
    if (!out.ok) {
        throw new UmhError('RPC_TOKEN_NOT_PRIVATE', `token file ACL could not be hardened: ${(out.stderr || out.stdout).trim().split('\n').slice(-3).join(' | ')}`);
    }
    verifyAclReport(parseAclReport(out.stdout));
}
function assertTokenFilePrivateWin(file) {
    const script = ACL_VERIFY_SCRIPT_TEMPLATE.replace('<PATH>', psQuotePath(file));
    const out = runPowerShell(script);
    if (!out.ok) {
        throw new UmhError('RPC_TOKEN_NOT_PRIVATE', `token file ACL could not be read (fail closed)`);
    }
    verifyAclReport(parseAclReport(out.stdout));
}
async function hardenTokenFilePrivate(file) {
    if (IS_WIN) {
        hardenAndVerifyTokenFileWin(file);
        return;
    }
    await chmod(file, 0o600);
}
async function assertTokenFilePrivate(file) {
    if (IS_WIN) {
        assertTokenFilePrivateWin(file);
        return;
    }
    const st = await stat(file);
    if ((st.mode & 0o077) !== 0) {
        throw new UmhError('RPC_TOKEN_NOT_PRIVATE', `rpc token file is readable by others: ${file}`);
    }
}
/**
 * 加载或创建 RPC token：已存在 → 校验私有性后复用（服务重启不轮换，Gateway
 * 绑定跨重启有效）；不存在 → 生成 32 字节 hex 并收紧 ACL 后独立校验。
 */
export async function loadOrCreateRpcToken(securityDir) {
    const dir = join(securityDir, RPC_TOKEN_DIR);
    const file = rpcTokenPath(securityDir);
    await mkdir(dir, { recursive: true });
    let existing = null;
    try {
        existing = (await readFile(file, 'utf8')).trim();
    }
    catch {
        existing = null;
    }
    if (existing !== null) {
        if (existing === '') {
            throw new UmhError('RPC_TOKEN_INVALID', 'rpc token file exists but is empty');
        }
        await assertTokenFilePrivate(file);
        return { token: existing, path: file };
    }
    const token = randomBytes(32).toString('hex');
    try {
        await writeFile(file, `${token}\n`, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
    }
    catch (err) {
        throw new UmhError('RPC_TOKEN_INVALID', `rpc token file could not be created: ${err.message}`);
    }
    await hardenTokenFilePrivate(file);
    await assertTokenFilePrivate(file);
    return { token, path: file };
}
const ISSUED_OPS_CAP = 10_000;
function requireNonEmptyString(value, what) {
    if (typeof value !== 'string' || value.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', `${what} must be a non-empty string`);
    }
    return value;
}
function requirePlainObject(value, what) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        throw new UmhError('INVALID_ARGUMENT', `${what} must be an object`);
    }
    return value;
}
function assertBindingShape(binding) {
    const obj = requirePlainObject(binding, 'binding');
    if (!CLIENT_IDS.has(String(obj['clientId']))) {
        throw new UmhError('INVALID_ARGUMENT', `unknown clientId: ${String(obj['clientId'])}`);
    }
    if (!ROLES.has(String(obj['role']))) {
        throw new UmhError('INVALID_ARGUMENT', 'binding role must be reader|recorder|admin');
    }
    requireNonEmptyString(obj['bindingId'], 'binding.bindingId');
}
/** 解析 memory_search wire 入参（query/scope/limit；未知字段拒绝）。 */
function parseSearchArgs(value) {
    const wire = requirePlainObject(value, 'memory_search input');
    for (const key of Object.keys(wire)) {
        if (key !== 'query' && key !== 'scope' && key !== 'limit') {
            throw new UmhError('INVALID_ARGUMENT', `unknown field: ${key}`);
        }
    }
    const query = requireNonEmptyString(wire['query'], 'field query');
    const limit = validateSearchLimit(wire['limit']);
    return { query, scope: wire['scope'], limit };
}
/** 解析 memory_get wire 入参（memory_id；未知字段拒绝）。 */
function parseGetArgs(value) {
    const wire = requirePlainObject(value, 'memory_get input');
    for (const key of Object.keys(wire)) {
        if (key !== 'memory_id') {
            throw new UmhError('INVALID_ARGUMENT', `unknown field: ${key}`);
        }
    }
    return { memoryId: requireNonEmptyString(wire['memory_id'], 'field memory_id') };
}
export function createDispatcher(opts) {
    /** prepareOperation 签发的 operationId（dispatch 只接受服务签发的 ID）。 */
    const issued = new Map();
    function requireIssuedOperation(operationId) {
        if (!issued.has(operationId)) {
            throw new UmhError('INVALID_ARGUMENT', 'operationId must be obtained from prepareOperation (the service generates operation ids, never the model)');
        }
        return operationId;
    }
    return {
        async dispatch(req) {
            const binding = req?.binding;
            assertBindingShape(binding);
            const tool = requireNonEmptyString(req?.tool, 'tool');
            const args = req?.args ?? {};
            // 每 binding 限 scope/tool：模型工具面白名单（管理工具不在 dispatch 面，
            // 私有管理通路走 /admin/*）；reader 只读（ROLE_FORBIDDEN）。
            if (!MODEL_TOOLS.has(tool)) {
                throw new UmhError('TOOL_FORBIDDEN', `tool ${tool} is not on the model-facing tool surface (management tools go through the private admin path)`);
            }
            if (binding.role === 'reader' && !READER_TOOLS.has(tool)) {
                throw new UmhError('ROLE_FORBIDDEN', `binding role reader cannot use ${tool}`);
            }
            switch (tool) {
                case 'memory_remember': {
                    // parseRemember 严格拒绝未知字段（模型携带 role/approved 类提权字段在此拒绝）。
                    const input = parseRemember(args);
                    const scope = resolveWriteScope(binding, input.scope);
                    const trusted = req.operationId !== undefined
                        ? { operationId: requireIssuedOperation(requireNonEmptyString(req.operationId, 'operationId')) }
                        : undefined;
                    return opts.pool.run(scope, (engine) => submitCandidate(input, binding, engine, trusted));
                }
                case 'memory_recall': {
                    const input = parseRecall(args);
                    const scopes = resolveReadScopes(binding, input.scope);
                    if (opts.readService === undefined) {
                        throw new UmhError('READ_SERVICE_UNAVAILABLE', 'read service is wired by C09; recall is not available yet');
                    }
                    return opts.readService.recall(binding, input, scopes);
                }
                case 'memory_search': {
                    const parsed = parseSearchArgs(args);
                    const scopes = resolveReadScopes(binding, parsed.scope);
                    if (opts.readService === undefined) {
                        throw new UmhError('READ_SERVICE_UNAVAILABLE', 'read service is wired by C09; search is not available yet');
                    }
                    return opts.readService.search(binding, parsed.query, parsed.scope, scopes, parsed.limit);
                }
                case 'memory_get': {
                    const parsed = parseGetArgs(args);
                    const scopes = resolveReadScopes(binding, 'auto');
                    if (opts.readService === undefined) {
                        throw new UmhError('READ_SERVICE_UNAVAILABLE', 'read service is wired by C09; get is not available yet');
                    }
                    return opts.readService.get(binding, parsed.memoryId, scopes);
                }
                case 'memory_status': {
                    return {
                        binding: {
                            clientId: binding.clientId,
                            role: binding.role,
                            bindingId: binding.bindingId,
                            ...(binding.projectId !== undefined ? { projectId: binding.projectId } : {}),
                        },
                        readScopes: resolveReadScopes(binding, 'auto'),
                        readServiceWired: opts.readService !== undefined,
                        workers: opts.pool.registry(),
                    };
                }
                default:
                    // GREEN: 模型工具面白名单（TOOL_FORBIDDEN）。
                    throw new UmhError('INVALID_ARGUMENT', `unknown tool: ${tool}`);
            }
        },
        async prepareOperation(bindingId) {
            requireNonEmptyString(bindingId, 'bindingId');
            const operationId = `op_${randomBytes(16).toString('hex')}`;
            if (issued.size >= ISSUED_OPS_CAP) {
                const oldest = issued.keys().next().value;
                if (oldest !== undefined)
                    issued.delete(oldest);
            }
            issued.set(operationId, bindingId);
            return { operationId };
        },
    };
}
// ---------------------------------------------------------------------------
// loopback RPC server
// ---------------------------------------------------------------------------
class ProtocolError extends Error {
    status;
    code;
    constructor(status, code, message) {
        super(message);
        this.status = status;
        this.code = code;
    }
}
function sendJson(res, status, payload) {
    if (res.headersSent) {
        res.destroy();
        return;
    }
    const body = JSON.stringify(payload);
    res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) });
    res.end(body);
}
function readBody(req, cap) {
    return new Promise((resolve, reject) => {
        const chunks = [];
        let total = 0;
        let settled = false;
        req.on('data', (chunk) => {
            if (settled)
                return; // 超限后停止收集（响应先行，socket 随后关闭）
            total += chunk.length;
            if (cap !== null && total > cap) {
                settled = true;
                reject(new ProtocolError(413, 'BODY_TOO_LARGE', `request body exceeds ${cap} bytes`));
                return;
            }
            chunks.push(chunk);
        });
        req.on('end', () => {
            if (!settled)
                resolve(Buffer.concat(chunks));
        });
        req.on('error', () => {
            if (!settled) {
                settled = true;
                reject(new ProtocolError(400, 'PROTOCOL_ERROR', 'request stream failed'));
            }
        });
    });
}
function requireAuth(req, token) {
    const header = req.headers['authorization'];
    if (typeof header !== 'string' || !header.startsWith('Bearer ')) {
        throw new ProtocolError(401, 'UNAUTHORIZED', 'missing bearer token');
    }
    const provided = header.slice('Bearer '.length);
    // 恒时比较（先哈希对齐长度，避免 timingSafeEqual 长度先行短路）。
    const a = createHash('sha256').update(provided).digest();
    const b = createHash('sha256').update(token).digest();
    if (!timingSafeEqual(a, b)) {
        throw new ProtocolError(401, 'UNAUTHORIZED', 'invalid bearer token');
    }
}
function requireNonce(body, expected) {
    const provided = body['nonce'];
    if (typeof provided !== 'string') {
        throw new ProtocolError(403, 'FORBIDDEN', 'admin route requires the instance nonce');
    }
    const a = createHash('sha256').update(provided).digest();
    const b = createHash('sha256').update(expected).digest();
    if (!timingSafeEqual(a, b)) {
        throw new ProtocolError(403, 'FORBIDDEN', 'instance nonce does not match; refusing administrative action');
    }
}
function parseScope(value) {
    const obj = (() => {
        try {
            return requirePlainObject(value, 'scope');
        }
        catch {
            throw new ProtocolError(400, 'PROTOCOL_ERROR', 'scope must be an object');
        }
    })();
    if (obj['kind'] === 'global')
        return { kind: 'global' };
    if (obj['kind'] === 'project' && typeof obj['projectId'] === 'string' && obj['projectId'] !== '') {
        return { kind: 'project', projectId: obj['projectId'] };
    }
    throw new ProtocolError(400, 'PROTOCOL_ERROR', `scope must be {kind:'global'} or {kind:'project',projectId}`);
}
function parseJsonBody(raw) {
    let parsed;
    try {
        parsed = JSON.parse(raw.toString('utf8'));
    }
    catch {
        throw new ProtocolError(400, 'PROTOCOL_ERROR', 'request body is not valid JSON');
    }
    try {
        return requirePlainObject(parsed, 'request body');
    }
    catch {
        throw new ProtocolError(400, 'PROTOCOL_ERROR', 'request body must be a JSON object');
    }
}
function requireFields(body, allowed) {
    for (const key of Object.keys(body)) {
        if (!allowed.includes(key)) {
            throw new ProtocolError(400, 'PROTOCOL_ERROR', `unknown request field: ${key}`);
        }
    }
}
async function handleRequest(req, res, opts) {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    const route = url.pathname;
    const method = req.method ?? '';
    if (method === 'GET') {
        requireAuth(req, opts.token);
        if (route === '/health') {
            sendJson(res, 200, {
                ok: true,
                pid: process.pid,
                protocolVersion: SERVICE_PROTOCOL_VERSION,
            });
            return;
        }
        if (route === '/admin/workers') {
            sendJson(res, 200, { ok: true, result: await opts.admin.workers() });
            return;
        }
        sendJson(res, 404, { error: { code: 'NOT_FOUND', message: `no route: ${route}` } });
        return;
    }
    if (method !== 'POST') {
        sendJson(res, 405, { error: { code: 'METHOD_NOT_ALLOWED', message: `method ${method} is not allowed` } });
        return;
    }
    requireAuth(req, opts.token);
    // body 上限 256KiB（contracts.RPC_INGRESS_BODY_MAX_BYTES）→ 413 协议错误。
    const raw = await readBody(req, RPC_INGRESS_BODY_MAX_BYTES);
    const body = parseJsonBody(raw);
    switch (route) {
        case '/rpc/dispatch': {
            requireFields(body, ['binding', 'tool', 'args', 'operationId']);
            const result = await opts.dispatcher.dispatch({
                binding: body['binding'],
                tool: body['tool'],
                args: body['args'],
                ...(typeof body['operationId'] === 'string' ? { operationId: body['operationId'] } : {}),
            });
            sendJson(res, 200, { ok: true, result });
            return;
        }
        case '/rpc/prepareOperation': {
            requireFields(body, ['bindingId']);
            if (typeof body['bindingId'] !== 'string' || body['bindingId'] === '') {
                throw new ProtocolError(400, 'PROTOCOL_ERROR', 'bindingId must be a non-empty string');
            }
            const result = await opts.dispatcher.prepareOperation(body['bindingId']);
            sendJson(res, 200, { ok: true, result });
            return;
        }
        case '/admin/review': {
            requireFields(body, ['nonce', 'scope', 'decision']);
            requireNonce(body, opts.nonce);
            const decision = requirePlainObject(body['decision'], 'decision');
            const result = await opts.admin.review(parseScope(body['scope']), decision);
            sendJson(res, 200, { ok: true, result });
            return;
        }
        case '/admin/reconcile': {
            requireFields(body, ['nonce', 'scope', 'operationId']);
            requireNonce(body, opts.nonce);
            const operationId = requireNonEmptyString(body['operationId'], 'operationId');
            const result = await opts.admin.reconcile(parseScope(body['scope']), operationId);
            sendJson(res, 200, { ok: true, result });
            return;
        }
        case '/admin/listPending': {
            requireFields(body, ['nonce', 'scope']);
            requireNonce(body, opts.nonce);
            const result = await opts.admin.listPending(parseScope(body['scope']));
            sendJson(res, 200, { ok: true, result });
            return;
        }
        case '/admin/stop': {
            requireFields(body, ['nonce']);
            requireNonce(body, opts.nonce);
            sendJson(res, 200, { ok: true, result: { stopping: true } });
            opts.requestStop();
            return;
        }
        default:
            sendJson(res, 404, { error: { code: 'NOT_FOUND', message: `no route: ${route}` } });
    }
}
export async function startRpcServer(opts) {
    const host = opts.host ?? DEFAULT_BIND_HOST;
    // 对内 RPC 不是对外网络服务（设计 §5）：绑定严格 127.0.0.1，其余拒绝。
    if (host !== '127.0.0.1') {
        throw new UmhError('INVALID_BIND_ADDRESS', `the private RPC binds 127.0.0.1 only (refused: ${host}); v1 never exposes LAN`);
    }
    const server = createServer((req, res) => {
        void handleRequest(req, res, opts).catch((err) => {
            if (err instanceof ProtocolError) {
                sendJson(res, err.status, { error: { code: err.code, message: err.message } });
                return;
            }
            if (err instanceof UmhError) {
                // 业务错误 ≠ 协议错误：200 {ok:false,error}（rejected 语义走这里）。
                sendJson(res, 200, { ok: false, error: { code: err.code, message: err.message } });
                return;
            }
            sendJson(res, 500, { error: { code: 'INTERNAL', message: err.message } });
        });
    });
    await new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen({ host, port: 0 }, () => resolve());
    });
    const addr = server.address();
    const port = typeof addr === 'object' && addr !== null ? addr.port : 0;
    if (!port) {
        await closeServer(server);
        throw new UmhError('RPC_BIND_FAILED', 'loopback listener did not report a port');
    }
    return {
        port,
        url: `http://127.0.0.1:${port}`,
        close: () => closeServer(server),
    };
}
async function closeServer(server) {
    server.closeAllConnections?.();
    await new Promise((resolve) => server.close(() => resolve()));
}
// ---------------------------------------------------------------------------
// RPC 客户端（harness / CLI / ServiceHandle.admin 消费）
// ---------------------------------------------------------------------------
export async function rpcPost(url, path, token, body, timeoutMs = 20_000) {
    return rpcRequest(url, path, 'POST', token, body, timeoutMs);
}
export async function rpcGet(url, path, token, timeoutMs = 20_000) {
    return rpcRequest(url, path, 'GET', token, undefined, timeoutMs);
}
async function rpcRequest(url, path, method, token, body, timeoutMs) {
    const { request } = await import('node:http');
    const target = new URL(path, url);
    const payload = body === undefined ? undefined : JSON.stringify(body);
    return new Promise((resolve, reject) => {
        const req = request({
            hostname: target.hostname,
            port: target.port,
            path: `${target.pathname}${target.search}`,
            method,
            headers: {
                authorization: `Bearer ${token}`,
                ...(payload !== undefined ? { 'content-type': 'application/json' } : {}),
            },
            timeout: timeoutMs,
        }, (res) => {
            const status = res.statusCode ?? 0;
            const chunks = [];
            res.on('data', (chunk) => chunks.push(chunk));
            res.on('end', () => {
                const text = Buffer.concat(chunks).toString('utf8');
                try {
                    resolve({ status, json: JSON.parse(text) });
                }
                catch {
                    reject(new UmhError('RPC_BAD_RESPONSE', `${method} ${path} returned non-JSON body`));
                }
            });
            res.on('error', reject);
        });
        req.on('timeout', () => req.destroy(new UmhError('RPC_TIMEOUT', `${method} ${path} timed out`)));
        req.on('error', reject);
        if (payload !== undefined)
            req.write(payload);
        req.end();
    });
}
function unwrapRpc(json) {
    if (json['ok'] === true)
        return json['result'];
    const err = json['error'];
    throw new UmhError(String(err?.code ?? 'RPC_ERROR'), String(err?.message ?? 'rpc call failed'));
}
/**
 * 私有管理通路客户端（ServiceHandle.admin / CLI stop/review 消费）。
 * 认证分级：review/reconcile/listPending/stop 走 POST /admin/*（token + 实例
 * nonce 双凭据）；workers() 走 GET /admin/workers（token-only 只读，不带 nonce）。
 */
export class ServiceAdminClient {
    url;
    token;
    nonce;
    constructor(url, token, nonce) {
        this.url = url;
        this.token = token;
        this.nonce = nonce;
    }
    async review(scope, decision) {
        return unwrapRpc((await rpcPost(this.url, '/admin/review', this.token, { nonce: this.nonce, scope, decision })).json);
    }
    async reconcile(scope, operationId) {
        return unwrapRpc((await rpcPost(this.url, '/admin/reconcile', this.token, { nonce: this.nonce, scope, operationId })).json);
    }
    async listPending(scope) {
        return unwrapRpc((await rpcPost(this.url, '/admin/listPending', this.token, { nonce: this.nonce, scope })).json);
    }
    async workers() {
        return unwrapRpc((await rpcGet(this.url, '/admin/workers', this.token)).json);
    }
    async stop() {
        await rpcPost(this.url, '/admin/stop', this.token, { nonce: this.nonce });
    }
}
