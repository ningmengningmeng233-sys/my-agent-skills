/**
 * apps/service/main — 中央服务进程入口（C07）。
 *
 * stdout 只输出机器可读的启动协议行（ready / error 各一行），诊断走 stderr
 * （总入口约束：stdout 不混诊断）。
 *
 * argv：
 *   --umh-home <path>   必填，UMH 根（沙盒/生产均显式传入）
 *   --python <path>     引擎解释器（默认 python）
 *   --host <addr>       仅接受 127.0.0.1（非法 bind addr 拒绝启动）
 *
 * ready 行：{"type":"ready","pid":N,"port":P,"url":"http://127.0.0.1:P",...}
 * error 行：{"type":"error","code":"...","ownerPid":N?,"message":"..."} → exit 1
 */
import process from 'node:process';
import { UmhError } from '../../packages/contracts/index.js';
import { runServiceRuntime } from '../../packages/service/owner.js';
function argValue(argv, name) {
    const idx = argv.indexOf(name);
    if (idx < 0 || idx + 1 >= argv.length)
        return undefined;
    return argv[idx + 1];
}
function emitError(code, message, ownerPid) {
    const payload = { type: 'error', code, message };
    if (ownerPid !== undefined)
        payload['ownerPid'] = ownerPid;
    process.stdout.write(`${JSON.stringify(payload)}\n`);
}
const umhHome = argValue(process.argv.slice(2), '--umh-home');
if (umhHome === undefined || umhHome.trim() === '') {
    emitError('INVALID_ARGUMENT', '--umh-home is required');
    process.stderr.write('usage: node apps/service/main.js --umh-home <path> [--python <path>] [--host 127.0.0.1]\n');
    process.exit(2);
}
const python = argValue(process.argv.slice(2), '--python') ?? 'python';
const host = argValue(process.argv.slice(2), '--host');
try {
    const runtime = await runServiceRuntime({
        umhHome,
        python,
        ...(host !== undefined ? { host } : {}),
    });
    process.stdout.write(`${JSON.stringify({
        type: 'ready',
        pid: runtime.pid,
        port: runtime.port,
        url: runtime.url,
        instanceId: runtime.instanceId,
        protocolVersion: runtime.protocolVersion,
    })}\n`);
    await runtime.stopped;
    // 让 stdout 的 ready 行先落盘再退出。
    await new Promise((resolve) => setImmediate(resolve));
    process.exit(0);
}
catch (err) {
    if (err instanceof UmhError) {
        const ownerPid = err.ownerPid;
        emitError(err.code, err.message, ownerPid);
        process.stderr.write(`service boot failed: [${err.code}] ${err.message}\n`);
        process.exit(1);
    }
    emitError('INTERNAL', err.message);
    process.stderr.write(`service boot crashed: ${err.stack ?? String(err)}\n`);
    process.exit(1);
}
