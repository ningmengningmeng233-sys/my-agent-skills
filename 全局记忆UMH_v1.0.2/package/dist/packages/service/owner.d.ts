/**
 * packages/service/owner — 服务所有者与运行记录（C07）。
 *
 * 设计 §5/§10 + 工单 C07 + 总控裁决：
 *   - 单一所有者：owner record = {instanceId(随机), PID, process start, nonce,
 *     protocol version, loopback port}；锁获取失败即拒绝启动，绝不无锁继续。
 *   - stale 判定用完整记录（nonce + process start time），绝不只看 PID；存活
 *     且 start 吻合 → ALREADY_RUNNING（带 ownerPid）；PID 已死或 start 不符 →
 *     陈旧锁安全接管（重命名记录，绝不向任何进程发信号——foreign PID 不可 kill）。
 *   - 记录无法解析/无法核验 → fail closed 拒绝启动（OWNER_LOCK_CORRUPT /
 *     OWNER_LOCK_UNVERIFIED），锁文件保持原样。
 *   - boot readiness（裁决 1）：先初始化 global worker 并 health 成功，再写
 *     ready owner record —— owner 测试的初始 worker 计数为 1；项目 worker 按
 *     绑定请求懒启动，第二个 Gateway 绝不再起 global 实例。
 *   - 锁/原子文件复用 C01 批准的 MemoraX 模式（O_EXCL + 原子 rename 写），
 *     不携带 cloud provider。
 *   - startService({umhHome,python}) 拉起 apps/service/main.js 子进程并等待
 *     ready 行；ServiceHandle = {pid,url,stop(),admin}；stopService(handle)
 *     经 nonce 核验的 /admin/stop 优雅停机——只终止自己 spawn 且 nonce 核验的
 *     进程。失败时（exit/stderr）在抛错前捕获子进程 stderr 尾部与退出码，
 *     证据随测试失败信息保留（makeSandbox 清理发生在其后，无法销毁）。
 *
 * 运行时布局（设计 §6）：
 *   <umhHome>/runtime/owner.lock    O_EXCL 锁 + 全量 owner record（权威）
 *   <umhHome>/runtime/owner.json    ready record（stop/status 读取）
 *   <umhHome>/runtime/status.json   worker registry 快照（CLI status 消费）
 *   <umhHome>/security/rpc/token    RPC token（rpc.ts 管理）
 */
import { ServiceAdminClient, type ReadService } from './rpc.js';
export declare const SERVICE_PROTOCOL_VERSION_EXPORT = 1;
/** 全量 owner record（stale 判定的完整依据；绝不只看 PID）。 */
export interface OwnerRecord {
    version: 1;
    protocolVersion: number;
    instanceId: string;
    pid: number;
    processStartMs: number;
    nonce: string;
    port: number;
    state: 'booting' | 'ready';
    startedAt: string;
}
export declare function runtimeDirOf(umhHome: string): string;
/** 本进程启动时刻（epoch ms）。 */
export declare function selfProcessStartMs(): number;
/** PID 存活探测（signal 0；EPERM 视为存活——fail closed 方向）。 */
export declare function isPidAlive(pid: number): boolean;
/**
 * 查询任意 PID 的 process start time（epoch ms）。
 * Windows：PowerShell Get-Process（与自报公式同源系统时钟，比较容差 2s）。
 * POSIX：/proc/<pid>/stat starttime（自举 ticks）→ 与 btime 换算 epoch ms。
 * 查询不可用/进程消失 → null（调用方 fail closed 或判 stale）。
 */
export declare function queryProcessStartMs(pid: number): number | null;
export interface OwnerLock {
    record: OwnerRecord;
    /** boot readiness 后提升为 ready 并写 port（同时写 owner.json ready record）。 */
    promoteReady(port: number): Promise<void>;
    /** 释放自己的 runtime 记录（nonce 核验后才删除；绝不碰他人记录）。 */
    release(): Promise<void>;
}
/**
 * 获取 owner 锁。O_EXCL 失败 → 读取既有记录并做完整 stale 判定：
 *   记录不可解析            → OWNER_LOCK_CORRUPT（fail closed，锁保持原样）
 *   PID 已死                → 陈旧 → 清除后重试
 *   PID 存活但 start 不符   → 陈旧（PID 复用/伪造）→ 清除后重试
 *   PID 存活且 start 吻合   → ALREADY_RUNNING(ownerPid)（绝不发信号）
 *   start 无法查询          → OWNER_LOCK_UNVERIFIED（fail closed）
 */
export declare function acquireOwnerLock(umhHome: string): Promise<OwnerLock>;
/** 读取 ready record（owner.json；不存在或损坏 → null）。 */
export declare function readReadyRecord(umhHome: string): Promise<OwnerRecord | null>;
export interface ServiceRuntimeOptions {
    umhHome: string;
    python: string;
    host?: string;
    policyFile?: string;
    /**
     * 读服务注入点（语义不变）：测试/特殊部署可注入 fake 覆盖。C09 起缺省接入
     * 真实读服务（core/read.ts createReadService，绑定本运行时的引擎池）——
     * C07 时代"缺省不注入即 READ_SERVICE_UNAVAILABLE"的缺省行为由此替换。
     */
    readService?: ReadService;
}
export interface ServiceRuntime {
    pid: number;
    port: number;
    url: string;
    instanceId: string;
    nonce: string;
    protocolVersion: number;
    stop(): Promise<void>;
    stopped: Promise<void>;
}
export declare function runServiceRuntime(opts: ServiceRuntimeOptions): Promise<ServiceRuntime>;
export interface StartServiceOptions {
    umhHome: string;
    python?: string;
    /** 完整替换子进程 env（沙盒隔离用）；缺省继承当前进程 env。 */
    env?: NodeJS.ProcessEnv;
    readyTimeoutMs?: number;
}
export interface ServiceHandle {
    pid: number;
    url: string;
    nonce: string;
    stop(): Promise<void>;
    admin: ServiceAdminClient;
}
/**
 * 启动中央服务子进程并等待 ready 行。失败（拒绝/崩溃/超时）时抛出的错误包含
 * 退出码与 stderr 尾部 —— 证据在抛错时已固化，随后才发生测试沙盒清理。
 */
export declare function startService(opts: StartServiceOptions): Promise<ServiceHandle>;
/** 停止 startService 启动的服务（nonce 核验的优雅停机）。 */
export declare function stopService(handle: ServiceHandle): Promise<void>;
