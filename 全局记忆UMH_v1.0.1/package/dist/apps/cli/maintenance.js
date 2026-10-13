/**
 * apps/cli/maintenance — umh backup/restore/rebuild 管理路由（O02）。
 *
 * stdout 只输出机器可读 JSON；用法/诊断走 stderr。
 * 退出码：0 成功；1 操作/验收失败（含恢复冲突，不自动覆盖）；2 参数/权限错误。
 */
import { isAbsolute, resolve } from 'node:path';
import { UmhError } from '../../packages/contracts/index.js';
import { createBackup } from '../../packages/ops/backup.js';
import { applyRestore, planRestore } from '../../packages/ops/restore.js';
import { forgetScoped, rebuildScope, restoreScoped } from '../../packages/ops/rebuild.js';
import { acquireOwnerLock } from '../../packages/service/owner.js';
import { loadAdminCredentials, UsageError } from './lifecycle.js';
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
function requireUmhHome(argv) {
    const value = argValue(argv, '--umh-home');
    if (value === undefined || value.trim() === '' || !isAbsolute(value)) {
        throw new UsageError('--umh-home <absolute path> is required');
    }
    return resolve(value);
}
function scopeOf(argv) {
    const projectId = argValue(argv, '--project');
    return projectId === undefined ? { kind: 'global' } : { kind: 'project', projectId };
}
/** umh backup --umh-home <path> */
export async function runBackupRoute(argv) {
    const umhHome = requireUmhHome(argv);
    for (const token of argv) {
        if (token.startsWith('--') && token !== '--umh-home') {
            throw new UsageError(`unknown flag for backup: ${token}`);
        }
    }
    // 维护语义（O02 修复轮）：owner 锁即「拒绝新写」的真实实现——
    //   服务在线 → 锁核验 fail-closed 抛 ALREADY_RUNNING（在线写者无排空屏障，
    //   在线快照不承诺一致，绝不出 consistent:true）；
    //   服务离线 → 锁到手后 umh start 同被拒（新写者进不来）⇒ 零写者离线快照，
    //   consistent:true 由该凭据背书；异常同样经 createBackup 的 finally 解除。
    const holder = { lock: null };
    try {
        const result = await createBackup({
            umhHome,
            beginMaintenance: async () => {
                holder.lock = await acquireOwnerLock(umhHome);
            },
            endMaintenance: async () => {
                const lock = holder.lock;
                holder.lock = null;
                if (lock !== null) {
                    try {
                        await lock.release();
                    }
                    catch (err) {
                        // ops 层 finally 对 endMaintenance 吞错（宿主无关合同）；释放失败
                        // 至少要上 stderr——静默滞留的维护锁只剩 stale 接管自愈一条路。
                        diag(`umh backup: maintenance lock release failed (${err.message}); the stale lock is reclaimable once this process exits`);
                    }
                }
            },
            // 离线 + owner 锁在手 ⇒ 不存在内存态写者可排空；durable 写已 fsync
            // （C06-M5）。drainAndFlush 的离线语义即空操作。
            drainAndFlush: async () => { },
        });
        out({ status: 'backed_up', backupId: result.backupId, manifestPath: result.manifestPath, consistent: result.consistent });
        return 0;
    }
    catch (err) {
        if (err instanceof UmhError && err.code === 'ALREADY_RUNNING') {
            diag('umh backup refused: the central service is running; stop it first (umh stop) for a consistent offline backup');
        }
        throw err;
    }
}
/** umh restore preview|apply --manifest <path> --target <path> */
export async function runRestoreRoute(argv) {
    const [subcommand, ...rest] = argv;
    if (subcommand !== 'preview' && subcommand !== 'apply') {
        throw new UsageError('restore requires: preview --manifest <path> --target <path> | apply --manifest <path> --target <path>');
    }
    const manifest = argValue(rest, '--manifest');
    const target = argValue(rest, '--target');
    if (manifest === undefined || manifest.trim() === '' || target === undefined || target.trim() === '' || !isAbsolute(target)) {
        throw new UsageError('restore requires --manifest <path> --target <absolute path>');
    }
    for (const token of rest) {
        if (token.startsWith('--') && token !== '--manifest' && token !== '--target') {
            throw new UsageError(`unknown flag for restore: ${token}`);
        }
    }
    if (subcommand === 'preview') {
        const plan = await planRestore(resolve(manifest), resolve(target));
        out({ status: 'restore_planned', planId: plan.id, backupId: plan.backupId, targetRoot: plan.targetRoot, changes: plan.changes.length });
        return 0;
    }
    const plan = await planRestore(resolve(manifest), resolve(target));
    try {
        await applyRestore(plan);
    }
    catch (err) {
        if (err instanceof UmhError && (err.code === 'RESTORE_TARGET_CONFLICT' || err.code === 'RESTORE_TARGET_CHANGED')) {
            out({ error: { code: err.code, message: err.message } });
            diag(`umh restore failed: [${err.code}] ${err.message}`);
            return 1;
        }
        throw err;
    }
    out({ status: 'restored', backupId: plan.backupId, targetRoot: plan.targetRoot });
    return 0;
}
/**
 * umh forget|restore|rebuild|pending --umh-home <path> [--project <id>] [--id <node>]
 * 管理面（运行中服务；绝不本地起竞争 worker——rebuild/forget/restore 经
 * EnginePool 直连引擎根；listPending 走私有管理通路）。
 */
