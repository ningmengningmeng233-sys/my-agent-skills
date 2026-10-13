/**
 * packages/engine/transport — md_cg worker 进程通信（C04）。
 *
 * Boot 配方逐字来自 C01 冻结合同（docs/upstream-audit/engine-contract.json
 * environment 节，源自 .lemon/standalone 独立运行探针）：
 *   argv  = [<python>, -X, utf8, -m, md_cg.mcp_server]（数组 spawn，shell=false）
 *   cwd   = stage（vendored md_cg 运行单元：md_cg/、utf8_boot.py、
 *           data/policy.json、LICENSE —— vendor/mdcg，由 scripts/vendor-mdcg.py
 *           按 C01 清单逐字节复制并验证）
 *   env   = MDCG_ROOT / MDCG_STATE_ROOT / MDCG_DATA_ROOT / MDCG_AUX_ROOT /
 *           MDCG_POLICY_FILE / MDCG_SUSTAIN=0 / MDCG_MCP_SURFACE=full /
 *           MDCG_TENANT / MDCG_TOKEN_FILE / PYTHONUTF8 / PYTHONIOENCODING，
 *           HOME/USERPROFILE 指向实例私有合成目录。
 *
 * 身份（生产形态，C01 identity-* 探针）：经 md_cg.tokens.issue 按会话签发限权
 * 令牌（role 映射 admin→designer / recorder→record / reader→output），明文只走
 * MDCG_TOKEN 进程环境，绝不落合同/日志/持久层；MDCG_LEGACY_ENV_AUTH 恒不启用。
 * 传输层使用官方 MCP SDK v1（@modelcontextprotocol/sdk 1.31.0）Client +
 * StdioClientTransport（C01 DEPENDENCIES-PIN 固定代际，深路径入口）；
 * Python 上游 PROTOCOL_VERSION=2024-11-05 在 SDK SUPPORTED_PROTOCOL_VERSIONS
 * 内，initialize 协商兼容（tests/engine 实测）。
 *
 * supervisor 纪律（设计 §5）：一个作用域实例独占一个 worker，stdin 请求由上层
 * 引擎串行化（FIFO）；本模块只提供单连接调用与关闭。
 */
import { spawnSync } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { UmhError } from '../contracts/index.js';
/** UMH 引擎角色 → 上游令牌角色（C01 roles.umhMapping，探针证据固定）。 */
export const UPSTREAM_ROLE = Object.freeze({
    admin: 'designer',
    recorder: 'record',
    reader: 'output',
});
/** 允许从宿主 env 继承的键（与 tests/support/harness.mjs INHERIT_ENV_KEYS 同口径）。 */
const INHERIT_ENV_KEYS = Object.freeze([
    'PATH', 'PATHEXT', 'SYSTEMDRIVE', 'SYSTEMROOT', 'WINDIR', 'COMSPEC',
    'PROGRAMFILES', 'PROGRAMFILES(X86)', 'COMMONPROGRAMFILES',
    'COMMONPROGRAMFILES(X86)', 'NUMBER_OF_PROCESSORS', 'OS',
    'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER', 'PROCESSOR_LEVEL',
    'PROCESSOR_REVISION',
]);
const WORKER_ARGV = ['-X', 'utf8', '-m', 'md_cg.mcp_server'];
/**
 * 构造 worker（或令牌签发子进程）env：白名单继承宿主基本项，MDCG_ 前缀变量与
 * 身份类变量一律不继承（token 按实例签发，防止上层环境凭据泄漏进引擎实例）。
 * token 为 null 时用于令牌签发子进程（签发路径绝不携带旧凭据）。
 */
