/**
 * packages/core/secrets.ts — 写前 secret 拦截（C05）。
 *
 * 设计 §8：写入前拦截疑似密钥、密码、token、cookie、私钥等；命中时不把原始
 * 载荷写进候选、负记忆或日志；错误日志仅保留操作、ID、状态与哈希。
 *
 * 规则来源（移植，不自研语义判断）：
 *   - 11 条 forbidden 规则逐字移植自 C01 固定的上游策略
 *     vendor/mdcg/data/policy.json（上游 SHA bfbdcb923da1ef2880dd3f9f979cf419d9211eb0）。
 *     其中 `\bsk-[A-Za-z0-9_\-]{20,}` 的写入闸门 REJECT 行为已由 C01 探针
 *     writechain-secret-redacted 实证（docs/upstream-audit/engine-contract.json）。
 *     Python `(?i)` 内联旗标转为 JS RegExp 的 i 旗标，语义一致。
 *   - COOKIE_HEADER / SESSION_COOKIE_PAIR 为 C05 新增，补设计 §8 点名的
 *     cookie 覆盖缺口。
 *   - 上游口径「宁可漏报也不误伤」：长 hex（Git SHA）等易误伤模式刻意不收；
 *     本模块保持同一口径，模糊内容交给审核而不是拒掉。
 *
 * 输出合同：detectSecret 只返回 matched 与 ruleIds，永不返回匹配原文；
 * 日志消费方只能拿到 ruleId（正文哈希由写通路 C06 统一附加）。
 */
export type SecretScanTarget = {
    title?: string;
    content?: string;
    evidence?: string;
    sourceId?: string;
};
export type SecretScanResult = {
    matched: boolean;
    ruleIds: string[];
};
export type SecretRule = {
    /** 稳定规则标识；日志与拒绝原因只引用 id，不引用正文。 */
    id: string;
    pattern: RegExp;
    /** 规则出处（审计溯源；不是正文，可进入回执与日志面）。 */
    source: string;
};
export declare const SECRET_RULES: readonly SecretRule[];
/**
 * 扫描候选四个文本字段（title/content/evidence/sourceId）。
 * 非 string 字段按缺失处理；返回 ruleIds 按规则表顺序去重，确定性输出。
 * 永不返回匹配原文、位置偏移或载荷片段。
 */
export declare function detectSecret(input: SecretScanTarget): SecretScanResult;