export async function runForgetRoute(argv) {
    const umhHome = requireUmhHome(argv);
    const id = argValue(argv, '--id');
    if (id === undefined || id.trim() === '')
        throw new UsageError('forget requires --id <nodeId>');
    const { ServiceAdminClient } = await import('../../packages/service/rpc.js');
    const creds = await loadAdminCredentials(umhHome);
    const admin = new ServiceAdminClient(`http://127.0.0.1:${creds.record.port}`, creds.token, creds.record.nonce);
    admin.umhHome = umhHome;
    await forgetScoped(scopeOf(argv), id, admin);
    out({ status: 'forgotten', id });
    return 0;
}
export async function runRestoreNodeRoute(argv) {
    const umhHome = requireUmhHome(argv);
    const id = argValue(argv, '--id');
    if (id === undefined || id.trim() === '')
        throw new UsageError('restore requires --id <nodeId>');
    const creds = await loadAdminCredentials(umhHome);
    const { ServiceAdminClient } = await import('../../packages/service/rpc.js');
    const admin = new ServiceAdminClient(`http://127.0.0.1:${creds.record.port}`, creds.token, creds.record.nonce);
    admin.umhHome = umhHome;
    await restoreScoped(scopeOf(argv), id, admin);
    out({ status: 'node_restored', id });
    return 0;
}
export async function runRebuildRoute(argv) {
    const umhHome = requireUmhHome(argv);
    for (const token of argv) {
        if (token.startsWith('--') && token !== '--umh-home' && token !== '--project') {
            throw new UsageError(`unknown flag for rebuild: ${token}`);
        }
    }
    const creds = await loadAdminCredentials(umhHome);
    const { ServiceAdminClient } = await import('../../packages/service/rpc.js');
    const admin = new ServiceAdminClient(`http://127.0.0.1:${creds.record.port}`, creds.token, creds.record.nonce);
    admin.umhHome = umhHome;
    await rebuildScope(scopeOf(argv), admin);
    out({ status: 'rebuilt', scope: scopeOf(argv) });
    return 0;
}
export async function runPendingRoute(argv) {
    const umhHome = requireUmhHome(argv);
    const creds = await loadAdminCredentials(umhHome);
    const { rpcPost } = await import('../../packages/service/rpc.js');
    const response = await rpcPost(`http://127.0.0.1:${creds.record.port}`, '/admin/listPending', creds.token, {
        nonce: creds.record.nonce,
        scope: scopeOf(argv),
    });
    const json = response.json;
    if (json['ok'] !== true) {
        const err = json['error'];
        throw new UmhError(String(err?.code ?? 'RPC_ERROR'), String(err?.message ?? 'rpc call failed'));
    }
    out(json['result']);
    return 0;
}
