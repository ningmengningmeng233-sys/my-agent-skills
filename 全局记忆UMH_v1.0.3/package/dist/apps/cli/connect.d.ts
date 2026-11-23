/**
 * apps/cli/connect — connect / disconnect 命令层（R01：真实 adapter 路由）。
 *
 * 命令（R01 冻结）：
 *   umh connect <clientId> --variant <v> --workspace <dir> --dry-run --out <plan.json>
 *       [--umh-home <path>] [--config-path <absolute path>]
 *   umh connect apply <plan.json>
 *   umh disconnect --receipt <receipt.json> [--umh-home <path>]
 *
 * R01 路由（补充工单 L108-114 代码块）：
 *   const adapter = resolveAdapter(clientId, { umhHome });
 *   const detection = await adapter.detect({ home: userHome(), variant,
 *     ...(configPath ? { configPath } : {}) });
 *   const plan = await adapter.preview({ detection, binding, launch });
 *
 * 信任属性（C08 裁决 + A 阶段全局约束）：
 *   - projectId 只由本命令经 C03 resolveProject(--workspace) 解析（受信管理面），
 *     绝不接受客户端/请求参数传入的绑定内容。
 *   - apply 步骤把完整 Binding（含已解析物理 projectId）经
 *     writeTrustedBindingFile 写入 `<umhHome>/config/bindings/<bindingId>.json`
 *     （C08 Gateway 读取侧契约：文件名= bindingId、防遍历校验、缺文件 fail-closed）。
 *   - 服务 token 绝不出现在 plan/receipt/绑定文件/env/argv/output（makeLaunchSpec
 *     的 env 恒为空；token 由 Gateway 从私有文件读取）。
 *   - --dry-run 零副作用（除用户显式 --out 请求的 plan 文件与 sidecar）。
 *
 * 落盘布局（--out 为 plan.json 时，均在本目录）：
 *   plan.json            ConfigPlan（受保护文件，mode 0o600）
 *   plan.binding.json    受信绑定 sidecar（apply 时消费；连接内容以 connect 解析为准）
 *   plan.receipt.json    apply 产出的 Receipt（disconnect --receipt 的输入）
 *
 * R01 去猜测（补充工单 L108）：
 *   - 去掉生产 detectGeneric（`<home>/.<clientId>/mcp.json` 探针）与
 *     `.<client>/mcp.json` 猜路径；configPath 只来自显式 --config-path（必须绝对
 *     路径，相对路径拒绝），不传即 adapter 发现模式；
 *   - 未知 variant 由 adapter 拒绝（本层不代判变体合法性）；
 *   - 未知数据库/内部应用数据一律不读（CLI 只读显式 configPath 与 adapter 内部
 *     的官方文档化候选）。
 */
import type { Detection } from '../../packages/adapters/contracts.js';
import type { LaunchSpec } from '../../packages/adapters/contracts.js';
export declare const CLIENT_IDS: ReadonlySet<string>;
/** umh connect <clientId> --variant <v> --workspace <dir> --dry-run --out <plan.json> [--config-path <abs>] */
export declare function runConnectRoute(argv: string[]): Promise<number>;
/** umh connect apply <plan.json> [--fail-after <config|binding>] */
export declare function runConnectApplyRoute(argv: string[]): Promise<number>;
/** umh connect reconcile --connection <id> --umh-home <root> */
export declare function runConnectReconcileRoute(argv: string[]): Promise<number>;
/**
 * umh connect trust --connection <id> --umh-home <root> --confirm-reviewed
 *
 * 手工连接信任链（补充设计 2026-10-06 §2.2）：管理者显式确认已审阅计划与手工
 * 配置范围后，经既有受信管理面（writeTrustedBindingFile）发布该连接的受信绑定。
 * 语义与拒绝面见 trustManualConnection 合同；本层只做参数防御与 MANUAL_CONFIRM_REQUIRED
 * 的富化输出（nextActions + nativeVerified=false，绝不伪称模型/native 验证）。
 */
export declare function runConnectTrustRoute(argv: string[]): Promise<number>;
/**
 * umh disconnect --connection <id> --umh-home <root> [--confirm-manual-removal]
 * umh disconnect --receipt <receipt.json> [--umh-home <path>]（legacy 兼容）
 *
 * R03 路由（补充工单 L170-176 代码块与顺序）：
 *   - root 从显式参数或 record 取，两者冲突拒绝（CONNECTION_ROOT_MISMATCH，
 *     由 disconnectConnection 内 requireSameRoot 抛）；
 *   - legacy 仅安全读取 receipt launch 推断（umhHomeFromReceipt），手工无 root
 *     拒绝（CONNECTION_ROOT_REQUIRED），绝不猜默认家目录；
 *   - 自动先 adapter.remove，配置冲突保留 binding；成功后检查 bindingHash 相符再删；
 *   - 手工返回 manual_cleanup_required 与步骤，无确认非 0；只有明确
 *     --confirm-manual-removal 后可清本连接 binding，仍不伪称模型/native 验证；
 *   - delete 失败返回 needs_reconcile 且非 0（connection.ts 内处理，本层只透传）。
 */
export declare function runDisconnectRoute(argv: string[]): Promise<number>;
export type { Detection, LaunchSpec };
