/**
 * packages/adapters/config-plan — 配置计划（preview）构造（A01）。
 *
 * preview 只读合同：本模块绝不写盘 —— 计划只计算 afterBytes/afterHash，落盘由
 * transaction.applyConfigPlan 在锁内经 beforeHash 核验后原子执行。
 *
 * owned 段不可覆盖他人内容（Review Focus A01）：pointer 处已有非 UMH 内容时
 * 计划失败（OWNED_POINTER_OCCUPIED），绝不静默改写第三方字段。
 *
 * 格式纪律（A 阶段全局约束）：JSON 用原生解析（字段保留可证明，键序由
 * JSON.parse 保持）；TOML/YAML 的固定版解析器无法证明注释/格式保留 → 只输出
 * 手工片段（manualInstructions），绝不做破坏性 stringify。七端各自的精确检出
 * 与配方由 A02+ 的 adapter 提供。
 */
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { isAbsolute } from 'node:path';
import { randomBytes } from 'node:crypto';
import { UmhError } from '../contracts/index.js';
import { isValidBindingId } from './contracts.js';
/** 通用 JSON 客户端默认 owned 段：mcpServers 命名空间下的 umh-memory 服务项。 */
export const DEFAULT_OWNED_POINTER = '/mcpServers/umh-memory';
const CLIENT_IDS = new Set([
    'cherry', 'qoder', 'zcode', 'codex', 'claude', 'minimax', 'dsh', 'generic',
]);
/** SHA256 hex（UTF-8 字符串）。 */
export function sha256Hex(text) {
    return createHash('sha256').update(text, 'utf8').digest('hex');
}
/** JSON 值深比较（键序不敏感；数组有序）。 */
export function deepEqual(a, b) {
    if (a === b)
        return true;
    if (typeof a !== typeof b)
        return false;
    if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object')
        return false;
    if (Array.isArray(a) !== Array.isArray(b))
        return false;
    if (Array.isArray(a) && Array.isArray(b)) {
        if (a.length !== b.length)
            return false;
        return a.every((item, i) => deepEqual(item, b[i]));
    }
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    if (ka.length !== kb.length)
        return false;
    return ka.every((key) => Object.prototype.hasOwnProperty.call(b, key) &&
        deepEqual(a[key], b[key]));
}
/** JSON Pointer（RFC 6901）段解析；仅支持对象键（数组下标不支持 → 显式报错）。 */
function pointerSegments(pointer) {
    if (typeof pointer !== 'string' || !pointer.startsWith('/')) {
        throw new UmhError('OWNED_POINTER_INVALID', `owned pointer must be a JSON pointer like '/mcpServers/umh-memory'`);
    }
    return pointer
        .split('/')
        .slice(1)
        .map((seg) => seg.replace(/~1/g, '/').replace(/~0/g, '~'));
}
/** 读取 JSON Pointer 指向的值；不存在返回 undefined。 */
export function getPointer(doc, pointer) {
    let cur = doc;
    for (const seg of pointerSegments(pointer)) {
        if (cur === null || typeof cur !== 'object' || Array.isArray(cur))
            return undefined;
        cur = cur[seg];
    }
    return cur;
}
/** 写入 JSON Pointer（中间对象按需创建；类型冲突显式报错）。 */
export function setPointer(doc, pointer, value) {
    const segs = pointerSegments(pointer);
    let cur = doc;
    for (let i = 0; i < segs.length - 1; i++) {
        const seg = segs[i];
        const next = cur[seg];
        if (next === undefined) {
            const created = {};
            cur[seg] = created;
            cur = created;
        }
        else if (next !== null && typeof next === 'object' && !Array.isArray(next)) {
            cur = next;
        }
        else {
            throw new UmhError('OWNED_POINTER_INVALID', `owned pointer ${pointer} crosses a non-object value at '${seg}'`);
        }
    }
    cur[segs[segs.length - 1]] = value;
}
/** 删除 JSON Pointer 指向的键（父容器保留）；返回是否确有删除。 */
export function deletePointer(doc, pointer) {
    const segs = pointerSegments(pointer);
    let cur = doc;
    for (let i = 0; i < segs.length - 1; i++) {
        if (cur === null || typeof cur !== 'object' || Array.isArray(cur))
            return false;
        cur = cur[segs[i]];
    }
    if (cur === null || typeof cur !== 'object' || Array.isArray(cur))
        return false;
    const parent = cur;
    if (!Object.prototype.hasOwnProperty.call(parent, segs[segs.length - 1]))
        return false;
    delete parent[segs[segs.length - 1]];
    return true;
}
function requireLaunchShape(launch) {
    if (launch === null || typeof launch !== 'object' || Array.isArray(launch)) {
        throw new UmhError('INVALID_ARGUMENT', 'launch must be a LaunchSpec object');
    }
    const l = launch;
    if (typeof l.command !== 'string' || l.command === '') {
        throw new UmhError('INVALID_ARGUMENT', 'launch.command must be a non-empty string');
    }
    if (!Array.isArray(l.args) || l.args.some((a) => typeof a !== 'string')) {
        throw new UmhError('INVALID_ARGUMENT', 'launch.args must be an array of strings');
    }
    if (l.env === undefined || l.env === null || typeof l.env !== 'object' || Array.isArray(l.env)) {
        throw new UmhError('INVALID_ARGUMENT', 'launch.env must be a Record<string,string>');
    }
    for (const [k, v] of Object.entries(l.env)) {
        if (typeof v !== 'string') {
            throw new UmhError('INVALID_ARGUMENT', `launch.env.${k} must be a string`);
        }
    }
    return { command: l.command, args: [...l.args], env: { ...l.env } };
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
/** launch 对象在客户端配置中的落地形态（与 LaunchSpec 同源，字段一一对应）。 */
function launchEntry(launch) {
    return { command: launch.command, args: [...launch.args], env: { ...launch.env } };
}
function manualFragment(path, pointer, launch, format) {
    const where = path === null ? 'the client configuration' : path;
    return (`UMH did not auto-edit ${where} (format '${format}' cannot prove unrelated-field/comment preservation). ` +
        `Manually add an MCP stdio server named 'umh-memory' at ${pointer} with exactly: ` +
        `command=${launch.command} args=${JSON.stringify(launch.args)} env=${JSON.stringify(launch.env)}`);
}
/** 为单个 JSON 配置文件构造 owned 段变更。 */
async function planJsonChange(path, pointer, launch) {
    if (!isAbsolute(path)) {
        throw new UmhError('PLAN_INVALID', `detection.configPaths entries must be absolute paths: ${path}`);
    }
    const bytes = await readFileOrNull(path);
    const beforeHash = bytes === null ? null : createHash('sha256').update(bytes).digest('hex');
    let doc = {};
    if (bytes !== null) {
        try {
            const parsed = JSON.parse(bytes.toString('utf8'));
            if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
                throw new UmhError('CONFIG_UNPARSEABLE', `config is not a JSON object: ${path}`);
            }
            doc = parsed;
        }
        catch (err) {
            if (err instanceof UmhError)
                throw err;
            throw new UmhError('CONFIG_UNPARSEABLE', `config is not valid JSON: ${path}`);
        }
    }
    const beforeOwned = getPointer(doc, pointer) ?? null;
    const desired = launchEntry(launch);
    // owned 段纪律（Review Focus A01）：pointer 处已有内容且与本次将写内容不同
    // → 拒绝计划（可能是用户或其它工具的服务项，绝不覆盖第三方内容）。
    if (beforeOwned !== null && !deepEqual(beforeOwned, desired)) {
        throw new UmhError('OWNED_POINTER_OCCUPIED', `${path} already has other content at ${pointer}; refusing to overwrite (choose another pointer or use manual steps)`);
    }
    setPointer(doc, pointer, desired);
    const endsWithNewline = bytes === null ? true : bytes.toString('utf8').endsWith('\n');
    const afterBytes = JSON.stringify(doc, null, 2) + (endsWithNewline ? '\n' : '');
    const change = {
        path,
        format: 'json',
        beforeHash,
        afterHash: sha256Hex(afterBytes),
        ownedPointer: pointer,
        beforeOwned,
        afterOwned: desired,
        afterBytes,
    };
    // preview 只读：绝不写盘；afterBytes 由 applyConfigPlan 在锁内核验后原子发布。
    return change;
}
/** 通用默认计划：verified JSON 检出 → 自动 owned 段计划；否则只给手工片段。 */
export function createConfigPlan(input) {
    return makeOwnedEntryPlan(input.detection, DEFAULT_OWNED_POINTER, input.launch, input.binding);
}
/**
 * 在 detection 指定的每个配置文件的 pointer 处安置 UMH owned 段。
 * format='json' → 全自动（JSON 原生解析，字段保留可证明）；其余格式 → 只输出
 * 手工片段（未核实格式禁止破坏性 stringify）。
 */
