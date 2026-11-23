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
import { type ClientId, type MemoryRecord, type RememberInput, type ScopeRef } from '../contracts/index.js';
/** UMH 记忆固定写入的 md_cg 层（scope/type 经 tags 携带，不与 layer 混同）。 */
export declare const MEMORY_LAYER = "knowledge";
/** 上游 nodefile 的节点 id 白名单（md_cg.mdcg.add：^[A-Za-z0-9_.@-]{1,128}$）。 */
export declare const NODE_ID_PATTERN: RegExp;
/**
 * 把用户已提交内容包进 CCG 六要素模板。六要素只重述 RememberInput 的固定字段
 * （title/type/scope/evidence），正文原样置于末尾 —— 不生成新的事实。
 */
export declare function wrapContent(input: RememberInput, scope: ScopeRef): string;
/**
 * 还原 wrapContent 的输出；上游对正文尾部换行归一（追加单个换行），故读取侧
 * 统一剥尾。非本引擎包裹的正文原样返回（title 为空串，由调用方兜底）。
 */
export declare function unwrapContent(text: string): {
    title: string;
    content: string;
};
/** 内部元数据标签构造（值经 encodeURIComponent；键固定枚举）。 */
export declare function buildNativeTags(opts: {
    input: RememberInput;
    scope: ScopeRef;
    operationId: string;
    clientId: ClientId;
    sourceId?: string;
    wrapped: string;
}): string[];
/**
 * native 节点视图（mdcg_get 响应 / mdcg_search node / mdcg_recall pack 项）→
 * MemoryRecord。id 即 UMH memoryId（stableNodeId）；umh:* 内部标签在此剥离。
 */
export declare function nodeToRecord(node: {
    id: unknown;
    frontmatter?: unknown;
    content: unknown;
}): MemoryRecord;
/** 从 native 提案项（review_list）或节点 tags 中解出 operationId。 */
export declare function decodeOperationId(tags: unknown): string;
/** 内容寻址标签（findDuplicate 精确匹配用）：wrap 后内容的 SHA256 前 16 位。 */
export declare function contentHashTag(wrapped: string): string;
/** 校验 UMH memoryId 能否作为稳定 node_id（P0-1 白名单，fail-fast 在引擎边界）。 */
export declare function assertNodeId(memoryId: string): void;
