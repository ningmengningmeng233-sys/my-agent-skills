/**
 * apps/cli/review.ts — 管理审核命令层（C06）。
 *
 * 设计 §7/§10：审核不进入默认模型工具面，由本地管理 CLI 执行。stdout 只输出
 * 机器可读 JSON（协议/机器结果），用法诊断走 stderr。C07 接线后由 cli/main
 * 路由 `review` 命令；引擎与回执存储工厂经 deps 注入（进程内默认管理绑定
 * 是 admin——CLI 是本地管理员的人机入口，不接收模型角色覆盖）。
 *
 * 用法：
 *   review list
 *   review decide --proposal <pid> --decision <accept|reject|supersede>
 *                 --reason <text> [--old <memoryId>]...
 *
 * 退出码：0 = 命令完成（裁决结果无论 committed/rejected/pending/unknown 均为
 * 如实上报）；1 = 运行时 UmhError（stdout 输出 {error:{code,message}}）；
 * 2 = 用法错误（stderr 提示）。
 */
import type { Binding, MemoryEngine } from '../../packages/contracts/index.js';
import type { ReceiptStoreFactory } from '../../packages/core/write.js';
export type ReviewCliDeps = {
    engine: MemoryEngine;
    storeFactory: ReceiptStoreFactory;
    /** 覆盖默认管理绑定（测试/服务接线用；CLI 默认即本地 admin）。 */
    binding?: Binding;
    /** 输出汇（默认 console.log）；测试注入收集器。 */
    out?: (line: string) => void;
    err?: (line: string) => void;
};
export declare function runReviewCommand(argv: string[], deps: ReviewCliDeps): Promise<number>;
