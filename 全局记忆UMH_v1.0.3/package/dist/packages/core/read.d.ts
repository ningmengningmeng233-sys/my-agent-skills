/**
 * packages/core/read.ts — 作用域读取、active 过滤与共享 token 预算（C09）。
 *
 * 职责（工单 C09 + 累积裁决）：
 *   - recall/search/get 三入口在引擎池之上按绑定解析可读作用域
 *     （resolveReadScopes，同一纯函数；绝不信任模型输入的 scope 越权），
 *     逐作用域调用引擎，保留单库排序、分组输出（project 组在前）。
 *   - active 过滤（supersede 补充探针 NOT_PROVEN 后的有界裁决）：引擎内
 *     prefilter 不可行（原生召回不按标签过滤），读层排除 superseded/pending/
 *     rejected/conflict/unknown 记录 —— 排除集来自 C06 回执台账
 *     （listSupersedeRelations + 非 committed 回执的 memoryId），不造第二真源、
 *     不改 vendor。superseded 记录先耗引擎预算再被排除属有界偏差，O01 终验。
 *   - 预算（上游引擎计数/裁剪口径，禁止字符÷4 估算）：双作用域时 global
 *     25%（Math.floor）/ project 其余（75% 口径，工单冻结骨架）；单作用域全给
 *     该侧。未耗完的一侧可再分配：project 剩余并入 global 授权；global 剩余且
 *     project 已耗尽其分配时对 project 二次召回并按 native id 去重（上游排序
 *     无偏移机制，二次召回可能重复装包，去重后仅输出增量）。tokensUsed 为
 *     各次引擎上报值之和，恒不超预算；零预算的一侧绝不以非法 budget 调引擎。
 *   - 公共引用：记录返回客户端前统一转 handle（contracts.encodeHandle，
 *     全局 g:<nativeId> / 项目 p:<projectId>:<nativeId>），Engine 内部仍用
 *     nativeId；记录必须标 scope（按被查物理根权威标注，不信任记录自报）与
 *     来源。nativeId 不可编码（含冒号/路径分隔符的外来节点）剔除并告警，
 *     不让单条脏数据打断整个召回。
 *   - get 不信任 handle 授权：解码后按绑定重新验证作用域；伪造/格式非法/
 *     他项目/不存在一律 NOT_FOUND（返回 null），不触碰他库引擎、不透露其它
 *     项目存在性。
 */
import { type Binding, type MemoryEngine, type MemoryRecord, type QueryScope, type RecallInput, type RecallResult, type ScopeRef } from '../contracts/index.js';
/** 读层所需的池能力（EnginePool 结构化子集；测试注入 fake）。 */
export interface ReadEnginePool {
    get(scope: ScopeRef): Promise<MemoryEngine>;
}
export declare function recall(input: RecallInput, binding: Binding, pool: ReadEnginePool): Promise<RecallResult>;
export declare function search(query: string, scope: QueryScope, limit: number, binding: Binding, pool: ReadEnginePool): Promise<MemoryRecord[]>;
export declare function get(memoryId: string, binding: Binding, pool: ReadEnginePool): Promise<MemoryRecord | null>;
/**
 * rpc.ts ReadService 的薄适配：service 边界已完成 wire 解析与
 * resolveReadScopes 鉴权解析；本适配持有池引用，按冻结的 core 签名从绑定重新
 * 派生作用域（同一纯函数，结果一致）。返回结构化类型与 rpc.ts ReadService
 * 兼容，core 层不反向依赖 service 层。
 */
export declare function createReadService(pool: ReadEnginePool): {
    recall(binding: Binding, input: RecallInput, scopes: ScopeRef[]): Promise<RecallResult>;
    search(binding: Binding, query: string, scope: QueryScope, scopes: ScopeRef[], limit: number): Promise<MemoryRecord[]>;
    get(binding: Binding, memoryId: string, scopes: ScopeRef[]): Promise<MemoryRecord | null>;
};
