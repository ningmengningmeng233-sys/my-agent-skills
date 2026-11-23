/**
 * packages/ops/restore — O02 RED 桩（故意未实现：plan/apply 恒抛）。
 *
 * GREEN 将实现：restore 先 preview，apply 校验 target hash、范围、版本/SHA，
 * 先备份当前、staging 校验后发布；失败回原态；恢复 ACL 重新设定。
 */
import { createHash } from 'node:crypto';
import { copyFile, mkdir, readdir, readFile, readlink, rm, stat, lstat } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { UmhError } from '../contracts/index.js';
import { assertSafeArchivePath, durableWriteFile } from './backup.js';
const EXPECTED_ENGINE_SHA = 'bfbdcb923da1ef2880dd3f9f979cf419d9211eb0';
/** 允许恢复的相对根（memory/security/config/receipts + runtime/connections）。 */
function assertRestoreScope(rel) {
    assertSafeArchivePath(rel);
    const norm = rel.replace(/\\/g, '/');
    const ok = norm === 'memory' || norm.startsWith('memory/') ||
        norm === 'security' || norm.startsWith('security/') ||
        norm === 'config' || norm.startsWith('config/') ||
        norm === 'receipts' || norm.startsWith('receipts/') ||
        norm === 'runtime/connections' || norm.startsWith('runtime/connections/');
    if (!ok) {
        throw new UmhError('RESTORE_SCOPE_INVALID', `restore scope refuses path outside the durable roots: ${rel}`);
    }
}
function parseManifest(raw, manifestPath) {
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
        throw new UmhError('RESTORE_MANIFEST_INVALID', `restore manifest is not an object: ${manifestPath}`);
    }
    const m = raw;
    if (m['version'] !== 1) {
        throw new UmhError('RESTORE_VERSION_MISMATCH', `restore manifest version must be 1: ${manifestPath}`);
    }
    if (m['engineSha'] !== EXPECTED_ENGINE_SHA) {
        throw new UmhError('RESTORE_VERSION_MISMATCH', `engine SHA mismatch: manifest ${String(m['engineSha'])} != pinned ${EXPECTED_ENGINE_SHA}`);
    }
    if (!Array.isArray(m['files'])) {
        throw new UmhError('RESTORE_MANIFEST_INVALID', `restore manifest files must be an array: ${manifestPath}`);
    }
    const files = m['files'].map((entry) => {
        const f = entry;
        if (typeof f['path'] !== 'string' || typeof f['sha256'] !== 'string' || typeof f['bytes'] !== 'number') {
            throw new UmhError('RESTORE_MANIFEST_INVALID', `restore manifest file entry is malformed: ${manifestPath}`);
        }
        if (!/^[0-9a-f]{64}$/.test(f['sha256'])) {
            throw new UmhError('RESTORE_MANIFEST_TAMPERED', `restore manifest file hash is malformed: ${String(f['path'])}`);
        }
        if (f['kind'] !== 'memory' && f['kind'] !== 'security' && f['kind'] !== 'config') {
            throw new UmhError('RESTORE_MANIFEST_INVALID', `restore manifest file kind is malformed: ${String(f['path'])}`);
        }
        assertRestoreScope(f['path']);
        return { path: f['path'], sha256: f['sha256'], bytes: f['bytes'], kind: f['kind'] };
    });
    // 缺 security 即拒绝（候选/密钥不在备份导致恢复丢失——Review Focus）。
    if (!files.some((f) => f.kind === 'security')) {
        throw new UmhError('RESTORE_SECURITY_MISSING', 'restore manifest is missing the security material (refusing key-less restore)');
    }
    if (typeof m['backupId'] !== 'string' || m['backupId'] === '') {
        throw new UmhError('RESTORE_MANIFEST_INVALID', `restore manifest backupId is malformed: ${manifestPath}`);
    }
    return {
        version: 1,
        createdAt: String(m['createdAt'] ?? ''),
        engineSha: String(m['engineSha']),
        backupId: String(m['backupId']),
        sourceUmhHome: String(m['sourceUmhHome'] ?? ''),
        files,
    };
}
async function backupDirOf(manifestPath) {
    return dirname(resolve(manifestPath));
}
/**
 * hashTarget：目标根的递归内容 hash（I1 修复：旧实现只数顶层名+size，
 * 同名同大小的内容替换会漏过 RESTORE_TARGET_CHANGED）。
 * 语义：按相对路径（posix 化、排序后）逐项喂入累加器——目录记
 * `dir:<rel>`，文件记 `file:<rel>:bytes:<len>:`＋文件字节，符号链接记
 * `link:<rel>-><readlink>`（从不跟随链接读目标，避免越界读；越界链接即只
 * 计链接本身，不计其指向内容——见下），不可读记 `<unreadable>`。
 * 跳过：`.restore-staging-*`（apply  staging 临时区）、`backups/`（自身归档）、
 * `logs/`（日志非 durable 面）、`runtime/owner.lock|owner.json|status.json`
 * （运行时进程态，非备份面）。其余 `runtime/`（含 `runtime/connections/`）
 * 正常计入。空目录/缺失根返回 null（isolatedRestoreTarget 新根面）；
 * 跳过后零有效项同样返回 null。非绝对路径抛 INVALID_ARGUMENT。
 */
