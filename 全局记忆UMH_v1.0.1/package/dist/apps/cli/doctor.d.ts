/**
 * apps/cli/doctor — umh doctor 运维诊断命令（O03）。
 *
 * 用法：
 *   umh doctor [--umh-home <path>] [--python <path>] [--node <path>] [--json]
 *
 * 退出码：
 *   0 诊断 PASS / WARN；
 *   1 诊断 FAIL（检查项存在阻断性故障）；
 *   2 参数错误。
 */
export declare function runDoctorRoute(argv: string[]): Promise<number>;
