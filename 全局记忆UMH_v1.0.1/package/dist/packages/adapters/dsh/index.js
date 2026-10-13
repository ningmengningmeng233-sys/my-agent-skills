/**
 * packages/adapters/dsh — DSH 原生 MCP Client 适配器（A08）：cli 单变体，profile 显式选择。
 *
 * 核心语义（工单 A08 + A 阶段全局约束；官方文档 2026-10-03 核实，见
 * .lemon/evidence/A08/research.md）：
 *
 * 官方三源逐字冻结：
 *   - plugin 键（mcp-client README 逐字）：每 server 一条 Cordis patch row，
 *     `{id, name:'@deepseek-ai/dsh-mcp-client', config:{serverName, transport,
 *     command/args/env/cwd}}`；UMH 的 owned row = `id=memory-umh`
 *    （与官方三记忆示例 `memory-memorix`/`memory-mcp-reference`/`memory-engram`
 *     错开）+ `config.serverName=umh`（`[A-Za-z0-9_-]{1,32}`，作用域内唯一）+
 *     stdio launch（command/args/env，env 恒空）。entry 形态照官方示例抄，
 *     **绝不凭 npm 包名猜 YAML key**。
 *   - patch 文件与 profile（CLI README §Profiles + reference README + mcp-memory
 *     指南逐字）：自动写入面 = 显式选中的 profile 补丁
 *     `<DSH_HOME>/profiles/<profile>/cordis.patch.yml`；DSH_HOME 缺省 `~/.dsh`
 *     （reference README 失败诊断 `default ~/.dsh/logs/` 逐字），非空 DSH_HOME
 *     环境变量迁址。家级 `$DSH_HOME/cordis.patch.yml`（全机共享层）永不自动写
 *     （命中即手工片段）。多 profile 未选 → PROFILE_REQUIRED（A03/A07 同型先例，
 *     绝不隐式选第一项）；显式选择 = detect(opts.configPath) 独占（preview 做
 *     basename/`profiles/<profile>/` 形态守卫）。
 *   - 工具命名（mcp-client README 逐字）：`mcp__<serverName>__<rawName>`；
 *     UMH 的 serverName=umh → A09 记录真实 qualified 工具名与 native DSH 调用。
 *
 * YAML 编辑策略（本单最难点，工单明示"成熟 YAML parser 保留 comments"）：
 * `yaml@2.9.1`（C01 固定版）的文档模型满足自动编辑门：
 *   1. 文本级行追加：安装 = 在**文档末尾**追加一个 `- insert:` op（含 owned 单
 *      row）；已有全部字节逐字保留 —— 测试断言 after.startsWith(before)；
 *   2. 每次编辑后 `YAML.parse` 校验语法有效；
 *   3. 语义 diff：parse(before) 去掉 owned row 与 parse(after) 去掉 owned row
 *      deepEqual（无关 plugin/注释的解析等价性），owned row 值 deepEqual 预期。
 * 任何一步不满足 → fail-closed（绝不发布）。owned 冲突（含第三方同名
 * serverName、异 id 同 serverName）→ OWNED_POINTER_OCCUPIED 拒绝；文件非严格
 * YAML → 手工片段（changes 恒空）。
 *
 * 严格 YAML 门（preview 只读判定，不写盘）：
 *   - 空文件 / 仅注释 / 非 list 顶层 / 解析错误（doc.errors 非空）→ 手工；
 *   - 顶层每 op 须为含唯一键 `insert` 的 map 且其值为 row 的 seq → 否则手工；
 *   - 每 row 须为 map 且 `id` 为非空 string → 否则手工；
 *   - owned row 若在场则其 `config` 须为 map 且 serverName/command/args/env
 *     四键全为标量可比形态（含 `!!js` 的行：无关 row 原样保留；owned row 含
 *     `!!js` 即视为占用 → OWNED_POINTER_OCCUPIED，绝不求值）；
 *   - 家级共享层路径（basename `cordis.patch.yml` 但不在 `profiles/<profile>/`
 *     下）→ 手工。
 * `remove` 侧同门复核（缺省坏文件 → conflict 不触碰，绝不整文件还原备份）。
 *
 * 序列化：自写 owned op 序列化器 —— 双引号基本字符串（`\\` `"` 与控制字符按
 * YAML 双引号规则转义，中文/空格原样）、args 流式 seq、env `{}` 空 map 行内。
 * owned op 内零注释（注释归属无法区分 → 新 op 不带注释是归属纪律）。
 * `cwd` 不写（gateway 参数全为绝对路径，不引入 `!!js` 求值面）。
 *
 * apply = parseConfigPlan fail-closed → 拒其它端计划 → 全新安装目标预创建缺失
 * 父目录（profile 目录可能不存在；A05/A06/A07 先例）→ applyConfigPlan（A01
 * 事务：锁内 beforeHash → 备份 → 原子发布 → published 语义）→ saveReceipt。
 * 受信绑定文件绝不由 adapter 写入（对齐 A02-A07 先例）。
 *
 * remove（YAML 版三方比对，adapter 端实现 —— A01 removeOwnedConfig 对非 JSON
 * 格式诚实拒绝 MANUAL_REMOVAL_REQUIRED）：值级 before/after/current 三方比对 +
 * 卸载前 owned row 文本与 canonical 序列化逐字比对守卫（行尾注释/行内注释/
 * 增删键/重排格式等"解析值不变但文本有别"的用户内容一律 CONFIG_CHANGED 不触
 * 碰 —— A05 修复轮 Important-1 同型）；切除动作复用 A01 applyConfigPlan（临时
 * 反转计划：锁/beforeHash 复核/备份/原子发布全部继承，计划本身即刻丢弃、不落
 * 回执）；UMH 新建且逐字节未动的文件 → 整文件移除（A01 语义，先留适配器本地备
 * 份）。手工回执（changes=[]）→ removed:false + 清理指引（A03 修复先例）。
 *
 * validate 三项诚实：connection/policy=UNVERIFIED（真实客户端证据归 A09）、
 * sharedReadWrite=NOT_RUN（agent 未实际调用工具前不得判 PASS/FAIL）。
 *
 * 规则注入（policy-block 端内接线，A01 Minor#8）：记忆策略只进 owned 的
 * 「UMH MEMORY POLICY v1」标记块，落点项目 AGENTS.md（官方记忆指南只给
 * additive 指导语，未约定指令文件 —— 按本工作区既有文件；markdown 非
 * ConfigChange 支持格式，用户手工粘贴）。existing 内容不替换（mergePolicyBlock
 * 幂等合同）。
 *
 * provider/凭据/记忆保护面：`$DSH_HOME/.credentials.yaml` 与 sessions 永不出现
 * 在检出面（结构性零触碰 + 专测钉死），零读零写；原生记忆不停用。
 */
