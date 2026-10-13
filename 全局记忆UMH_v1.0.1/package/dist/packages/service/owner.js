/**
 * packages/service/owner — 服务所有者与运行记录（C07）。
 *
 * 设计 §5/§10 + 工单 C07 + 总控裁决：
 *   - 单一所有者：owner record = {instanceId(随机), PID, process start, nonce,
 *     protocol version, loopback port}；锁获取失败即拒绝启动，绝不无锁继续。
 *   - stale 判定用完整记录（nonce + process start time），绝不只看 PID；存活
 *     且 start 吻合 → ALREADY_RUNNING（带 ownerPid）；PID 已死或 start 不符 →
 *     陈旧锁安全接管（重命名记录，绝不向任何进程发信号——foreign PID 不可 kill）。
 *   - 记录无法解析/无法核验 → fail closed 拒绝启动（OWNER_LOCK_CORRUPT /
 *     OWNER_LOCK_UNVERIFIED），锁文件保持原样。
 *   - boot readiness（裁决 1）：先初始化 global worker 并 health 成功，再写
 *     ready owner record —— owner 测试的初始 worker 计数为 1；项目 worker 按
 *     绑定请求懒启动，第二个 Gateway 绝不再起 global 实例。
 *   - 锁/原子文件复用 C01 批准的 MemoraX 模式（O_EXCL + 原子 rename 写），
 *     不携带 cloud provider。
 *   - startService({umhHome,python}) 拉起 apps/service/main.js 子进程并等待
 *     ready 行；ServiceHandle = {pid,url,stop(),admin}；stopService(handle)
 *     经 nonce 核验的 /admin/stop 优雅停机——只终止自己 spawn 且 nonce 核验的
 *     进程。失败时（exit/stderr）在抛错前捕获子进程 stderr 尾部与退出码，
 *     证据随测试失败信息保留（makeSandbox 清理发生在其后，无法销毁）。
 *
 * 运行时布局（设计 §6）：
 *   <umhHome>/runtime/owner.lock    O_EXCL 锁 + 全量 owner record（权威）
 *   <umhHome>/runtime/owner.json    ready record（stop/status 读取）
 *   <umhHome>/runtime/status.json   worker registry 快照（CLI status 消费）
 *   <umhHome>/security/rpc/token    RPC token（rpc.ts 管理）
 */
