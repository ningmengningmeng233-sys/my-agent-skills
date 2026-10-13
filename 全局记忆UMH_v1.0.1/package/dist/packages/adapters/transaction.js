/**
 * packages/adapters/transaction — 配置事务（apply/remove）与受信绑定文件写入（A01）。
 *
 * 事务链（工单冻结顺序，逐文件执行）：
 *   锁内 beforeHash 检查 → 私有备份（backupOriginal）→ 只改 owned 字段 →
 *   原子发布（publishAtomic）→ receipt。
 *
 * C01 批准文件原语（receipt.ts/owner.ts 同一 MemoraX 模式）：
 *   - 跨进程排他锁：O_EXCL（'wx'）+ 有界重试 + 超龄（60s）让位，锁体
 *     `<umhHome>/config/tx-locks/<sha256(归一化路径)>.lock`，全部 UMH 状态收在
 *     umhHome 之下、不污染客户端目录；
 *   - 原子写：同目录临时文件 + rename（Windows MoveFileEx REPLACE_EXISTING，
 *     读取方只见完整文件）；失败清理临时文件；
 *   - 受保护文件 mode 0o600。
 *
 * 撤销（removeOwnedConfig）做 owned 段三方比对（beforeOwned / afterOwned /
 * 当前值）：用户改过 owned 段 → conflict（CONFIG_CHANGED），绝不整文件还原
 * 备份；无关字段与用户后改内容原样保留（Review Focus A01）。配置文件为
 * symlink/junction 时拒绝（CONFIG_SYMLINK）；目标位于 umhHome 内部（绑定/
 * token/回执等 UMH 自身状态）时拒绝（CONFIG_PATH_FORBIDDEN）。
 *
 * applied 语义（评审修复轮 1）：applyConfigPlan 返回值以**非枚举属性**携带
 * published（本次是否确有发布，幂等跳过不计）。published 不进入冻结 Receipt
 * 字段形状 —— JSON.stringify（持久化）与 parseReceipt（fail-closed 校验）均
 * 不可见；调用方据此推导 applied =「本次发布过内容」（全新安装无备份也 true，
 * 幂等重放 false），不得再用 backupPaths.length 推导。
 *
 * 受信绑定写入路径（C08 裁决，A01 职责）：writeTrustedBindingFile 把完整
 * Binding（含已解析物理 projectId）写入 `<umhHome>/config/bindings/<bindingId>.json`
 * （文件名= bindingId、防遍历校验、逻辑占位 projectId 拒绝、内容幂等）。该函数
 * 只能由受信管理面（apps/cli/connect.ts）调用；applyConfigPlan 本身绝不从
 * plan 数据生成或改写绑定文件 —— 项目绑定不可被客户端请求参数更改。
 */
import { mkdir, open, readFile, rename, rm, stat, writeFile, lstat } from 'node:fs/promises';
import { createHash, randomBytes } from 'node:crypto';
import { basename, dirname, isAbsolute, join } from 'node:path';
import process from 'node:process';
import { UmhError } from '../contracts/index.js';
import { isValidBindingId, PROJECT_ID_PATTERN } from './contracts.js';
import { deletePointer, deepEqual, getPointer, parseConfigChange, parseConfigPlan, sha256Hex, } from './config-plan.js';
import { normalizeWorkspacePath } from '../scope/identity.js';
const CLIENT_IDS = new Set([
    'cherry', 'qoder', 'zcode', 'codex', 'claude', 'minimax', 'dsh', 'generic',
]);
const ROLES = new Set(['reader', 'recorder', 'admin']);
const LOCK_TIMEOUT_MS = 10_000;
const STALE_LOCK_MS = 60_000;
const RETRY_DELAY_MS = 25;
const IS_WIN = process.platform === 'win32';
// ---------------------------------------------------------------------------
// 基础原语
// ---------------------------------------------------------------------------
function sha256Buf(bytes) {
    return createHash('sha256').update(bytes).digest('hex');
}
function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}
async function readFileOrNull(path) {
    try {
        return await readFile(path);
    }
    catch (err) {
        if (err.code === 'ENOENT')
            return null;
        throw new UmhError('CONFIG_PATH_INVALID', `config path is not readable: ${path}`);
    }
}
/**
 * 配置文件只允许普通文件：symlink/junction（lstat 判定）→ CONFIG_SYMLINK，
 * 目录 → CONFIG_PATH_INVALID；不存在（ENOENT，即将新建）→ 放行。
 * 祖先目录中的链接由 preview/apply 双方按同一路径一致解析（读写同源），
 * 最终组件的链接是“改写越界”的直接向量，在此拒绝。
 */
