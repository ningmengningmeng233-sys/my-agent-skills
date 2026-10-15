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
import { UmhError } from '../../packages/contracts/index.js';
import { reviewCandidate, useReceiptStoreFactory } from '../../packages/core/write.js';
const USAGE = 'usage: review list | review decide --proposal <pid> --decision <accept|reject|supersede> --reason <text> [--old <memoryId>]...';
const DECISIONS = new Set(['accept', 'reject', 'supersede']);
function parseDecideArgs(argv) {
    let proposalId = '';
    let decision = '';
    let reason = '';
    const supersedes = [];
    for (let i = 0; i < argv.length; i++) {
        const flag = argv[i];
        const value = argv[i + 1];
        if (flag === '--proposal' && value !== undefined) {
            proposalId = value;
            i++;
        }
        else if (flag === '--decision' && value !== undefined) {
            decision = value;
            i++;
        }
        else if (flag === '--reason' && value !== undefined) {
            reason = value;
            i++;
        }
        else if (flag === '--old' && value !== undefined) {
            supersedes.push(value);
            i++;
        }
        else {
            throw new UsageError(`unknown or incomplete flag: ${flag}`);
        }
    }
    if (proposalId === '' || reason === '')
        throw new UsageError('--proposal and --reason are required');
    if (!DECISIONS.has(decision))
        throw new UsageError('--decision must be accept|reject|supersede');
    const out = { proposalId, decision: decision, reason };
    if (supersedes.length > 0)
        out.supersedes = supersedes;
    return out;
}
class UsageError extends Error {
}
const DEFAULT_ADMIN_BINDING = Object.freeze({
    clientId: 'generic',
    role: 'admin',
    bindingId: 'cli-admin',
});
export async function runReviewCommand(argv, deps) {
    const out = deps.out ?? ((line) => console.log(line));
    const err = deps.err ?? ((line) => console.error(line));
    const [command, ...rest] = argv;
    if (command === 'list') {
        const pending = await deps.engine.listPending();
        out(JSON.stringify(pending));
        return 0;
    }
    if (command === 'decide') {
        let decision;
        try {
            decision = parseDecideArgs(rest);
        }
        catch (e) {
            if (e instanceof UsageError) {
                err(`${e.message}\n${USAGE}`);
                return 2;
            }
            throw e;
        }
        // 裁决经 reviewCandidate：admin 闸 + 读回核验 + 回执转移都在 core 完成。
        useReceiptStoreFactory(deps.storeFactory);
        try {
            const outcome = await reviewCandidate(decision, deps.binding ?? DEFAULT_ADMIN_BINDING, deps.engine);
            out(JSON.stringify(outcome));
            return 0;
        }
        catch (e) {
            if (e instanceof UmhError) {
                out(JSON.stringify({ error: { code: e.code, message: e.message } }));
                return 1;
            }
            throw e;
        }
    }
    err(USAGE);
    return 2;
}
