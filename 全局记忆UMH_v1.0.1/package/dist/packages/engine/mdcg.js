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
import { readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { UmhError, } from '../contracts/index.js';
import { MEMORY_LAYER, assertNodeId, buildNativeTags, contentHashTag, decodeOperationId, nodeToRecord, wrapContent, } from './mapping.js';
import { ensureDirectories, issueToken, WorkerConnection, } from './transport.js';
/** vendored md_cg 运行单元（scripts/vendor-mdcg.py 按 C01 清单逐字节固定）。 */
const DEFAULT_STAGE = fileURLToPath(new URL('../../../vendor/mdcg/', import.meta.url));
const ROLES = new Set(['reader', 'recorder', 'admin']);
const CLIENT_IDS = new Set([
    'cherry', 'qoder', 'zcode', 'codex', 'claude', 'minimax', 'dsh', 'generic',
]);
const MEMORY_TYPES = new Set([
    'preference', 'fact', 'project', 'decision', 'procedure', 'lesson',
    'pitfall', 'constraint', 'environment', 'architecture', 'reference',
]);
const SEARCH_LIMIT_MAX = 100;
const BUDGET_TOKENS_MAX = 8192;
class MdCgEngine {
    scope;
    root;
    role;
    conn;
    queue = Promise.resolve();
    closed = false;
    constructor(conn, scope, root, role) {
        this.scope = scope;
        this.root = root;
        this.role = role;
        this.conn = conn;
    }
    /** FIFO 串行化：单 worker stdin 请求顺序执行（supervisor 纪律，C04 范围）。 */
    enqueue(fn) {
        const run = this.queue.then(fn, fn);
        this.queue = run.then(() => undefined, () => undefined);
        return run;
    }
    call(name, args) {
        if (this.closed)
            throw new UmhError('ENGINE_CLOSED', 'engine already closed');
        return this.enqueue(() => this.conn.call(name, args));
    }
    async health() {
        try {
            await this.call('mdcg_whoami', {});
            return { available: true };
        }
        catch {
            return { available: false };
        }
    }
    async propose(input, meta) {
        this.validateProposeArgs(input, meta);
        // 权限两层：引擎令牌角色 + 请求绑定角色（均须可写）。
        if (this.role === 'reader') {
            throw new UmhError('ROLE_FORBIDDEN', 'reader engine token cannot propose');
        }
        if (meta.binding.role === 'reader') {
            throw new UmhError('ROLE_FORBIDDEN', 'binding role reader cannot propose');
        }
        assertNodeId(meta.memoryId);
        requireNonEmpty(meta.operationId, 'operationId');
        const scopeKind = this.scope.kind;
        if (input.scope !== scopeKind) {
            throw new UmhError('SCOPE_MISMATCH', `engine scope is ${scopeKind}, input scope is ${input.scope}`);
        }
        const wrapped = wrapContent(input, this.scope);
        const tags = buildNativeTags({
            input,
            scope: this.scope,
            operationId: meta.operationId,
            clientId: meta.binding.clientId,
            sourceId: input.sourceId,
            wrapped,
        });
        // C01 propose-candidate：mdcg_propose 只入原生审核队列，返回 pid，不落盘。
        const value = await this.call('mdcg_propose', {
            node_id: meta.memoryId,
            content: wrapped,
            layer: MEMORY_LAYER,
            tags,
        });
        const proposalId = requirePid(value);
        return {
            // 原生响应只在 Engine 内解释；候选不是提交（C01 candidate-not-committed-yet）。
            committed: false,
            memoryId: meta.memoryId,
            proposalId,
            reasonCode: value['dedup'] === true ? 'duplicate' : undefined,
        };
    }
    async review(input) {
        if (this.role !== 'admin') {
            throw new UmhError('REVIEW_FORBIDDEN', 'review is restricted to the private management path (admin worker)');
        }
        requireNonEmpty(input.proposalId, 'proposalId');
        requireNonEmpty(input.reason, 'reason');
        if (input.decision === 'supersede') {
            throw new UmhError('SUPERSEDE_UNSUPPORTED', 'native supersede path (edit/merge) is not proven by the C01 contract');
        }
        if (input.decision !== 'accept' && input.decision !== 'reject') {
            throw new UmhError('INVALID_ARGUMENT', `invalid review decision: ${String(input.decision)}`);
        }
        const value = await this.call('mdcg_review_decide', {
            pid: input.proposalId,
            decision: input.decision,
            reason: input.reason,
        });
        if (value['ok'] === false) {
            const code = String(value['error'] ?? 'review_failed');
            if (code === 'pid_not_found') {
                throw new UmhError('PROPOSAL_NOT_FOUND', `proposal not found: ${input.proposalId}`);
            }
            if (code === 'already_decided') {
                throw new UmhError('PROPOSAL_ALREADY_DECIDED', `proposal already decided: ${input.proposalId}`);
            }
            throw new UmhError('ENGINE_NATIVE_ERROR', `mdcg_review_decide: ${code}`);
        }
        // 原生响应只在 Engine 内解释；不能将 ok 当 committed。
        const nativeId = typeof value['node_id'] === 'string' ? value['node_id'] : undefined;
        const committed = value['committed'] === true; // 绑定公式：仅 cg 写入链响应携带 committed
        if (nativeId !== undefined) {
            const visible = (await this.getRaw(nativeId)) !== null;
            if (committed && !visible) {
                throw new UmhError('COMMIT_NOT_VISIBLE', 'native commit cannot be read back');
            }
            // mdcg_review_decide 响应无 committed 字段：落盘与否只认读回可见性。
            return { committed: visible, memoryId: nativeId, proposalId: input.proposalId };
        }
        return { committed, proposalId: input.proposalId };
    }
    async findOperation(operationId) {
        requireNonEmpty(operationId, 'operationId');
        const opTag = `umh:op:${encodeURIComponent(operationId)}`;
        // pending：审核队列（C01 review-gate-recorder-list-denied：非 admin 被原生拒绝）。
        let pending;
        try {
            const value = await this.call('mdcg_review_list', {});
            const items = Array.isArray(value['pending']) ? value['pending'] : [];
            pending = items.find((item) => tagsOf(item).includes(opTag));
        }
        catch (err) {
            if (!(err instanceof UmhError) || err.code !== 'ACCESS_DENIED')
                throw err;
            // 非 admin 令牌读不到裁决面：pending 不可见，仅按 committed/absent 回答。
        }
        if (pending !== undefined) {
            return {
                state: 'pending',
                proposalId: typeof pending['pid'] === 'string' ? pending['pid'] : undefined,
                memoryId: typeof pending['id'] === 'string' ? pending['id'] : undefined,
            };
        }
        // committed：按 umh:op 标签检索（索引对象含 content+tags），客户端精确匹配。
        const found = await this.searchByTag(opTag);
        if (found !== null) {
            return { state: 'committed', memoryId: String(found.id) };
        }
        return { state: 'absent' };
    }
    async get(id) {
        requireNonEmpty(id, 'id');
        const node = await this.getRaw(id);
        return node === null ? null : nodeToRecord(node);
    }
    async findDuplicate(input) {
        this.validateInputShape(input);
        const wrapped = wrapContent(input, this.scope);
        const found = await this.searchByTag(contentHashTag(wrapped));
        return found === null ? null : nodeToRecord(found);
    }
    async recall(input) {
        requireNonEmpty(input.query, 'query');
        if (!Number.isInteger(input.budgetTokens) || input.budgetTokens < 1 || input.budgetTokens > BUDGET_TOKENS_MAX) {
            throw new UmhError('INVALID_ARGUMENT', `budgetTokens must be an integer between 1 and ${BUDGET_TOKENS_MAX}`);
        }
        // C01 recall-budget-pack：pack 携带节点（tokens_used≤budget）。
        const value = await this.call('mdcg_recall', {
            query: input.query,
            budget_tokens: input.budgetTokens,
        });
        const pack = Array.isArray(value['pack']) ? value['pack'] : [];
        const memories = pack.map((item) => nodeToRecord(item));
        const skipped = Array.isArray(value['skipped']) ? value['skipped'] : [];
        return {
            memories,
            tokensUsed: typeof value['tokens_used'] === 'number' ? value['tokens_used'] : 0,
            budgetTokens: typeof value['budget'] === 'number' ? value['budget'] : input.budgetTokens,
            truncated: pack.some((item) => item['truncated'] === true),
            warnings: skipped.map((s) => `skipped: ${String(s)}`),
        };
    }
    async search(query, limit) {
        requireNonEmpty(query, 'query');
        if (!Number.isInteger(limit) || limit < 1 || limit > SEARCH_LIMIT_MAX) {
            throw new UmhError('INVALID_ARGUMENT', `limit must be an integer between 1 and ${SEARCH_LIMIT_MAX}`);
        }
        // C01 search-find-committed：结果含 node id/score/state。
        const value = await this.call('mdcg_search', { query, k: limit });
        const results = Array.isArray(value['results']) ? value['results'] : [];
        return results.map((r) => {
            const node = r['node'];
            if (!node)
                throw new UmhError('ENGINE_BAD_RESPONSE', 'mdcg_search result missing node');
            return nodeToRecord({
                id: node['id'],
                frontmatter: node['frontmatter'],
                content: node['content'],
            });
        });
    }
    async listPending() {
        const value = await this.call('mdcg_review_list', {});
        const pending = Array.isArray(value['pending']) ? value['pending'] : [];
        return pending.map((item) => {
            const it = item;
            return {
                proposalId: typeof it['pid'] === 'string' ? it['pid'] : '',
                operationId: decodeOperationId(it['tags']),
            };
        });
    }
    async forget(_id) {
        // mdcg_forget 未列入 C01 operations —— C01 未证明的工具禁用，显式停用。
        throw new UmhError('UPSTREAM_NOT_PROVEN', 'native forget is not proven by the C01 contract; wire after a future probe');
    }
    async restore(_id) {
        throw new UmhError('UPSTREAM_NOT_PROVEN', 'native restore is not proven by the C01 contract; wire after a future probe');
    }
    async rebuildIndex() {
        // C01 index-rebuild-rescan：_index.json 是派生物（可重建白名单内唯一索引），
        // 删除后 worker 按节点真源全量重扫；轻量检索触发 _maybe_reload_index。
        await rm(join(this.root, '_index.json'), { force: true });
        await this.call('mdcg_search', { query: 'umh index rebuild probe', k: 1 });
    }
    async close() {
        if (this.closed)
            return;
        this.closed = true;
        await this.enqueue(() => this.conn.close());
    }
    // ------------------------------------------------------------------
    async getRaw(nodeId) {
        const value = await this.call('mdcg_get', { node_id: nodeId });
        if (value['ok'] === false && value['error'] === 'not_found')
            return null;
        return { id: value['id'], frontmatter: value['frontmatter'], content: value['content'] };
    }
    /** 按 `umh:` 内部标签检索并精确匹配（标签随 content+tags 进索引，C01 检索口径）。 */
    async searchByTag(tag) {
        const value = await this.call('mdcg_search', { query: tag, k: SEARCH_LIMIT_MAX });
        const results = Array.isArray(value['results']) ? value['results'] : [];
        for (const r of results) {
            const node = r['node'];
            if (!node)
                continue;
            if (tagsOf(node).includes(tag)) {
                return { id: node['id'], frontmatter: node['frontmatter'], content: node['content'] };
            }
        }
        return null;
    }
    validateProposeArgs(input, meta) {
        this.validateInputShape(input);
        if (meta === null || typeof meta !== 'object' || Array.isArray(meta)) {
            throw new UmhError('INVALID_ARGUMENT', 'propose meta must be an object');
        }
        const binding = meta.binding;
        if (binding === null || typeof binding !== 'object' || Array.isArray(binding)) {
            throw new UmhError('INVALID_ARGUMENT', 'binding must be an object');
        }
        if (!CLIENT_IDS.has(binding.clientId)) {
            throw new UmhError('INVALID_ARGUMENT', `unknown clientId: ${String(binding.clientId)}`);
        }
        if (binding.role !== 'reader' && binding.role !== 'recorder' && binding.role !== 'admin') {
            throw new UmhError('INVALID_ARGUMENT', `unknown binding role: ${String(binding.role)}`);
        }
        requireNonEmpty(binding.bindingId, 'bindingId');
        requireNonEmpty(meta.memoryId, 'memoryId');
        requireNonEmpty(meta.operationId, 'operationId');
    }
    validateInputShape(input) {
        if (input === null || typeof input !== 'object' || Array.isArray(input)) {
            throw new UmhError('INVALID_ARGUMENT', 'input must be a RememberInput object');
        }
        if (!MEMORY_TYPES.has(input.type)) {
            throw new UmhError('INVALID_ARGUMENT', `unknown memory type: ${String(input.type)}`);
        }
        if (input.scope !== 'global' && input.scope !== 'project') {
            throw new UmhError('INVALID_ARGUMENT', `invalid scope: ${String(input.scope)}`);
        }
        requireNonEmpty(input.title, 'title');
        requireNonEmpty(input.content, 'content');
        requireNonEmpty(input.evidence, 'evidence');
    }
}
function tagsOf(item) {
    const it = item;
    const fm = it?.['frontmatter'];
    const tags = (it?.['tags'] ?? fm?.['tags']);
    return Array.isArray(tags) ? tags.map((t) => String(t)) : [];
}
function requirePid(value) {
    const pid = value['pid'];
    if (typeof pid !== 'string' || pid === '') {
        throw new UmhError('ENGINE_BAD_RESPONSE', 'mdcg_propose returned no pid');
    }
    return pid;
}
function requireNonEmpty(value, field) {
    if (typeof value !== 'string' || value.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', `field ${field} must be a non-empty string`);
    }
}
/**
 * 创建单作用域 MdCgEngine：目录就绪 → 策略 fail-closed 闸 → 按实例签发限权
 * 令牌 → spawn worker 并完成 MCP initialize → whoami 核对身份。
 */
export async function createMdCgEngine(opts) {
    const { scope, root, auxRoot, python, policyFile, role } = opts;
    if (!ROLES.has(role)) {
        throw new UmhError('INVALID_ARGUMENT', `role must be reader|recorder|admin (got ${String(role)})`);
    }
    if (scope === null || typeof scope !== 'object' || Array.isArray(scope)) {
        throw new UmhError('INVALID_ARGUMENT', 'scope must be a ScopeRef object');
    }
    const scopeKind = scope.kind;
    if (scopeKind !== 'global' && scopeKind !== 'project') {
        throw new UmhError('INVALID_ARGUMENT', `unknown scope kind: ${String(scopeKind)}`);
    }
    if (typeof root !== 'string' || root === '' || typeof auxRoot !== 'string' || auxRoot === '') {
        throw new UmhError('INVALID_ARGUMENT', 'root and auxRoot must be non-empty strings');
    }
    if (typeof python !== 'string' || python === '') {
        throw new UmhError('INVALID_ARGUMENT', 'python must be a non-empty string');
    }
    if (typeof policyFile !== 'string' || policyFile === '') {
        throw new UmhError('INVALID_ARGUMENT', 'policyFile must be a non-empty string');
    }
    const cfg = {
        python,
        stage: DEFAULT_STAGE,
        root,
        auxRoot,
        stateRoot: join(auxRoot, 'state'),
        dataRoot: join(auxRoot, 'data'),
        policyFile,
        role,
        tenant: 'umh',
        actor: `umh-${scope.kind === 'project' ? 'project' : 'global'}-${role}`,
    };
    await assertPolicyUsable(policyFile);
    await ensureDirectories(cfg);
    const { token } = issueToken(cfg);
    const conn = await WorkerConnection.spawn(cfg, token);
    const engine = new MdCgEngine(conn, scope, root, role);
    try {
        await assertPrincipalMatches(conn, cfg);
    }
    catch (err) {
        try {
            await conn.close();
        }
        catch { /* 尽力关闭 */ }
        throw err;
    }
    return engine;
}
/**
 * 策略 fail-closed 闸：上游对不可用写入策略本就 fail-closed（C01 writechain
 * 探针与 stderr 口径「写入将 fail-closed」）；引擎在边界先行校验，损坏/缺失/
 * 非对象的策略文件直接拒绝启动，写路径由此拒绝放行。
 */
async function assertPolicyUsable(policyFile) {
    let text;
    try {
        text = await readFile(policyFile, 'utf8');
    }
    catch {
        throw new UmhError('INVALID_POLICY', `policy file is not readable: ${policyFile}`);
    }
    let parsed;
    try {
        parsed = JSON.parse(text);
    }
    catch {
        throw new UmhError('INVALID_POLICY', 'policy file is not valid JSON');
    }
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new UmhError('INVALID_POLICY', 'policy file must be a JSON object');
    }
}
/** boot 后身份核对：令牌生效、角色/权限与引擎配置一致（C01 identity-* 口径）。 */
async function assertPrincipalMatches(conn, cfg) {
    const expected = { admin: 'designer', recorder: 'record', reader: 'output' };
    let who;
    try {
        who = await conn.call('mdcg_whoami', {});
    }
    catch (err) {
        throw new UmhError('ENGINE_BOOT_FAILED', `worker identity probe failed: ${err.message}`);
    }
    const principal = (who['principal'] ?? {});
    if (principal['auth_mode'] !== 'token') {
        throw new UmhError('ENGINE_BOOT_FAILED', 'worker did not authenticate via token');
    }
    if (principal['role'] !== expected[cfg.role]) {
        throw new UmhError('ENGINE_BOOT_FAILED', `worker role ${String(principal['role'])} does not match engine role ${cfg.role}`);
    }
    const canWrite = principal['can_write'] === true;
    if (cfg.role === 'reader' && canWrite) {
        throw new UmhError('ENGINE_BOOT_FAILED', 'reader engine must hold a non-writing token');
    }
    if (cfg.role !== 'reader' && !canWrite) {
        throw new UmhError('ENGINE_BOOT_FAILED', 'recorder/admin engine must hold a writing token');
    }
    if (cfg.role === 'admin' && principal['can_admin'] !== true) {
        throw new UmhError('ENGINE_BOOT_FAILED', 'admin engine must hold an admin-capable token');
    }
    if (cfg.role !== 'admin' && principal['can_admin'] === true) {
        throw new UmhError('ENGINE_BOOT_FAILED', 'non-admin engine must not hold admin capability');
    }
}
