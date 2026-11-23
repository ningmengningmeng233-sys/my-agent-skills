/**
 * apps/cli/main — umh 管理入口（C07）。
 *
 * 路由（工单 C07：仅 import 已存在模块；A01 增补 connect/disconnect 接线）：
 *   init / start / stop / status  → apps/cli/lifecycle.ts
 *   review list|decide            → 运行中服务的私有管理通路 /admin/review|/admin/listPending
 *   reconcile                     → /admin/reconcile
 *   connect [apply] / disconnect  → apps/cli/connect.ts（A01 安全配置事务命令层）
 *   backup / restore / forget / rebuild / pending → apps/cli/maintenance.ts（O02）
 * O03 增补 doctor —— 本文件不预留、不实现。
 *
 * stdout 只输出机器可读 JSON；用法/诊断走 stderr。
 * 退出码：0 完成；1 运行时 UmhError（stdout 输出 {error:{code,message}}）；
 * 2 用法错误（stderr 提示）。
 *
 * review/reconcile 在本进程不起 worker（设计 §5：每个作用域由中央服务持有一个
 * worker）；服务未运行时明确报 SERVICE_NOT_RUNNING，绝不 spawn 竞争 worker。
 */
export {};
