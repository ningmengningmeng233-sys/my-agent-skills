/**
 * packages/ops/doctor — UMH 运维与运行环境诊断（O03）。
 *
 * 检查项（工单 O03）：
 *   - 解释器（python / node，版本与可用性，支持 python/py 启动器）
 *   - 配置权限（不能 read 通过推断 write；真实执行探测写入）
 *   - lock owner（owner.lock / owner.json 完整性，stale 判定，foreignPID 辨识）
 *   - 端口占用（52400 或配置端口是否被外来进程占用）
 *   - 安全令牌（服务在线时 rpc token 可读性与非空）
 *   - engine handshake 版本（UPSTREAM-PIN 与 md_cg 导入能力）
 *   - policy（策略文件存在性、JSON 有效性与 forbidden 规则结构）
 *   - 索引存储（memory/global、memory/projects 可访问性）
 */
import { spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { access, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { Socket } from 'node:net';
import { resolve, join } from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { isPidAlive, queryProcessStartMs } from '../service/owner.js';
const PINNED_ENGINE_SHA = 'bfbdcb923da1ef2880dd3f9f979cf419d9211eb0';
const DEFAULT_STAGE = fileURLToPath(new URL('../../../vendor/mdcg/', import.meta.url));
const DEFAULT_POLICY_FILE = fileURLToPath(new URL('../../../vendor/mdcg/data/policy.json', import.meta.url));
let cachedDisconnectedDrives = null;
/** Windows 下断开的网络驱动器（避免 SMB redirector 产生 30-90 秒内核超时）。 */
function getDisconnectedDrives() {
    if (cachedDisconnectedDrives !== null)
        return cachedDisconnectedDrives;
    cachedDisconnectedDrives = new Set();
    if (process.platform !== 'win32')
        return cachedDisconnectedDrives;
    try {
        const out = spawnSync('net', ['use'], { timeout: 3000 });
        if (out.stdout) {
            const textLatin1 = out.stdout.toString('latin1');
            const gbkDisconnected = Buffer.from([0xd2, 0xd1, 0xb6, 0xcf, 0xbf, 0xaa]); // '已断开'
            const lines = textLatin1.split('\n');
            for (const line of lines) {
                const m = line.match(/([a-zA-Z]):\s+\\\\/);
                if (m) {
                    const drive = m[1].toUpperCase();
                    if (line.includes('Disconnected') ||
                        line.includes('Unavailable') ||
                        out.stdout.includes(gbkDisconnected)) {
                        cachedDisconnectedDrives.add(drive);
                    }
                }
            }
        }
    }
    catch {
        /* 尽力探测 */
    }
    return cachedDisconnectedDrives;
}
function isDisconnectedPath(filePath) {
    if (process.platform !== 'win32')
        return false;
    const m = filePath.match(/^([a-zA-Z]):/);
    if (!m)
        return false;
    return getDisconnectedDrives().has(m[1].toUpperCase());
}
function normalizePath(p) {
    const resolved = resolve(p);
    // 在 Windows 上统一盘符大写（如 c:\ → C:\）
    if (process.platform === 'win32' && /^[a-z]:/i.test(resolved)) {
        return resolved[0].toUpperCase() + resolved.slice(1);
    }
    return resolved;
}
function probePort(port, timeoutMs = 500) {
    return new Promise((res) => {
        const socket = new Socket();
        let settled = false;
        const finish = (open) => {
            if (settled)
                return;
            settled = true;
            socket.destroy();
            res(open);
        };
        socket.setTimeout(timeoutMs);
        socket.once('connect', () => finish(true));
        socket.once('timeout', () => finish(false));
        socket.once('error', () => finish(false));
        socket.connect(port, '127.0.0.1');
    });
}
export async function runDoctor(opts) {
    const checks = [];
    const umhHome = normalizePath(opts.umhHome);
    // 1. Python 解释器检查（支持 python / py，-X utf8 兼容）
    let pythonPassed = false;
    let resolvedPython = opts.python || process.env.UMH_PYTHON || 'python';
    if (isDisconnectedPath(resolvedPython)) {
        checks.push({
            name: 'python',
            status: 'FAIL',
            reason: `Python interpreter path '${resolvedPython}' is on a disconnected network drive`,
        });
    }
    else {
        try {
            let pyOut = spawnSync(resolvedPython, ['-X', 'utf8', '-c', 'import sys; print(sys.version.split()[0])'], { encoding: 'utf8', shell: false, windowsHide: true, timeout: 5000 });
            if ((pyOut.status !== 0 || pyOut.error) && resolvedPython === 'python' && process.platform === 'win32') {
                // 尝试 Windows py 启动器
                const fallback = spawnSync('py', ['-3', '-c', 'import sys; print(sys.version.split()[0])'], { encoding: 'utf8', shell: false, windowsHide: true, timeout: 5000 });
                if (fallback.status === 0 && !fallback.error) {
                    pyOut = fallback;
                    resolvedPython = 'py';
                }
            }
            if (pyOut.status === 0 && !pyOut.error) {
                pythonPassed = true;
                checks.push({
                    name: 'python',
                    status: 'PASS',
                    reason: `Python version ${pyOut.stdout.trim()} available via ${resolvedPython}`,
                });
            }
            else {
                const err = pyOut.error?.message || pyOut.stderr?.trim() || `exit code ${pyOut.status}`;
                checks.push({
                    name: 'python',
                    status: 'FAIL',
                    reason: `Python interpreter unusable at '${resolvedPython}': ${err}`,
                });
            }
        }
        catch (err) {
            checks.push({
                name: 'python',
                status: 'FAIL',
                reason: `Python interpreter check failed: ${err.message}`,
            });
        }
    }
    // 2. Node.js 解释器检查
    const nodeExecutable = opts.node || process.execPath;
    if (isDisconnectedPath(nodeExecutable)) {
        checks.push({
            name: 'node',
            status: 'FAIL',
            reason: `Node interpreter path '${nodeExecutable}' is on a disconnected network drive`,
        });
    }
    else {
        try {
            const nodeOut = spawnSync(nodeExecutable, ['--version'], {
                encoding: 'utf8',
                shell: false,
                windowsHide: true,
                timeout: 5000,
            });
            if (nodeOut.status === 0 && !nodeOut.error) {
                checks.push({
                    name: 'node',
                    status: 'PASS',
                    reason: `Node version ${nodeOut.stdout.trim()} at ${nodeExecutable}`,
                });
            }
            else {
                const err = nodeOut.error?.message || nodeOut.stderr?.trim() || `exit code ${nodeOut.status}`;
                checks.push({
                    name: 'node',
                    status: 'FAIL',
                    reason: `Node interpreter unusable at '${nodeExecutable}': ${err}`,
                });
            }
        }
        catch (err) {
            checks.push({
                name: 'node',
                status: 'FAIL',
                reason: `Node interpreter check failed: ${err.message}`,
            });
        }
    }
    // 3. 配置权限与文件系统写入能力（不能 read 通过推断 write）
    if (isDisconnectedPath(umhHome)) {
        checks.push({
            name: 'permissions',
            status: 'FAIL',
            reason: `Write permission probe failed for '${umhHome}': disconnected network drive (cannot infer write from read)`,
        });
        checks.push({
            name: 'owner_lock',
            status: 'FAIL',
            reason: `Cannot read runtime directory on disconnected network drive '${umhHome}'`,
        });
        checks.push({
            name: 'token',
            status: 'FAIL',
            reason: `Cannot read security directory on disconnected network drive '${umhHome}'`,
        });
        checks.push({
            name: 'index',
            status: 'FAIL',
            reason: `Cannot access memory directory on disconnected network drive '${umhHome}'`,
        });
    }
    else {
        const probeFile = join(umhHome, `.doctor-write-probe-${randomBytes(6).toString('hex')}`);
        try {
            await mkdir(umhHome, { recursive: true });
            const payload = `doctor-probe-${Date.now()}`;
            await writeFile(probeFile, payload, 'utf8');
            const readBack = await readFile(probeFile, 'utf8');
            if (readBack !== payload) {
                throw new Error('read-back payload mismatch');
            }
            await rm(probeFile, { force: true });
            checks.push({
                name: 'permissions',
                status: 'PASS',
                reason: `umhHome writable and verified by probe write at ${umhHome}`,
            });
        }
        catch (err) {
            try {
                await rm(probeFile, { force: true });
            }
            catch { /* 尽力 */ }
            checks.push({
                name: 'permissions',
                status: 'FAIL',
                reason: `Write permission probe failed for '${umhHome}': ${err.message} (cannot infer write from read)`,
            });
        }
    }
    // 4. Policy 策略文件检查
    if (opts.policyFile) {
        // 显式指定 policyFile 时：文件不存在或无法解析必须 FAIL，绝不静默回退
        try {
            const policyRaw = await readFile(opts.policyFile, 'utf8');
            const parsed = JSON.parse(policyRaw);
            if (typeof parsed !== 'object' || parsed === null) {
                throw new Error('policy is not a valid JSON object');
            }
            checks.push({
                name: 'policy',
                status: 'PASS',
                reason: `Policy file verified at ${opts.policyFile}`,
            });
        }
        catch (err) {
            checks.push({
                name: 'policy',
                status: 'FAIL',
                reason: `Policy file unreadable or invalid at '${opts.policyFile}': ${err.message}`,
            });
        }
    }
    else {
        const policyCandidate = join(umhHome, 'config', 'policy.json');
        let checkedPolicyPath = null;
        try {
            let policyRaw;
            if (isDisconnectedPath(umhHome)) {
                policyRaw = await readFile(DEFAULT_POLICY_FILE, 'utf8');
                checkedPolicyPath = DEFAULT_POLICY_FILE;
            }
            else {
                try {
                    policyRaw = await readFile(policyCandidate, 'utf8');
                    checkedPolicyPath = policyCandidate;
                }
                catch {
                    policyRaw = await readFile(DEFAULT_POLICY_FILE, 'utf8');
                    checkedPolicyPath = DEFAULT_POLICY_FILE;
                }
            }
            const parsed = JSON.parse(policyRaw);
            if (typeof parsed !== 'object' || parsed === null) {
                throw new Error('policy is not a valid JSON object');
            }
            checks.push({
                name: 'policy',
                status: 'PASS',
                reason: `Policy file verified at ${checkedPolicyPath}`,
            });
        }
        catch (err) {
            checks.push({
                name: 'policy',
                status: 'FAIL',
                reason: `Policy file unreadable or invalid: ${err.message}`,
            });
        }
    }
    // 5. Lock owner、foreignPID 与运行状态检查
    const runtimeDir = join(umhHome, 'runtime');
    const lockFile = join(runtimeDir, 'owner.lock');
    const ownerJsonFile = join(runtimeDir, 'owner.json');
    let lockOwnerPid = null;
    let isServiceAlive = false;
    let isForeignPid = false;
    if (!isDisconnectedPath(umhHome)) {
        try {
            const lockRaw = await readFile(lockFile, 'utf8');
            let lockRecord;
            try {
                lockRecord = JSON.parse(lockRaw);
            }
            catch {
                checks.push({
                    name: 'owner_lock',
                    status: 'FAIL',
                    reason: 'owner.lock exists but is corrupt or invalid JSON',
                });
                lockRecord = null;
            }
            if (lockRecord && typeof lockRecord.pid === 'number') {
                lockOwnerPid = lockRecord.pid;
                if (!isPidAlive(lockRecord.pid)) {
                    checks.push({
                        name: 'owner_lock',
                        status: 'WARN',
                        reason: `owner.lock references dead PID ${lockRecord.pid} (stale lock)`,
                    });
                }
                else {
                    // 活进程：核验证实是否为 foreignPID（根据 OwnerRecord.processStartMs 真实启动时刻）
                    // 允许经 opts.queryProcessStartMs 注入 seam 测试分支
                    const queryStart = opts.queryProcessStartMs ?? queryProcessStartMs;
                    const start = queryStart(lockRecord.pid);
                    const recordedStart = typeof lockRecord.processStartMs === 'number'
                        ? lockRecord.processStartMs
                        : typeof lockRecord.processStart === 'number'
                            ? lockRecord.processStart
                            : null;
                    if (recordedStart === null) {
                        isForeignPid = true;
                        checks.push({
                            name: 'owner_lock',
                            status: 'FAIL',
                            reason: `owner lock has unverified start time (missing processStartMs field) for live PID ${lockRecord.pid}`,
                        });
                        checks.push({
                            name: 'foreign_pid',
                            status: 'FAIL',
                            reason: `Unverified process ${lockRecord.pid} holds owner lock`,
                        });
                    }
                    else if (start === null) {
                        // PID 存活但系统查询不到 start time：输出 WARN/UNVERIFIED，绝不声称 verified PID
                        checks.push({
                            name: 'owner_lock',
                            status: 'WARN',
                            reason: `owner lock process start time unverifiable for live PID ${lockRecord.pid}; cannot claim verified PID`,
                        });
                    }
                    else if (Math.abs(start - recordedStart) > 5000) {
                        isForeignPid = true;
                        checks.push({
                            name: 'owner_lock',
                            status: 'FAIL',
                            reason: `owner lock points to foreign PID ${lockRecord.pid} (start time mismatch: recorded ${recordedStart}, actual ${start})`,
                        });
                        checks.push({
                            name: 'foreign_pid',
                            status: 'FAIL',
                            reason: `Foreign process ${lockRecord.pid} holds owner lock`,
                        });
                    }
                    else {
                        isServiceAlive = true;
                        checks.push({
                            name: 'owner_lock',
                            status: 'PASS',
                            reason: `Service active with verified PID ${lockRecord.pid}`,
                        });
                    }
                }
            }
        }
        catch (err) {
            if (err.code === 'ENOENT') {
                checks.push({
                    name: 'owner_lock',
                    status: 'PASS',
                    reason: 'No owner lock present (clean stop state)',
                });
            }
            else {
                checks.push({
                    name: 'owner_lock',
                    status: 'FAIL',
                    reason: `Failed to read owner.lock: ${err.message}`,
                });
            }
        }
    }
    // 6. 端口占用检查（owner.json 中端口或默认端口 52400）
    let targetPort = 52400;
    let ownerHasPort = false;
    if (!isDisconnectedPath(umhHome)) {
        try {
            const ownerJsonRaw = await readFile(ownerJsonFile, 'utf8');
            const ownerJson = JSON.parse(ownerJsonRaw);
            if (typeof ownerJson.port === 'number') {
                targetPort = ownerJson.port;
                ownerHasPort = true;
            }
        }
        catch {
            /* owner.json 不存在走默认端口 */
        }
    }
    const portOpen = await probePort(targetPort);
    if (portOpen) {
        if (isForeignPid || !isServiceAlive) {
            checks.push({
                name: 'port',
                status: 'FAIL',
                reason: `Port ${targetPort} is occupied by an external/foreign process`,
            });
        }
        else {
            checks.push({
                name: 'port',
                status: 'PASS',
                reason: `Service listening on port ${targetPort}`,
            });
        }
    }
    else {
        if (isServiceAlive && ownerHasPort) {
            checks.push({
                name: 'port',
                status: 'FAIL',
                reason: `Service is marked alive (PID ${lockOwnerPid}) but port ${targetPort} is not accepting connections`,
            });
        }
        else {
            checks.push({
                name: 'port',
                status: 'PASS',
                reason: `Port ${targetPort} is available`,
            });
        }
    }
    // 7. Token 权限与存在性检查
    if (!isDisconnectedPath(umhHome)) {
        const tokenFile = join(umhHome, 'security', 'rpc', 'token');
        try {
            const token = (await readFile(tokenFile, 'utf8')).trim();
            if (token === '') {
                throw new Error('rpc token file is empty');
            }
            checks.push({
                name: 'token',
                status: 'PASS',
                reason: 'RPC token file is present and readable',
            });
        }
        catch (err) {
            if (isServiceAlive || ownerHasPort) {
                checks.push({
                    name: 'token',
                    status: 'FAIL',
                    reason: `Service is active or configured but RPC token is unreadable: ${err.message}`,
                });
            }
            else {
                checks.push({
                    name: 'token',
                    status: 'PASS',
                    reason: 'Service not active; token uninitialized or clear',
                });
            }
        }
    }
    // 8. Engine handshake 与版本检查
    if (pythonPassed) {
        try {
            const probeOut = spawnSync(resolvedPython, ['-X', 'utf8', '-c', 'import md_cg; print(getattr(md_cg, "__file__", "ok"))'], {
                cwd: DEFAULT_STAGE,
                env: {
                    ...process.env,
                    PYTHONPATH: DEFAULT_STAGE,
                    PYTHONUTF8: '1',
                },
                encoding: 'utf8',
                shell: false,
                windowsHide: true,
                timeout: 5000,
            });
            if (probeOut.status === 0 && !probeOut.error) {
                checks.push({
                    name: 'engine',
                    status: 'PASS',
                    reason: `Engine handshake compatible (upstream SHA pin ${PINNED_ENGINE_SHA.slice(0, 7)})`,
                });
            }
            else {
                checks.push({
                    name: 'engine',
                    status: 'WARN',
                    reason: `Engine handshake probe exited non-zero: ${probeOut.stderr?.trim() || probeOut.status}`,
                });
            }
        }
        catch (err) {
            checks.push({
                name: 'engine',
                status: 'WARN',
                reason: `Engine handshake probe failed: ${err.message}`,
            });
        }
    }
    else {
        checks.push({
            name: 'engine',
            status: 'NOT_RUN',
            reason: 'Engine check skipped due to missing Python interpreter',
        });
    }
    // 9. 索引目录检查（只读面探测，绝不主动 mkdir 写盘）
    if (!isDisconnectedPath(umhHome)) {
        const globalMemory = join(umhHome, 'memory', 'global');
        const projectsMemory = join(umhHome, 'memory', 'projects');
        let globalAccessible = false;
        let projectsAccessible = false;
        try {
            await access(globalMemory);
            globalAccessible = true;
        }
        catch {
            globalAccessible = false;
        }
        try {
            await access(projectsMemory);
            projectsAccessible = true;
        }
        catch {
            projectsAccessible = false;
        }
        if (globalAccessible && projectsAccessible) {
            checks.push({
                name: 'index',
                status: 'PASS',
                reason: `Memory storage and index directories accessible at ${umhHome}/memory`,
            });
        }
        else {
            checks.push({
                name: 'index',
                status: 'WARN',
                reason: `Memory storage uninitialized (memory/global or memory/projects missing); run umh init to initialize`,
            });
        }
    }
    // 判定总状态：FAIL 优先，其次 WARN，否则 PASS
    const hasFail = checks.some((c) => c.status === 'FAIL');
    const hasWarn = checks.some((c) => c.status === 'WARN');
    const overallStatus = hasFail ? 'FAIL' : hasWarn ? 'WARN' : 'PASS';
    return {
        status: overallStatus,
        checks,
    };
}