function buildWorkerEnv(cfg, token) {
    const env = {};
    const allow = new Set(INHERIT_ENV_KEYS.map((k) => k.toUpperCase()));
    for (const [key, value] of Object.entries(process.env)) {
        if (value !== undefined && allow.has(key.toUpperCase()))
            env[key] = value;
    }
    const home = join(cfg.auxRoot, 'home');
    Object.assign(env, {
        HOME: home,
        USERPROFILE: home,
        TEMP: join(cfg.auxRoot, 'tmp'),
        TMP: join(cfg.auxRoot, 'tmp'),
        PYTHONPATH: cfg.stage,
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        // vendored 单元保持纯净：不向 vendor/mdcg 写 __pycache__。
        PYTHONDONTWRITEBYTECODE: '1',
        MDCG_ROOT: cfg.root,
        MDCG_STATE_ROOT: cfg.stateRoot,
        MDCG_DATA_ROOT: cfg.dataRoot,
        MDCG_AUX_ROOT: cfg.auxRoot,
        MDCG_POLICY_FILE: cfg.policyFile,
        MDCG_SUSTAIN: '0',
        MDCG_MCP_SURFACE: 'full',
        MDCG_TENANT: cfg.tenant,
        MDCG_TOKEN_FILE: join(cfg.auxRoot, '_tokens.json'),
    });
    if (token !== null)
        env['MDCG_TOKEN'] = token;
    return env;
}
/** 令牌签发子进程代码：明文 token 只出现在 stdout（父进程立即消费，不落盘）。 */
const ISSUE_CODE = [
    'import json, os',
    'from md_cg import tokens',
    'r = tokens.issue(',
    '    role=os.environ["UMH_ISSUE_ROLE"],',
    '    actor=os.environ["UMH_ISSUE_ACTOR"],',
    '    tenant=os.environ["MDCG_TENANT"],',
    '    issued_by="umh-engine")',
    'print(json.dumps({"token": r["token"], "token_id": r.get("token_id"),',
    '                  "role": r.get("role"), "actor": r.get("actor")}))',
].join('\n');
/**
 * 经上游 md_cg.tokens.issue 签发一枚限权令牌（C01 identity-* 探针配方）。
 * 明文只在进程内存传递；令牌记录落 MDCG_TOKEN_FILE（aux/_tokens.json，0600）。
 */