export async function makeOwnedEntryPlan(detection, pointer, launch, binding) {
    if (detection === null || typeof detection !== 'object') {
        throw new UmhError('INVALID_ARGUMENT', 'detection must be a Detection object');
    }
    if (!CLIENT_IDS.has(detection.clientId)) {
        throw new UmhError('INVALID_ARGUMENT', `detection.clientId is not a known client: ${String(detection.clientId)}`);
    }
    if (!Array.isArray(detection.configPaths)) {
        throw new UmhError('INVALID_ARGUMENT', 'detection.configPaths must be an array');
    }
    if (!isValidBindingId(binding?.bindingId)) {
        throw new UmhError('INVALID_BINDING_ID', `binding id is not a safe file name: ${String(binding?.bindingId)}`);
    }
    const checkedLaunch = requireLaunchShape(launch);
    const changes = [];
    const manualInstructions = [];
    if (detection.format === 'json') {
        for (const path of detection.configPaths) {
            changes.push(await planJsonChange(path, pointer, checkedLaunch));
        }
    }
    else {
        // 未核实格式：只输出手工片段，绝不做破坏性 stringify。
        for (const path of detection.configPaths) {
            manualInstructions.push(manualFragment(path, pointer, checkedLaunch, detection.format));
        }
        if (detection.configPaths.length === 0) {
            manualInstructions.push(manualFragment(null, pointer, checkedLaunch, detection.format));
        }
    }
    return {
        id: `plan_${randomBytes(12).toString('hex')}`,
        clientId: detection.clientId,
        bindingId: binding.bindingId,
        launch: checkedLaunch,
        changes,
        manualInstructions,
        dryRun: true,
        recipeVersion: 1,
    };
}
/** 纯手工计划（无字节变更；UI/未核实端使用）。 */
export function makeManualPlan(clientId, binding, launch, steps) {
    if (!CLIENT_IDS.has(clientId)) {
        throw new UmhError('INVALID_ARGUMENT', `clientId is not a known client: ${String(clientId)}`);
    }
    if (!isValidBindingId(binding?.bindingId)) {
        throw new UmhError('INVALID_BINDING_ID', `binding id is not a safe file name: ${String(binding?.bindingId)}`);
    }
    const checkedLaunch = requireLaunchShape(launch);
    if (!Array.isArray(steps) || steps.length === 0 || steps.some((s) => typeof s !== 'string' || s.trim() === '')) {
        throw new UmhError('INVALID_ARGUMENT', 'manual steps must be a non-empty array of non-empty strings');
    }
    return {
        id: `plan_${randomBytes(12).toString('hex')}`,
        clientId,
        bindingId: binding.bindingId,
        launch: checkedLaunch,
        changes: [],
        manualInstructions: [...steps],
        dryRun: true,
        recipeVersion: 1,
    };
}
/** 校验并收敛磁盘上加载的 ConfigPlan（未知字段/形状不符 → PLAN_INVALID）。 */
export function parseConfigPlan(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        throw new UmhError('PLAN_INVALID', 'plan file must contain a JSON object');
    }
    const v = value;
    const allowed = new Set(['id', 'clientId', 'bindingId', 'launch', 'changes', 'manualInstructions', 'dryRun', 'recipeVersion']);
    for (const key of Object.keys(v)) {
        if (!allowed.has(key))
            throw new UmhError('PLAN_INVALID', `unknown plan field: ${key}`);
    }
    if (typeof v['id'] !== 'string' || v['id'] === '')
        throw new UmhError('PLAN_INVALID', 'plan id must be a non-empty string');
    if (!CLIENT_IDS.has(String(v['clientId'])))
        throw new UmhError('PLAN_INVALID', `unknown plan clientId: ${String(v['clientId'])}`);
    if (!isValidBindingId(v['bindingId']))
        throw new UmhError('PLAN_INVALID', 'plan bindingId is not a safe file name');
    const launch = requireLaunchShape(v['launch']);
    if (v['dryRun'] !== true)
        throw new UmhError('PLAN_INVALID', 'plan dryRun must be true');
    if (v['recipeVersion'] !== 1)
        throw new UmhError('PLAN_INVALID', 'plan recipeVersion must be 1');
    if (!Array.isArray(v['changes']))
        throw new UmhError('PLAN_INVALID', 'plan changes must be an array');
    const changes = v['changes'].map((raw) => parseConfigChange(raw));
    if (!Array.isArray(v['manualInstructions']) || v['manualInstructions'].some((s) => typeof s !== 'string')) {
        throw new UmhError('PLAN_INVALID', 'plan manualInstructions must be an array of strings');
    }
    return {
        id: v['id'],
        clientId: v['clientId'],
        bindingId: v['bindingId'],
        launch,
        changes,
        manualInstructions: v['manualInstructions'],
        dryRun: true,
        recipeVersion: 1,
    };
}
const HEX64 = /^[0-9a-f]{64}$/;
/** 校验并收敛单个 ConfigChange（PLAN_INVALID on 形状不符）。 */
export function parseConfigChange(raw) {
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
        throw new UmhError('PLAN_INVALID', 'plan change must be an object');
    }
    const c = raw;
    const allowed = new Set(['path', 'format', 'beforeHash', 'afterHash', 'ownedPointer', 'beforeOwned', 'afterOwned', 'afterBytes']);
    for (const key of Object.keys(c)) {
        if (!allowed.has(key))
            throw new UmhError('PLAN_INVALID', `unknown change field: ${key}`);
    }
    if (typeof c['path'] !== 'string' || !isAbsolute(c['path'])) {
        throw new UmhError('PLAN_INVALID', 'change path must be an absolute string');
    }
    if (c['format'] !== 'json' && c['format'] !== 'toml' && c['format'] !== 'yaml') {
        throw new UmhError('PLAN_INVALID', `change format must be json|toml|yaml: ${String(c['format'])}`);
    }
    if (c['beforeHash'] !== null && (typeof c['beforeHash'] !== 'string' || !HEX64.test(c['beforeHash']))) {
        throw new UmhError('PLAN_INVALID', 'change beforeHash must be sha256 hex or null');
    }
    if (typeof c['afterHash'] !== 'string' || !HEX64.test(c['afterHash'])) {
        throw new UmhError('PLAN_INVALID', 'change afterHash must be sha256 hex');
    }
    if (typeof c['ownedPointer'] !== 'string' || !c['ownedPointer'].startsWith('/')) {
        throw new UmhError('PLAN_INVALID', 'change ownedPointer must be a JSON pointer');
    }
    if (c['beforeOwned'] === undefined || c['afterOwned'] === undefined) {
        throw new UmhError('PLAN_INVALID', 'change beforeOwned/afterOwned must be present (null allowed)');
    }
    if (typeof c['afterBytes'] !== 'string') {
        throw new UmhError('PLAN_INVALID', 'change afterBytes must be a string');
    }
    return {
        path: c['path'],
        format: c['format'],
        beforeHash: c['beforeHash'],
        afterHash: c['afterHash'],
        ownedPointer: c['ownedPointer'],
        beforeOwned: c['beforeOwned'],
        afterOwned: c['afterOwned'],
        afterBytes: c['afterBytes'],
    };
}
