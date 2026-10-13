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
import type { ClientAdapter, ConfigPlan, Detection, LaunchSpec } from '../contracts.js';
/**
 * Cherry Studio 配方（= docs/clients/recipes/cherry.json 的代码镜像；测试强制
 * deepEqual 同步）。manualSteps 为模板：{{command}}/{{args}}/{{env}} 由 preview 用
 * plan.launch 插值。步骤按 2026-10-03 官方文档核实措辞（设置 → MCP → MCP 服务器）。
 */
export declare const CHERRY_RECIPE: {
    readonly variant: "desktop";
    readonly version: "2.1.3";
    readonly officialSource: "https://docs.cherryai.com.cn/advanced-basic/extensions/mcp.md";
    readonly verifiedAt: "2026-10-03";
    readonly configLocation: "Cherry Studio internal app data (UI-managed; Windows default %APPDATA%\\CherryStudio). UMH never reads or writes it; there is no user-facing config file to edit.";
    readonly format: "ui";
    readonly ownedPointer: "ui://mcp-servers/umh-memory (the server entry named 'umh-memory' inside Cherry Studio's MCP server list, created and removed by the user in the UI)";
    readonly policyLocation: "target assistant/Agent system prompt (manual paste of the 'UMH MEMORY POLICY v1' block; see docs/clients/cherry.md)";
    readonly manualSteps: readonly ["Open Cherry Studio and go to Settings (设置) → MCP → MCP Servers (MCP 服务器) → Add (添加); choose manual creation (手动创建).", "Set the connection type to local command / stdio (本地命令，标准输入输出). Fill the form exactly: name (名称) = umh-memory; type (类型) = stdio; command (命令) = {{command}}; arguments (参数) = {{args}}; environment variables (环境变量) = {{env}}. UMH keeps env empty by design: the gateway reads its service credential from a private UMH-side file, never from env/argv/client config.", "Save and start the server, then check the connection and the exposed tools (启动并查看工具): the umh-memory entry must be running and list the UMH tools including memory_status.", "Bind UMH to the target assistant/Agent: Work (工作) → Agent menu → Edit (编辑) → MCP → enable umh-memory. For a plain chat assistant use: manage assistants → edit assistant → MCP. Official rule: 连接不等于绑定 — a connected server is NOT automatically available to every assistant; normal chat without this binding will not use UMH.", "Inject the memory policy: paste the 'BEGIN UMH MEMORY POLICY v1' instruction block (see docs/clients/cherry.md) into that assistant's/Agent's system prompt, keeping the BEGIN/END markers so re-runs never duplicate the block.", "Verify from the assistant/Agent: send a new message (config changes take effect from the next message) and ask it to run the memory_status tool; do not report onboarding success until the assistant actually calls it."];
    readonly schemaValidated: true;
};
/** Cherry Studio 适配器：desktop 变体、纯 UI 端（零写盘）。 */
export declare const cherryAdapter: ClientAdapter;
export type { ClientAdapter, ConfigPlan, Detection, LaunchSpec };