export function issueToken(cfg) {
    const env = buildWorkerEnv(cfg, null);
    env['UMH_ISSUE_ROLE'] = UPSTREAM_ROLE[cfg.role];
    env['UMH_ISSUE_ACTOR'] = cfg.actor;
    const out = spawnSync(cfg.python, ['-X', 'utf8', '-c', ISSUE_CODE], {
        cwd: cfg.stage,
        env,
        encoding: 'utf8',
        shell: false,
        windowsHide: true,
        timeout: 60_000,
    });
    if (out.error || out.status !== 0) {
        const detail = (out.stderr || out.error?.message || '').trim().split('\n').slice(-3).join('\n');
        throw new UmhError('TOKEN_ISSUANCE_FAILED', `md_cg.tokens.issue failed: ${detail}`);
    }
    const lines = (out.stdout || '').trim().split('\n');
    const last = lines[lines.length - 1];
    let parsed;
    try {
        parsed = JSON.parse(last);
    }
    catch {
        throw new UmhError('TOKEN_ISSUANCE_FAILED', 'token issuance produced no JSON line');
    }
    if (typeof parsed.token !== 'string' || parsed.token === '') {
        throw new UmhError('TOKEN_ISSUANCE_FAILED', 'token issuance returned no plaintext token');
    }
    return { token: parsed.token, tokenId: String(parsed.token_id ?? '') };
}
/** 采集 worker stderr 尾部（诊断用，stdout 绝不混诊断 —— C01 合同纪律）。 */
class StderrTail {
    buf = '';
    onData(chunk) {
        this.buf += chunk.toString('utf8');
        if (this.buf.length > 64 * 1024)
            this.buf = this.buf.slice(-64 * 1024);
    }
    tail() {
        return this.buf.trim().split('\n').slice(-8).join('\n');
    }
}
/** 一个已连接的 md_cg MCP worker 会话。 */
export class WorkerConnection {
    client;
    transport;
    stderr = new StderrTail();
    closed = false;
    constructor(client, transport) {
        this.client = client;
        this.transport = transport;
        const err = transport.stderr;
        if (err)
            err.on('data', (chunk) => this.stderr.onData(chunk));
    }
    /** 按 C01 boot 配方 spawn worker 并完成 MCP initialize 协商。 */
    static async spawn(cfg, token) {
        const transport = new StdioClientTransport({
            command: cfg.python,
            args: [...WORKER_ARGV],
            cwd: cfg.stage,
            env: buildWorkerEnv(cfg, token),
            stderr: 'pipe',
        });
        const client = new Client({ name: 'umh-engine', version: '0.1.0' });
        try {
            await client.connect(transport);
        }
        catch (err) {
            try {
                await transport.close();
            }
            catch { /* 尽力关闭 */ }
            throw new UmhError('ENGINE_BOOT_FAILED', `md_cg worker failed MCP initialize: ${err.message}`);
        }
        return new WorkerConnection(client, transport);
    }
    /**
     * 调用 native 工具并解析 results.content[0].text 的 JSON。
     * 上游工具错误以 isError:true + {error: "..."} 返回 —— 在此映射为 UmhError。
     */
    async call(name, args) {
        if (this.closed)
            throw new UmhError('ENGINE_CLOSED', 'engine worker already closed');
        let result;
        try {
            result = (await this.client.callTool({ name, arguments: args }));
        }
        catch (err) {
            throw new UmhError('ENGINE_TRANSPORT_ERROR', `native tool ${name} transport error: ${err.message}`);
        }
        const content = result['content'];
        const first = Array.isArray(content) ? content[0] : undefined;
        const text = first && first.type === 'text'
            ? String(first.text ?? '')
            : '';
        let value;
        try {
            value = JSON.parse(text);
        }
        catch {
            throw new UmhError('ENGINE_BAD_RESPONSE', `native tool ${name} returned non-JSON text`);
        }
        if (value === null || typeof value !== 'object' || Array.isArray(value)) {
            throw new UmhError('ENGINE_BAD_RESPONSE', `native tool ${name} returned a non-object payload`);
        }
        const obj = value;
        // 上游约定：工具异常 → isError:true + {error}；结构化响应（如 mdcg_get 的
        // {ok:false,error:'not_found'}）是数据，不是故障 —— 用 isError 区分。
        const errorText = typeof obj['error'] === 'string' ? obj['error'] : '';
        if (result['isError'] === true && errorText !== '')
            throw mapNativeError(name, errorText);
        return obj;
    }
    /** 关闭会话并终止 worker 进程（t.after / close 的清理责任）。 */
    async close() {
        if (this.closed)
            return;
        this.closed = true;
        try {
            await this.client.close();
        }
        catch (err) {
            throw new UmhError('ENGINE_CLOSE_FAILED', `worker close failed: ${err.message}`);
        }
    }
    diagnostics() {
        return this.stderr.tail();
    }
}
/** 上游工具错误文本 → UmhError（错误码单点映射）。 */
function mapNativeError(tool, errorText) {
    if (errorText.startsWith('AccessDenied')) {
        return new UmhError('ACCESS_DENIED', `${tool}: ${errorText}`);
    }
    if (errorText.startsWith('TimeoutError')) {
        return new UmhError('QUEUE_LOCK_TIMEOUT', `${tool}: ${errorText}`);
    }
    return new UmhError('ENGINE_NATIVE_ERROR', `${tool}: ${errorText}`);
}
/** 建立引擎实例所需的目录（根、aux、state、data、合成 HOME/tmp）。 */
export async function ensureDirectories(cfg) {
    const dirs = [
        cfg.root,
        cfg.auxRoot,
        cfg.stateRoot,
        cfg.dataRoot,
        join(cfg.auxRoot, 'home'),
        join(cfg.auxRoot, 'tmp'),
    ];
    for (const dir of dirs)
        await mkdir(dir, { recursive: true });
}