import { createHash, randomBytes } from 'node:crypto';
import { mkdir, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join } from 'node:path';
import process from 'node:process';
import { parseDocument, isMap, isSeq } from 'yaml';
import { UmhError } from '../../contracts/index.js';
import { deepEqual, makeManualPlan, parseConfigPlan, sha256Hex } from '../config-plan.js';
import { buildPolicyBlock } from '../policy-block.js';
import { applyConfigPlan, loadReceiptById, saveReceipt, umhHomeOfPlan } from '../transaction.js';
/**
 * DSH 的 owned 段：profile 补丁 `<DSH_HOME>/profiles/<profile>/cordis.patch.yml`
 * 的 `- insert:` op 下 `id=memory-umh` 的 row（plugin 键
 * `@deepseek-ai/dsh-mcp-client`，`config.serverName=umh`，stdio launch）。
 * pointer 写法沿用 A01 JSON-pointer 风格（remove/计划内定位用）。
 */
export const DSH_OWNED_POINTER = '/insert/memory-umh';
/** owned row 的冻结 id（与官方三记忆示例 id 错开，不冲突）。 */
export const DSH_OWNED_ID = 'memory-umh';
/** 冻结的 plugin 键（mcp-client README 逐字，绝不凭 npm 包名猜）。 */
export const DSH_PLUGIN_NAME = '@deepseek-ai/dsh-mcp-client';
/** 冻结的 serverName（官方 `[A-Za-z0-9_-]{1,32}` 规则内，作用域内唯一）。 */
export const DSH_SERVER_NAME = 'umh';
/** profile 补丁文件名（显式选择守卫用）。 */
const PATCH_BASENAME = 'cordis.patch.yml';
/**
 * UMH 记忆策略指令（渲染为 BEGIN/END UMH MEMORY POLICY v1 块，落点项目 AGENTS.md）。
 * 措辞纪律：不含 token/secret/bearer 等字样（plan/recipe 制品面禁词测试）。
 */
export const DSH_POLICY_INSTRUCTIONS = [
    '在回答前可调用 memory_status/memory_recall/memory_search/memory_get 读取项目共享记忆；读取失败不得阻塞回答。',
    '涉及用户偏好、项目决策、可复用事实时调用 memory_remember 提交记忆；一次只提交一条、正文自含。',
    '绝不把凭据、密钥或认证材料写入记忆正文。',
];
/**
 * DSH 配方（= docs/clients/recipes/dsh.json 的代码镜像；测试强制 deepEqual 同步）。
 * manualSteps 每条带节标签：[manual]=手工路径（未选 profile/家级共享层/非严格
 * YAML）、[auto]=自动路径附加指引。占位符
 * {{command}}/{{args}}/{{env}}/{{policy}} 由 preview 用 plan.launch 与模块策略插值。
 * 步骤按 2026-10-03 官方文档核实措辞（profile 补丁层/合并纪律/serverName 规则）。
 */
export const DSH_RECIPE = {
    variant: 'cli',
    version: '0.0.1-rc.1',
    officialSource: 'https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md; profiles: https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md; memory overlays: https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/mcp-memory.md',
    verifiedAt: '2026-10-03',
    configLocation: "cli: <DSH_HOME>/profiles/<profile>/cordis.patch.yml where <DSH_HOME> resolves as non-empty DSH_HOME, else ~/.dsh (official: apps/cli README 'A profile directory holds a package.json and a cordis.patch.yml'; reference README 'boots the profile at $DSH_HOME/profiles/<name>' and 'default ~/.dsh/logs/'). The machine-wide $DSH_HOME/cordis.patch.yml shared layer is never an auto-edit target; provider credentials in $DSH_HOME/.credentials.yaml are never detection targets and are never read or written",
    format: 'yaml',
    ownedPointer: "/insert/memory-umh — one '- insert:' patch row with id 'memory-umh' and name '@deepseek-ai/dsh-mcp-client' (the frozen plugin key, never guessed from an npm package name), whose config carries serverName 'umh' ([A-Za-z0-9_-]{1,32}, unique inside one registration scope) with a stdio transport ({command,args,env}; env stays {}). UMH appends only this row via text-level append + parse validation (never whole-file stringify; unrelated plugins, comments and !!js lines are preserved byte-for-byte)",
    policyLocation: "project AGENTS.md (the official memory guide ships only an additive model-instruction sentence and documents no instruction-file convention — verify against your own setup) — paste the 'BEGIN UMH MEMORY POLICY v1' block and keep the BEGIN/END markers",
    manualSteps: [
        '[manual] Choose the profile explicitly first: the per-profile patch <DSH_HOME>/profiles/<profile>/cordis.patch.yml (shipped names include web/headless/sdk/sdk-minimal/acp; custom names are user-created). When several profiles hold a patch file, UMH refuses to guess and reports PROFILE_REQUIRED — re-run detection with the explicit cordis.patch.yml path of the profile you want to connect. The machine-wide $DSH_HOME/cordis.patch.yml shared layer is never auto-edited.',
        '[manual] UMH did not auto-edit a DSH patch file (no safely auto-editable profile cordis.patch.yml was found — it may live under a custom DSH_HOME, use an unsupported shape, or not be parseable YAML, and UMH will not risk losing your plugins). Hand-merge one \'- insert:\' row as described next; UMH never edits $DSH_HOME/cordis.patch.yml (machine-shared layer) or $DSH_HOME/.credentials.yaml (provider credentials).',
        '[manual] The row to add is exactly: id = memory-umh; name = @deepseek-ai/dsh-mcp-client; config.serverName = umh; config.transport = stdio; command = {{command}}; args = {{args}}; env = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config. After discovery the tools appear as mcp__umh__<tool> (for example mcp__umh__memory_status).',
        '[manual] Inject the memory policy into the project AGENTS.md: paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}',
        '[manual] Reload the profile (restart DSH or enable HMR) and confirm the umh tools are listed; then ask the agent to run memory_status. Do not report onboarding success until the agent actually calls the tool.',
        '[auto] UMH appended the memory-umh row to the explicitly selected <DSH_HOME>/profiles/<profile>/cordis.patch.yml (a pre-edit backup and a rollback receipt were recorded under the UMH home). Every other plugin row, comments and expression lines were preserved byte-for-byte; UMH never reads sessions or credentials.',
        '[auto] Reload the profile (restart DSH or enable HMR) and confirm the umh tools are listed as mcp__umh__<tool>; then ask the agent to run memory_status. Do not report onboarding success until the agent actually calls the tool.',
        '[auto] Inject the memory policy into the project AGENTS.md: paste the following block, keeping the BEGIN/END markers so re-runs never duplicate it:\n{{policy}}',
    ],
    schemaValidated: true,
};
const RECEIPT_ID_PATTERN = /^rcpt_[0-9a-f]{32}$/;
/** 工单冻结 variant（cli 单变体；未知变体响亮拒绝，绝不静默降级）。 */
const VARIANTS = new Set(['cli']);
// ---------------------------------------------------------------------------
// YAML 双引号基本字符串（owned op 序列化用；中文/空格原样）
// ---------------------------------------------------------------------------
/** YAML 双引号标量：`\` `"` 与控制字符转义；中文/空格原样。 */
function yamlDoubleQuoted(value) {
    let out = '"';
    for (const ch of value) {
        if (ch === '\\')
            out += '\\\\';
        else if (ch === '"')
            out += '\\"';
        else if (ch === '\b')
            out += '\\b';
        else if (ch === '\t')
            out += '\\t';
        else if (ch === '\n')
            out += '\\n';
        else if (ch === '\f')
            out += '\\f';
        else if (ch === '\r')
            out += '\\r';
        else {
            const code = ch.codePointAt(0);
            if (code < 0x20 || code === 0x7f)
                out += `\\u${code.toString(16).padStart(4, '0')}`;
            else
                out += ch;
        }
    }
    return `${out}"`;
}
/** launch 对象在客户端配置中的落地形态（与 LaunchSpec 同源，字段一一对应）。 */
function launchEntryOf(launch) {
    return { command: launch.command, args: [...launch.args], env: { ...launch.env } };
}
/**
 * canonical owned op（owned row 的唯一文本形态；op 内零注释）。
 * 与 probe 验证的形态一致：args 非空时块式 seq，env 空时行内 `{}`。
 */
