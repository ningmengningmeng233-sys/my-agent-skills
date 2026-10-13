/**
 * packages/engine/mapping — native md_cg 数据 ↔ UMH 内部 camelCase 类型翻译（C04）。
 *
 * 本模块是全仓库唯一允许解释 native 工具/数据形态的地方（设计 §5：
 * MdCgEngine 是唯一允许依赖 md_cg 协议细节的组件）。映射只依据 C01 冻结合同
 * （docs/upstream-audit/engine-contract.json）实测的响应形态：
 *   - mdcg_get / mdcg_search.node：{id, frontmatter{tags, layer, created_at,
 *     lifecycle_state, verification_state, writer, reviewer, sensitivity...}, content}
 *   - mdcg_propose → {pid, dedup}；mdcg_review_decide → {ok, node_id, ...}
 *   - metadataRoundtrip：tags/layer/content/稳定 node id/writer 归因全保真，
 *     上游 nodefile 对正文尾部换行归一（实测追加单个换行）。
 *
 * 元数据装载（工单：source、scope、type、operation 引用保存在 native metadata
 * 中）：全部经 native frontmatter.tags（C01 tagsRoundtrip 证明保真），以 `umh:`
 * 前缀命名空间编码（值经 encodeURIComponent），读回时剥离，不进公共 tags。
 * 正文 CCG 模板仅包裹用户已提交内容（六要素行全部来自 RememberInput 的固定
 * 重述），不生成新的事实。
 *
 * scope 与 md_cg layer 含义不同（设计 §7），不得互换：UMH 记忆固定写入
 * layer='knowledge'，scope/type 走 tags。
 */
import { createHash } from 'node:crypto';
import { UmhError } from '../contracts/index.js';
/** UMH 记忆固定写入的 md_cg 层（scope/type 经 tags 携带，不与 layer 混同）。 */
export const MEMORY_LAYER = 'knowledge';
/** 内部元数据标签命名空间。 */
const TAG_PREFIX = 'umh:';
const CCG_ELEMENTS = Object.freeze([
    '功能名',
    '生效条件',
    '子功能',
    '执行',
    '验证方式',
    '不适用条件',
]);
/** 上游 nodefile 的节点 id 白名单（md_cg.mdcg.add：^[A-Za-z0-9_.@-]{1,128}$）。 */
export const NODE_ID_PATTERN = /^[A-Za-z0-9_.@-]{1,128}$/;
function enc(value) {
    return encodeURIComponent(value);
}
function dec(value) {
    try {
        return decodeURIComponent(value);
    }
    catch {
        return value;
    }
}
/** 单行化：CCG 要素行不承载换行（换行会把正文切进错误槽）。 */
function oneLine(value) {
    return value.replace(/[\r\n]+/g, ' ').trim();
}
/**
 * 把用户已提交内容包进 CCG 六要素模板。六要素只重述 RememberInput 的固定字段
 * （title/type/scope/evidence），正文原样置于末尾 —— 不生成新的事实。
 */
export function wrapContent(input, scope) {
    const condition = scope.kind === 'project'
        ? `在 UMH 项目作用域（projectId=${oneLine(scope.projectId)}）内引用该记忆时`
        : '在 UMH 全局作用域内引用该记忆时';
    const header = [
        `# 功能名：${oneLine(input.title)}`,
        `# 生效条件：${condition}`,
        `# 子功能：UMH ${input.type} 记忆`,
        '# 执行：见正文',
        `# 验证方式：${oneLine(input.evidence)}`,
        scope.kind === 'project' ? '# 不适用条件：全局作用域与其他项目' : '# 不适用条件：绑定项目作用域',
    ].join('\n');
    return `${header}\n\n${input.content}`;
}
/**
 * 还原 wrapContent 的输出；上游对正文尾部换行归一（追加单个换行），故读取侧
 * 统一剥尾。非本引擎包裹的正文原样返回（title 为空串，由调用方兜底）。
 */
