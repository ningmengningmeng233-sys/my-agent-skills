/**
 * packages/ops/backup — O02 一致备份（GREEN 实现）。
 *
 * 工单 O02 L95：实现拒绝新写并 drain→flush→完整 snapshot→hash/manifest→
 * 恢复服务；异常也解除 maintenance。归档拒绝绝对/../ADS 及越界链接，
 * 不把 backup 递归打入自身。备份配额轮转（R05 A01-M2：保留最近 N 个，
 * 无界增长拒绝或轮转——本实现保留最近 BACKUP_KEEP 个，超量删最旧）。
 *
 * 备份纳入（Global Constraints + O02 必交付对照 + R05 派单）：
 *   memory（memory/global + memory/projects/**：queue/active/superseded/
 *     trash/keys/_audit/_index_log/hippocampus 审核历史/删除历史；不含 runtime
 *     进程态：owner.lock/owner.json/status.json、PID/锁、旧进程 nonce 不备份，
 *     恢复后不复用——补充计划 O02 行）；
 *   security（security/**：凭据根 master.key/_tokens.json/token/aux state+data；
 *     恢复后 ACL 重新设定——token 私有 0o600/Windows ACL）；
 *   config（config/bindings 安装绑定 hash 验证 + runtime/connections 安装引用
 *     ConnectionRecord + receipts/** 回执八字段 + config/tx-locks；
 *     R04 回执引用＋hash 形状：connection records 的 bindingHash/planHash/
 *     receiptId/receiptPath 原样纳入备份）。
 * backups/umh 自身、logs（不存在亦不建）、runtime 进程态绝不纳入。
 *
 * Maintenance 语义：「拒绝新写」由宿主实现（CLI 经 owner 锁：服务在线→
 * fail-closed 拒绝 ALREADY_RUNNING；离线→锁在手、umh start 同被拒，零写者
 * 离线快照才配 consistent:true）；本模块的备份排他锁（backups/umh/.backup.lock：
 * 并发备份串行化，异常也释放）；drainAndFlush 由宿主提供（离线无内存态可排
 * 空）；snapshot 指拷贝期源文件集合冻结。拷贝核对：每文件 copy 后对源与副本
 * 各算 sha256，不一致即 BACKUP_COPY_MISMATCH 并删除半成品——manifest 只描述
 * 已核对副本，绝不发布未核对快照。
 */
