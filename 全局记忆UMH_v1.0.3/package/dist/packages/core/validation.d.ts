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
import type { RememberInput } from '../contracts/index.js';
export type CandidateVerdict = {
    allowed: boolean;
    reasonCode?: 'SECRET_DETECTED' | 'EVIDENCE_REQUIRED' | 'NOT_DURABLE';
};
/**
 * 候选校验：secret 拦截 → evidence 必填 → 长期价值。
 * 返回值只含 allowed 与 reasonCode，永不携带候选原文。
 */
export declare function validateCandidate(input: RememberInput): CandidateVerdict;
