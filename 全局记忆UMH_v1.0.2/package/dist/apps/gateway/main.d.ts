/**
 * apps/gateway/main — stdio MCP 入口（C08）。
 *
 * CLI：`node dist/apps/gateway/main.js --binding <bindingId> --umh-home <root>`
 * 绑定在启动时从受信配置加载（总控裁决 binding）：
 *   `<umhHome>/config/bindings/<bindingId>.json` —— 完整 Binding JSON（含已
 *   解析物理 projectId），由受信管理面/A01 写入，C08 只定义读契约。model
 *   无权重载、无权覆盖。缺文件/非法 JSON/非法 binding → 启动 fail-closed。
 *
 * stdout 纪律：stdout 只承载 JSON-RPC 协议。无启动 banner、无 secret、无诊断
 * （一律 stderr）。service 不可达 → 启动即 fail-closed 可诊断错误（不用静默
 * 空结果糊弄客户端）。
 */
import { type Binding } from '../../packages/contracts/index.js';
/** 受信绑定加载（C08 读契约）：缺文件/非法 JSON/非法形状一律 fail-closed。 */
export declare function loadTrustedBinding(umhHome: string, bindingId: string): Promise<Binding>;
