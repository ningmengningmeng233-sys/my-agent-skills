/**
 * apps/cli/maintenance — umh backup/restore/rebuild 管理路由（O02）。
 *
 * stdout 只输出机器可读 JSON；用法/诊断走 stderr。
 * 退出码：0 成功；1 操作/验收失败（含恢复冲突，不自动覆盖）；2 参数/权限错误。
 */
/** umh backup --umh-home <path> */
export declare function runBackupRoute(argv: string[]): Promise<number>;
/** umh restore preview|apply --manifest <path> --target <path> */
export declare function runRestoreRoute(argv: string[]): Promise<number>;
/**
 * umh forget|restore|rebuild|pending --umh-home <path> [--project <id>] [--id <node>]
 * 管理面（运行中服务；绝不本地起竞争 worker——rebuild/forget/restore 经
 * EnginePool 直连引擎根；listPending 走私有管理通路）。
 */
export declare function runForgetRoute(argv: string[]): Promise<number>;
export declare function runRestoreNodeRoute(argv: string[]): Promise<number>;
export declare function runRebuildRoute(argv: string[]): Promise<number>;
export declare function runPendingRoute(argv: string[]): Promise<number>;
