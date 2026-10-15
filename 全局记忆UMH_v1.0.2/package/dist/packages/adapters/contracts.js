/**
 * packages/adapters/contracts — A01 共用适配底座的冻结接口（计划 v1「固定接口与测试底座」）。
 *
 * 固定类型（工单原文，禁止改名）：
 *   LaunchSpec / Detection / ConfigChange / ConfigPlan / Receipt / ClientAdapter
 * 六个共用函数按职责实现在同级模块并在本文件统一公开：
 *   config-plan.ts  createConfigPlan / makeManualPlan / makeOwnedEntryPlan
 *   launch.ts       makeLaunchSpec
 *   transaction.ts  applyConfigPlan / removeOwnedConfig（+ 受信绑定文件写入 C08 裁决路径）
 *   policy-block.ts BEGIN/END UMH MEMORY POLICY v1 指令块
 *
 * 信任属性（A 阶段全局约束）：
 *   - LaunchSpec 绝不携带 token：服务 token 由 Gateway 从 <umhHome>/security/rpc/token
 *     私有文件读取，不进客户端 JSON/env/argv。
 *   - ConfigPlan/Receipt 是本机受保护文件，字段集固定、不含任何 secret。
 *   - 项目绑定只能由受信管理面（connect CLI → resolveProject）解析并写入绑定文件，
 *     绝不由客户端请求参数决定（Review Focus A01）。
 */
// ---------------------------------------------------------------------------
// 共用校验原语
// ---------------------------------------------------------------------------
/**
 * bindingId 文件名安全形态（与 C08 Gateway 读取侧同级约束，防遍历）：
 * 首字符字母数字，其余允许字母数字与 . _ -，总长 ≤128。
 */
export const BINDING_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
/** bindingId 安全校验：拒绝空、`.`、`..`、路径分隔符与越界长度。 */
export function isValidBindingId(bindingId) {
    return (typeof bindingId === 'string' &&
        BINDING_ID_PATTERN.test(bindingId) &&
        bindingId !== '.' &&
        bindingId !== '..');
}
/** projectId 物理形态（C03 冻结）：'prj_' + 64 位小写 hex；逻辑占位 id 一律拒绝。 */
export const PROJECT_ID_PATTERN = /^prj_[0-9a-f]{64}$/;
// ---------------------------------------------------------------------------
// 共用函数公开面（实现见各同级模块；本文件统一 re-export）
// ---------------------------------------------------------------------------
export { createConfigPlan, makeManualPlan, makeOwnedEntryPlan, parseConfigPlan, parseConfigChange, DEFAULT_OWNED_POINTER, getPointer, setPointer, deletePointer, deepEqual, sha256Hex, } from './config-plan.js';
export { makeLaunchSpec } from './launch.js';
export { applyConfigPlan, removeOwnedConfig, writeTrustedBindingFile, deleteTrustedBindingFile, saveReceipt, loadReceiptById, loadReceiptFile, parseReceipt, writeProtectedJson, } from './transaction.js';
export { POLICY_BEGIN_MARKER, POLICY_END_MARKER, buildPolicyBlock, mergePolicyBlock, hasPolicyBlock, } from './policy-block.js';