async function assertNotSymlink(path) {
    let info;
    try {
        info = await lstat(path);
    }
    catch (err) {
        if (err.code === 'ENOENT')
            return;
        throw new UmhError('CONFIG_PATH_INVALID', `config path is not inspectable: ${path}`);
    }
    if (info.isSymbolicLink()) {
        throw new UmhError('CONFIG_SYMLINK', `config path is a symlink/junction; refusing to write through it: ${path}`);
    }
    if (!info.isFile()) {
        throw new UmhError('CONFIG_PATH_INVALID', `config path is not a regular file: ${path}`);
    }
}
/** 目标是否位于 umhHome 内部（UMH 自身状态：绑定/token/回执/备份绝不可被计划改写）。 */
function isInsideUmhHome(path, umhHome) {
    const home = normalizeWorkspacePath(umhHome);
    const target = normalizeWorkspacePath(path);
    if (target === home)
        return true;
    const sep = IS_WIN ? '\\' : '/';
    return target.startsWith(home.endsWith(sep) ? home : home + sep);
}
// ---------------------------------------------------------------------------
// 锁（C01 批准模式：O_EXCL + 有界重试 + 超龄让位）
// ---------------------------------------------------------------------------
/** 锁内执行临界区：beforeHash 检查 → 备份 → 原子发布必须原子于其它进程视角。 */
async function withPathLock(umhHome, configPath, fn) {
    const dir = join(umhHome, 'config', 'tx-locks');
    await mkdir(dir, { recursive: true });
    const lockFile = join(dir, `${sha256Hex(normalizeWorkspacePath(configPath))}.lock`);
    const deadline = Date.now() + LOCK_TIMEOUT_MS;
    for (;;) {
        try {
            const handle = await open(lockFile, 'wx');
            try {
                await handle.writeFile(`${process.pid} ${new Date().toISOString()}\n`, 'utf8');
            }
            finally {
                await handle.close().catch(() => { });
            }
            break;
        }
        catch (err) {
            if (err.code !== 'EEXIST') {
                throw new UmhError('TX_LOCK_ERROR', `config transaction lock acquire failed: ${err.message}`);
            }
            let stale = false;
            try {
                stale = Date.now() - (await stat(lockFile)).mtimeMs > STALE_LOCK_MS;
            }
            catch {
                stale = false;
            }
            if (stale) {
                // 超龄让位（不按 PID kill —— 计划 §4：锁身份无法验证 fail closed）。
                await rm(lockFile, { force: true }).catch(() => { });
                continue;
            }
            if (Date.now() >= deadline) {
                throw new UmhError('TX_LOCK_TIMEOUT', 'config transaction lock could not be acquired in time');
            }
            await delay(RETRY_DELAY_MS + Math.floor(Math.random() * 25));
        }
    }
    try {
        return await fn();
    }
    finally {
        await rm(lockFile, { force: true }).catch(() => { });
    }
}
// ---------------------------------------------------------------------------
// 备份与原子发布（transaction.ts 内部函数；调用 C01 批准文件原语）
// ---------------------------------------------------------------------------
/** 私有备份：把当前字节存入 `<umhHome>/backups/client-config/`（mode 0o600）。文件不存在返回 null。 */
async function backupOriginal(path, umhHome) {
    const bytes = await readFileOrNull(path);
    if (bytes === null)
        return null;
    const dir = join(umhHome, 'backups', 'client-config');
    await mkdir(dir, { recursive: true });
    const stem = sha256Hex(normalizeWorkspacePath(path)).slice(0, 8);
    let lastError = null;
    for (let attempt = 0; attempt < 3; attempt++) {
        const backupPath = join(dir, `${basename(path)}.${stem}.${new Date().toISOString().replace(/[:.]/g, '-')}.${randomBytes(4).toString('hex')}.bak`);
        try {
            await writeFile(backupPath, bytes, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
            return backupPath;
        }
        catch (err) {
            lastError = err;
            if (err.code !== 'EEXIST')
                break;
        }
    }
    throw new UmhError('CONFIG_BACKUP_FAILED', `backup write failed for ${path}: ${lastError?.message ?? 'unknown'}`);
}
/** 原子发布：同目录临时文件 + rename；保留原文件 mode（新建按 0o600）。 */
async function publishAtomic(path, afterBytes) {
    const tmp = join(dirname(path), `.${basename(path)}.umh-${randomBytes(6).toString('hex')}.tmp`);
    let mode;
    try {
        mode = (await stat(path)).mode | 0o200; // 替换文件保持可写（只读位不复制）
    }
    catch {
        mode = 0o600; // 新建文件按受保护形态落地
    }
    try {
        await writeFile(tmp, afterBytes, { encoding: 'utf8', mode });
        // 同目录 rename：Windows 走 MoveFileEx REPLACE_EXISTING，读取方只见完整文件。
        await rename(tmp, path);
    }
    catch (err) {
        await rm(tmp, { force: true }).catch(() => { });
        throw new UmhError('CONFIG_PUBLISH_FAILED', `atomic publish failed (${path}): ${err.message}`);
    }
}
// ---------------------------------------------------------------------------
// apply
// ---------------------------------------------------------------------------
/**
 * 从 plan.launch.args 提取 `--umh-home`（makeLaunchSpec 冻结形态保证存在）。
 * 导出供各端 adapter 复用（apply 的回执落盘点与事务同源；A03 修复轮 1 DRY）。
 */
export function umhHomeOfPlan(plan) {
    const args = plan.launch.args;
    const idx = args.indexOf('--umh-home');
    if (idx < 0 || idx + 1 >= args.length || args[idx + 1].trim() === '') {
        throw new UmhError('PLAN_INVALID', 'plan.launch.args must carry --umh-home (makeLaunchSpec contract)');
    }
    return args[idx + 1];
}
/**
 * 应用配置计划：逐文件（锁内）核验 beforeHash → 幂等跳过 / 备份 → 原子发布。
 * beforeHash 不符（preview 之后文件被改）→ CONFIG_CHANGED，绝不覆盖用户内容。
 * 计划形状经 parseConfigPlan 收敛校验（PLAN_INVALID fail-closed）。
 * 返回值以非枚举属性携带 published（本次是否确有发布）—— 见模块头注释
 * 「applied 语义」；幂等跳过不计入 published。
 * 注意：多文件计划在第 N 个文件失败时前 N-1 个已发布（无跨文件两阶段）；
 * 恢复路径 = 重新 connect --dry-run 生成反映当前状态的新计划。
 */
export async function applyConfigPlan(plan) {
    const checked = parseConfigPlan(plan);
    const umhHome = checked.changes.length === 0 ? null : umhHomeOfPlan(checked);
    const backupPaths = [];
    let published = false;
    for (const change of checked.changes) {
        if (umhHome !== null && isInsideUmhHome(change.path, umhHome)) {
            throw new UmhError('CONFIG_PATH_FORBIDDEN', `plan may not rewrite UMH state inside the umh home: ${change.path}`);
        }
        await withPathLock(umhHome, change.path, async () => {
            await assertNotSymlink(change.path);
            const current = await readFileOrNull(change.path);
            const currentHash = current === null ? null : sha256Buf(current);
            // 幂等优先：已处于目标状态（重复 apply / 计划重放）→ 无备份、无写盘、未发布。
            if (currentHash !== null && currentHash === change.afterHash) {
                return;
            }
            // 此后才谈“陈旧”：文件既不是目标状态、也不匹配 preview 时的状态。
            if (currentHash !== change.beforeHash) {
                throw new UmhError('CONFIG_CHANGED', `preview is stale: ${change.path} changed since preview`);
            }
            const backup = await backupOriginal(change.path, umhHome);
            if (backup !== null)
                backupPaths.push(backup);
            await publishAtomic(change.path, change.afterBytes);
            published = true;
        });
    }
    const receipt = {
        id: `rcpt_${randomBytes(16).toString('hex')}`,
        clientId: checked.clientId,
        bindingId: checked.bindingId,
        changes: checked.changes,
        backupPaths,
    };
    // 非枚举附着：冻结 Receipt 字段形状不动（持久化 JSON 与 parseReceipt 的
    // Object.keys 视野都不含 published），A02+ 调用方无需剥离即可安全落盘回执。
    return Object.defineProperty(receipt, 'published', {
        value: published,
        enumerable: false,
    });
}
// ---------------------------------------------------------------------------
// remove（撤销：只删 UMH owned 字段；绝不整文件还原备份）
// ---------------------------------------------------------------------------
/** 从回执 changes 的 afterOwned（{command,args,env}）推导 umhHome（锁与备份所需）。 */
function umhHomeOfReceipt(receipt) {
    for (const change of receipt.changes) {
        const after = change.afterOwned;
        if (after !== null && typeof after === 'object' && !Array.isArray(after)) {
            const args = after.args;
            if (Array.isArray(args)) {
                const idx = args.indexOf('--umh-home');
                if (idx >= 0 && idx + 1 < args.length && typeof args[idx + 1] === 'string' && args[idx + 1] !== '') {
                    return args[idx + 1];
                }
            }
        }
    }
    throw new UmhError('RECEIPT_INVALID', 'receipt does not carry --umh-home in any change; cannot lock or back up safely');
}
function serializeDoc(doc, originalEndsWithNewline) {
    return JSON.stringify(doc, null, 2) + (originalEndsWithNewline ? '\n' : '');
}
/**
 * 撤销 UMH 配置：逐文件做 owned 段三方比对（beforeOwned / afterOwned / 当前值）。
 * - 当前值 === afterOwned → 外科手术删除 owned 键（先备份；无关内容原样）；
 * - 当前值 === beforeOwned（用户已还原）→ 无需动作；
 * - 其它（用户改过 owned 段）→ conflict，绝不触碰该文件；
 * - 文件由 UMH 新建（beforeHash null）且未被用户改动 → 整文件移除；
 * - 文件缺失/已还原 → 视为已移除（幂等）。
 * 任何 conflict → removed:false；UMH 字段之外的一切内容不被本函数改动。
 */
export async function removeOwnedConfig(receipt) {
    const checked = parseReceipt(receipt);
    const conflicts = [];
    const umhHome = checked.changes.length === 0 ? null : umhHomeOfReceipt(checked);
    for (const change of checked.changes) {
        if (change.format !== 'json') {
            conflicts.push(`${change.path}: surgical removal of '${change.format}' is not supported; edit manually to drop ${change.ownedPointer} (MANUAL_REMOVAL_REQUIRED)`);
            continue;
        }
        if (umhHome !== null && isInsideUmhHome(change.path, umhHome)) {
            conflicts.push(`${change.path}: refusing to operate inside the umh home (CONFIG_PATH_FORBIDDEN)`);
            continue;
        }
        await withPathLock(umhHome, change.path, async () => {
            await assertNotSymlink(change.path);
            const bytes = await readFileOrNull(change.path);
            if (bytes === null)
                return; // 文件已不在 → owned 段必然已移除（幂等）
            const text = bytes.toString('utf8');
            let doc;
            try {
                doc = JSON.parse(text);
            }
            catch {
                conflicts.push(`${change.path}: config is no longer valid JSON; refusing to edit (CONFIG_UNPARSEABLE)`);
                return;
            }
            if (doc === null || typeof doc !== 'object' || Array.isArray(doc)) {
                conflicts.push(`${change.path}: config is not a JSON object; refusing to edit (CONFIG_UNPARSEABLE)`);
                return;
            }
            // UMH 创建的文件且自 apply 后未被任何改动 → 整文件移除（先备份留证）。
            if (change.beforeHash === null && sha256Buf(bytes) === change.afterHash) {
                await backupOriginal(change.path, umhHome);
                await rm(change.path, { force: false });
                return;
            }
            const currentOwned = getPointer(doc, change.ownedPointer) ?? null;
            if (deepEqual(currentOwned, change.afterOwned)) {
                await backupOriginal(change.path, umhHome);
                deletePointer(doc, change.ownedPointer);
                await publishAtomic(change.path, serializeDoc(doc, text.endsWith('\n')));
                return;
            }
            if (deepEqual(currentOwned, change.beforeOwned)) {
                return; // 用户已还原为安装前状态：UMH 字段已不在，无需动作
            }
            conflicts.push(`${change.path}: owned segment ${change.ownedPointer} was modified after installation (CONFIG_CHANGED)`);
        }).catch((err) => {
            if (err instanceof UmhError) {
                conflicts.push(`${change.path}: [${err.code}] ${err.message}`);
                return;
            }
            throw err;
        });
    }
    return { removed: conflicts.length === 0, conflicts };
}
// ---------------------------------------------------------------------------
// 受信绑定写入路径（C08 裁决：A01 职责）
// ---------------------------------------------------------------------------
/** 校验绑定形状；逻辑占位 projectId / 越界 bindingId / 未知角色一律拒绝。 */
function validateBindingShape(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        throw new UmhError('BINDING_INVALID', 'binding must be a JSON object');
    }
    const obj = value;
    if (!CLIENT_IDS.has(String(obj['clientId']))) {
        throw new UmhError('BINDING_INVALID', `binding has unknown clientId: ${String(obj['clientId'])}`);
    }
    if (!ROLES.has(String(obj['role']))) {
        throw new UmhError('BINDING_INVALID', 'binding role must be reader|recorder|admin');
    }
    if (!isValidBindingId(obj['bindingId'])) {
        throw new UmhError('INVALID_BINDING_ID', `binding id is not a safe file name: ${String(obj['bindingId'])}`);
    }
    const binding = {
        clientId: obj['clientId'],
        role: obj['role'],
        bindingId: obj['bindingId'],
    };
    if (obj['projectId'] !== undefined && obj['projectId'] !== null) {
        if (typeof obj['projectId'] !== 'string' || !PROJECT_ID_PATTERN.test(obj['projectId'])) {
            // C08 裁决：绑定文件必须携带已解析物理 projectId；逻辑占位 id 在写入时拒绝。
            throw new UmhError('INVALID_PROJECT_ID', `binding projectId must be 'prj_' followed by 64 lowercase hex characters (resolve the workspace first)`);
        }
        binding.projectId = obj['projectId'];
    }
    if (obj['workspace'] !== undefined && obj['workspace'] !== null) {
        if (typeof obj['workspace'] !== 'string' || obj['workspace'].trim() === '') {
            throw new UmhError('BINDING_INVALID', 'binding workspace must be a non-empty string');
        }
        binding.workspace = obj['workspace'];
    }
    return binding;
}
/**
 * 受信绑定写入（C08 读取侧契约）：完整 Binding（含已解析物理 projectId）原子
 * 落盘 `<umhHome>/config/bindings/<bindingId>.json`（mode 0o600；内容与既有一致
 * 时跳过 → 幂等）。只能由受信管理面调用；绑定内容绝不来自客户端请求参数。
 */
