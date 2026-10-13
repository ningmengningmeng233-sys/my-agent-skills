/**
 * apps/cli/doctor — umh doctor 运维诊断命令（O03）。
 *
 * 用法：
 *   umh doctor [--umh-home <path>] [--python <path>] [--node <path>] [--json]
 *
 * 退出码：
 *   0 诊断 PASS / WARN；
 *   1 诊断 FAIL（检查项存在阻断性故障）；
 *   2 参数错误。
 */
import { homedir } from 'node:os';
import { isAbsolute, join, resolve } from 'node:path';
import process from 'node:process';
import { runDoctor } from '../../packages/ops/doctor.js';
import { UsageError } from './lifecycle.js';
function argValue(argv, name) {
    const idx = argv.indexOf(name);
    if (idx < 0 || idx + 1 >= argv.length)
        return undefined;
    return argv[idx + 1];
}
function out(payload) {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
}
function diag(message) {
    process.stderr.write(`${message}\n`);
}
export async function runDoctorRoute(argv) {
    const rawHome = argValue(argv, '--umh-home') ?? process.env.UMH_HOME ?? join(homedir(), '.universal-memory-hub');
    const python = argValue(argv, '--python');
    const node = argValue(argv, '--node');
    const jsonRequested = argv.includes('--json');
    // 校验未知参数
    for (let i = 0; i < argv.length; i++) {
        const arg = argv[i];
        if (arg === '--json')
            continue;
        if (arg === '--umh-home' || arg === '--python' || arg === '--node') {
            i++;
            continue;
        }
        if (arg.startsWith('--')) {
            throw new UsageError(`unknown flag for doctor: ${arg}`);
        }
    }
    const umhHome = isAbsolute(rawHome) ? resolve(rawHome) : resolve(process.cwd(), rawHome);
    let result;
    try {
        result = await runDoctor({ umhHome, python, node });
    }
    catch (err) {
        diag(`Doctor run encountered unexpected error: ${err.message}`);
        return 1;
    }
    // CLI 契约：管理入口 stdout 始终输出机器可读 JSON；--json 保留为显式兼容开关
    out(result);
    if (result.status === 'FAIL') {
        diag('UMH Doctor check failed: one or more checks reported FAIL');
        return 1;
    }
    return 0;
}