function canonicalOwnedOp(entry) {
    const lines = [
        '- insert:',
        '    - id: memory-umh',
        `      name: ${yamlDoubleQuoted(DSH_PLUGIN_NAME)}`,
        '      config:',
        `        serverName: ${DSH_SERVER_NAME}`,
        '        transport: stdio',
        `        command: ${yamlDoubleQuoted(entry.command)}`,
    ];
    if (entry.args.length === 0) {
        lines.push('        args: []');
    }
    else {
        lines.push('        args:');
        for (const arg of entry.args)
            lines.push(`          - ${yamlDoubleQuoted(arg)}`);
    }
    const envKeys = Object.keys(entry.env);
    if (envKeys.length === 0) {
        lines.push('        env: {}');
    }
    else {
        lines.push('        env:');
        for (const key of envKeys)
            lines.push(`          ${yamlDoubleQuoted(key)}: ${yamlDoubleQuoted(entry.env[key])}`);
    }
    return `${lines.join('\n')}\n`;
}
/** 追加 owned op：已有字节逐字保留；缺行尾补行尾（行尾跟随文件风格）。 */
function appendOwnedOp(raw, op) {
    if (raw === '')
        return op;
    const eol = raw.includes('\r\n') ? '\r\n' : '\n';
    const base = raw.endsWith('\n') ? raw : raw + eol;
    return base + op.replace(/\n/g, eol);
}
// ---------------------------------------------------------------------------
// 小原语
// ---------------------------------------------------------------------------
function sha256Buffer(bytes) {
    return createHash('sha256').update(bytes).digest('hex');
}
async function pathExists(path) {
    try {
        await stat(path);
        return true;
    }
    catch {
        return false;
    }
}
/** 非空环境变量（空串/空白视为未设置；minimax A07 同型语义）。 */
function nonEmptyEnv(name) {
    const value = process.env[name];
    if (typeof value !== 'string')
        return null;
    const trimmed = value.trim();
    return trimmed === '' ? null : trimmed;
}
/** DSH home 解析：非空 DSH_HOME → 默认 `<home>/.dsh`（官方缺省 `~/.dsh`）。 */
function dshHomeOf(home) {
    const redirected = nonEmptyEnv('DSH_HOME');
    if (redirected !== null)
        return redirected;
    return join(home, '.dsh');
}
/** 某 profile 的补丁路径。 */
function patchPathFor(dshHome, profile) {
    return join(dshHome, 'profiles', profile, PATCH_BASENAME);
}
/** 家级共享层路径（全机共享，UMH 永不自动写）。 */
function homePatchPath(dshHome) {
    return join(dshHome, PATCH_BASENAME);
}
/** recipe 步骤模板插值：GUI/手工复制字段与 plan.launch 严格同源（A01 头部合同）。 */
function interpolateStep(step, launch, policyBlock) {
    return step
        .replaceAll('{{command}}', launch.command)
        .replaceAll('{{args}}', JSON.stringify(launch.args))
        .replaceAll('{{env}}', JSON.stringify(launch.env))
        .replaceAll('{{policy}}', policyBlock);
}
/** 取指定节标签的 recipe 步骤（剥标签、插值）。 */
function stepsFor(tag, launch) {
    const prefix = `[${tag}] `;
    const policyBlock = buildPolicyBlock(DSH_POLICY_INSTRUCTIONS);
    return DSH_RECIPE.manualSteps
        .filter((step) => step.startsWith(prefix))
        .map((step) => interpolateStep(step.slice(prefix.length), launch, policyBlock));
}
function requireDshDetection(detection) {
    if (detection === null || typeof detection !== 'object' || detection.clientId !== 'dsh') {
        throw new UmhError('INVALID_ARGUMENT', 'dsh adapter requires a detection for clientId dsh');
    }
}
function requireVariant(variant) {
    // variant 决定写入路径（cli 单变体）：未知变体响亮拒绝，绝不静默降级。
    if (!VARIANTS.has(variant)) {
        throw new UmhError('INVALID_ARGUMENT', `dsh variant must be 'cli': ${String(variant)}`);
    }
}
function requireReceiptId(input) {
    const receiptId = input?.receiptId;
    if (typeof receiptId !== 'string' || !RECEIPT_ID_PATTERN.test(receiptId)) {
        throw new UmhError('RECEIPT_INVALID', `dsh adapter requires a rcpt_<32hex> receipt id: ${String(receiptId)}`);
    }
    return receiptId;
}
/**
 * 严格 YAML 判定：parseDocument 无 error + 顶层为 op 的 seq（空 seq 允许：全新
 * profile 补丁，UMH 首写）+ 每 op 为含唯一键 `insert` 的 map 且其值为 row seq +
 * 每 row 为含非空 string id 的 map。owned row（id=memory-umh）在场则其 config
 * 须为 map（serverName/command/args/env 的 `!!js` 形态在文本侧处理）。
 * Quint：注释/`!!js` 行不进语义门（字节保留由 append 保证），但 owned row 的
 * `!!js` 占用在调用方按文本判定。
 */
