/**
 * packages/adapters/policy-block — UMH 记忆策略指令块（A01）。
 *
 * marker 冻结：`BEGIN UMH MEMORY POLICY v1` / `END UMH MEMORY POLICY v1`。
 * 幂等合同（工单）：重复 apply 不追加第二块、保留原指令 —— 只要 BEGIN marker
 * 在场（含损坏残段 BEGIN-without-END），一律原样保留，绝不重写、绝不追加。
 * 块体用 HTML 注释包裹，保证 markdown/指令文件中语法安全。
 */
export const POLICY_BEGIN_MARKER = 'BEGIN UMH MEMORY POLICY v1';
export const POLICY_END_MARKER = 'END UMH MEMORY POLICY v1';
/** 渲染策略块（markdown 安全：HTML 注释包裹，指令逐条成行）。至少需要一条指令。 */
export function buildPolicyBlock(instructions) {
    const lines = [];
    for (const instruction of instructions) {
        if (typeof instruction !== 'string' || instruction.trim() === '') {
            throw new Error('policy instruction must be a non-empty string');
        }
        lines.push(`- ${instruction}`);
    }
    if (lines.length === 0) {
        throw new Error('policy block requires at least one instruction');
    }
    return `<!-- ${POLICY_BEGIN_MARKER} -->\n${lines.join('\n')}\n<!-- ${POLICY_END_MARKER} -->`;
}
/** 文本中是否已存在策略块（含损坏残段：只要有 BEGIN marker 即视为在场）。 */
export function hasPolicyBlock(text) {
    if (typeof text !== 'string')
        return false;
    return text.includes(POLICY_BEGIN_MARKER);
}
/**
 * 把策略块并入既有文本。
 * - 已有块（含损坏残段）→ 原样返回（changed:false）：保留原指令，绝不追加第二块。
 * - 无块 → 追加一个新块（空行分隔；保证尾部换行）。
 */
export function mergePolicyBlock(existing, instructions) {
    if (hasPolicyBlock(existing)) {
        return { text: existing, changed: false };
    }
    const block = buildPolicyBlock(instructions);
    const base = typeof existing === 'string' && existing !== '' ? existing : '';
    const prefix = base === '' ? '' : base.endsWith('\n') ? `${base}\n` : `${base}\n\n`;
    return { text: `${prefix}${block}\n`, changed: true };
}
