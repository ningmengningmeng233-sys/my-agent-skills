/**
 * apps/cli/lifecycle — umh init/start/stop/status 命令层（C07）。
 *
 * 设计 §10：管理入口职责；总入口约束：stdout 只输出机器可读 JSON，诊断走
 * stderr。start 在本进程内前台运行服务（Windows v1 无守护进程化）；重复
 * start 由 owner 锁拒绝（ALREADY_RUNNING + ownerPid）。stop 绝不按 PID kill：
 * 读 ready record → POST /admin/stop（nonce 核验）；记录指向死 PID 时只清理
 * 属于该记录的 runtime 文件（nonce 匹配才删）。
 *
 * 边界：CLI 绝不在本进程起引擎 worker（设计 §5：每个作用域由中央服务持有一个
 * worker）；review/reconcile 走运行中服务的私有管理通路（cli/main 路由）。
 */
import { type OwnerRecord } from '../../packages/service/owner.js';
export declare class UsageError extends Error {
}
/** 初始化 UMH 根骨架（幂等；不启动任何进程）。 */
export declare function runInitCommand(argv: string[]): Promise<number>;
export declare function runStartCommand(argv: string[]): Promise<number>;
export declare function runStopCommand(argv: string[]): Promise<number>;
export declare function runStatusCommand(argv: string[]): Promise<number>;
export declare function loadAdminCredentials(umhHome: string): Promise<{
    record: OwnerRecord;
    token: string;
}>;
