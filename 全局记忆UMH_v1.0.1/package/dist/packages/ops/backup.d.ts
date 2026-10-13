/**
 * packages/ops/backup — O02 一致备份（GREEN 实现）。
 *
 * 工单 O02 L95：实现拒绝新写并 drain→flush→完整 snapshot→hash/manifest→
 * 恢复服务；异常也解除 maintenance。归档拒绝绝对/../ADS 及越界链接，
 * 不把 backup 递归打入自身。备份配额轮转（R05 A01-M2：保留最近 N 个，
 * 无界增长拒绝或轮转——本实现保留最近 BACKUP_KEEP 个，超量删最旧）。
 *
 * 备份纳入（Global Constraints + O02 必交付对照 + R05 派单）：
 *   memory（memory/global + memory/projects/**：queue/active/superseded/
 *     trash/keys/_audit/_index_log/hippocampus 审核历史/删除历史；不含 runtime
 *     进程态：owner.lock/owner.json/status.json、PID/锁、旧进程 nonce 不备份，
 *     恢复后不复用——补充计划 O02 行）；
 *   security（security/**：凭据根 master.key/_tokens.json/token/aux state+data；
 *     恢复后 ACL 重新设定——token 私有 0o600/Windows ACL）；
 *   config（config/bindings 安装绑定 hash 验证 + runtime/connections 安装引用
 *     ConnectionRecord + receipts/** 回执八字段 + config/tx-locks；
 *     R04 回执引用＋hash 形状：connection records 的 bindingHash/planHash/
 *     receiptId/receiptPath 原样纳入备份）。
 * backups/umh 自身、logs（不存在亦不建）、runtime 进程态绝不纳入。
 *
 * Maintenance 语义：「拒绝新写」由宿主实现（CLI 经 owner 锁：服务在线→
 * fail-closed 拒绝 ALREADY_RUNNING；离线→锁在手、umh start 同被拒，零写者
 * 离线快照才配 consistent:true）；本模块的备份排他锁（backups/umh/.backup.lock：
 * 并发备份串行化，异常也释放）；drainAndFlush 由宿主提供（离线无内存态可排
 * 空）；snapshot 指拷贝期源文件集合冻结。拷贝核对：每文件 copy 后对源与副本
 * 各算 sha256，不一致即 BACKUP_COPY_MISMATCH 并删除半成品——manifest 只描述
 * 已核对副本，绝不发布未核对快照。
 */
import type { BackupResult } from './contracts.js';
export type BackupService = {
    umhHome: string;
    beginMaintenance?: () => Promise<void>;
    endMaintenance?: () => Promise<void>;
    drainAndFlush?: () => Promise<void>;
};
/** 备份配额：保留最近 N 个（R05 A01-M2 无界增长→轮转）。 */
export declare const BACKUP_KEEP = 3;
/** 备份根（umhHome 内；归档自身排除面）。 */
export declare function backupRootOf(umhHome: string): string;
/** 归档内相对路径合法性（拒绝绝对/../ADS 及越界链接目标）。 */
export declare function assertSafeArchivePath(rel: string): void;
/** fsync 落盘写（C06-M5：receipt 落盘无 fsync → 本备份路径 durable 写）。 */
export declare function durableWriteFile(path: string, data: string | Buffer): Promise<void>;
/**
 * 一致备份：备份排他锁→drainAndFlush（宿主）→snapshot 拷贝（每文件源/副本
 * hash 核对，不一致拒发）→hash/manifest 落盘→配额轮转；异常也释放锁、
 * 解除宿主维护（finally）并清理半成品归档。
 */
export declare function createBackup(service: BackupService): Promise<BackupResult>;