export async function writeTrustedBindingFile(umhHome, binding) {
    if (typeof umhHome !== 'string' || umhHome.trim() === '' || !isAbsolute(umhHome)) {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be an absolute path');
    }
    const checked = validateBindingShape(binding);
    const dir = join(umhHome, 'config', 'bindings');
    await mkdir(dir, { recursive: true });
    const file = join(dir, `${checked.bindingId}.json`);
    const content = `${JSON.stringify(checked, null, 2)}\n`;
    const existing = await readFileOrNull(file);
    if (existing !== null && existing.toString('utf8') === content) {
        return file; // 幂等：内容一致不重写
    }
    await writeProtectedJson(file, checked);
    return file;
}
/** 撤下受信绑定文件（完整移除成功后由管理面调用）；返回是否确有文件被删。 */
export async function deleteTrustedBindingFile(umhHome, bindingId) {
    if (typeof umhHome !== 'string' || umhHome.trim() === '' || !isAbsolute(umhHome)) {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be an absolute path');
    }
    if (!isValidBindingId(bindingId)) {
        throw new UmhError('INVALID_BINDING_ID', `binding id is not a safe file name: ${String(bindingId)}`);
    }
    const file = join(umhHome, 'config', 'bindings', `${bindingId}.json`);
    const existed = (await readFileOrNull(file)) !== null;
    await rm(file, { force: true });
    return existed;
}
// ---------------------------------------------------------------------------
// 回执持久化（adapter 底座共用；CLI 另有 <plan>.receipt.json 落盘路径）
// ---------------------------------------------------------------------------
/** 校验并收敛磁盘上加载的 Receipt（未知字段/形状不符 → RECEIPT_INVALID）。 */
export function parseReceipt(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        throw new UmhError('RECEIPT_INVALID', 'receipt must be a JSON object');
    }
    const r = value;
    const allowed = new Set(['id', 'clientId', 'bindingId', 'changes', 'backupPaths']);
    for (const key of Object.keys(r)) {
        if (!allowed.has(key))
            throw new UmhError('RECEIPT_INVALID', `unknown receipt field: ${key}`);
    }
    if (typeof r['id'] !== 'string' || r['id'] === '')
        throw new UmhError('RECEIPT_INVALID', 'receipt id must be a non-empty string');
    if (!CLIENT_IDS.has(String(r['clientId'])))
        throw new UmhError('RECEIPT_INVALID', `unknown receipt clientId: ${String(r['clientId'])}`);
    if (!isValidBindingId(r['bindingId']))
        throw new UmhError('RECEIPT_INVALID', 'receipt bindingId is not a safe file name');
    if (!Array.isArray(r['changes']))
        throw new UmhError('RECEIPT_INVALID', 'receipt changes must be an array');
    if (!Array.isArray(r['backupPaths']) || r['backupPaths'].some((p) => typeof p !== 'string')) {
        throw new UmhError('RECEIPT_INVALID', 'receipt backupPaths must be an array of strings');
    }
    return {
        id: r['id'],
        clientId: r['clientId'],
        bindingId: r['bindingId'],
        changes: r['changes'].map((raw) => parseConfigChange(raw)),
        backupPaths: r['backupPaths'],
    };
}
/** 受保护 JSON 落盘（原子；mode 0o600）。ConfigPlan/Receipt/绑定文件共用。 */
export async function writeProtectedJson(path, value) {
    await mkdir(dirname(path), { recursive: true });
    const tmp = join(dirname(path), `.${basename(path)}.${randomBytes(6).toString('hex')}.tmp`);
    try {
        await writeFile(tmp, `${JSON.stringify(value, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
        await rename(tmp, path);
    }
    catch (err) {
        await rm(tmp, { force: true }).catch(() => { });
        throw new UmhError('CONFIG_PUBLISH_FAILED', `protected json write failed (${path}): ${err.message}`);
    }
}
/** 回执落盘到 `<umhHome>/receipts/adapters/<id>.json`（adapter 底座共用查找点）。 */
export async function saveReceipt(umhHome, receipt) {
    const file = join(umhHome, 'receipts', 'adapters', `${receipt.id}.json`);
    await writeProtectedJson(file, receipt);
    return file;
}
/** 按 receiptId 从 umhHome 底座查找点加载回执。 */
export async function loadReceiptById(umhHome, receiptId) {
    if (typeof umhHome !== 'string' || umhHome.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be a non-empty string');
    }
    if (typeof receiptId !== 'string' || !/^rcpt_[0-9a-f]{32}$/.test(receiptId)) {
        throw new UmhError('RECEIPT_INVALID', `receipt id has an unexpected shape: ${String(receiptId)}`);
    }
    return loadReceiptFile(join(umhHome, 'receipts', 'adapters', `${receiptId}.json`));
}
/** 从显式路径加载回执（CLI disconnect --receipt）。 */
export async function loadReceiptFile(path) {
    const bytes = await readFileOrNull(path);
    if (bytes === null)
        throw new UmhError('RECEIPT_UNKNOWN', `receipt file not found: ${path}`);
    let parsed;
    try {
        parsed = JSON.parse(bytes.toString('utf8'));
    }
    catch (err) {
        throw new UmhError('RECEIPT_INVALID', `receipt file is not valid JSON (${path}): ${err.message}`);
    }
    return parseReceipt(parsed);
}
