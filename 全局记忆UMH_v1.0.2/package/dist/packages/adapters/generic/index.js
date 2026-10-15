/**
 * packages/adapters/generic — 通用 MCP 客户端适配器（A09）。
 *
 * 核心语义（工单 A09 + A 阶段全局约束 + 设计 §9 通用 MCP 行）：
 * 通用 MCP 是"一等能力"（设计 §9）—— 新增 MCP 客户端只配置通用 Gateway
 * 即可读取已有中央记忆，Core 与数据库格式不变（tests/e2e/new-client.test.mjs
 * 实证）。专用适配器只负责配置便利性；generic 不猜写任何 client config。
 *
 *   - detect：只读存在性探测（stat 目录），version 恒 null（不猜版本），
 *     format='json'、state='unverified' 恒定（未知客户端无已核实可写路径），
 *     configPaths 恒 []。variant 原样保留（各变体独立标识；空变体缺省 'mcp'）。
 *     绝不进入目录读内容、绝不解析安装元数据（cherry 先例）。
 *   - preview：零写盘，恒 makeManualPlan（changes 恒空、只产出已核实的通用
 *     JSON 手工片段），绝不因 detection 声称某种可写格式而改道自动写盘
 *     （猜测式写库被禁止；cherry 同型）。中/英简版 policy 均先 recall、后候选、
 *     pending 不说已记住（候选不报保存成功，设计 §7 状态合同）。
 *     {{command}}/{{args}}/{{env}}/{{policy}} 由 preview 用 plan.launch 与模块
 *     策略插值 —— GUI/手工复制字段单一来源（A01 头部合同：
 *     spawnMcpFromLaunch 直接读 plan.launch，绝不从 manualInstructions 解析命令）。
 *   - apply：只生成回执、不写任何字节：applied=false（A01 published 语义：
 *     零发布）；拒绝其它端的计划与携带 changes 的计划（通用端永不发布）。
 *     受信绑定文件由受信管理面（apps/cli/connect.ts 的 apply 路由）写入 ——
 *     apply 无绑定形状，绝不从 plan 数据伪造绑定（C08 裁决；A02-A08 先例）。
 *   - validate：三项诚实 connection/policy=UNVERIFIED、sharedReadWrite=NOT_RUN
 *     （未实际调用前不得判 PASS/FAIL；真实证据归七端现场检查表）。
 *   - remove：removed=false + conflicts 手工清理指引（通用端不存在可自动移除
 *     的 UMH 磁盘足迹；声称自动移除成功即为误报；cherry 先例）。
 *
 * launch.env 恒空（makeLaunchSpec 冻结）；plan 全链路无 token/secret/bearer。
 */
import { createHash } from 'node:crypto';
import { UmhError } from '../../contracts/index.js';
import { makeManualPlan, parseConfigPlan } from '../config-plan.js';
import { buildPolicyBlock } from '../policy-block.js';
/**
 * 通用 JSON 端的 owned 段（与 A01 缺省指针一致 —— 手工片段的目标位置说明用，
 * 通用端永不自动写盘）。
 */
export const GENERIC_OWNED_POINTER = '/mcpServers/umh-memory';
/**
 * UMH 记忆策略指令（中文简版；渲染为 BEGIN/END UMH MEMORY POLICY v1 块）。
 * 措辞纪律：先 recall、后候选、pending 不说已记住；不含 token/secret/bearer
 * 等字样（plan 制品面禁词测试）。
 */
export const GENERIC_POLICY_INSTRUCTIONS_CN = [
    '先用 memory_status/memory_recall/memory_search/memory_get 读取已有共享记忆；读取失败不得阻塞回答。',
    '涉及用户偏好、项目决策、可复用事实时再用 memory_remember 提交候选；一次只提交一条、正文自含。',
    '候选只返回 pending_review 时不说已记住，等审核通过可读回后再使用。',
    '绝不把凭据、密钥或认证材料写入记忆正文。',
];
/**
 * UMH 记忆策略指令（英文简版；同一先 recall 后候选纪律）。
 * 措辞纪律同中文简版。
 */