import { spawn, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import process from 'node:process';
import { UmhError } from '../contracts/index.js';
import { createReadService } from '../core/read.js';
import { openReceiptStore } from '../core/receipt.js';
import { reconcile, reviewCandidate, useReceiptStoreFactory } from '../core/write.js';
import { EnginePool, createRealEngineFactory } from './pool.js';
import { DEFAULT_BIND_HOST, SERVICE_PROTOCOL_VERSION, ServiceAdminClient, createDispatcher, loadOrCreateRpcToken, rpcTokenPath, startRpcServer, } from './rpc.js';
const RUNTIME_DIR = 'runtime';
const LOCK_FILE = 'owner.lock';
const READY_FILE = 'owner.json';
const STATUS_FILE = 'status.json';
export const SERVICE_PROTOCOL_VERSION_EXPORT = SERVICE_PROTOCOL_VERSION;
export function runtimeDirOf(umhHome) {
    return join(umhHome, RUNTIME_DIR);
}
// ---------------------------------------------------------------------------
// 进程身份（PID 存活 + process start time 全量核验）
// ---------------------------------------------------------------------------
/** 本进程启动时刻（epoch ms）。 */
export function selfProcessStartMs() {
    return Date.now() - process.uptime() * 1000;
}
/** PID 存活探测（signal 0；EPERM 视为存活——fail closed 方向）。 */
export function isPidAlive(pid) {
    try {
        process.kill(pid, 0);
        return true;
    }
    catch (err) {
        const code = err.code;
        if (code === 'EPERM')
            return true;
        return false;
    }
}
/** PowerShell 输出 → epoch ms（FILETIME 100ns since 1601）。 */
const FILETIME_EPOCH_DELTA_MS = 11_644_473_600_000;
/**
 * 查询任意 PID 的 process start time（epoch ms）。
 * Windows：PowerShell Get-Process（与自报公式同源系统时钟，比较容差 2s）。
 * POSIX：/proc/<pid>/stat starttime（自举 ticks）→ 与 btime 换算 epoch ms。
 * 查询不可用/进程消失 → null（调用方 fail closed 或判 stale）。
 */
export function queryProcessStartMs(pid) {
    if (process.platform === 'win32') {
        const script = `$ErrorActionPreference='Stop'; try { $p=Get-Process -Id ${pid} -ErrorAction Stop; ` +
            `[Console]::Out.Write($p.StartTime.ToFileTimeUtc().ToString()) } catch { [Console]::Out.Write('GONE') }`;
        let out;
        try {
            out = spawnSyncPowerShell(script);
        }
        catch {
            return null;
        }
        if (!out.ok)
            return null;
        const text = out.stdout.trim();
        if (text === 'GONE' || !/^\d+$/.test(text))
            return null;
        return Number(text) / 10000 - FILETIME_EPOCH_DELTA_MS;
    }
    try {
        const statText = readProcStat(pid);
        if (statText === null)
            return null;
        const fields = statText.split(/\s+/);
        const starttimeTicks = Number(fields[21]);
        if (!Number.isFinite(starttimeTicks))
            return null;
        const btime = readProcBootTime();
        if (btime === null)
            return null;
        return btime * 1000 + (starttimeTicks * 1000) / 100; // CLK_TCK=100 惯例
    }
    catch {
        return null;
    }
}
function spawnSyncPowerShell(script) {
    const out = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script], { encoding: 'utf8', timeout: 15_000, windowsHide: true, shell: false });
    if (out.error)
        return { ok: false, stdout: out.stdout ?? '', stderr: String(out.error.message) };
    return { ok: out.status === 0, stdout: out.stdout ?? '', stderr: out.stderr ?? '' };
}
function readProcStat(pid) {
    try {
        return readFileSync(`/proc/${pid}/stat`, 'utf8');
    }
    catch {
        return null;
    }
}
function readProcBootTime() {
    try {
        const text = readFileSync('/proc/stat', 'utf8');
        const line = text.split('\n').find((l) => l.startsWith('btime '));
        if (line === undefined)
            return null;
        const value = Number(line.slice('btime '.length).trim());
        return Number.isFinite(value) ? value : null;
    }
    catch {
        return null;
    }
}
/** start time 比较容差（自报公式 vs Get-Process，系统时钟同一来源）。 */
const PROCESS_START_TOLERANCE_MS = 2000;
// ---------------------------------------------------------------------------
// 原子写（MemoraX 模式：同目录临时文件 + rename）
// ---------------------------------------------------------------------------
async function atomicWriteJson(file, value) {
    const tmp = `${file}.${randomBytes(4).toString('hex')}.tmp`;
    await writeFile(tmp, JSON.stringify(value, null, 2), { encoding: 'utf8', mode: 0o600 });
    await rename(tmp, file);
}
async function readJsonFile(file) {
    let raw;
    try {
        raw = await readFile(file, 'utf8');
    }
    catch {
        return null;
    }
    try {
        const parsed = JSON.parse(raw);
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed))
            return null;
        return parsed;
    }
    catch {
        return null;
    }
}
function randomId(bytes = 16) {
    return randomBytes(bytes).toString('hex');
}
function newRecord(state, port) {
    return {
        version: 1,
        protocolVersion: SERVICE_PROTOCOL_VERSION,
        instanceId: randomId(),
        pid: process.pid,
        processStartMs: selfProcessStartMs(),
        nonce: randomId(32),
        port,
        state,
        startedAt: new Date().toISOString(),
    };
}
function isOwnerRecord(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value))
        return false;
    const r = value;
    return (r['version'] === 1 &&
        typeof r['instanceId'] === 'string' && r['instanceId'] !== '' &&
        typeof r['pid'] === 'number' &&
        typeof r['processStartMs'] === 'number' &&
        typeof r['nonce'] === 'string' && r['nonce'] !== '' &&
        typeof r['port'] === 'number' &&
        (r['state'] === 'booting' || r['state'] === 'ready'));
}
/**
 * 完整记录核验（总控裁决）：nonce + process start time 全量判定，绝不只看 PID，
 * 绝不向任何进程发信号。
 *   记录不可解析            → corrupt（fail closed）
 *   PID 已死                → stale
 *   PID 存活但 start 不符   → stale（PID 复用/伪造记录）
 *   PID 存活且 start 吻合   → live（ALREADY_RUNNING）
 *   start 无法查询          → unverified（fail closed）
 */
