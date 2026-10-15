/**
 * packages/adapters/cherry — Cherry Studio 桌面端适配器（A02，UI 端）。
 *
 * 核心语义（工单 A02 + A 阶段全局约束）：Cherry Studio 是纯 UI 客户端 —— MCP 服务器
 * 与助手/Agent 的绑定存放在其内部应用数据（Windows 默认 %APPDATA%\CherryStudio），
 * 没有面向用户的可编辑配置文件。因此本适配器：
 *   - preview 零写盘：恒走 makeManualPlan（changes 恒空、只产出已核实的手工步骤），
 *     绝不因 detection 声称某种可写格式而改道自动写盘（猜测式写库被禁止）；
 *   - 手工步骤来自 CHERRY_RECIPE（docs/clients/recipes/cherry.json 的代码镜像，同步
 *     由 tests/adapters/cherry.test.mjs 强制），其中 {{command}}/{{args}}/{{env}}
 *     占位符由与自动接入端相同的 makeLaunchSpec 产物插值 —— GUI 复制字段单一来源
 *     （A01 头部合同：手工文案仅作指导，generic 的 spawnMcpFromLaunch 直接读
 *     plan.launch）；
 *   - detect 只做只读存在性探测（stat 目录），绝不进入目录读内容、绝不解析安装
 *     元数据（version 恒 null，避免猜报）；format='ui'、state='unverified' 恒定
 *     （UI 配置格式只有读内部数据库才能"核实"，而这被全局约束禁止）；
 *   - apply 只生成回执、不写任何字节：applied=false（A01 published 语义：零发布）；
 *     拒绝其它端的计划与携带 changes 的计划（UI 端永不发布）。受信绑定文件由受信
 *     管理面（apps/cli/connect.ts 的 apply 路由）写入 —— apply(plan) 无绑定形状，
 *     绝不从 plan 数据伪造绑定（C08 裁决）；
 *   - validate 三项诚实：connection/policy=UNVERIFIED（真实客户端证据归 A09）、
 *     sharedReadWrite=NOT_RUN（助手未实际调用工具前不得判 PASS/FAIL）；
 *   - remove 返回 removed=false + conflicts 手工清理指引（UI 端不存在可自动移除的
 *     UMH 磁盘足迹；声称自动移除成功即为误报）。
 *
 * 官方步骤来源（2026-10-03 核实）：https://docs.cherryai.com.cn/advanced-basic/extensions/mcp.md
 * （docs.cherry-ai.com 301 重定向至此）。关键官方规则：连接不等于绑定 —— MCP 服务器
 * 必须绑定到目标助手/Agent 才生效，普通聊天不会自动使用（工作 → Agent → 编辑 → MCP；
 * 普通聊天助手为 管理助手 → 编辑助手 → MCP）。
 */
import { stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import process from 'node:process';
import { UmhError } from '../../contracts/index.js';
import { makeManualPlan, parseConfigPlan } from '../config-plan.js';
/**
 * Cherry Studio 配方（= docs/clients/recipes/cherry.json 的代码镜像；测试强制
 * deepEqual 同步）。manualSteps 为模板：{{command}}/{{args}}/{{env}} 由 preview 用
 * plan.launch 插值。步骤按 2026-10-03 官方文档核实措辞（设置 → MCP → MCP 服务器）。
 */
export const CHERRY_RECIPE = {
    variant: 'desktop',
    version: '2.1.3',
    officialSource: 'https://docs.cherryai.com.cn/advanced-basic/extensions/mcp.md',
    verifiedAt: '2026-10-03',
    configLocation: 'Cherry Studio internal app data (UI-managed; Windows default %APPDATA%\\CherryStudio). UMH never reads or writes it; there is no user-facing config file to edit.',
    format: 'ui',
    ownedPointer: "ui://mcp-servers/umh-memory (the server entry named 'umh-memory' inside Cherry Studio's MCP server list, created and removed by the user in the UI)",
    policyLocation: "target assistant/Agent system prompt (manual paste of the 'UMH MEMORY POLICY v1' block; see docs/clients/cherry.md)",
    manualSteps: [
        'Open Cherry Studio and go to Settings (设置) → MCP → MCP Servers (MCP 服务器) → Add (添加); choose manual creation (手动创建).',
        'Set the connection type to local command / stdio (本地命令，标准输入输出). Fill the form exactly: name (名称) = umh-memory; type (类型) = stdio; command (命令) = {{command}}; arguments (参数) = {{args}}; environment variables (环境变量) = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.',
        'Save and start the server, then check the connection and the exposed tools (启动并查看工具): the umh-memory entry must be running and list the UMH tools including memory_status.',
        'Bind UMH to the target assistant/Agent: Work (工作) → Agent menu → Edit (编辑) → MCP → enable umh-memory. For a plain chat assistant use: manage assistants → edit assistant → MCP. Official rule: 连接不等于绑定 — a connected server is NOT automatically available to every assistant; normal chat without this binding will not use UMH.',
        "Inject the memory policy: paste the 'BEGIN UMH MEMORY POLICY v1' instruction block (see docs/clients/cherry.md) into that assistant's/Agent's system prompt, keeping the BEGIN/END markers so re-runs never duplicate the block.",
        'Verify from the assistant/Agent: send a new message (config changes take effect from the next message) and ask it to run the memory_status tool; do not report onboarding success until the assistant actually calls it.',
    ],
    schemaValidated: true,
};
const RECEIPT_ID_PATTERN = /^rcpt_[0-9a-f]{32}$/;
async function pathExists(path) {
    try {
        await stat(path);
        return true;
    }
    catch {
        return false;
    }
}
/** 平台标准安装/数据目录候选（win32/linux 全部位于 home 之下；darwin 另有 /Applications）。 */
function probePaths(home) {
    switch (process.platform) {
        case 'win32':
            return [
                join(home, 'AppData', 'Roaming', 'CherryStudio'),
                join(home, 'AppData', 'Local', 'Programs', 'Cherry Studio'),
            ];
        case 'darwin':
            return ['/Applications/Cherry Studio.app', join(home, 'Library', 'Application Support', 'CherryStudio')];
        default:
            return [join(home, '.config', 'CherryStudio')];
    }
}
/** recipe 步骤模板插值：GUI 复制字段与 plan.launch 严格同源（A01 头部合同）。 */
function interpolateStep(step, launch) {
    return step
        .replaceAll('{{command}}', launch.command)
        .replaceAll('{{args}}', JSON.stringify(launch.args))
        .replaceAll('{{env}}', JSON.stringify(launch.env));
}
function requireCherryDetection(detection) {
    if (detection === null || typeof detection !== 'object' || detection.clientId !== 'cherry') {
        throw new UmhError('INVALID_ARGUMENT', 'cherry adapter requires a detection for clientId cherry');
    }
}
function requireReceiptId(input) {
    const receiptId = input?.receiptId;
    if (typeof receiptId !== 'string' || !RECEIPT_ID_PATTERN.test(receiptId)) {
        throw new UmhError('RECEIPT_INVALID', `cherry adapter requires a rcpt_<32hex> receipt id: ${String(receiptId)}`);
    }
    return receiptId;
}
/** Cherry Studio 适配器：desktop 变体、纯 UI 端（零写盘）。 */
export const cherryAdapter = {
    async detect(opts) {
        if (opts === null || typeof opts !== 'object' || typeof opts.home !== 'string' || opts.home.trim() === '') {
            throw new UmhError('INVALID_ARGUMENT', 'detect requires a home directory');
        }
        // 变体独立标识：variant 原样保留（Cherry Studio 现实中只有桌面形态，desktop 为缺省）。
        const variant = typeof opts.variant === 'string' && opts.variant.trim() !== '' ? opts.variant : 'desktop';
        const candidates = probePaths(opts.home);
        if (typeof opts.configPath === 'string' && opts.configPath !== '')
            candidates.push(opts.configPath);
        let installed = false;
        for (const candidate of candidates) {
            if (await pathExists(candidate)) {
                installed = true;
                break;
            }
        }
        return {
            clientId: 'cherry',
            variant,
            version: null,
            installed,
            configPaths: [],
            format: 'ui',
            state: 'unverified',
        };
    },
    async preview(input) {
        requireCherryDetection(input?.detection);
        // 纯手工计划：与 detection 声称的格式无关 —— UI 端永不计划任何自动写盘。
        return makeManualPlan('cherry', input.binding, input.launch, CHERRY_RECIPE.manualSteps.map((step) => interpolateStep(step, input.launch)));
    },
    async apply(plan) {
        const parsed = parseConfigPlan(plan);
        if (parsed.clientId !== 'cherry') {
            throw new UmhError('PLAN_INVALID', `cherry apply refuses plans for another client: ${parsed.clientId}`);
        }
        if (parsed.changes.length !== 0) {
            throw new UmhError('PLAN_INVALID', 'cherry is a UI-managed client: apply refuses plans that carry config changes');
        }
        // 只生成回执：零发布（applied=false 沿用 A01 published 语义），零写盘；
        // receiptId 由 plan.id 确定性推导（可追溯，无需持久化 —— UI 端的完成确认在 validate/用户）。
        const receiptId = `rcpt_${createHash('sha256').update(`cherry:${parsed.id}`).digest('hex').slice(0, 32)}`;
        return { receiptId, applied: false };
    },
    async validate(input) {
        requireReceiptId(input);
        // 诚实三态：适配层无从观察 UI 手工步骤是否完成（A09 才取得真实 Cherry 证据）；
        // 助手未实际调用工具前 sharedReadWrite 恒为 NOT_RUN（工单明示）。
        return { connection: 'UNVERIFIED', sharedReadWrite: 'NOT_RUN', policy: 'UNVERIFIED' };
    },
    async remove(input) {
        requireReceiptId(input);
        // UI 端没有 UMH 写入的磁盘足迹可自动移除；removed=false + 手工指引（conflicts 通道）。
        return {
            removed: false,
            conflicts: [
                'cherry is a UI-managed client: UMH never wrote any client config, so there is nothing to auto-remove (removed stays false; claiming automated success would be a false report).',
                'Manual cleanup: in Cherry Studio delete the umh-memory MCP server entry (设置 → MCP → MCP 服务器 → umh-memory → 删除) and unbind it from every assistant/Agent (工作 → Agent 菜单 → 编辑 → MCP).',
                "Manual cleanup: delete the whole UMH MEMORY POLICY block (BEGIN to END markers) from each assistant's/Agent's system prompt.",
                'If the trusted binding file was created via umh connect apply, revoke it with: umh disconnect --receipt <receipt.json> (it lives in <umhHome>/config/bindings/ and has no Cherry UI counterpart).',
            ],
        };
    },
};