import { createHash, randomBytes } from 'node:crypto';
import { copyFile, mkdir, open, readdir, readFile, readlink, rename, rm } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { UmhError } from '../contracts/index.js';
/** 备份配额：保留最近 N 个（R05 A01-M2 无界增长→轮转）。 */
export const BACKUP_KEEP = 3;
/** 备份根（umhHome 内；归档自身排除面）。 */
export function backupRootOf(umhHome) {
    return join(umhHome, 'backups', 'umh');
}
/** 归档内相对路径合法性（拒绝绝对/../ADS 及越界链接目标）。 */
export function assertSafeArchivePath(rel) {
    if (typeof rel !== 'string' || rel === '') {
        throw new UmhError('BACKUP_PATH_INVALID', 'archive path must be a non-empty string');
    }
    if (isAbsolute(rel)) {
        throw new UmhError('BACKUP_PATH_ESCAPE', `absolute archive path is refused: ${rel}`);
    }
    const parts = rel.split(/[\\/]/);
    if (parts.some((p) => p === '..' || p === '' || p === '.')) {
        throw new UmhError('BACKUP_PATH_ESCAPE', `dot-dot archive path is refused: ${rel}`);
    }
    if (/:/.test(rel)) {
        throw new UmhError('BACKUP_PATH_ESCAPE', `ADS/drive archive path is refused: ${rel}`);
    }
}
/** 备份纳入根（相对 umhHome）：memory/security/config 面 + receipts/connection 台账。 */
const INCLUDE_ROOTS = Object.freeze(['memory', 'security', 'config', 'receipts']);
/** 备份排除（相对路径前缀）：自身归档/runtime 进程态/logs（runtime/connections 除外）。 */
function excludedRel(rel) {
    const norm = rel.replace(/\\/g, '/');
    if (norm === 'backups' || norm.startsWith('backups/'))
        return true;
    if (norm === 'runtime/connections' || norm.startsWith('runtime/connections/'))
        return false;
    if (norm === 'runtime' || norm.startsWith('runtime/'))
        return true;
    if (norm === 'logs' || norm.startsWith('logs/'))
        return true;
    return false;
}
function kindOf(rel) {
    const norm = rel.replace(/\\/g, '/');
    if (norm === 'memory' || norm.startsWith('memory/'))
        return 'memory';
    if (norm === 'security' || norm.startsWith('security/'))
        return 'security';
    return 'config';
}
async function sha256File(path) {
    const bytes = await readFile(path);
    return { sha256: createHash('sha256').update(bytes).digest('hex'), bytes: bytes.length };
}
/** fsync 落盘写（C06-M5：receipt 落盘无 fsync → 本备份路径 durable 写）。 */
export async function durableWriteFile(path, data) {
    await mkdir(dirname(path), { recursive: true });
    const tmp = join(dirname(path), `.o02-${randomBytes(6).toString('hex')}.tmp`);
    const handle = await open(tmp, 'w', 0o600);
    try {
        await handle.writeFile(data);
        await handle.sync();
    }
    finally {
        await handle.close().catch(() => { });
    }
    await rename(tmp, path);
}
async function collectFiles(umhHome) {
    const out = [];
    async function walk(dir) {
        let entries;
        try {
            entries = await readdir(dir, { withFileTypes: true });
        }
        catch (err) {
            if (err.code === 'ENOENT')
                return;
            throw err;
        }
        for (const entry of entries) {
            const full = join(dir, entry.name);
            const rel = relative(umhHome, full);
            if (excludedRel(rel))
                continue;
            if (entry.isSymbolicLink()) {
                // umhHome 耐久根内一律拒绝符号链接（无论目标内外）：fail-closed，
                // in-root 链接在 umhHome 内近乎不存在，无需走 referent 字节面。
                throw new UmhError('BACKUP_PATH_ESCAPE', `symlink inside durable roots is refused: ${rel}`);
            }
            if (entry.isDirectory()) {
                await walk(full);
            }
            else if (entry.isFile()) {
                assertSafeArchivePath(rel);
                // 越界链接：实路径必须仍在 umhHome 内。
                const real = await resolveSymlinkTarget(full);
                if (real !== null) {
                    const relReal = relative(resolve(umhHome), resolve(real));
                    if (relReal === '' || relReal.startsWith('..') || isAbsolute(relReal)) {
                        throw new UmhError('BACKUP_PATH_ESCAPE', `out-of-root symlink is refused: ${rel}`);
                    }
                }
                out.push(rel);
            }
        }
    }
    for (const root of INCLUDE_ROOTS) {
        await walk(join(umhHome, root));
    }
    // runtime/connections 是 config kind（安装引用面），单独纳入（runtime 其余仍排除）。
    await walk(join(umhHome, 'runtime', 'connections'));
    out.sort();
    return out;
}
async function resolveSymlinkTarget(path) {
    try {
        const link = await readlink(path);
        return resolve(dirname(path), link);
    }
    catch {
        return null;
    }
}
function engineShaOf() {
    // UPSTREAM-PIN dsh-memory pinnedSha（引擎版本真源；恢复时版本/SHA 不符即拒绝）。
    return 'bfbdcb923da1ef2880dd3f9f979cf419d9211eb0';
}
async function withBackupLock(umhHome, fn) {
    const lockFile = join(backupRootOf(umhHome), '.backup.lock');
    await mkdir(dirname(lockFile), { recursive: true });
    let handle = null;
    try {
        handle = await open(lockFile, 'wx', 0o600);
        await handle.writeFile(`${process.pid}\n`, 'utf8');
    }
    catch (err) {
        await handle?.close().catch(() => { });
        throw new UmhError('BACKUP_BUSY', `another backup is running: ${err.message}`);
    }
    try {
        return await fn();
    }
    finally {
        await handle.close().catch(() => { });
        await rm(lockFile, { force: true }).catch(() => { });
    }
}
async function rotateBackups(umhHome) {
    const root = backupRootOf(umhHome);
    let entries;
    try {
        entries = (await readdir(root)).filter((n) => n !== '.backup.lock').sort();
    }
    catch {
        return;
    }
    // 无 manifest 的残骸不是发布面（发布面=带 manifest 的完整归档；rotation 持
    // 备份锁运行、无并发备份，残骸只来自崩溃/失败路径，回收安全），先回收残骸
    // 再对有效归档做配额轮转——否则失败残留永远无法被配额回收。
    const published = [];
    for (const entry of entries) {
        let hasManifest = false;
        try {
            await readFile(join(root, entry, 'manifest.json'));
            hasManifest = true;
        }
        catch {
            hasManifest = false;
        }
        if (hasManifest) {
            published.push(entry);
        }
        else {
            await rm(join(root, entry), { recursive: true, force: true }).catch(() => { });
        }
    }
    while (published.length > BACKUP_KEEP) {
        const oldest = published.shift();
        // 旧归档删除失败（外部占用等）不得冒泡：外层 catch 会把刚核对完的新备份
        // 连带清掉——配额是尽力面，下次轮转重试。
        await rm(join(root, oldest), { recursive: true, force: true }).catch(() => { });
    }
}
/**
 * 一致备份：备份排他锁→drainAndFlush（宿主）→snapshot 拷贝（每文件源/副本
 * hash 核对，不一致拒发）→hash/manifest 落盘→配额轮转；异常也释放锁、
 * 解除宿主维护（finally）并清理半成品归档。
 */
