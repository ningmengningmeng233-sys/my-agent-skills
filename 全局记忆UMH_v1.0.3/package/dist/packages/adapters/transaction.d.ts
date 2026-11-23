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
import type { Binding } from '../contracts/index.js';
import type { ConfigPlan, Receipt } from './contracts.js';
/**
 * 从 plan.launch.args 提取 `--umh-home`（makeLaunchSpec 冻结形态保证存在）。
 * 导出供各端 adapter 复用（apply 的回执落盘点与事务同源；A03 修复轮 1 DRY）。
 */
export declare function umhHomeOfPlan(plan: ConfigPlan): string;
/**
 * 应用配置计划：逐文件（锁内）核验 beforeHash → 幂等跳过 / 备份 → 原子发布。
 * beforeHash 不符（preview 之后文件被改）→ CONFIG_CHANGED，绝不覆盖用户内容。
 * 计划形状经 parseConfigPlan 收敛校验（PLAN_INVALID fail-closed）。
 * 返回值以非枚举属性携带 published（本次是否确有发布）—— 见模块头注释
 * 「applied 语义」；幂等跳过不计入 published。
 * 注意：多文件计划在第 N 个文件失败时前 N-1 个已发布（无跨文件两阶段）；
 * 恢复路径 = 重新 connect --dry-run 生成反映当前状态的新计划。
 */
export declare function applyConfigPlan(plan: ConfigPlan): Promise<Receipt & {
    readonly published: boolean;
}>;
/**
 * 撤销 UMH 配置：逐文件做 owned 段三方比对（beforeOwned / afterOwned / 当前值）。
 * - 当前值 === afterOwned → 外科手术删除 owned 键（先备份；无关内容原样）；
 * - 当前值 === beforeOwned（用户已还原）→ 无需动作；
 * - 其它（用户改过 owned 段）→ conflict，绝不触碰该文件；
 * - 文件由 UMH 新建（beforeHash null）且未被用户改动 → 整文件移除；
 * - 文件缺失/已还原 → 视为已移除（幂等）。
 * 任何 conflict → removed:false；UMH 字段之外的一切内容不被本函数改动。
 */
export declare function removeOwnedConfig(receipt: Receipt): Promise<{
    removed: boolean;
    conflicts: string[];
}>;
/**
 * 受信绑定写入（C08 读取侧契约）：完整 Binding（含已解析物理 projectId）原子
 * 落盘 `<umhHome>/config/bindings/<bindingId>.json`（mode 0o600；内容与既有一致
 * 时跳过 → 幂等）。只能由受信管理面调用；绑定内容绝不来自客户端请求参数。
 */
export declare function writeTrustedBindingFile(umhHome: string, binding: Binding): Promise<string>;
/** 撤下受信绑定文件（完整移除成功后由管理面调用）；返回是否确有文件被删。 */
export declare function deleteTrustedBindingFile(umhHome: string, bindingId: string): Promise<boolean>;
/** 校验并收敛磁盘上加载的 Receipt（未知字段/形状不符 → RECEIPT_INVALID）。 */
export declare function parseReceipt(value: unknown): Receipt;
/** 受保护 JSON 落盘（原子；mode 0o600）。ConfigPlan/Receipt/绑定文件共用。 */
export declare function writeProtectedJson(path: string, value: unknown): Promise<void>;
/** 回执落盘到 `<umhHome>/receipts/adapters/<id>.json`（adapter 底座共用查找点）。 */
export declare function saveReceipt(umhHome: string, receipt: Receipt): Promise<string>;
/** 按 receiptId 从 umhHome 底座查找点加载回执。 */
export declare function loadReceiptById(umhHome: string, receiptId: string): Promise<Receipt>;
/** 从显式路径加载回执（CLI disconnect --receipt）。 */
export declare function loadReceiptFile(path: string): Promise<Receipt>;
