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
import { type ScopeRef } from '../contracts/index.js';
/**
 * 解析作用域对应的引擎物理根。
 * @param umhHome UMH 根目录（默认 %USERPROFILE%\.universal-memory-hub，由调用方提供）
 * @param scope   ScopeRef（{kind:'global'} 或 {kind:'project',projectId}）
 */
export declare function resolveEngineRoot(umhHome: string, scope: ScopeRef): string;
