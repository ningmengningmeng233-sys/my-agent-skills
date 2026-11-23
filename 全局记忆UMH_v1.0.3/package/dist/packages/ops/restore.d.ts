import type { RestorePlan } from './contracts.js';
/**
 * hashTarget：目标根的递归内容 hash（I1 修复：旧实现只数顶层名+size，
 * 同名同大小的内容替换会漏过 RESTORE_TARGET_CHANGED）。
 * 语义：按相对路径（posix 化、排序后）逐项喂入累加器——目录记
 * `dir:<rel>`，文件记 `file:<rel>:bytes:<len>:`＋文件字节，符号链接记
 * `link:<rel>-><readlink>`（从不跟随链接读目标，避免越界读；越界链接即只
 * 计链接本身，不计其指向内容——见下），不可读记 `<unreadable>`。
 * 跳过：`.restore-staging-*`（apply  staging 临时区）、`backups/`（自身归档）、
 * `logs/`（日志非 durable 面）、`runtime/owner.lock|owner.json|status.json`
 * （运行时进程态，非备份面）。其余 `runtime/`（含 `runtime/connections/`）
 * 正常计入。空目录/缺失根返回 null（isolatedRestoreTarget 新根面）；
 * 跳过后零有效项同样返回 null。非绝对路径抛 INVALID_ARGUMENT。
 */
export declare function hashTarget(targetRoot: string): Promise<string | null>;
/**
 * restore preview：读 manifest→版本/SHA/范围/篡改校验→归档源文件 hash
 * 全量核对→target 冲突检查（非空含 FOREIGN 即冲突，退出 1 面）→出 plan。
 */
export declare function planRestore(manifestPath: string, targetRoot: string): Promise<RestorePlan>;
/**
 * apply：validated 门→target hash 比对（后改即停）→先备份当前→staging
 * 校验后发布→ACL 重设；失败回原态（pre-restore 快照回滚）。
 */
export declare function applyRestore(plan: RestorePlan): Promise<void>;
