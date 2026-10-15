/**
 * packages/adapters/config-plan — 配置计划（preview）构造（A01）。
 *
 * preview 只读合同：本模块绝不写盘 —— 计划只计算 afterBytes/afterHash，落盘由
 * transaction.applyConfigPlan 在锁内经 beforeHash 核验后原子执行。
 *
 * owned 段不可覆盖他人内容（Review Focus A01）：pointer 处已有非 UMH 内容时
 * 计划失败（OWNED_POINTER_OCCUPIED），绝不静默改写第三方字段。
 *
 * 格式纪律（A 阶段全局约束）：JSON 用原生解析（字段保留可证明，键序由
 * JSON.parse 保持）；TOML/YAML 的固定版解析器无法证明注释/格式保留 → 只输出
 * 手工片段（manualInstructions），绝不做破坏性 stringify。七端各自的精确检出
 * 与配方由 A02+ 的 adapter 提供。
 */
import type { Binding, ClientId } from '../contracts/index.js';
import type { ConfigChange, ConfigPlan, CreateConfigPlanInput, Detection, LaunchSpec } from './contracts.js';
/** 通用 JSON 客户端默认 owned 段：mcpServers 命名空间下的 umh-memory 服务项。 */
export declare const DEFAULT_OWNED_POINTER = "/mcpServers/umh-memory";
/** SHA256 hex（UTF-8 字符串）。 */
export declare function sha256Hex(text: string): string;
/** JSON 值深比较（键序不敏感；数组有序）。 */
export declare function deepEqual(a: unknown, b: unknown): boolean;
/** 读取 JSON Pointer 指向的值；不存在返回 undefined。 */
export declare function getPointer(doc: unknown, pointer: string): unknown;
/** 写入 JSON Pointer（中间对象按需创建；类型冲突显式报错）。 */
export declare function setPointer(doc: Record<string, unknown>, pointer: string, value: unknown): void;
/** 删除 JSON Pointer 指向的键（父容器保留）；返回是否确有删除。 */
export declare function deletePointer(doc: Record<string, unknown>, pointer: string): boolean;
/** 通用默认计划：verified JSON 检出 → 自动 owned 段计划；否则只给手工片段。 */
export declare function createConfigPlan(input: CreateConfigPlanInput): Promise<ConfigPlan>;
/**
 * 在 detection 指定的每个配置文件的 pointer 处安置 UMH owned 段。
 * format='json' → 全自动（JSON 原生解析，字段保留可证明）；其余格式 → 只输出
 * 手工片段（未核实格式禁止破坏性 stringify）。
 */
export declare function makeOwnedEntryPlan(detection: Detection, pointer: string, launch: LaunchSpec, binding: Binding): Promise<ConfigPlan>;
/** 纯手工计划（无字节变更；UI/未核实端使用）。 */
export declare function makeManualPlan(clientId: ClientId, binding: Binding, launch: LaunchSpec, steps: readonly string[]): ConfigPlan;
/** 校验并收敛磁盘上加载的 ConfigPlan（未知字段/形状不符 → PLAN_INVALID）。 */
export declare function parseConfigPlan(value: unknown): ConfigPlan;
/** 校验并收敛单个 ConfigChange（PLAN_INVALID on 形状不符）。 */
export declare function parseConfigChange(raw: unknown): ConfigChange;