async function verifyExistingLock(lockPath) {
    let raw;
    try {
        raw = await readFile(lockPath, 'utf8');
    }
    catch {
        return { verdict: 'stale', record: null }; // 锁在读取前消失：按 stale 重试
    }
    let parsed;
    try {
        parsed = JSON.parse(raw);
    }
    catch {
        return { verdict: 'corrupt' };
    }
    if (!isOwnerRecord(parsed))
        return { verdict: 'corrupt' };
    const record = parsed;
    if (!isPidAlive(record.pid))
        return { verdict: 'stale', record };
    const foreignStart = queryProcessStartMs(record.pid);
    if (foreignStart === null)
        return { verdict: 'unverified', record };
    if (Math.abs(foreignStart - record.processStartMs) <= PROCESS_START_TOLERANCE_MS) {
        return { verdict: 'live', record };
    }
    return { verdict: 'stale', record };
}
/** 接管 stale 记录时，仅清除属于该记录（nonce 匹配）的 ready/status 快照。 */
async function cleanStaleSnapshots(umhHome, stale) {
    if (stale === null)
        return;
    const dir = runtimeDirOf(umhHome);
    const ready = await readJsonFile(join(dir, READY_FILE));
    if (ready !== null && ready['nonce'] === stale.nonce)
        await rm(join(dir, READY_FILE), { force: true });
    const status = await readJsonFile(join(dir, STATUS_FILE));
    if (status !== null && status['nonce'] === stale.nonce)
        await rm(join(dir, STATUS_FILE), { force: true });
}
/**
 * 获取 owner 锁。O_EXCL 失败 → 读取既有记录并做完整 stale 判定：
 *   记录不可解析            → OWNER_LOCK_CORRUPT（fail closed，锁保持原样）
 *   PID 已死                → 陈旧 → 清除后重试
 *   PID 存活但 start 不符   → 陈旧（PID 复用/伪造）→ 清除后重试
 *   PID 存活且 start 吻合   → ALREADY_RUNNING(ownerPid)（绝不发信号）
 *   start 无法查询          → OWNER_LOCK_UNVERIFIED（fail closed）
 */