function strictDocOf(text) {
    const doc = parseDocument(text);
    if (doc.errors.length > 0)
        return null;
    if (doc.contents === null) {
        // 空文件 / 仅注释：严格可写（首写 canonical op；注释行由 append 保留）。
        return { doc, ops: [], ownedOpIndex: -1, ownedRowIndex: -1 };
    }
    if (!isSeq(doc.contents))
        return null;
    const ops = [];
    let ownedOpIndex = -1;
    let ownedRowIndex = -1;
    for (const [opIndex, op] of doc.contents.items.entries()) {
        if (!isMap(op))
            return null;
        // op 须为含唯一键 `insert` 的 map（items 级判定，不依赖 keys() 语义）。
        if (op.items.length !== 1)
            return null;
        const keyNode = op.items[0].key;
        if (typeof keyNode.value !== 'string' || keyNode.value !== 'insert') {
            return null;
        }
        const rows = op.get('insert');
        if (!isSeq(rows))
            return null;
        for (const row of rows.items) {
            if (!isMap(row))
                return null;
            const id = row.get('id');
            if (typeof id !== 'string' || id.trim() === '')
                return null;
            if (id === DSH_OWNED_ID && ownedOpIndex < 0) {
                ownedOpIndex = opIndex;
                ownedRowIndex = rows.items.indexOf(row);
            }
        }
        const ownedIndex = rows.items.findIndex((row) => isMap(row) && row.get('id') === DSH_OWNED_ID);
        ops.push({ node: op, rows: rows.items, ownedIndex, ownedNode: ownedIndex < 0 ? null : rows.items[ownedIndex] });
    }
    return { doc, ops, ownedOpIndex, ownedRowIndex };
}
/** owned row 的 config 在场且为 map 时取值；否则 null（调用方按占用/缺席分派）。 */
function ownedConfigOf(ownedNode, doc) {
    if (!isMap(ownedNode))
        return null;
    const config = ownedNode.get('config');
    if (config === undefined || config === null)
        return null;
    if (!isMap(config))
        return null;
    return config.toJS(doc);
}
/** 文本是否在 owned row 范围内含 `!!js`（求值面污染 → 占用拒绝，绝不求值改写）。 */
function ownedTextHasJsTag(raw, ownedNode) {
    const range = ownedNode?.range;
    if (!Array.isArray(range) || typeof range[0] !== 'number' || typeof range[1] !== 'number')
        return false;
    return raw.slice(range[0], range[1]).includes('!!js');
}
/** 无关 row 范围文本（含注释）—— remove 侧 canonical 守卫的比对基。 */
function rowTextOf(raw, rowNode) {
    const range = rowNode?.range;
    if (!Array.isArray(range) || typeof range[0] !== 'number' || typeof range[1] !== 'number')
        return null;
    return raw.slice(range[0], range[1]);
}
/**
 * 语义 doc（去掉 owned row 后的 parse 等价体）：owned row 所在 op 去掉该 row；
 * 空 op（rows 归零）整 op 去掉。用于编辑前后无关内容等价 diff。
 */
function docWithoutOwned(text) {
    const strict = strictDocOf(text);
    if (strict === null)
        return Symbol('unparseable');
    // 空文件/仅注释的 doc.toJS() 为 null（contents null）→ 视为空 op 列表。
    const js = (strict.doc.toJS() ?? []);
    const out = [];
    for (const op of js) {
        const rows = op['insert'].filter((row) => row['id'] !== DSH_OWNED_ID);
        if (rows.length > 0)
            out.push({ insert: rows });
    }
    return out;
}
/**
 * 编辑后不变量（fail-closed，任何一条不满足绝不发布）：
 *   1. after 仍是有效严格 YAML；
 *   2. owned row 值 = 预期（present）或不存在（absent）；
 *   3. 语义 diff：before/after 去掉 owned row 后 deepEqual（首次新建无 before，跳过 3）。
 */
function requireYamlEditInvariants(params) {
    const afterStrict = strictDocOf(params.afterRaw);
    if (afterStrict === null) {
        throw new UmhError('PLAN_INVALID', `internal invariant violation: post-edit YAML is not strict (${params.path})`);
    }
    const afterJs = afterStrict.doc.toJS();
    const ownedRows = [];
    for (const op of afterJs) {
        for (const row of op['insert']) {
            if (row['id'] === DSH_OWNED_ID)
                ownedRows.push(row);
        }
    }
    if (params.mode === 'present') {
        if (ownedRows.length !== 1 || !deepEqual(ownedRows[0], params.desiredOwned ?? null)) {
            throw new UmhError('PLAN_INVALID', `internal invariant violation: post-edit YAML does not carry the desired owned row (${params.path})`);
        }
    }
    if (params.mode === 'absent' && ownedRows.length !== 0) {
        throw new UmhError('PLAN_INVALID', `internal invariant violation: post-edit YAML still carries the owned row (${params.path})`);
    }
    if (params.beforeRaw !== null) {
        if (!deepEqual(docWithoutOwned(params.beforeRaw), docWithoutOwned(params.afterRaw))) {
            throw new UmhError('PLAN_INVALID', `internal invariant violation: post-edit YAML does not preserve unrelated content (${params.path})`);
        }
    }
}
/** 期望的 owned row 值（语义 diff 与 owned 值比对用；与 canonicalOwnedOp 同源）。 */
function desiredOwnedRow(launch) {
    const entry = launchEntryOf(launch);
    return {
        id: DSH_OWNED_ID,
        name: DSH_PLUGIN_NAME,
        config: {
            serverName: DSH_SERVER_NAME,
            transport: 'stdio',
            command: entry.command,
            args: [...entry.args],
            env: { ...entry.env },
        },
    };
}
// ---------------------------------------------------------------------------
// detect（只读：stat + 目录枚举 + 严格 YAML 判读；绝不写盘、不读凭据/sessions）
// ---------------------------------------------------------------------------
/** 候选 profile 补丁全集（`<dshHome>/profiles/<name>/cordis.patch.yml` 存在者）。 */
async function candidatePatches(dshHome) {
    const profilesDir = join(dshHome, 'profiles');
    let entries;
    try {
        entries = await readdir(profilesDir, { withFileTypes: true });
    }
    catch {
        return [];
    }
    const found = [];
    for (const entry of entries) {
        if (!entry.isDirectory())
            continue;
        const candidate = join(profilesDir, entry.name, PATCH_BASENAME);
        if (await pathExists(candidate))
            found.push(candidate);
    }
    return found.sort();
}
/**
 * 显式 configPath 的 profile 形态判定（preview 侧守卫；detect 只做独占受理）：
 * basename 必须为 `cordis.patch.yml` 且位于 `profiles/<profile>/` 下。家级共享层
 * （basename 对但不在 profiles 下）与其它外来补丁形态统一走手工（永不自动写出
 * 官方布局之外的文件，也绝不猜测 profile 归属）。
 */
