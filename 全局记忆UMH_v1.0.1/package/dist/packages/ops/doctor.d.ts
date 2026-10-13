/**
 * packages/ops/doctor — UMH 运维与运行环境诊断（O03）。
 *
 * 检查项（工单 O03）：
 *   - 解释器（python / node，版本与可用性，支持 python/py 启动器）
 *   - 配置权限（不能 read 通过推断 write；真实执行探测写入）
 *   - lock owner（owner.lock / owner.json 完整性，stale 判定，foreignPID 辨识）
 *   - 端口占用（52400 或配置端口是否被外来进程占用）
 *   - 安全令牌（服务在线时 rpc token 可读性与非空）
 *   - engine handshake 版本（UPSTREAM-PIN 与 md_cg 导入能力）
 *   - policy（策略文件存在性、JSON 有效性与 forbidden 规则结构）
 *   - 索引存储（memory/global、memory/projects 可访问性）
 */
import type { DoctorOptions, DoctorResult } from './contracts.js';
export declare function runDoctor(opts: DoctorOptions): Promise<DoctorResult>;