export async function acquireOwnerLock(umhHome) {
    const dir = runtimeDirOf(umhHome);
    await mkdir(dir, { recursive: true });
    const lockPath = join(dir, LOCK_FILE);
    for (let attempt = 0; attempt < 5; attempt++) {
        const record = newRecord('booting', 0);
        try {
            await writeFile(lockPath, JSON.stringify(record, null, 2), { encoding: 'utf8', flag: 'wx', mode: 0o600 });
            return makeLock(umhHome, record);
        }
        catch (err) {
            const code = err.code;
            if (code !== 'EEXIST') {
                throw new UmhError('OWNER_LOCK_FAILED', `owner lock could not be created: ${err.message}`);
            }
            const existing = await verifyExistingLock(lockPath);
            if (existing.verdict === 'live') {
                throw alreadyRunningError(existing.record);
            }
            if (existing.verdict === 'corrupt') {
                // fail closed：既不接管也不无锁继续，锁保持原样待人工处置。
                throw new UmhError('OWNER_LOCK_CORRUPT', 'owner lock cannot be parsed as an owner record; refusing to start lockless (remove the stale file manually after verifying no service runs)');
            }
            if (existing.verdict === 'unverified') {
                throw new UmhError('OWNER_LOCK_UNVERIFIED', `owner record names live pid ${existing.record.pid} whose process start time could not be verified; refusing startup (fail closed)`);
            }
            // stale：安全接管（重命名记录，绝不向被点名的 PID 发送任何信号）。
            await cleanStaleSnapshots(umhHome, existing.record);
            try {
                await rm(lockPath, { force: true });
            }
            catch {
                throw new UmhError('OWNER_LOCK_FAILED', 'stale owner lock could not be removed');
            }
        }
    }
    throw new UmhError('OWNER_LOCK_CONTENTION', 'owner lock contention did not resolve within retry budget');
}
function alreadyRunningError(record) {
    const err = new UmhError('ALREADY_RUNNING', `another service owner already holds this UMH home (pid ${record.pid}, instance ${record.instanceId})`);
    err.ownerPid = record.pid;
    return err;
}
function makeLock(umhHome, record) {
    const dir = runtimeDirOf(umhHome);
    const lockPath = join(dir, LOCK_FILE);
    const readyPath = join(dir, READY_FILE);
    const statusPath = join(dir, STATUS_FILE);
    return {
        record,
        async promoteReady(port) {
            const ready = { ...record, port, state: 'ready' };
            // 先写 ready record（owner.json），再原子重写锁记录；second starter 在两步
            // 之间读到 booting 记录 → 仍按 ALREADY_RUNNING 拒绝（PID/start 已在）。
            await atomicWriteJson(readyPath, ready);
            await atomicWriteJson(lockPath, ready);
        },
        async release() {
            // nonce 核验：只有锁上仍是自己 nonce 时才删除（绝不碰接管者的记录）。
            const current = await readJsonFile(lockPath);
            if (current !== null) {
                if (current['nonce'] === record.nonce)
                    await rm(lockPath, { force: true });
            }
            else {
                await rm(lockPath, { force: true });
            }
            const readyNow = await readJsonFile(readyPath);
            if (readyNow !== null && readyNow['nonce'] === record.nonce)
                await rm(readyPath, { force: true });
            const status = await readJsonFile(statusPath);
            if (status !== null && status['nonce'] === record.nonce)
                await rm(statusPath, { force: true });
        },
    };
}
/** 读取 ready record（owner.json；不存在或损坏 → null）。 */
export async function readReadyRecord(umhHome) {
    const value = await readJsonFile(join(runtimeDirOf(umhHome), READY_FILE));
    return value !== null && isOwnerRecord(value) && value.state === 'ready' ? value : null;
}
async function writeStatusSnapshot(umhHome, nonce, workers) {
    await atomicWriteJson(join(runtimeDirOf(umhHome), STATUS_FILE), {
        version: 1,
        nonce,
        updatedAt: new Date().toISOString(),
        workers,
    });
}
// ---------------------------------------------------------------------------
// 服务运行时组装（owner 锁 → token → pool → global worker health → RPC → ready）
// ---------------------------------------------------------------------------
const DEFAULT_POLICY_FILE = fileURLToPath(new URL('../../../vendor/mdcg/data/policy.json', import.meta.url));
export async function runServiceRuntime(opts) {
    if (typeof opts.umhHome !== 'string' || opts.umhHome.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be a non-empty string');
    }
    if (typeof opts.python !== 'string' || opts.python === '') {
        throw new UmhError('INVALID_ARGUMENT', 'python must be a non-empty string');
    }
    const umhHome = opts.umhHome;
    const host = opts.host ?? DEFAULT_BIND_HOST;
    const lock = await acquireOwnerLock(umhHome);
    let pool = null;
    let server = null;
    let stopCalled = false;
    let stoppedResolve;
    const stopped = new Promise((resolve) => {
        stoppedResolve = resolve;
    });
    const writeStatus = () => {
        void writeStatusSnapshot(umhHome, lock.record.nonce, pool?.registry() ?? []).catch(() => undefined);
    };
    try {
        // token（文件私有；ACL 校验失败即拒绝启动）
        const { token } = await loadOrCreateRpcToken(join(umhHome, 'security'));
        // pool + 回执存储工厂（C06 carry：启动时显式注册一次，不重复注册）
        pool = new EnginePool({
            factory: createRealEngineFactory(umhHome, opts.python, opts.policyFile ?? DEFAULT_POLICY_FILE),
            onWorkersChanged: writeStatus,
        });
        useReceiptStoreFactory(async (hint) => openReceiptStore(umhHome, hint.kind === 'global' ? { kind: 'global' } : { kind: 'project', projectId: String(hint.projectId ?? '') }));
        // boot readiness（裁决 1）：global worker 初始化 + health 成功，先于 ready record
        const globalEngine = await pool.get({ kind: 'global' });
        const health = await globalEngine.health();
        if (!health.available) {
            throw new UmhError('ENGINE_BOOT_FAILED', 'global worker failed health check during boot');
        }
        // C09 接线：读工具缺省走真实读服务（core/read.ts）；显式注入仍可覆盖。
        const dispatcher = createDispatcher({
            pool,
            readService: opts.readService ?? createReadService(pool),
        });
        const admin = createAdminSurface(pool);
        server = await startRpcServer({
            token,
            nonce: lock.record.nonce,
            host,
            dispatcher,
            admin,
            requestStop: () => void gracefulStop(),
        });
        // ready record（global worker 已 health 通过）
        await lock.promoteReady(server.port);
        writeStatus();
    }
    catch (err) {
        if (server !== null) {
            try {
                await server.close();
            }
            catch { /* 尽力 */ }
        }
        if (pool !== null) {
            try {
                await pool.stop();
            }
            catch { /* 尽力 */ }
        }
        try {
            await lock.release();
        }
        catch { /* 尽力 */ }
        throw err;
    }
    async function gracefulStop() {
        if (stopCalled)
            return stopped;
        stopCalled = true;
        try {
            await server?.close();
        }
        catch { /* 尽力 */ }
        try {
            await pool?.stop();
        }
        catch { /* 尽力 */ }
        try {
            await lock.release();
        }
        catch { /* 尽力 */ }
        stoppedResolve();
    }
    return {
        pid: process.pid,
        port: server.port,
        url: server.url,
        instanceId: lock.record.instanceId,
        nonce: lock.record.nonce,
        protocolVersion: SERVICE_PROTOCOL_VERSION,
        stop: gracefulStop,
        stopped,
    };
}
/** 服务侧私有管理通路实现（admin 绑定是服务自身的本地管理员身份）。 */
function createAdminSurface(pool) {
    function adminBindingFor(scope) {
        return scope.kind === 'project'
            ? { clientId: 'generic', role: 'admin', bindingId: 'service-admin', projectId: scope.projectId }
            : { clientId: 'generic', role: 'admin', bindingId: 'service-admin' };
    }
    return {
        async review(scope, decision) {
            const engine = await pool.get(scope);
            return reviewCandidate(decision, adminBindingFor(scope), engine);
        },
        async reconcile(scope, operationId) {
            const engine = await pool.get(scope);
            return reconcile(operationId, adminBindingFor(scope), engine);
        },
        async listPending(scope) {
            const engine = await pool.get(scope);
            return engine.listPending();
        },
        async workers() {
            return pool.registry();
        },
    };
}
// ---------------------------------------------------------------------------
// startService / stopService（子进程 launcher + 私有管理通路句柄）
// ---------------------------------------------------------------------------
const SERVICE_ENTRY = fileURLToPath(new URL('../../apps/service/main.js', import.meta.url));
const DEFAULT_READY_TIMEOUT_MS = 120_000;
/** 采集子进程 stderr/stdout 尾部：失败证据在抛错前捕获（先于 makeSandbox 清理）。 */
class OutputTail {
    buf = '';
    onData(chunk) {
        this.buf += chunk.toString('utf8');
        if (this.buf.length > 32 * 1024)
            this.buf = this.buf.slice(-32 * 1024);
    }
    tail(lines = 12) {
        return this.buf.trim().split('\n').slice(-lines).join('\n');
    }
    get raw() {
        return this.buf;
    }
}
function childExitSummary(child, code, signal) {
    if (code !== null)
        return `exit code ${code}`;
    if (signal !== null)
        return `signal ${signal}`;
    return 'still running';
}
/**
 * 启动中央服务子进程并等待 ready 行。失败（拒绝/崩溃/超时）时抛出的错误包含
 * 退出码与 stderr 尾部 —— 证据在抛错时已固化，随后才发生测试沙盒清理。
 */
