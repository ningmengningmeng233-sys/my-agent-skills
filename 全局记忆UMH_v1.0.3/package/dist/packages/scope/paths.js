/**
 * packages/scope/paths — 作用域到引擎物理根的映射（C03）。
 *
 * 设计 §6：项目库按物理根隔离，避免检索后才过滤导致跨项目内容泄漏。
 *   global 根 = <umhHome>/memory/global
 *   project 根 = <umhHome>/memory/projects/<projectId>
 *
 * projectId 物理合法性在此作为唯一强制点：
 * 只接受 'prj_' + 64 位小写 hex（C03 冻结形态）。其余一律 UmhError('INVALID_PROJECT_ID')，
 * 因此遍历 ID（../../escape）、绝对路径、内嵌分隔符、错误前缀/长度/大小写全部被拒绝 ——
 * 形态合法即保证 join 结果必然位于 <umhHome>/memory/projects/ 之下。
 * 绑定层的逻辑占位 ID（如 fixtures 的 'proj-fixture'）不是合法物理根 ID。
 */
import { join } from 'node:path';
import { UmhError } from '../contracts/index.js';
/** projectId 冻结形态：'prj_' + 64 位小写 hex（identity.ts computeProjectId 的输出）。 */
const PROJECT_ID_PATTERN = /^prj_[0-9a-f]{64}$/;
/**
 * 解析作用域对应的引擎物理根。
 * @param umhHome UMH 根目录（默认 %USERPROFILE%\.universal-memory-hub，由调用方提供）
 * @param scope   ScopeRef（{kind:'global'} 或 {kind:'project',projectId}）
 */
export function resolveEngineRoot(umhHome, scope) {
    if (typeof umhHome !== 'string' || umhHome.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'umhHome must be a non-empty string');
    }
    if (scope === null || typeof scope !== 'object' || Array.isArray(scope)) {
        throw new UmhError('INVALID_ARGUMENT', 'scope must be a ScopeRef object');
    }
    if (scope.kind === 'global') {
        return join(umhHome, 'memory', 'global');
    }
    if (scope.kind === 'project') {
        const { projectId } = scope;
        if (typeof projectId !== 'string' || !PROJECT_ID_PATTERN.test(projectId)) {
            throw new UmhError('INVALID_PROJECT_ID', `project id must be 'prj_' followed by 64 lowercase hex characters`);
        }
        return join(umhHome, 'memory', 'projects', projectId);
    }
    throw new UmhError('INVALID_ARGUMENT', `scope kind must be 'global' or 'project'`);
}
