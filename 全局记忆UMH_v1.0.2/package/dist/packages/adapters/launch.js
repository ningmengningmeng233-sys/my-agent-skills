/**
 * packages/adapters/launch — LaunchSpec 构造（A01）。
 *
 * 冻结形态（工单 v1）：绝对 Node 路径 + 绝对 gateway.js + `--binding <bindingId>` +
 * `--umh-home <root>`；env 恒为空对象 —— 服务 token 由 Gateway 进程从
 * `<umhHome>/security/rpc/token` 私有文件读取（C07/C08 契约），绝不嵌入客户端
 * JSON/env/argv。makeLaunchSpec 在类型层面使 token 泄漏不可能发生。
 */
import { isAbsolute } from 'node:path';
import { UmhError } from '../contracts/index.js';
import { isValidBindingId } from './contracts.js';
/**
 * 构造客户端配置中的 stdio MCP 启动项。
 * 所有路径必须是绝对路径（相对路径会让客户端按各自 cwd 解释 → 启动错位）。
 */
export function makeLaunchSpec(binding, paths) {
    if (binding === null || typeof binding !== 'object' || Array.isArray(binding)) {
        throw new UmhError('INVALID_ARGUMENT', 'binding must be a binding object');
    }
    if (!isValidBindingId(binding.bindingId)) {
        throw new UmhError('INVALID_BINDING_ID', `binding id is not a safe file name: ${String(binding.bindingId)}`);
    }
    for (const [field, value] of [
        ['nodeExecutable', paths?.nodeExecutable],
        ['gatewayJs', paths?.gatewayJs],
        ['umhHome', paths?.umhHome],
    ]) {
        if (typeof value !== 'string' || value.trim() === '' || !isAbsolute(value)) {
            throw new UmhError('INVALID_ARGUMENT', `paths.${field} must be an absolute path`);
        }
    }
    return {
        command: paths.nodeExecutable,
        args: [paths.gatewayJs, '--binding', binding.bindingId, '--umh-home', paths.umhHome],
        env: {},
    };
}