export async function startService(opts) {
    if (typeof opts.umhHome !== 'string' || opts.umhHome.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be a non-empty string');
    }
    const python = opts.python ?? 'python';
    const timeoutMs = opts.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS;
    const args = [SERVICE_ENTRY, '--umh-home', opts.umhHome, '--python', python];
    const child = spawn(process.execPath, args, {
        env: opts.env ?? process.env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
    });
    const out = new OutputTail();
    const errTail = new OutputTail();
    child.stdout?.on('data', (c) => out.onData(c));
    child.stderr?.on('data', (c) => errTail.onData(c));
    let settled = false;
    const fail = (message) => {
        const detail = [
            `service launch failed: ${message}`,
            `pid: ${child.pid ?? '?'} ${childExitSummary(child, child.exitCode, child.signalCode)}`,
            errTail.raw.trim() !== '' ? `stderr tail:\n${errTail.tail(16)}` : 'stderr: (empty)',
            out.raw.trim() !== '' ? `stdout tail:\n${out.tail(8)}` : 'stdout: (empty)',
        ].join('\n');
        const err = new Error(detail);
        err.name = 'ServiceLaunchError';
        return err;
    };
    const readyLine = await new Promise((resolve) => {
        let buf = '';
        const done = (value) => {
            if (settled)
                return;
            settled = true;
            clearTimeout(timer);
            resolve(value);
        };
        const timer = setTimeout(() => {
            try {
                child.kill();
            }
            catch { /* 尽力 */ }
            done({ __timeout: true });
        }, timeoutMs);
        const onLine = (line) => {
            const trimmed = line.trim();
            if (trimmed === '' || !trimmed.startsWith('{'))
                return;
            try {
                const parsed = JSON.parse(trimmed);
                if (parsed['type'] === 'ready' || parsed['type'] === 'error')
                    done(parsed);
            }
            catch { /* 非 JSON 行忽略 */ }
        };
        child.stdout?.on('data', (c) => {
            buf += c.toString('utf8');
            let idx;
            while ((idx = buf.indexOf('\n')) >= 0) {
                onLine(buf.slice(0, idx));
                buf = buf.slice(idx + 1);
            }
        });
        child.on('exit', () => done(null));
    });
    if (readyLine === null) {
        await waitChildExit(child, 10_000);
        throw fail(`service exited before becoming ready (${childExitSummary(child, child.exitCode, child.signalCode)})`);
    }
    if (readyLine['__timeout'] === true) {
        throw fail(`service did not become ready within ${timeoutMs}ms`);
    }
    if (readyLine['type'] === 'error') {
        await waitChildExit(child, 10_000);
        const err = new UmhError(String(readyLine['code'] ?? 'SERVICE_ERROR'), String(readyLine['message'] ?? 'service refused'));
        const ownerPid = readyLine['ownerPid'];
        if (typeof ownerPid === 'number')
            err.ownerPid = ownerPid;
        err.detail = `stderr tail:\n${errTail.tail(8)}`;
        throw err;
    }
    const pid = child.pid;
    const port = Number(readyLine['port']);
    const url = String(readyLine['url'] ?? `http://127.0.0.1:${port}`);
    // 父进程核验子进程写入的 ready record（完整记录，不只信 stdout）。
    const record = await readReadyRecord(opts.umhHome);
    if (record === null || record.pid !== pid || record.port !== port || typeof record.nonce !== 'string') {
        try {
            child.kill();
        }
        catch { /* 尽力 */ }
        throw fail('ready owner record is missing or does not match the launched process');
    }
    let token;
    try {
        token = (await readFile(rpcTokenPath(join(opts.umhHome, 'security')), 'utf8')).trim();
    }
    catch (err) {
        try {
            child.kill();
        }
        catch { /* 尽力 */ }
        throw fail(`rpc token unreadable after ready: ${err.message}`);
    }
    const admin = new ServiceAdminClient(url, token, record.nonce);
    return {
        pid,
        url,
        nonce: record.nonce,
        stop: async () => {
            // 只终止自己 spawn 且 nonce 核验的进程：先走 /admin/stop（优雅），
            // 超时未退才对本子进程升级 SIGTERM/SIGKILL。
            try {
                await admin.stop();
            }
            catch { /* 服务可能已退出 */ }
            const exited = await waitChildExit(child, 15_000);
            if (!exited) {
                try {
                    child.kill();
                }
                catch { /* 尽力 */ }
                await waitChildExit(child, 5_000);
            }
        },
        admin,
    };
}
function waitChildExit(child, timeoutMs) {
    if (child.exitCode !== null || child.signalCode !== null)
        return Promise.resolve(true);
    return new Promise((resolve) => {
        const timer = setTimeout(() => resolve(false), timeoutMs);
        child.once('exit', () => {
            clearTimeout(timer);
            resolve(true);
        });
    });
}
/** 停止 startService 启动的服务（nonce 核验的优雅停机）。 */
export async function stopService(handle) {
    await handle.stop();
}
