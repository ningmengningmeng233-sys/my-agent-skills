/**
 * packages/core/validation.ts — 入参与长期价值校验（C05）。
 *
 * 保存通路位置（设计 §7）：schema 校验 → secret 拦截 → 长期价值检查 → …。
 * 工单 C05 固定顺序（代码块逐字约束）：
 *   if (detectSecret(input).matched) return {allowed:false,reasonCode:'SECRET_DETECTED'};
 *   if (!input.evidence.trim())      return {allowed:false,reasonCode:'EVIDENCE_REQUIRED'};
 *
 * 长期价值检查是显式的、保守的规则集合（NOT_DURABLE），只覆盖三类有明确
 * 形态特征的瞬时内容（临时待办、原始 shell 输出、纯寒暄）；本模块不宣称
 * 能判断一切自然语言，模糊内容一律 allowed 留审核（设计 §7：候选不混入
 * 正常召回，审核可确认用户决定，但不能把自然语言断言改名为机器验证）。
 *
 * 日志纪律（设计 §8）：拒绝日志只允许 ruleId / 内容哈希 / 状态，不携带候选原文。
 */
import { detectSecret } from './secrets.js';
/**
 * DUR-1 临时待办：title 或 content 以待办标记开头（允许可选的 markdown 列表
 * 前缀）。中文标记后要求分隔符或行尾，避免误伤「待办管理约定」一类以
 * 同词开头的耐久约定；西文 todo 要求冒号。只锚定开头——正文中间提到
 * TODO 的耐久约定不受影响。允许漏报，模糊情形留审核。
 */
const TODO_MARKER = /^(?:[-*+]\s+|\d+[.)]\s+)?(?:todo\s*[:：]|待办事项\s*[:：]|待办\s*[:：]|提醒我(?=$|[\s:：,，。!！?？])|稍后提醒(?=$|[\s:：,，。!！?？])|remind me\b|note to self\b)/i;
/**
 * DUR-2 原始 shell 输出：content 以 shell 提示符开头（POSIX `$ `、PowerShell
 * `PS X:\`、cmd 盘符提示）。锚定开头——正文中间引用命令的 procedure 不受影响。
 */
const SHELL_PROMPT = /^(?:\$(?=\s)|PS\s+[A-Za-z]:\\|[A-Za-z]:\\[^>\r\n]{0,80}>)/;
/**
 * DUR-3 纯寒暄：content 整体（去首尾空白）就是一句问候/致谢/应答加标点。
 * 只对整体匹配生效——以问候开头的正记录不受影响。
 */
const CHITCHAT_ONLY = /^(?:你好|您好|hello|hi|hey|嗨|哈喽|喂|在吗|早上好|下午好|晚上好|晚安|谢谢|多谢|辛苦了|thanks|thank you|好的|收到|了解|嗯|嗯嗯|ok|okay)[\s!！?？,，。.~～、]*$/i;
function matchesAny(pattern, texts) {
    return texts.some((text) => typeof text === 'string' && text.length > 0 && pattern.test(text));
}
/**
 * 长期价值检查（规则版）。只扫描 title/content；evidence 是引用性声明
 * （设计 §7：模型生成的 evidence 不自动证明内容正确），仅凭“证据只引用了
 * 瞬时上下文”不构成拒绝条件。
 */
function isDurableEnough(title, content) {
    if (matchesAny(TODO_MARKER, [title, content]))
        return false;
    if (typeof content === 'string' && SHELL_PROMPT.test(content))
        return false;
    if (typeof content === 'string' && CHITCHAT_ONLY.test(content.trim()))
        return false;
    return true;
}
/**
 * 候选校验：secret 拦截 → evidence 必填 → 长期价值。
 * 返回值只含 allowed 与 reasonCode，永不携带候选原文。
 */
export function validateCandidate(input) {
    if (detectSecret({
        title: input.title,
        content: input.content,
        evidence: input.evidence,
        sourceId: input.sourceId,
    }).matched) {
        return { allowed: false, reasonCode: 'SECRET_DETECTED' };
    }
    if (typeof input.evidence !== 'string' || input.evidence.trim().length === 0) {
        return { allowed: false, reasonCode: 'EVIDENCE_REQUIRED' };
    }
    if (!isDurableEnough(input.title, input.content)) {
        return { allowed: false, reasonCode: 'NOT_DURABLE' };
    }
    return { allowed: true };
}
