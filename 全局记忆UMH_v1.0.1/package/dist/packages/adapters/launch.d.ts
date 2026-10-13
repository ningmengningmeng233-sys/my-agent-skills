/**
 * packages/adapters/launch — LaunchSpec 构造（A01）。
 *
 * 冻结形态（工单 v1）：绝对 Node 路径 + 绝对 gateway.js + `--binding <bindingId>` +
 * `--umh-home <root>`；env 恒为空对象 —— 服务 token 由 Gateway 进程从
 * `<umhHome>/security/rpc/token` 私有文件读取（C07/C08 契约），绝不嵌入客户端
 * JSON/env/argv。makeLaunchSpec 在类型层面使 token 泄漏不可能发生。
 */
import type { Binding } from '../contracts/index.js';
import type { LaunchPaths, LaunchSpec } from './contracts.js';
/**
 * 构造客户端配置中的 stdio MCP 启动项。
 * 所有路径必须是绝对路径（相对路径会让客户端按各自 cwd 解释 → 启动错位）。
 */
export declare function makeLaunchSpec(binding: Binding, paths: LaunchPaths): LaunchSpec;