function profileOfExplicitPath(configPath) {
    if (!isAbsolute(configPath))
        return null;
    if (basename(configPath) !== PATCH_BASENAME)
        return null;
    const parent = dirname(configPath);
    const grandparent = dirname(parent);
    if (basename(grandparent) !== 'profiles')
        return null;
    const profile = basename(parent);
    if (profile.trim() === '' || profile === '.' || profile === '..')
        return null;
    return profile;
}
async function detectCli(home, configPath) {
    const dshHome = dshHomeOf(home);
    if (typeof configPath === 'string' && configPath.trim() !== '') {
        // 显式选择：独占该路径（qoder/codex/claude/minimax 同型先例：detect 只做存在性/
        // 严格 YAML 判读，不做路径形态守卫；不明形态交由 preview 抛 PROFILE_REQUIRED）。
        const selected = configPath;
        const exists = await pathExists(selected);
        let verified = false;
        if (exists) {
            try {
                verified = strictDocOf(await readFile(selected, 'utf8')) !== null;
            }
            catch {
                verified = false;
            }
        }
        return {
            clientId: 'dsh',
            variant: 'cli',
            version: null,
            installed: exists || (await candidatePatches(dshHome)).length > 0,
            configPaths: [selected],
            format: 'yaml',
            state: verified ? 'verified' : 'unverified',
        };
    }
    // 发现模式：列出存在的 profile 补丁候选；多候选交由 preview 抛 PROFILE_REQUIRED。
    const found = await candidatePatches(dshHome);
    const installed = found.length > 0;
    let state = 'unverified';
    if (found.length === 1) {
        try {
            state = strictDocOf(await readFile(found[0], 'utf8')) !== null ? 'verified' : 'unverified';
        }
        catch {
            state = 'unverified';
        }
    }
    return { clientId: 'dsh', variant: 'cli', version: null, installed, configPaths: found, format: 'yaml', state };
}
// ---------------------------------------------------------------------------
// preview（只读；YAML 文本追加计划）
// ---------------------------------------------------------------------------
/**
 * 构造 owned row 变更：文件缺失 → 新建；严格 YAML 且 owned 缺席 → 末尾追加；
 * owned 值已等于预期 → 幂等（afterBytes = 当前字节）；值不同 → OWNED_POINTER_OCCUPIED；
 * 非严格 YAML → 调用方降级手工片段。家级共享层 → 调用方降级手工片段。
 */
