/**
 * apps/gateway/main — stdio MCP 入口（C08）。
 *
 * CLI：`node dist/apps/gateway/main.js --binding <bindingId> --umh-home <root>`
 * 绑定在启动时从受信配置加载（总控裁决 binding）：
 *   `<umhHome>/config/bindings/<bindingId>.json` —— 完整 Binding JSON（含已
 *   解析物理 projectId），由受信管理面/A01 写入，C08 只定义读契约。model
 *   无权重载、无权覆盖。缺文件/非法 JSON/非法 binding → 启动 fail-closed。
 *
 * stdout 纪律：stdout 只承载 JSON-RPC 协议。无启动 banner、无 secret、无诊断
 * （一律 stderr）。service 不可达 → 启动即 fail-closed 可诊断错误（不用静默
 * 空结果糊弄客户端）。
 */
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import process from 'node:process';
import { UmhError } from '../../packages/contracts/index.js';
import { GatewayRpcClient } from '../../packages/gateway/rpc-client.js';
import { startGateway } from '../../packages/gateway/server.js';
function argValue(argv, name) {
    const idx = argv.indexOf(name);
    if (idx < 0 || idx + 1 >= argv.length)
        return undefined;
    return argv[idx + 1];
}
function diag(message) {
    process.stderr.write(`${message}\n`);
}
const CLIENT_IDS = new Set([
    'cherry', 'qoder', 'zcode', 'codex', 'claude', 'minimax', 'dsh', 'generic',
]);
const ROLES = new Set(['reader', 'recorder', 'admin']);
/** bindingId 文件名校验（防遍历）：仅允许字母数字与 - _ .（与服务侧同级约束）。 */
function requireSafeBindingId(bindingId) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(bindingId) || bindingId === '.' || bindingId === '..') {
        throw new UmhError('INVALID_BINDING_ID', `binding id is not a safe file name: ${bindingId}`);
    }
    return bindingId;
}
function assertBindingShape(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        throw new UmhError('BINDING_INVALID', 'binding file must contain a JSON object');
    }
    const obj = value;
    if (!CLIENT_IDS.has(String(obj['clientId']))) {
        throw new UmhError('BINDING_INVALID', `binding file has unknown clientId: ${String(obj['clientId'])}`);
    }
    if (!ROLES.has(String(obj['role']))) {
        throw new UmhError('BINDING_INVALID', 'binding file role must be reader|recorder|admin');
    }
    if (typeof obj['bindingId'] !== 'string' || obj['bindingId'].trim() === '') {
        throw new UmhError('BINDING_INVALID', 'binding file bindingId must be a non-empty string');
    }
    if (obj['projectId'] !== undefined && typeof obj['projectId'] !== 'string') {
        throw new UmhError('BINDING_INVALID', 'binding file projectId must be a string');
    }
    const binding = obj;
    if (process.env['UMH_GATEWAY_DEBUG'] === '1') {
        diag(`gateway: binding ${binding.bindingId} loaded (role=${binding.role})`);
    }
    return binding;
}
/** 受信绑定加载（C08 读契约）：缺文件/非法 JSON/非法形状一律 fail-closed。 */
export async function loadTrustedBinding(umhHome, bindingId) {
    const safe = requireSafeBindingId(bindingId);
    const file = join(umhHome, 'config', 'bindings', `${safe}.json`);
    let raw;
    try {
        raw = await readFile(file, 'utf8');
    }
    catch {
        throw new UmhError('BINDING_UNKNOWN', `binding '${bindingId}' is not recognized by this UMH home (no trusted binding file); refusing startup (fail closed)`);
    }
    let parsed;
    try {
        parsed = JSON.parse(raw);
    }
    catch {
        throw new UmhError('BINDING_INVALID', `binding file for '${bindingId}' is not valid JSON; refusing startup (fail closed)`);
    }
    const binding = assertBindingShape(parsed);
    if (binding.bindingId !== bindingId) {
        throw new UmhError('BINDING_MISMATCH', `binding file name '${bindingId}' does not match its bindingId '${binding.bindingId}'; refusing startup (fail closed)`);
    }
    return binding;
}
function readRpcToken(umhHome) {
    // Gateway 是 token-only RPC client：读本地私有 token 文件（不持有 nonce）。
    return readFile(join(umhHome, 'security', 'rpc', 'token'), 'utf8').then((t) => t.trim());
}
async function readServiceUrl(umhHome) {
    // 运行中服务的 ready record → loopback url（服务未运行 → 可诊断启动错误）。
    let parsed;
    try {
        parsed = JSON.parse(await readFile(join(umhHome, 'runtime', 'owner.json'), 'utf8'));
    }
    catch {
        throw new UmhError('SERVICE_UNAVAILABLE', 'central service is not running (no ready record); start it before connecting a gateway');
    }
    const record = parsed;
    const port = record['port'];
    if (typeof port !== 'number' || !Number.isInteger(port) || port <= 0) {
        throw new UmhError('SERVICE_UNAVAILABLE', 'central service ready record is malformed; refusing gateway startup');
    }
    return `http://127.0.0.1:${port}`;
}
async function main() {
    const argv = process.argv.slice(2);
    const bindingId = argValue(argv, '--binding');
    const umhHome = argValue(argv, '--umh-home');
    if (bindingId === undefined || bindingId.trim() === '') {
        diag('usage: node apps/gateway/main.js --binding <bindingId> --umh-home <path>');
        return 2;
    }
    if (umhHome === undefined || umhHome.trim() === '') {
        diag('usage: node apps/gateway/main.js --binding <bindingId> --umh-home <path>');
        return 2;
    }
    try {
        const binding = await loadTrustedBinding(umhHome, bindingId);
        const url = await readServiceUrl(umhHome);
        const token = await readRpcToken(umhHome);
        const rpc = new GatewayRpcClient({ url, token });
        // Expired/unknown binding 的第二重核验：service 侧 binding 形状校验 ——
        // dispatch 探针（memory_status）失败即 fail-fast，不带病启动。
        try {
            await rpc.dispatch({ binding, tool: 'memory_status', args: {} });
        }
        catch (err) {
            throw new UmhError('BINDING_REJECTED', `central service does not recognize binding '${bindingId}': ${err.message}`);
        }
        const handle = await startGateway({ binding, rpc });
        if (process.env['UMH_GATEWAY_DEBUG'] === '1') {
            diag(`gateway: serving binding ${binding.bindingId} (pid ${process.pid})`);
        }
        // stdin EOF → 只释放自己的连接；绝不停全局 service。
        await new Promise((resolve) => {
            const done = () => resolve();
            process.stdin.on('end', done);
            process.stdin.on('close', done);
            process.stdin.resume();
        });
        await handle.close();
        // stdout 落盘后再退出，避免协议尾行丢失。
        await new Promise((resolve) => setImmediate(resolve));
        return 0;
    }
    catch (err) {
        if (err instanceof UmhError) {
            diag(`gateway startup failed: [${err.code}] ${err.message}`);
            await new Promise((resolve) => setImmediate(resolve));
            return 1;
        }
        diag(`gateway startup crashed: ${err.stack ?? String(err)}`);
        await new Promise((resolve) => setImmediate(resolve));
        return 1;
    }
}
const exitCode = await main();
process.exit(exitCode);