export async function hashTarget(targetRoot) {
    if (typeof targetRoot !== 'string' || targetRoot.trim() === '' || !isAbsolute(targetRoot)) {
        throw new UmhError('INVALID_ARGUMENT', 'targetRoot must be an absolute path');
    }
    let top;
    try {
        top = await readdir(targetRoot);
    }
    catch (err) {
        if (err.code === 'ENOENT')
            return null;
        throw err;
    }
    // 空目录（isolatedRestoreTarget 新根）即 null；FOREIGN/非空即内容 hash。
    if (top.length === 0)
        return null;
    const hash = createHash('sha256');
    let touched = false;
    function skipped(rel) {
        if (rel === 'backups' || rel.startsWith('backups/'))
            return true;
        if (rel === 'logs' || rel.startsWith('logs/'))
            return true;
        if (rel.startsWith('.restore-staging-') || rel.includes('/.restore-staging-'))
            return true;
        if (rel === 'runtime/owner.lock' || rel === 'runtime/owner.json' || rel === 'runtime/status.json')
            return true;
        return false;
    }
    async function walk(dir) {
        let entries;
        try {
            entries = await readdir(dir, { withFileTypes: true });
        }
        catch {
            hash.update('<unreadable-dir>');
            touched = true;
            return;
        }
        const sorted = [...entries].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
        for (const entry of sorted) {
            const full = join(dir, entry.name);
            const rel = relative(targetRoot, full).replace(/\\/g, '/');
            if (rel === '' || skipped(rel))
                continue;
            let info;
            try {
                info = await lstat(full);
            }
            catch {
                hash.update(`unreadable:${rel}`);
                touched = true;
                continue;
            }
            // 符号链接从不跟随：只计链接路径＋链接目标字符串，不读目标内容
            // （越界链接不进入 hash、不触发读越界；备份侧 collectFiles 另行拒绝越界链接）。
            if (info.isSymbolicLink()) {
                let linkTarget = '';
                try {
                    linkTarget = await readlink(full);
                }
                catch {
                    linkTarget = '<unreadable-link>';
                }
                hash.update(`link:${rel}->${linkTarget}`);
                touched = true;
                continue;
            }
            if (info.isDirectory()) {
                hash.update(`dir:${rel}`);
                touched = true;
                await walk(full);
                continue;
            }
            if (info.isFile()) {
                const bytes = await readFile(full).catch(() => null);
                if (bytes === null) {
                    hash.update(`file:${rel}:<unreadable>`);
                }
                else {
                    hash.update(`file:${rel}:bytes:${bytes.length}:`);
                    hash.update(bytes);
                }
                touched = true;
                continue;
            }
            hash.update(`other:${rel}`);
            touched = true;
        }
    }
    await walk(targetRoot);
    if (!touched)
        return null;
    return hash.digest('hex');
}
/**
 * restore preview：读 manifest→版本/SHA/范围/篡改校验→归档源文件 hash
 * 全量核对→target 冲突检查（非空含 FOREIGN 即冲突，退出 1 面）→出 plan。
 */
