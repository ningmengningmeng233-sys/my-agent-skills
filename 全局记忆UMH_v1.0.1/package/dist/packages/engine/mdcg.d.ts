/**
 * packages/engine/mdcg — MdCgEngine：固定 md_cg 分发单元的 MemoryEngine wrapper（C04）。
 *
 * 唯一允许依赖 md_cg 协议细节的组件（设计 §5）。native 工具名/参数/响应形态
 * 全部取自 C01 冻结合同（docs/upstream-audit/engine-contract.json operations，
 * 探针实测），映射翻译在 mapping.ts 单点完成：
 *
 *   health()        → mdcg_whoami（读面探活）
 *   propose()       → mdcg_propose（候选队列；{pid,dedup}，永不直接提交）
 *   review()        → mdcg_review_decide（accept/reject；裁决落盘后读回验证）
 *   findOperation() → mdcg_review_list（pending，admin）+ mdcg_search（committed，
 *                     umh:op 标签检索 + 客户端精确匹配）
 *   get()           → mdcg_get（not_found → null；候选不冒充已保存）
 *   findDuplicate() → mdcg_search（umh:hash 内容寻址标签 + 精确匹配）
 *   recall()        → mdcg_recall（budget_tokens 预算装包）
 *   search()        → mdcg_search（k=limit）
 *   listPending()   → mdcg_review_list（admin 裁决面）
 *   rebuildIndex()  → 删除派生 _index.json + 轻量检索触发全库重扫（C01
 *                     index-rebuild-rescan：索引是派生物，节点 .md 是唯一真源）
 *   forget/restore  → 停用（mdcg_forget/mdcg_restore 未列入 C01 operations，
 *                     C01 未证明的工具禁用；待后续探针证明后接线）
 *
 * committed 语义（工单绑定代码块）：native `ok:true` 不等于 committed；只有
 * native 响应的 `committed === true`（cg 写入链形态）或读回可见性（review 落盘
 * 路径，响应无 committed 字段）才构成提交，且提交必须经 native get 读回验证，
 * 不可见即 UmhError('COMMIT_NOT_VISIBLE')。
 *
 * 权限两层（工单）：服务内部 worker 按角色持有上游令牌（admin→designer 可裁决，
 * 私有凭据不出服务）；客户侧 Binding 在引擎边界复核——reader 绑定/reader 令牌
 * 不得写（ROLE_FORBIDDEN），review 只经私有管理通路（REVIEW_FORBIDDEN），
 * pool 内部 role=admin 不等于客户获得 admin。生产身份不用 legacy env admin
 * （transport 恒不设 MDCG_LEGACY_ENV_AUTH）。
 *
 * supervisor 纪律：一个作用域实例独占一个 worker；本引擎内所有 native 调用经
 * FIFO 串行化（单 worker stdin 顺序请求），并发调度属 C07 pool。
 */
import { type MemoryEngine, type ScopeRef } from '../contracts/index.js';
import { type EngineRole } from './transport.js';
export interface CreateMdCgEngineOptions {
    scope: ScopeRef;
    root: string;
    auxRoot: string;
    python: string;
    policyFile: string;
    role: EngineRole;
}
/**
 * 创建单作用域 MdCgEngine：目录就绪 → 策略 fail-closed 闸 → 按实例签发限权
 * 令牌 → spawn worker 并完成 MCP initialize → whoami 核对身份。
 */
export declare function createMdCgEngine(opts: CreateMdCgEngineOptions): Promise<MemoryEngine>;
