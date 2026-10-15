/**
 * packages/ops/contracts — O02 一致备份/恢复/重建公共合同。
 *
 * 冻结形状（原运维工单 O02 共用接口 L25-35）：
 *   BackupManifest.version 恒 1；files[].kind 仅 memory|security|config
 *   （receipts/config/bindings/connections 统一归 config；memory 覆盖
 *   queue/active/trash/keys/audit/deletions；security 覆盖凭据根）。
 *   engineSha 为 UPSTREAM-PIN dsh-memory pinnedSha（引擎版本真源），
 *   恢复时版本/SHA 不符即拒绝（RESTORE_VERSION_MISMATCH）。
 *
 * Maintenance 管理面扩展（工单 Interfaces）：
 *   管理 handle 增加 forget(scope,id), restore(scope,id),
 *   rebuildIndex(scope), listPending(scope) —— listPending 已在
 *   ServiceAdminClient 存在，此处仅作类型声明；forget/restore/rebuild
 *   由 packages/ops/rebuild.ts 经原生 worker 提供，不改 service/rpc。
 */
export {};