export function unwrapContent(text) {
    const norm = String(text ?? '').replace(/\r\n/g, '\n');
    const lines = norm.split('\n');
    const looksWrapped = lines.length >= CCG_ELEMENTS.length
        && CCG_ELEMENTS.every((name, i) => lines[i].startsWith(`# ${name}：`));
    if (!looksWrapped) {
        return { title: '', content: norm.replace(/\n+$/, '') };
    }
    const rest = lines.slice(CCG_ELEMENTS.length);
    if (rest.length > 0 && rest[0] === '')
        rest.shift();
    return {
        title: lines[0].slice('# 功能名：'.length).trim(),
        content: rest.join('\n').replace(/\n+$/, ''),
    };
}
/** 内部元数据标签构造（值经 encodeURIComponent；键固定枚举）。 */
export function buildNativeTags(opts) {
    const { input, scope, operationId, clientId, sourceId, wrapped } = opts;
    const tags = [
        `${TAG_PREFIX}v1`,
        `${TAG_PREFIX}op:${enc(operationId)}`,
        `${TAG_PREFIX}type:${enc(input.type)}`,
        `${TAG_PREFIX}scope:${enc(input.scope)}`,
    ];
    if (scope.kind === 'project')
        tags.push(`${TAG_PREFIX}prj:${enc(scope.projectId)}`);
    tags.push(`${TAG_PREFIX}client:${enc(clientId)}`);
    if (typeof sourceId === 'string' && sourceId !== '') {
        tags.push(`${TAG_PREFIX}srcid:${enc(sourceId)}`);
    }
    tags.push(contentHashTag(wrapped));
    return tags;
}
/**
 * native 节点视图（mdcg_get 响应 / mdcg_search node / mdcg_recall pack 项）→
 * MemoryRecord。id 即 UMH memoryId（stableNodeId）；umh:* 内部标签在此剥离。
 */
export function nodeToRecord(node) {
    const id = typeof node.id === 'string' ? node.id : String(node.id ?? '');
    const fm = (node.frontmatter ?? {});
    const rawTags = Array.isArray(fm['tags']) ? fm['tags'].map((t) => String(t)) : [];
    const userTags = rawTags.filter((t) => !t.startsWith(TAG_PREFIX));
    const internal = new Map();
    for (const tag of rawTags) {
        if (!tag.startsWith(TAG_PREFIX))
            continue;
        const body = tag.slice(TAG_PREFIX.length);
        const sep = body.indexOf(':');
        if (sep <= 0)
            continue;
        internal.set(body.slice(0, sep), dec(body.slice(sep + 1)));
    }
    const type = internal.get('type');
    const scopeKind = internal.get('scope');
    const prj = internal.get('prj');
    const scope = scopeKind === 'project' && prj
        ? { kind: 'project', projectId: prj }
        : { kind: 'global' };
    const { title, content } = unwrapContent(String(node.content ?? ''));
    const client = internal.get('client');
    const sourceId = internal.get('srcid');
    const source = {
        clientId: (client || 'generic'),
    };
    if (sourceId !== undefined)
        source.sourceId = sourceId;
    const createdRaw = typeof fm['created_at'] === 'number' ? fm['created_at'] : Number.NaN;
    const createdAt = Number.isFinite(createdRaw)
        ? new Date(createdRaw * 1000).toISOString()
        : new Date(0).toISOString();
    const list = (key) => {
        const raw = internal.get(key);
        if (raw === undefined || raw === '')
            return [];
        return raw.split(',').map((v) => dec(v)).filter((v) => v !== '');
    };
    const lifecycle = typeof fm['lifecycle_state'] === 'string' ? fm['lifecycle_state'] : 'active';
    return {
        schemaVersion: 1,
        id,
        type: (type || 'fact'),
        scope,
        title,
        content,
        status: lifecycle === 'active' ? 'active' : 'superseded',
        confidence: mapConfidence(fm['verification_state']),
        tags: userTags,
        source,
        provenance: { createdAt },
        relations: {
            supersedes: list('sup'),
            conflictsWith: list('conf'),
            derivedFrom: list('derived'),
        },
    };
}
/** verification_state → UMH confidence（固定映射；unverified 一律 uncertain）。 */
function mapConfidence(verificationState) {
    if (verificationState === 'verified')
        return 'confirmed';
    return 'uncertain';
}
/** 从 native 提案项（review_list）或节点 tags 中解出 operationId。 */
export function decodeOperationId(tags) {
    if (!Array.isArray(tags))
        return '';
    for (const tag of tags) {
        const t = String(tag);
        if (t.startsWith(`${TAG_PREFIX}op:`))
            return dec(t.slice(TAG_PREFIX.length + 3));
    }
    return '';
}
/** 内容寻址标签（findDuplicate 精确匹配用）：wrap 后内容的 SHA256 前 16 位。 */
export function contentHashTag(wrapped) {
    const hash = createHash('sha256').update(wrapped, 'utf8').digest('hex').slice(0, 16);
    return `${TAG_PREFIX}hash:${hash}`;
}
/** 校验 UMH memoryId 能否作为稳定 node_id（P0-1 白名单，fail-fast 在引擎边界）。 */
export function assertNodeId(memoryId) {
    if (typeof memoryId !== 'string' || !NODE_ID_PATTERN.test(memoryId)) {
        throw new UmhError('INVALID_MEMORY_ID', `memoryId must match ${NODE_ID_PATTERN} to be a stable native node id`);
    }
}