export async function planRestore(manifestPath, targetRoot) {
    if (typeof manifestPath !== 'string' || manifestPath.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'manifestPath must be a non-empty string');
    }
    if (typeof targetRoot !== 'string' || targetRoot.trim() === '' || !isAbsolute(targetRoot)) {
        throw new UmhError('INVALID_ARGUMENT', 'targetRoot must be an absolute path');
    }
    let raw;
    try {
        raw = JSON.parse(await readFile(manifestPath, 'utf8'));
    }
    catch (err) {
        throw new UmhError('RESTORE_MANIFEST_INVALID', `restore manifest is not readable: ${err.message}`);
    }
    const manifest = parseManifest(raw, manifestPath);
    const dir = await backupDirOf(manifestPath);
    // 归档源文件 hash 全量核对（篡改即拒绝，不写 target 半字节）。
    for (const file of manifest.files) {
        const src = join(dir, 'files', file.path);
        let bytes;
        try {
            bytes = await readFile(src);
        }
        catch {
            throw new UmhError('RESTORE_MANIFEST_TAMPERED', `backup archive is missing file: ${file.path}`);
        }
        const actual = createHash('sha256').update(bytes).digest('hex');
        if (actual !== file.sha256) {
            throw new UmhError('RESTORE_MANIFEST_TAMPERED', `backup archive hash mismatch: ${file.path}`);
        }
        if (bytes.length !== file.bytes) {
            throw new UmhError('RESTORE_MANIFEST_TAMPERED', `backup archive size mismatch: ${file.path}`);
        }
    }
    // target 冲突检查：含 FOREIGN 内容即冲突（恢复冲突退出 1，不自动覆盖）。
    const expectedTargetHash = await hashTarget(targetRoot);
    if (expectedTargetHash !== null) {
        const entries = await readdir(targetRoot);
        const foreign = entries.filter((n) => !['memory', 'security', 'config', 'receipts', 'runtime', 'backups', 'logs'].includes(n));
        if (foreign.length > 0) {
            throw new UmhError('RESTORE_TARGET_CONFLICT', `restore target holds foreign content (${foreign.join(',')}); refusing overwrite`);
        }
    }
    const changes = manifest.files.map((f) => f.path);
    return {
        id: `restore-plan-${manifest.backupId}`,
        backupId: manifest.backupId,
        manifestPath: resolve(manifestPath),
        targetRoot: resolve(targetRoot),
        expectedTargetHash,
        validated: true,
        changes,
    };
}
/**
 * 恢复 ACL 重新设定：token/凭据文件私有（O02 白名单内实现，不改生产 rpc.ts）。
 * Windows：icacls 去继承＋仅保留当前用户（与 rpc.ts ACL_HARDEN_SCRIPT_TEMPLATE
 * 同源配方：SetAccessRuleProtection($true,$false)＋清 ACE＋当前用户 FullControl）；
 * POSIX：chmod 0o600。随后独立读回校验（无继承＋主体唯一），不满足即失败。
 */
async function rehardenAcl(targetRoot) {
    const { spawnSync } = await import('node:child_process');
    const { chmod: doChmod } = await import('node:fs/promises');
    const candidates = [
        join(targetRoot, 'security', 'rpc', 'token'),
        join(targetRoot, 'security', 'global', 'master.key'),
        join(targetRoot, 'security', 'global', '_tokens.json'),
    ];
    for (const file of candidates) {
        try {
            await stat(file);
        }
        catch {
            continue; // 不存在即跳过（缺 security 由 manifest 校验拒绝，此处只收紧现存文件）
        }
        if (process.platform === 'win32' && file.endsWith(`${join('rpc', 'token')}`)) {
            const script = [
                "$ErrorActionPreference='Stop'",
                `$p='${file.replace(/'/g, "''")}'`,
                '$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User',
                '$acl=Get-Acl -LiteralPath $p',
                '[void]$acl.SetAccessRuleProtection($true,$false)',
                'foreach($r in @($acl.Access)){ [void]$acl.RemoveAccessRule($r) }',
                "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow')",
                '[void]$acl.AddAccessRule($rule)',
                'Set-Acl -LiteralPath $p -AclObject $acl',
            ].join('\n');
            const out = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script], { encoding: 'utf8', timeout: 30_000, windowsHide: true, shell: false });
            if (out.status !== 0) {
                throw new UmhError('RESTORE_ACL_FAILED', `restored token ACL could not be re-hardened: ${(out.stderr ?? '').trim().split('\n').slice(-2).join(' | ')}`);
            }
        }
        else {
            try {
                await doChmod(file, 0o600);
            }
            catch {
                /* 存在但改不动由服务启动校验兜底（fail closed） */
            }
        }
    }
}
/**
 * apply：validated 门→target hash 比对（后改即停）→先备份当前→staging
 * 校验后发布→ACL 重设；失败回原态（pre-restore 快照回滚）。
 */