export async function createBackup(service) {
    const umhHome = service.umhHome;
    if (typeof umhHome !== 'string' || umhHome.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be a non-empty string');
    }
    let maintenance = false;
    try {
        await service.beginMaintenance?.();
        maintenance = true;
        await service.drainAndFlush?.();
        const result = await withBackupLock(umhHome, async () => {
            const backupId = `backup-${new Date().toISOString().replace(/[:.]/g, '-')}-${randomBytes(4).toString('hex')}`;
            const destRoot = join(backupRootOf(umhHome), backupId);
            try {
                const rels = await collectFiles(umhHome);
                const files = [];
                for (const rel of rels) {
                    assertSafeArchivePath(rel);
                    const src = join(umhHome, rel);
                    const dest = join(destRoot, 'files', rel);
                    await mkdir(dirname(dest), { recursive: true });
                    await copyFile(src, dest);
                    // 拷贝核对：源与副本各算 hash，不一致（并发变更/拷贝损坏面）即拒发。
                    const srcHash = await sha256File(src);
                    const destHash = await sha256File(dest);
                    if (srcHash.sha256 !== destHash.sha256 || srcHash.bytes !== destHash.bytes) {
                        throw new UmhError('BACKUP_COPY_MISMATCH', `backup copy diverges from the source while copying ${rel}; refusing to publish an unverified snapshot`);
                    }
                    files.push({ path: rel.replace(/\\/g, '/'), sha256: destHash.sha256, bytes: destHash.bytes, kind: kindOf(rel) });
                }
                if (!files.some((f) => f.kind === 'security')) {
                    throw new UmhError('BACKUP_INCOMPLETE', 'backup is missing the security material (refusing incomplete snapshot)');
                }
                const manifest = {
                    version: 1,
                    createdAt: new Date().toISOString(),
                    engineSha: engineShaOf(),
                    backupId,
                    sourceUmhHome: umhHome,
                    files,
                };
                const manifestPath = join(destRoot, 'manifest.json');
                await durableWriteFile(manifestPath, JSON.stringify(manifest, null, 2));
                await rotateBackups(umhHome);
                return { backupId, manifestPath, consistent: true };
            }
            catch (err) {
                // 半成品不得以任何形式残留（发布面只有带 manifest 的完整归档）。
                await rm(destRoot, { recursive: true, force: true }).catch(() => { });
                throw err;
            }
        });
        return result;
    }
    finally {
        if (maintenance) {
            await service.endMaintenance?.().catch(() => { });
        }
    }
}