async function planYamlChange(path, launch) {
    const desired = desiredOwnedRow(launch);
    const entry = launchEntryOf(launch);
    let bytes;
    try {
        bytes = await readFile(path);
    }
    catch (err) {
        if (err.code === 'ENOENT') {
            // 全新文件：owned op 即全部内容（无无关内容可失）。
            const afterBytes = canonicalOwnedOp(entry);
            requireYamlEditInvariants({ path, beforeRaw: null, afterRaw: afterBytes, mode: 'present', desiredOwned: desired });
            return {
                path,
                format: 'yaml',
                beforeHash: null,
                afterHash: sha256Hex(afterBytes),
                ownedPointer: DSH_OWNED_POINTER,
                beforeOwned: null,
                afterOwned: desired,
                afterBytes,
            };
        }
        throw new UmhError('CONFIG_PATH_INVALID', `dsh config path is not readable: ${path}`);
    }
    const beforeRaw = bytes.toString('utf8');
    const beforeHash = sha256Buffer(bytes);
    const strict = strictDocOf(beforeRaw);
    if (strict === null) {
        throw new UmhError('CONFIG_UNPARSEABLE', `dsh config is not strict YAML: ${path}`);
    }
    // 同名 serverName 冲突守卫（异 id 同 serverName）：官方逐字"同名后条目加载失败"，
    // UMH 不制造这种冲突（owned id 本身不同名也拒 —— 作用域命名空间污染同属覆盖）。
    for (const op of strict.ops) {
        for (const row of op.rows) {
            if (!isMap(row))
                continue;
            const rowId = row.get('id');
            if (rowId === DSH_OWNED_ID)
                continue;
            const config = row.get('config');
            if (!isMap(config))
                continue;
            const serverName = config.get('serverName');
            if (serverName === DSH_SERVER_NAME) {
                throw new UmhError('OWNED_POINTER_OCCUPIED', `${path} already publishes serverName '${DSH_SERVER_NAME}' under another row id '${String(rowId)}'; refusing to collide (use manual steps)`);
            }
        }
    }
    if (strict.ownedOpIndex >= 0) {
        const ownedNode = strict.ops[strict.ownedOpIndex].ownedNode;
        if (ownedTextHasJsTag(beforeRaw, ownedNode)) {
            // owned 行含 !!js 求值面：绝不求值、绝不改写 → 占用拒绝。
            throw new UmhError('OWNED_POINTER_OCCUPIED', `${path} already has other content at ${DSH_OWNED_POINTER} (an entry carrying !!js expressions); refusing to overwrite (use manual steps)`);
        }
        const current = ownedConfigOf(ownedNode, strict.doc);
        // owned row 在场但 config 非 map（缺 config/标量 config）：他人内容 → 占用拒绝。
        if (current === null) {
            throw new UmhError('OWNED_POINTER_OCCUPIED', `${path} already has other content at ${DSH_OWNED_POINTER}; refusing to overwrite (choose another pointer or use manual steps)`);
        }
        const currentRow = strict.doc.toJS()[strict.ownedOpIndex]['insert'][strict.ops[strict.ownedOpIndex].ownedIndex];
        if (!deepEqual(currentRow, desired)) {
            throw new UmhError('OWNED_POINTER_OCCUPIED', `${path} already has other content at ${DSH_OWNED_POINTER}; refusing to overwrite (choose another pointer or use manual steps)`);
        }
        const afterBytes = beforeRaw; // 幂等：目标状态已在场
        requireYamlEditInvariants({ path, beforeRaw, afterRaw: afterBytes, mode: 'present', desiredOwned: desired });
        return {
            path,
            format: 'yaml',
            beforeHash,
            afterHash: sha256Hex(afterBytes),
            ownedPointer: DSH_OWNED_POINTER,
            beforeOwned: currentRow,
            afterOwned: desired,
            afterBytes,
        };
    }
    const afterBytes = appendOwnedOp(beforeRaw, canonicalOwnedOp(entry));
    requireYamlEditInvariants({ path, beforeRaw, afterRaw: afterBytes, mode: 'present', desiredOwned: desired });
    return {
        path,
        format: 'yaml',
        beforeHash,
        afterHash: sha256Hex(afterBytes),
        ownedPointer: DSH_OWNED_POINTER,
        beforeOwned: null,
        afterOwned: desired,
        afterBytes,
    };
}
// ---------------------------------------------------------------------------
// remove（YAML 版三方比对；切除动作复用 A01 applyConfigPlan 的锁/备份/原子发布）
// ---------------------------------------------------------------------------
/** 从回执 afterOwned（owned row）重建 LaunchSpec（形状不符 RECEIPT_INVALID）。 */
function launchFromOwned(row) {
    if (row === null || typeof row !== 'object' || Array.isArray(row)) {
        throw new UmhError('RECEIPT_INVALID', 'receipt change afterOwned must be an owned row object');
    }
    const config = row.config;
    if (config === null || typeof config !== 'object' || Array.isArray(config)) {
        throw new UmhError('RECEIPT_INVALID', 'receipt owned row config must be an object');
    }
    const { command, args, env } = config;
    if (typeof command !== 'string' || command === '') {
        throw new UmhError('RECEIPT_INVALID', 'receipt owned row command must be a non-empty string');
    }
    if (!Array.isArray(args) || args.some((a) => typeof a !== 'string')) {
        throw new UmhError('RECEIPT_INVALID', 'receipt owned row args must be an array of strings');
    }
    if (env === null || typeof env !== 'object' || Array.isArray(env)) {
        throw new UmhError('RECEIPT_INVALID', 'receipt owned row env must be a Record<string,string>');
    }
    const envOut = {};
    for (const [k, v] of Object.entries(env)) {
        if (typeof v !== 'string')
            throw new UmhError('RECEIPT_INVALID', `receipt owned row env.${k} must be a string`);
        envOut[k] = v;
    }
    return { command, args: [...args], env: envOut };
}
/** 适配器本地备份（A01 backupOriginal 为私有原语；同目录同命名约定，mode 0o600）。 */
async function backupBytes(path, umhHome, bytes) {
    const dir = join(umhHome, 'backups', 'client-config');
    await mkdir(dir, { recursive: true });
    const stem = sha256Hex(path.split('\\').join('/')).slice(0, 8);
    const backupPath = join(dir, `${basename(path)}.${stem}.${new Date().toISOString().replace(/[:.]/g, '-')}.${randomBytes(4).toString('hex')}.bak`);
    await writeFile(backupPath, bytes, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
}
async function readRawOrNull(path) {
    try {
        return await readFile(path);
    }
    catch (err) {
        if (err.code === 'ENOENT')
            return null;
        throw new UmhError('CONFIG_PATH_INVALID', `dsh config path is not readable: ${path}`);
    }
}
/** 从回执 afterOwned（owned row config.command/args/env 含 --umh-home）提取 umhHome。 */
function umhHomeOfLaunch(launch) {
    const idx = launch.args.indexOf('--umh-home');
    if (idx < 0 || idx + 1 >= launch.args.length || launch.args[idx + 1].trim() === '') {
        throw new UmhError('RECEIPT_INVALID', 'receipt launch entry must carry --umh-home; cannot locate the UMH home for locking and backup');
    }
    return launch.args[idx + 1];
}
/**
 * YAML 回执的三方比对卸载（值级 before/after/current + owned row 文本 canonical
 * 逐字守卫，A05 修复轮 Important-1 同型）。
 * 切除经临时反转计划走 applyConfigPlan（锁内 beforeHash 复核 → 备份 → 原子发布），
 * 该临时计划即刻丢弃、绝不落回执。UMH 新建且逐字节未动的文件 → 整文件移除。
 */
async function removeYamlOwned(receipt) {
    const launch = launchFromOwned(receipt.changes[0]?.afterOwned);
    const umhHome = umhHomeOfLaunch(launch);
    const conflicts = [];
    const removalChanges = [];
    const fileRemovals = [];
    for (const change of receipt.changes) {
        if (change.format !== 'yaml') {
            conflicts.push(`${change.path}: non-YAML change in a dsh receipt (${change.format}); refusing (UNSUPPORTED_FORMAT)`);
            continue;
        }
        const bytes = await readRawOrNull(change.path);
        if (bytes === null)
            continue; // 文件已不在 → owned 行必然已移除（幂等）
        const raw = bytes.toString('utf8');
        const currentHash = sha256Buffer(bytes);
        const strict = strictDocOf(raw);
        if (strict === null) {
            conflicts.push(`${change.path}: config is no longer strict YAML; refusing to edit (CONFIG_UNPARSEABLE)`);
            continue;
        }
        // UMH 新建且自安装后逐字节未动 → 整文件移除（A01 语义；先备份留证）。
        if (change.beforeHash === null && currentHash === change.afterHash) {
            fileRemovals.push({ path: change.path, bytes });
            continue;
        }
        const js = strict.doc.toJS();
        const currentRow = strict.ownedOpIndex < 0 || js === null
            ? null
            : (js[strict.ownedOpIndex]['insert'][strict.ops[strict.ownedOpIndex].ownedIndex] ??
                null);
        if (deepEqual(currentRow, change.afterOwned)) {
            const ownedNode = strict.ops[strict.ownedOpIndex].ownedNode;
            const ownedText = rowTextOf(raw, ownedNode);
            if (ownedText === null || ownedText.includes('!!js')) {
                conflicts.push(`${change.path}: the owned row text differs from the UMH-written canonical form (user expressions or edits inside) — refusing to remove it (CONFIG_CHANGED)`);
                continue;
            }
            // 切除 owned row：所在 op 去掉该 row；op 空则整 op 去掉。文本级切除后
            // canonical 守卫：切除后 doc 必须严格 + 无 owned + 无关等价。
            const opNode = strict.ops[strict.ownedOpIndex].node;
            void opNode;
            const afterRaw = cutOwnedRow(raw, strict, change.afterOwned);
            if (afterRaw === null) {
                conflicts.push(`${change.path}: the owned row cannot be located textually; edit manually (CONFIG_CHANGED)`);
                continue;
            }
            try {
                requireYamlEditInvariants({ path: change.path, beforeRaw: raw, afterRaw, mode: 'absent' });
            }
            catch (err) {
                conflicts.push(`${change.path}: ${err.message}`);
                continue;
            }
            removalChanges.push({
                path: change.path,
                format: 'yaml',
                beforeHash: currentHash,
                afterHash: sha256Hex(afterRaw),
                ownedPointer: change.ownedPointer,
                beforeOwned: currentRow,
                afterOwned: null,
                afterBytes: afterRaw,
            });
        }
        else if (deepEqual(currentRow, change.beforeOwned)) {
            // 用户已还原为安装前状态：owned 行已不在，无需动作。
        }
        else {
            conflicts.push(`${change.path}: owned segment ${change.ownedPointer} was modified after installation (CONFIG_CHANGED)`);
        }
    }
    if (conflicts.length === 0 && removalChanges.length > 0) {
        // 临时反转计划：owned 行 absent 的"安装"。复用 A01 事务的全部纪律
        // （锁内 beforeHash 复核 / 备份 / 原子发布）；计划即刻丢弃，不落回执。
        const plan = {
            id: `plan_${randomBytes(12).toString('hex')}`,
            clientId: 'dsh',
            bindingId: receipt.bindingId,
            launch,
            changes: removalChanges,
            manualInstructions: [],
            dryRun: true,
            recipeVersion: 1,
        };
        try {
            await applyConfigPlan(plan);
        }
        catch (err) {
            if (err instanceof UmhError) {
                return { removed: false, conflicts: [`${err.code}: ${err.message}`] };
            }
            throw err;
        }
    }
    if (conflicts.length === 0) {
        for (const removal of fileRemovals) {
            await backupBytes(removal.path, umhHome, removal.bytes);
            await rm(removal.path, { force: false });
        }
    }
    return { removed: conflicts.length === 0, conflicts };
}
/**
 * 文本级切除 owned row：复用 doc 模型定位 —— owned op 去掉 owned row 后 toString；
 * 前缀保真门：结果必须以"去掉 owned 文本后的 before"为语义等价体（由调用方的
 * requireYamlEditInvariants 复核无关等价 + 无 owned 双重守卫）。
 * 为保注释/!!js 行字节级幸存，切除以后缀保真实现：after 必须 startsWith
 * before 去掉 owned row 文本后的前缀？—— DSH 的 owned op 恒为文档末尾追加，
 * 此处走"前缀截断 + 尾随空白归一"：after = before[0:ownedStart] 去尾随空行；
 * 若 owned 非末尾（用户后移），返回 null → CONFIG_CHANGED 不触碰。
 */
function cutOwnedRow(raw, strict, _afterOwned) {
    const ownedNode = strict.ops[strict.ownedOpIndex]?.ownedNode;
    const range = ownedNode?.range;
    if (!Array.isArray(range) || typeof range[0] !== 'number' || typeof range[1] !== 'number')
        return null;
    const tail = raw.slice(range[1]);
    // owned 必须仍在文档末尾（含尾随空白）：用户把它移走/前后插入即视为改动。
    if (tail.trim() !== '') {
        // 例外：owned op 为文档末尾 op 且其后只有其它 op 的尾随换行？—— 尾随非空即改动。
        return null;
    }
    // 末尾 op 去掉 owned row 后的形态：若该 op 还有其它 rows，切除只能去 row 文本；
    // 否则整 op 去掉（含其 `- insert:` 头行）。
    const opRows = strict.ops[strict.ownedOpIndex].rows;
    if (opRows.length > 1) {
        // 多 row op：切除 owned row 文本行（含其前导缩进与换行），保留其余 rows。
        // 以 doc 模型重建该 op 文本会丢失注释 —— 不走重建，走行切除：
        // owned row 文本 = range 扩展到整行（含行尾换行）。
        let start = range[0];
        while (start > 0 && raw[start - 1] !== '\n')
            start -= 1;
        let end = range[1];
        while (end < raw.length && raw[end] !== '\n')
            end += 1;
        if (end < raw.length)
            end += 1;
        const after = raw.slice(0, start) + raw.slice(end);
        return after;
    }
    // 单 row op：整 op 去掉。op 头行起点 = op range 起点回溯到行首。
    const opNode = strict.ops[strict.ownedOpIndex].node;
    const opRange = opNode?.range;
    if (!Array.isArray(opRange) || typeof opRange[0] !== 'number')
        return null;
    let start = opRange[0];
    while (start > 0 && raw[start - 1] !== '\n')
        start -= 1;
    const after = raw.slice(0, start).replace(/[ \t\r\n]+$/, '') + (raw.slice(0, start).trim() === '' ? '' : '\n');
    // 全文档只剩 owned op → 空文档：返回 ''（调用方整文件移除分支已处理未动文件；
    // 此处是"新建后用户改了无关处又改回"之外的形态 —— 空余注释应保留，故不吞注释）。
    return after === '' ? '' : after;
}
// ---------------------------------------------------------------------------
// adapter 工厂（dshAdapter = 缺省单例；remove 需要绑定 umhHome 时用工厂）
// ---------------------------------------------------------------------------
/** 缺省 umhHome 解析：UMH_HOME 环境变量（connect CLI 同款约定；绝对路径才接受）。 */
function umhHomeFromEnv() {
    const value = process.env.UMH_HOME;
    return typeof value === 'string' && value.trim() !== '' && isAbsolute(value) ? value : null;
}
/**
 * 构造 DSH 适配器。options.umhHome 绑定 remove 的回执查找点
 * （`<umhHome>/receipts/adapters/<receiptId>.json`）；缺省单例 dshAdapter 回退到
 * UMH_HOME 环境变量，两者皆无 → remove 响亮拒绝（绝不猜默认家目录）。
 * apply 的回执落盘点始终取 plan.launch.args 的 --umh-home（makeLaunchSpec 冻结形态）。
 */
export function makeDshAdapter(options) {
    const boundHome = typeof options?.umhHome === 'string' && options.umhHome.trim() !== '' ? options.umhHome : null;
    async function resolveRemoveHome() {
        const home = boundHome ?? umhHomeFromEnv();
        if (home === null) {
            throw new UmhError('INVALID_ARGUMENT', 'dsh adapter remove needs a UMH home: construct via makeDshAdapter({ umhHome }) or set UMH_HOME (never guessed)');
        }
        return home;
    }
    return {
        async detect(opts) {
            if (opts === null || typeof opts !== 'object' || typeof opts.home !== 'string' || opts.home.trim() === '') {
                throw new UmhError('INVALID_ARGUMENT', 'detect requires a home directory');
            }
            requireVariant(opts.variant);
            return detectCli(opts.home, opts.configPath);
        },
        async preview(input) {
            requireDshDetection(input?.detection);
            const { detection, binding, launch } = input;
            requireVariant(detection.variant);
            const paths = Array.isArray(detection.configPaths) ? detection.configPaths : [];
            // 伪造检出（格式与官方文档化 YAML 矛盾）不能推进写入路径。
            if (paths.length > 0 && detection.format !== 'yaml') {
                throw new UmhError('INVALID_ARGUMENT', `dsh detection must report format 'yaml' (got '${String(detection.format)}'); a fabricated detection cannot drive YAML patch edits`);
            }
            // 多候选：绝不隐式选第一项（profile 必须人来定）。
            if (paths.length > 1) {
                throw new UmhError('PROFILE_REQUIRED', `multiple DSH profiles detected (${paths.join('; ')}); UMH never picks one automatically — ` +
                    're-run detect with the explicit cordis.patch.yml path of the profile to connect ' +
                    '(<DSH_HOME>/profiles/<profile>/cordis.patch.yml)');
            }
            if (paths.length === 0) {
                // 无检出：给已核实的手工建库步骤（选 profile 后创建补丁）。
                return makeManualPlan('dsh', binding, launch, stepsFor('manual', launch));
            }
            const path = paths[0];
            // 路径形态守卫（preview 侧；detect 只做独占受理，不做守卫）：
            //   - basename 错（非 cordis.patch.yml）→ PROFILE_REQUIRED（绝不猜）；
            //   - 家级共享层 / 其它外来补丁形态（basename 对但不在 profiles/<profile>/
            //     下）→ 手工片段（永不自动写出官方布局之外的文件，也绝不猜 profile 归属）。
            if (basename(path) !== PATCH_BASENAME) {
                throw new UmhError('PROFILE_REQUIRED', `the selected config path does not name a DSH profile patch ('<DSH_HOME>/profiles/<profile>/cordis.patch.yml'): ${path}`);
            }
            if (profileOfExplicitPath(path) === null) {
                return makeManualPlan('dsh', binding, launch, stepsFor('manual', launch));
            }
            let rawExists = true;
            try {
                await readFile(path);
            }
            catch (err) {
                if (err.code !== 'ENOENT') {
                    throw new UmhError('CONFIG_PATH_INVALID', `dsh config path is not readable: ${path}`);
                }
                rawExists = false;
            }
            if (rawExists) {
                const raw = (await readFile(path)).toString('utf8');
                if (strictDocOf(raw) === null) {
                    // 非严格 YAML：文本编辑无法证明用户内容幸存 → 手工片段，绝不破坏性改写。
                    return makeManualPlan('dsh', binding, launch, stepsFor('manual', launch));
                }
            }
            // 严格 YAML（或缺席即将新建）：文本追加计划（只加 owned row；
            // OWNED_POINTER_OCCUPIED 守卫在 planYamlChange）。
            const change = await planYamlChange(path, launch);
            const plan = {
                id: `plan_${randomBytes(12).toString('hex')}`,
                clientId: 'dsh',
                bindingId: binding.bindingId,
                launch: { command: launch.command, args: [...launch.args], env: { ...launch.env } },
                changes: [change],
                manualInstructions: stepsFor('auto', launch),
                dryRun: true,
                recipeVersion: 1,
            };
            // 冻结形状自检（含 launch 形状、change 字段、绝对路径），fail-closed。
            return parseConfigPlan(plan);
        },
        async apply(plan) {
            const parsed = parseConfigPlan(plan);
            if (parsed.clientId !== 'dsh') {
                throw new UmhError('PLAN_INVALID', `dsh apply refuses plans for another client: ${parsed.clientId}`);
            }
            // 全新安装（beforeHash null）时目标文件与其父目录可能都不存在（profile 从未
            // 创建）：A01 publishAtomic 的同目录临时文件写需要父目录在场；空目录创建是
            // 幂等物化步骤，不触碰任何既有字节（A05/A06/A07 先例）。
            for (const change of parsed.changes) {
                await stat(change.path).catch((err) => {
                    if (err.code === 'ENOENT')
                        return mkdir(dirname(change.path), { recursive: true });
                    return undefined; // 其它 stat 异常交由 applyConfigPlan 的既有检查响亮失败
                });
            }
            // applyConfigPlan（A01 事务）内部自带 --umh-home 契约；这里同源提取用于回执落盘。
            const umhHome = umhHomeOfPlan(parsed);
            const receipt = await applyConfigPlan(parsed);
            await saveReceipt(umhHome, receipt);
            // applied =「本次发布过内容」（A01 published 语义：幂等跳过不计；手工计划恒 false）。
            return { receiptId: receipt.id, applied: receipt.published };
        },
        async validate(input) {
            requireReceiptId(input);
            // 诚实三态：适配层未做真实连通性探针（A09 才取得真实 DSH 证据）；
            // agent 未实际调用工具前 sharedReadWrite 恒 NOT_RUN（工单明示）。
            return { connection: 'UNVERIFIED', sharedReadWrite: 'NOT_RUN', policy: 'UNVERIFIED' };
        },
        async remove(input) {
            const receiptId = requireReceiptId(input);
            const receipt = await loadReceiptById(await resolveRemoveHome(), receiptId);
            // 手工计划回执（家级共享层 / 非严格 YAML / 未选 profile 的手工路径，changes
            // 恒空）：UMH 从未写过任何客户端配置字节，没有可自动移除的磁盘足迹 ——
            // removed:false + 手工清理指引（cherry 先例：声称自动移除成功即为误报）。
            if (receipt.changes.length === 0) {
                return {
                    removed: false,
                    conflicts: [
                        'dsh manual onboarding: this receipt recorded no UMH-written config changes, so there is nothing to auto-remove (removed stays false; claiming automated success would be a false report).',
                        'Manual cleanup: delete the hand-merged memory-umh row (the \'- insert:\' entry with id memory-umh and name @deepseek-ai/dsh-mcp-client) from the DSH profile patch you edited (<DSH_HOME>/profiles/<profile>/cordis.patch.yml or the machine-wide $DSH_HOME/cordis.patch.yml). Never replace the whole file: it may already contain unrelated user patches.',
                        'Manual cleanup: delete the whole UMH MEMORY POLICY block (BEGIN to END markers) from the project AGENTS.md.',
                        'If the trusted binding file was created via umh connect apply, revoke it with: umh disconnect --receipt <receipt.json> (it lives in <umhHome>/config/bindings/ and has no DSH-side counterpart).',
                    ],
                };
            }
            // YAML 版三方比对（值级 before/after/current + owned row 文本 canonical 逐字
            // 守卫）；切除复用 A01 事务纪律。用户后改 owned 行 → conflict 不触碰。
            return removeYamlOwned(receipt);
        },
    };
}
/** DSH 适配器缺省单例（remove 依赖 UMH_HOME；测试/嵌入场景建议 makeDshAdapter 绑定）。 */
export const dshAdapter = makeDshAdapter();
