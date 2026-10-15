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
const UPSTREAM_SOURCE = 'ported from vendor/mdcg/data/policy.json forbidden rules ' +
    '(upstream dsh-memory bfbdcb923da1ef2880dd3f9f979cf419d9211eb0, C01-approved)';
const C05_SOURCE = 'added by C05 (design section 8 cookie coverage gap)';
export const SECRET_RULES = Object.freeze([
    {
        id: 'PRIVATE_KEY_BLOCK',
        pattern: /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
        source: UPSTREAM_SOURCE,
    },
    {
        id: 'SK_STYLE_API_KEY',
        pattern: /\bsk-[A-Za-z0-9_\-]{20,}/,
        source: UPSTREAM_SOURCE,
    },
    {
        id: 'AWS_ACCESS_KEY_ID',
        pattern: /\bAKIA[0-9A-Z]{16}\b/,
        source: UPSTREAM_SOURCE,
    },
    {
        id: 'GITHUB_TOKEN',
        pattern: /\bgh[pousr]_[A-Za-z0-9]{20,}\b/,
        source: UPSTREAM_SOURCE,
    },
    {
        id: 'API_KEY_ASSIGNMENT',
        pattern: /\bapi[_-]?key\s*[:=]\s*[A-Za-z0-9_\-]{16,}/i,
        source: UPSTREAM_SOURCE,
    },
    {
        id: 'SECRET_KEY_ASSIGNMENT',
        pattern: /\b(?:app[_-]?secret|appsecret|client[_-]?secret|secret[_-]?key|access[_-]?key)\s*[:=]?\s*[A-Za-z0-9]{16,}/i,
        source: UPSTREAM_SOURCE,
    },
    {
        id: 'PASSWORD_ASSIGNMENT',
        pattern: /\b(?:password|passwd|pwd|密码|口令)\s*[:=：]\s*\S{6,}/i,
        source: UPSTREAM_SOURCE,
    },
    {
        id: 'BEARER_TOKEN',
        pattern: /\bBearer\s+[A-Za-z0-9._\-]{20,}/i,
        source: UPSTREAM_SOURCE,
    },
    {
        id: 'URL_EMBEDDED_CREDENTIAL',
        pattern: /[a-z][a-z0-9+.\-]*:\/\/[^/\s:@]+:[^/\s@]+@/,
        source: UPSTREAM_SOURCE,
    },
    {
        id: 'MDCG_TOKEN_ID',
        pattern: /\btk_[0-9a-f]{8,}\b/,
        source: UPSTREAM_SOURCE,
    },
    {
        id: 'MDCG_FULL_TOKEN',
        pattern: /mdcg1\.[A-Za-z0-9._\-]{20,}/,
        source: UPSTREAM_SOURCE,
    },
    {
        id: 'COOKIE_HEADER',
        pattern: /(?:^|[\r\n])\s*(?:set-)?cookie\s*:/i,
        source: C05_SOURCE,
    },
    {
        id: 'SESSION_COOKIE_PAIR',
        pattern: /\b(?:jsessionid|phpsessid|sessionid|session[_-]?id|session)\s*=\s*[A-Za-z0-9_\-]{12,}/i,
        source: C05_SOURCE,
    },
]);
/**
 * 扫描候选四个文本字段（title/content/evidence/sourceId）。
 * 非 string 字段按缺失处理；返回 ruleIds 按规则表顺序去重，确定性输出。
 * 永不返回匹配原文、位置偏移或载荷片段。
 */
export function detectSecret(input) {
    const fields = [input.title, input.content, input.evidence, input.sourceId];
    const ruleIds = [];
    for (const rule of SECRET_RULES) {
        for (const text of fields) {
            if (typeof text === 'string' && text.length > 0 && rule.pattern.test(text)) {
                ruleIds.push(rule.id);
                break;
            }
        }
    }
    return { matched: ruleIds.length > 0, ruleIds };
}
