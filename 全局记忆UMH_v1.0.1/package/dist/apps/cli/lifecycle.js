/**
 * apps/cli/lifecycle — umh init/start/stop/status 命令层（C07）。
 *
 * 设计 §10：管理入口职责；总入口约束：stdout 只输出机器可读 JSON，诊断走
 * stderr。start 在本进程内前台运行服务（Windows v1 无守护进程化）；重复
 * start 由 owner 锁拒绝（ALREADY_RUNNING + ownerPid）。stop 绝不按 PID kill：
 * 读 ready record → POST /admin/stop（nonce 核验）；记录指向死 PID 时只清理
 * 属于该记录的 runtime 文件（nonce 匹配才删）。
 *
 * 边界：CLI 绝不在本进程起引擎 worker（设计 §5：每个作用域由中央服务持有一个
 * worker）；review/reconcile 走运行中服务的私有管理通路（cli/main 路由）。
 */
import { readFile, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import process from 'node:process';
import { UmhError } from '../../packages/contracts/index.js';
import { isPidAlive, readReadyRecord, runServiceRuntime, runtimeDirOf, } from '../../packages/service/owner.js';
import { DEFAULT_BIND_HOST, rpcPost, rpcTokenPath } from '../../packages/service/rpc.js';
export class UsageError extends Error {
}
function argValue(argv, name) {
    const idx = argv.indexOf(name);
    if (idx < 0 || idx + 1 >= argv.length)
        return undefined;
    return argv[idx + 1];
}
function requireUmhHome(argv) {
    const value = argValue(argv, '--umh-home');
    if (value === undefined || value.trim() === '') {
        throw new UsageError('--umh-home <path> is required');
    }
    return value;
}
function out(payload) {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
}
function diag(message) {
    process.stderr.write(`${message}\n`);
}
async function readJson(file) {
    try {
        const parsed = JSON.parse(await readFile(file, 'utf8'));
        return parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
            ? parsed
            : null;
    }
    catch {
        return null;
    }
}
async function readRpcToken(umhHome) {
    return (await readFile(rpcTokenPath(join(umhHome, 'security')), 'utf8')).trim();
}
// ---------------------------------------------------------------------------
// init
// ---------------------------------------------------------------------------
const INIT_DIRS = Object.freeze([
    'config',
    'memory',
    join('memory', 'global'),
    join('memory', 'projects'),
    'security',
    'runtime',
    'logs',
    'backups',
]);
/** 初始化 UMH 根骨架（幂等；不启动任何进程）。 */
export async function runInitCommand(argv) {
    const { mkdir } = await import('node:fs/promises');
    const umhHome = argValue(argv, '--umh-home') ?? process.env.UMH_HOME ?? join(homedir(), '.universal-memory-hub');
    for (const dir of INIT_DIRS) {
        await mkdir(join(umhHome, dir), { recursive: true });
    }
    out({ status: 'initialized', umhHome, dirs: INIT_DIRS });
    return 0;
}
// ---------------------------------------------------------------------------
// start（前台运行；重复 start 被 owner 锁拒绝）
// ---------------------------------------------------------------------------
export async function runStartCommand(argv) {
    const umhHome = requireUmhHome(argv);
    const python = argValue(argv, '--python') ?? 'python';
    const host = argValue(argv, '--host') ?? DEFAULT_BIND_HOST;
    try {
        const runtime = await runServiceRuntime({ umhHome, python, host });
        out({
            status: 'ready',
            pid: runtime.pid,
            port: runtime.port,
            url: runtime.url,
            instanceId: runtime.instanceId,
            protocolVersion: runtime.protocolVersion,
        });
        await runtime.stopped;
        return 0;
    }
    catch (err) {
        if (err instanceof UmhError) {
            const ownerPid = err.ownerPid;
            out(err.code === 'ALREADY_RUNNING'
                ? { status: 'refused', code: err.code, ownerPid: ownerPid ?? null, message: err.message }
                : { status: 'error', code: err.code, message: err.message });
            diag(`umh start failed: [${err.code}] ${err.message}`);
            return 1;
        }
        throw err;
    }
}
// ---------------------------------------------------------------------------
// stop（nonce 核验的优雅停机；绝不按 PID kill）
// ---------------------------------------------------------------------------
export async function runStopCommand(argv) {
    const umhHome = requireUmhHome(argv);
    const ready = await readReadyRecord(umhHome);
    if (ready === null) {
        out({ status: 'not_running' });
        return 0;
    }
    if (!isPidAlive(ready.pid)) {
        // 记录指向死 PID：陈旧记录安全清理（只删 nonce 匹配的自己的文件；
        // 不向任何进程发信号）。
        await cleanStaleRuntime(umhHome, ready);
        out({ status: 'not_running', cleanedStale: true, pid: ready.pid });
        return 0;
    }
    let token;
    try {
        token = await readRpcToken(umhHome);
    }
    catch (err) {
        out({ status: 'error', code: 'RPC_TOKEN_UNREADABLE', message: err.message });
        diag('rpc token unreadable; cannot authenticate the stop request');
        return 1;
    }
    try {
        await rpcPost(`http://127.0.0.1:${ready.port}`, '/admin/stop', token, { nonce: ready.nonce });
        out({ status: 'stopped', pid: ready.pid });
        return 0;
    }
    catch (err) {
        out({
            status: 'error',
            code: 'OWNER_UNREACHABLE',
            pid: ready.pid,
            message: `recorded owner ${ready.pid} is alive but the stop request failed: ${err.message}`,
        });
        diag('refusing to touch runtime records or signals while a live owner cannot be verified (fail closed)');
        return 1;
    }
}
async function cleanStaleRuntime(umhHome, ready) {
    const dir = runtimeDirOf(umhHome);
    const lock = await readJson(join(dir, 'owner.lock'));
    if (lock === null || lock['nonce'] === ready.nonce) {
        await rm(join(dir, 'owner.lock'), { force: true });
    }
    const status = await readJson(join(dir, 'status.json'));
    if (status === null || status['nonce'] === ready.nonce) {
        await rm(join(dir, 'status.json'), { force: true });
    }
    await rm(join(dir, 'owner.json'), { force: true });
}
// ---------------------------------------------------------------------------
// status（纯记录读取；不起进程、不连 RPC）
// ---------------------------------------------------------------------------
export async function runStatusCommand(argv) {
    const umhHome = requireUmhHome(argv);
    const dir = runtimeDirOf(umhHome);
    const ready = await readReadyRecord(umhHome);
    const lock = await readJson(join(dir, 'owner.lock'));
    const statusFile = await readJson(join(dir, 'status.json'));
    if (ready !== null) {
        const running = isPidAlive(ready.pid);
        const workers = statusFile !== null && statusFile['nonce'] === ready.nonce && Array.isArray(statusFile['workers'])
            ? statusFile['workers']
            : [];
        out({
            status: running ? 'running' : 'not_running',
            stale: !running,
            pid: ready.pid,
            url: `http://127.0.0.1:${ready.port}`,
            protocolVersion: ready.protocolVersion,
            instanceId: ready.instanceId,
            workers,
        });
        return 0;
    }
    if (lock !== null && typeof lock['pid'] === 'number' && isPidAlive(lock['pid'])) {
        out({
            status: 'booting',
            pid: lock['pid'],
            instanceId: typeof lock['instanceId'] === 'string' ? lock['instanceId'] : null,
            workers: [],
        });
        return 0;
    }
    out({
        status: 'not_running',
        stale: lock !== null || ready !== null,
        workers: [],
    });
    return 0;
}
// 供 cli/main 的 review/reconcile 路由复用（私有管理通路凭据）。
export async function loadAdminCredentials(umhHome) {
    const record = await readReadyRecord(umhHome);
    if (record === null) {
        throw new UmhError('SERVICE_NOT_RUNNING', 'the central service is not running; start it before administrative commands');
    }
    return { record, token: await readRpcToken(umhHome) };
}