export const GENERIC_POLICY_INSTRUCTIONS_EN = [
    'Recall existing shared memories first with memory_status/memory_recall/memory_search/memory_get; a failed recall never blocks the answer.',
    'Then submit candidates with memory_remember when user preferences, project decisions or reusable facts appear; one self-contained entry at a time.',
    'A candidate that only returns pending_review is not yet remembered: say so honestly and use it only after review confirms it is readable.',
    'Never write credentials, keys or authentication material into memory content.',
];
/** 通用手工片段模板（[auto] 节：唯一节；占位符由 preview 插值）。 */
export const GENERIC_MANUAL_STEPS_AUTO = [
    'Add a generic MCP stdio server named umh-memory with exactly: command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.',
    'Inject the memory policy (Chinese): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy_cn}}',
    'Inject the memory policy (English short version): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy_en}}',
    'Restart the client (or start a new session) and ask it to run memory_status; do not report onboarding success until the client actually calls the tool.',
];
/** 通用手工片段模板（[manual] 节：与 [auto] 同文；通用端无自动写盘故两节一致）。 */
export const GENERIC_MANUAL_STEPS_MANUAL = [
    'UMH did not auto-edit any client configuration (the generic client has no verified writable path; UMH never guesses a config location). Add a generic MCP stdio server named umh-memory with exactly: command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.',
    'Inject the memory policy (Chinese): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy_cn}}',
    'Inject the memory policy (English short version): paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy_en}}',
    'Restart the client (or start a new session) and ask it to run memory_status; do not report onboarding success until the client actually calls the tool.',
];
const RECEIPT_ID_PATTERN = /^rcpt_[0-9a-f]{32}$/;
function requireGenericDetection(detection) {
    if (detection === null || typeof detection !== 'object' || detection.clientId !== 'generic') {
        throw new UmhError('INVALID_ARGUMENT', 'generic adapter requires a detection for clientId generic');
    }
}
function requireReceiptId(input) {
    const receiptId = input?.receiptId;
    if (typeof receiptId !== 'string' || !RECEIPT_ID_PATTERN.test(receiptId)) {
        throw new UmhError('RECEIPT_INVALID', `generic adapter requires a rcpt_<32hex> receipt id: ${String(receiptId)}`);
    }
    return receiptId;
}
/** recipe 步骤模板插值：GUI/手工复制字段与 plan.launch 严格同源（A01 头部合同）。 */
function interpolateStep(step, launch) {
    return step
        .replaceAll('{{command}}', launch.command)
        .replaceAll('{{args}}', JSON.stringify(launch.args))
        .replaceAll('{{env}}', JSON.stringify(launch.env))
        .replaceAll('{{policy_cn}}', buildPolicyBlock(GENERIC_POLICY_INSTRUCTIONS_CN))
        .replaceAll('{{policy_en}}', buildPolicyBlock(GENERIC_POLICY_INSTRUCTIONS_EN))
        .replaceAll('{{policy}}', buildPolicyBlock(GENERIC_POLICY_INSTRUCTIONS_CN));
}
/** 通用 MCP 适配器：任意 variant、纯手工端（零写盘）。 */
export const genericAdapter = {
    async detect(opts) {
        if (opts === null || typeof opts !== 'object' || typeof opts.home !== 'string' || opts.home.trim() === '') {
            throw new UmhError('INVALID_ARGUMENT', 'detect requires a home directory');
        }
        // 变体独立标识：variant 原样保留（未知客户端的变体形态不可枚举；空变体缺省 'mcp'）。
        const variant = typeof opts.variant === 'string' && opts.variant.trim() !== '' ? opts.variant : 'mcp';
        return {
            clientId: 'generic',
            variant,
            version: null,
            installed: false,
            configPaths: [],
            format: 'json',
            state: 'unverified',
        };
    },
    async preview(input) {
        requireGenericDetection(input?.detection);
        // 纯手工计划：与 detection 声称的格式无关 —— 通用端永不计划任何自动写盘。
        return makeManualPlan('generic', input.binding, input.launch, GENERIC_MANUAL_STEPS_MANUAL.map((step) => interpolateStep(step, input.launch)));
    },
    async apply(plan) {
        const parsed = parseConfigPlan(plan);
        if (parsed.clientId !== 'generic') {
            throw new UmhError('PLAN_INVALID', `generic apply refuses plans for another client: ${parsed.clientId}`);
        }
        if (parsed.changes.length !== 0) {
            throw new UmhError('PLAN_INVALID', 'generic is a manually-configured client: apply refuses plans that carry config changes');
        }
        // 只生成回执：零发布（applied=false 沿用 A01 published 语义），零写盘；
        // receiptId 由 plan.id 确定性推导（可追溯，无需持久化 —— 通用端的完成确认在 validate/用户）。
        const receiptId = `rcpt_${createHash('sha256').update(`generic:${parsed.id}`).digest('hex').slice(0, 32)}`;
        return { receiptId, applied: false };
    },
    async validate(input) {
        requireReceiptId(input);
        // 诚实三态：适配层无从观察手工步骤是否完成（真实证据归七端现场检查表）；
        // 客户端未实际调用工具前 sharedReadWrite 恒为 NOT_RUN（工单明示）。
        return { connection: 'UNVERIFIED', sharedReadWrite: 'NOT_RUN', policy: 'UNVERIFIED' };
    },
    async remove(input) {
        requireReceiptId(input);
        // 通用端没有 UMH 写入的磁盘足迹可自动移除；removed=false + 手工指引（conflicts 通道）。
        return {
            removed: false,
            conflicts: [
                'generic is a manually-configured client: UMH never wrote any client config, so there is nothing to auto-remove (removed stays false; claiming automated success would be a false report).',
                'Manual cleanup: in the generic MCP client delete the umh-memory stdio server entry (command/args/env from the generic plan launch).',
                'Manual cleanup: delete the whole UMH MEMORY POLICY block (BEGIN to END markers) from the instruction file it was pasted into.',
                'If the trusted binding file was created via umh connect apply, revoke it with: umh disconnect --receipt <receipt.json> (it lives in <umhHome>/config/bindings/ and has no generic-client counterpart).',
            ],
        };
    },
};
