/**
 * apps/cli/main — umh 管理入口（C07）。
 *
 * 路由（工单 C07：仅 import 已存在模块；A01 增补 connect/disconnect 接线）：
 *   init / start / stop / status  → apps/cli/lifecycle.ts
 *   review list|decide            → 运行中服务的私有管理通路 /admin/review|/admin/listPending
 *   reconcile                     → /admin/reconcile
 *   connect [apply] / disconnect  → apps/cli/connect.ts（A01 安全配置事务命令层）
 *   backup / restore / forget / rebuild / pending → apps/cli/maintenance.ts（O02）
 * O03 增补 doctor —— 本文件不预留、不实现。
 *
 * stdout 只输出机器可读 JSON；用法/诊断走 stderr。
 * 退出码：0 完成；1 运行时 UmhError（stdout 输出 {error:{code,message}}）；
 * 2 用法错误（stderr 提示）。
 *
 * review/reconcile 在本进程不起 worker（设计 §5：每个作用域由中央服务持有一个
 * worker）；服务未运行时明确报 SERVICE_NOT_RUNNING，绝不 spawn 竞争 worker。
 */
import process from 'node:process';
import { UmhError } from '../../packages/contracts/index.js';
import { loadAdminCredentials, runInitCommand, runStartCommand, runStopCommand, runStatusCommand, UsageError, } from './lifecycle.js';
import { runConnectApplyRoute, runConnectReconcileRoute, runConnectRoute, runConnectTrustRoute, runDisconnectRoute } from './connect.js';
import { runBackupRoute, runForgetRoute, runPendingRoute, runRebuildRoute, runRestoreNodeRoute, runRestoreRoute } from './maintenance.js';
import { runDoctorRoute } from './doctor.js';
import { rpcPost } from '../../packages/service/rpc.js';
const USAGE = [
    'usage: umh init [--umh-home <path>]',
    '       umh start --umh-home <path> [--python <path>] [--host 127.0.0.1]',
    '       umh stop --umh-home <path>',
    '       umh status --umh-home <path>',
    '       umh doctor [--umh-home <path>] [--python <path>] [--node <path>] [--json]',
    '       umh connect <clientId> --variant <v> --workspace <dir> --dry-run --out <plan.json> [--umh-home <path>] [--config-path <abs>]',
    '       umh connect apply <plan.json>',
    '       umh connect trust --connection <id> --umh-home <path> --confirm-reviewed',
    '       umh connect reconcile --connection <id> --umh-home <path>',
    '       umh disconnect --connection <id> --umh-home <path> [--confirm-manual-removal]',
    '       umh disconnect --receipt <receipt.json> [--umh-home <path>] (legacy)',
    '       umh review --umh-home <path> [--project <projectId>] list',
    '       umh review --umh-home <path> [--project <projectId>] decide --proposal <pid> --decision <accept|reject|supersede> --reason <text> [--old <memoryId>]...',
    '       umh reconcile --umh-home <path> [--project <projectId>] --operation-id <opId>',
    '       umh backup --umh-home <path>',
    '       umh restore preview|apply --manifest <path> --target <path>',
    '       umh forget --umh-home <path> [--project <projectId>] --id <nodeId>',
    '       umh restore --umh-home <path> [--project <projectId>] --id <nodeId>',
    '       umh rebuild --umh-home <path> [--project <projectId>]',
    '       umh pending --umh-home <path> [--project <projectId>]',
].join('\n');
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
function scopeOf(argv) {
    const projectId = argValue(argv, '--project');
    return projectId === undefined ? { kind: 'global' } : { kind: 'project', projectId };
}
async function adminPost(argv, path, body) {
    const umhHome = argValue(argv, '--umh-home');
    if (umhHome === undefined || umhHome.trim() === '') {
        throw new UsageError('--umh-home <path> is required');
    }
    const { record, token } = await loadAdminCredentials(umhHome);
    const response = await rpcPost(`http://127.0.0.1:${record.port}`, path, token, {
        nonce: record.nonce,
        ...body,
    });
    const json = response.json;
    if (json['ok'] !== true) {
        const err = json['error'];
        throw new UmhError(String(err?.code ?? 'RPC_ERROR'), String(err?.message ?? 'rpc call failed'));
    }
    return json['result'];
}
function parseDecideArgs(argv) {
    let proposalId = '';
    let decision = '';
    let reason = '';
    const supersedes = [];
    for (let i = 0; i < argv.length; i++) {
        const flag = argv[i];
        const value = argv[i + 1];
        if (flag === '--proposal' && value !== undefined) {
            proposalId = value;
            i++;
        }
        else if (flag === '--decision' && value !== undefined) {
            decision = value;
            i++;
        }
        else if (flag === '--reason' && value !== undefined) {
            reason = value;
            i++;
        }
        else if (flag === '--old' && value !== undefined) {
            supersedes.push(value);
            i++;
        }
        else {
            throw new UsageError(`unknown or incomplete flag: ${String(flag)}`);
        }
    }
    if (proposalId === '' || reason === '')
        throw new UsageError('--proposal and --reason are required');
    if (decision !== 'accept' && decision !== 'reject' && decision !== 'supersede') {
        throw new UsageError('--decision must be accept|reject|supersede');
    }
    const parsed = { proposalId, decision, reason };
    if (supersedes.length > 0)
        parsed.supersedes = supersedes;
    return parsed;
}
/** umh review — 私有管理通路（运行中服务；绝不本地起 worker）。 */
async function runReviewRoute(argv) {
    // USAGE 文档化形态 `umh review --umh-home <path> list`：子命令 = 首个非全局
    // flag 参数；全局 flag（--umh-home/--project）可出现在子命令前后，仍由
    // adminPost/scopeOf 从完整 argv 摄入（O05 修正轮复审 Must-1 解析缺陷修复）。
    const globalFlags = new Set(['--umh-home', '--project']);
    let subcommand;
    const rest = [];
    for (let i = 0; i < argv.length; i++) {
        if (globalFlags.has(argv[i])) {
            if (argv[i + 1] !== undefined)
                i++;
            continue;
        }
        if (subcommand === undefined)
            subcommand = argv[i];
        else
            rest.push(argv[i]);
    }
    if (subcommand === 'list') {
        const pending = await adminPost(argv, '/admin/listPending', { scope: scopeOf(argv) });
        out(pending);
        return 0;
    }
    if (subcommand === 'decide') {
        const decision = parseDecideArgs(rest);
        const outcome = await adminPost(argv, '/admin/review', { scope: scopeOf(argv), decision });
        out(outcome);
        return 0;
    }
    throw new UsageError('review requires: list | decide ...');
}
/** umh reconcile — 私有管理通路对账（C06 语义在服务内执行）。 */
async function runReconcileRoute(argv) {
    const operationId = argValue(argv, '--operation-id');
    if (operationId === undefined || operationId.trim() === '') {
        throw new UsageError('--operation-id <opId> is required');
    }
    const outcome = await adminPost(argv, '/admin/reconcile', {
        scope: scopeOf(argv),
        operationId,
    });
    out(outcome);
    return 0;
}
async function main() {
    const [command, ...rest] = process.argv.slice(2);
    if (command === undefined) {
        diag(`${USAGE}`);
        return 2;
    }
    try {
        switch (command) {
            case 'init': return await runInitCommand(rest);
            case 'start': return await runStartCommand(rest);
            case 'stop': return await runStopCommand(rest);
            case 'status': return await runStatusCommand(rest);
            case 'connect': {
                // umh connect apply <plan.json> 为 connect 的子命令（工单冻结命令行）。
                // R02 新增恢复子命令：umh connect reconcile --connection <id> --umh-home <root>。
                // 2026-10-06 补充：umh connect trust —— 手工连接信任链的显式确认与受信
                // 绑定发布（补充设计 §2.2；发布绑定 ≠ 现场 PASS）。
                if (rest[0] === 'apply')
                    return await runConnectApplyRoute(rest.slice(1));
                if (rest[0] === 'trust')
                    return await runConnectTrustRoute(rest.slice(1));
                if (rest[0] === 'reconcile')
                    return await runConnectReconcileRoute(rest.slice(1));
                return await runConnectRoute(rest);
            }
            case 'disconnect': return await runDisconnectRoute(rest);
            case 'review': return await runReviewRoute(rest);
            case 'reconcile': return await runReconcileRoute(rest);
            case 'backup': return await runBackupRoute(rest);
            case 'restore': {
                // umh restore preview|apply … 为备份恢复面；umh restore --id … 为单节点恢复面。
                if (rest.includes('--id'))
                    return await runRestoreNodeRoute(rest);
                return await runRestoreRoute(rest);
            }
            case 'forget': return await runForgetRoute(rest);
            case 'rebuild': return await runRebuildRoute(rest);
            case 'pending': return await runPendingRoute(rest);
            case 'doctor': return await runDoctorRoute(rest);
            default: {
                diag(`unknown command: ${command}\n${USAGE}`);
                return 2;
            }
        }
    }
    catch (err) {
        if (err instanceof UsageError) {
            diag(`${err.message}\n${USAGE}`);
            return 2;
        }
        if (err instanceof UmhError) {
            // R03：disconnect 的失败 record state（needs_reconcile 等）同源输出，
            // 供 CLI 调用方无需二次读盘即可判定对账面（state 缺席即旧行为，保持兼容）。
            // 注意：dist 产物由 tsc 从本文件编译，`err as X` 断言在 JS 侧无运行时成本；
            // 直接读可选属性，避免跨包 class 身份（npm 软链/双实例）导致 instanceof 失灵。
            const extra = err.connectionState;
            out(typeof extra === 'string'
                ? { error: { code: err.code, message: err.message }, state: extra }
                : { error: { code: err.code, message: err.message } });
            diag(`umh ${command} failed: [${err.code}] ${err.message}`);
            return 1;
        }
        throw err;
    }
}
const exitCode = await main();
// 让 stdout JSON 先落盘再退出。
await new Promise((resolve) => setImmediate(resolve));
process.exit(exitCode);
