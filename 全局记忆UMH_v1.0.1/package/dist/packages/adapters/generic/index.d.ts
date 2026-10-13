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
import type { ClientAdapter, ConfigPlan, Detection, LaunchSpec } from '../contracts.js';
/**
 * 通用 JSON 端的 owned 段（与 A01 缺省指针一致 —— 手工片段的目标位置说明用，
 * 通用端永不自动写盘）。
 */
export declare const GENERIC_OWNED_POINTER = "/mcpServers/umh-memory";
/**
 * UMH 记忆策略指令（中文简版；渲染为 BEGIN/END UMH MEMORY POLICY v1 块）。
 * 措辞纪律：先 recall、后候选、pending 不说已记住；不含 token/secret/bearer
 * 等字样（plan 制品面禁词测试）。
 */
export declare const GENERIC_POLICY_INSTRUCTIONS_CN: readonly string[];
/**
 * UMH 记忆策略指令（英文简版；同一先 recall 后候选纪律）。
 * 措辞纪律同中文简版。
 */
export declare const GENERIC_POLICY_INSTRUCTIONS_EN: readonly string[];
/** 通用手工片段模板（[auto] 节：唯一节；占位符由 preview 插值）。 */
export declare const GENERIC_MANUAL_STEPS_AUTO: readonly string[];
/** 通用手工片段模板（[manual] 节：与 [auto] 同文；通用端无自动写盘故两节一致）。 */
export declare const GENERIC_MANUAL_STEPS_MANUAL: readonly string[];
/** 通用 MCP 适配器：任意 variant、纯手工端（零写盘）。 */
export declare const genericAdapter: ClientAdapter;
export type { ClientAdapter, ConfigPlan, Detection, LaunchSpec };
