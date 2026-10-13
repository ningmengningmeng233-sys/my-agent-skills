/**
 * apps/service/main — 中央服务进程入口（C07）。
 *
 * stdout 只输出机器可读的启动协议行（ready / error 各一行），诊断走 stderr
 * （总入口约束：stdout 不混诊断）。
 *
 * argv：
 *   --umh-home <path>   必填，UMH 根（沙盒/生产均显式传入）
 *   --python <path>     引擎解释器（默认 python）
 *   --host <addr>       仅接受 127.0.0.1（非法 bind addr 拒绝启动）
 *
 * ready 行：{"type":"ready","pid":N,"port":P,"url":"http://127.0.0.1:P",...}
 * error 行：{"type":"error","code":"...","ownerPid":N?,"message":"..."} → exit 1
 */
export {};