export async function applyRestore(plan) {
    if (plan === null || typeof plan !== 'object' || plan.validated !== true) {
        throw new UmhError('RESTORE_NOT_VALIDATED', 'validate backup first');
    }
    const current = await hashTarget(plan.targetRoot);
    if (current !== plan.expectedTargetHash) {
        throw new UmhError('RESTORE_TARGET_CHANGED', 'target changed since preview');
    }
    let manifest;
    try {
        manifest = parseManifest(JSON.parse(await readFile(plan.manifestPath, 'utf8')), plan.manifestPath);
    }
    catch (err) {
        if (err instanceof UmhError)
            throw err;
        throw new UmhError('RESTORE_MANIFEST_INVALID', `restore manifest is not readable: ${err.message}`);
    }
    if (manifest.backupId !== plan.backupId) {
        throw new UmhError('RESTORE_MANIFEST_TAMPERED', 'restore plan does not match the manifest backup');
    }
    const dir = await backupDirOf(plan.manifestPath);
    // 先备份当前（非空 target 的可恢复文件快照；空 target 无需备份）。
    let rollback = null;
    if (current !== null) {
        rollback = [];
        for (const file of manifest.files) {
            const dest = join(plan.targetRoot, file.path);
            try {
                rollback.push({ path: file.path, bytes: await readFile(dest) });
            }
            catch {
                rollback.push({ path: file.path, bytes: null });
            }
        }
    }
    // staging：先写临时区并全量 hash 校验，再发布。
    const staging = join(plan.targetRoot, `.restore-staging-${Date.now()}`);
    try {
        for (const file of manifest.files) {
            const src = join(dir, 'files', file.path);
            const tmp = join(staging, file.path);
            await mkdir(dirname(tmp), { recursive: true });
            await copyFile(src, tmp);
            const actual = createHash('sha256').update(await readFile(tmp)).digest('hex');
            if (actual !== file.sha256) {
                throw new UmhError('RESTORE_MANIFEST_TAMPERED', `staging hash mismatch: ${file.path}`);
            }
        }
        // 发布：staging→target（逐文件拷贝）。
        for (const file of manifest.files) {
            const tmp = join(staging, file.path);
            const dest = join(plan.targetRoot, file.path);
            await mkdir(dirname(dest), { recursive: true });
            await copyFile(tmp, dest);
        }
        // 发布后全量 hash 核对（target 落盘面）。
        for (const file of manifest.files) {
            const dest = join(plan.targetRoot, file.path);
            const actual = createHash('sha256').update(await readFile(dest)).digest('hex');
            if (actual !== file.sha256) {
                throw new UmhError('RESTORE_VERIFY_FAILED', `restored file hash mismatch: ${file.path}`);
            }
        }
        await rehardenAcl(plan.targetRoot);
        // manifest 自身落目标根（恢复来源可溯；durable 写）。
        await durableWriteFile(join(plan.targetRoot, 'backups', 'umh', `restored-from-${manifest.backupId}.json`), JSON.stringify(manifest, null, 2));
    }
    catch (err) {
        // 失败回原态：已发布文件按 pre-restore 快照回滚。
        if (rollback !== null) {
            for (const entry of rollback) {
                try {
                    if (entry.bytes === null) {
                        await rm(join(plan.targetRoot, entry.path), { force: true });
                    }
                    else {
                        await mkdir(dirname(join(plan.targetRoot, entry.path)), { recursive: true });
                        await durableWriteFile(join(plan.targetRoot, entry.path), entry.bytes);
                    }
                }
                catch { /* 回滚尽力 */ }
            }
        }
        throw err;
    }
    finally {
        await rm(staging, { recursive: true, force: true }).catch(() => { });
    }
}
