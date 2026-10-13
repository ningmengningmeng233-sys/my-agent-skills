/**
 * packages/scope/binding — 信任绑定到作用域的解析（C03）。
 *
 * 信任模型（设计 §6，冻结）：
 *   - Binding 来自受信的客户端接入配置；Gateway 绝不相信模型传来的任意 project_id。
 *   - 活动 Gateway 会话在其生命周期内固定 bindingId；配置重编辑不得把活动会话
 *     悄悄重绑到另一项目（重绑禁止的强制点在 C07 服务与 C08 Gateway 接线）。
 *   - 显式拆分/合并项目身份只由 admin 经本地管理入口执行，并做冲突检查。
 *
 * 职责边界：本模块只做 绑定 → ScopeRef 的解析（含 project 未绑定显式报错）。
 * projectId 的物理合法性（'prj_'+64 hex、拒绝越界）在 resolveEngineRoot（paths.ts）
 * 作为唯一强制点执行 —— 引擎根必须且只能经 resolveEngineRoot 解析。
 * 角色权限（reader 只读 / recorder 提交候选 / admin 管理入口）属于写路径与权限映射
 * 工单的显式映射测试，不在作用域解析内隐式判定。
 */
import { UmhError } from '../contracts/index.js';
const WRITE_SCOPES = new Set(['global', 'project']);
const READ_SCOPES = new Set(['auto', 'global', 'project']);
function requireBindingObject(binding) {
    if (binding === null || typeof binding !== 'object' || Array.isArray(binding)) {
        throw new UmhError('INVALID_ARGUMENT', 'binding must be a binding object');
    }
}
/** 可选 projectId：undefined/null/空串视为未绑定 → null；非字符串 → INVALID_ARGUMENT。 */
function optionalProjectId(binding) {
    const { projectId } = binding;
    if (projectId === undefined || projectId === null || projectId === '')
        return null;
    if (typeof projectId !== 'string') {
        throw new UmhError('INVALID_ARGUMENT', 'binding.projectId must be a string');
    }
    return projectId;
}
/**
 * 解析写入作用域。
 * scope='project' 且未绑定 → UmhError('PROJECT_NOT_BOUND')：项目写入禁止静默落全局。
 */
export function resolveWriteScope(binding, scope) {
    requireBindingObject(binding);
    if (typeof scope !== 'string' || !WRITE_SCOPES.has(scope)) {
        throw new UmhError('INVALID_ARGUMENT', `write scope must be 'global' or 'project'`);
    }
    if (scope === 'global')
        return { kind: 'global' };
    const projectId = optionalProjectId(binding);
    if (projectId === null) {
        throw new UmhError('PROJECT_NOT_BOUND', 'bind a project before writing');
    }
    return { kind: 'project', projectId };
}
/**
 * 解析读取作用域集合。
 * - 'global' → [global]
 * - 'project' → [project]；未绑定 → PROJECT_NOT_BOUND（不存在可读的项目根）
 * - 'auto' → 未绑定 [global]；已绑定 [project, global]（project 在前，两个独立物理根）
 */
export function resolveReadScopes(binding, scope) {
    requireBindingObject(binding);
    if (typeof scope !== 'string' || !READ_SCOPES.has(scope)) {
        throw new UmhError('INVALID_ARGUMENT', `read scope must be 'auto', 'global' or 'project'`);
    }
    if (scope === 'global')
        return [{ kind: 'global' }];
    if (scope === 'project') {
        const projectId = optionalProjectId(binding);
        if (projectId === null) {
            throw new UmhError('PROJECT_NOT_BOUND', 'bind a project before reading project scope');
        }
        return [{ kind: 'project', projectId }];
    }
    const projectId = optionalProjectId(binding);
    if (projectId === null)
        return [{ kind: 'global' }];
    return [{ kind: 'project', projectId }, { kind: 'global' }];
}
