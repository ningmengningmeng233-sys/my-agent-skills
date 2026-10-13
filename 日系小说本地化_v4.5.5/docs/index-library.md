# 索引库管理

优先使用 `lib add <path> --name <name>` 登记用户索引库；工作区 `.env` 和 CLI 参数优先于用户登记。索引库只读，按章节复杂度动态注入参考资料：普通章最多一张风格卡和一条章节索引；多场景、长章或事件密集章最多两张风格卡和两条章节索引，配额由 Token 预算策略（`compute_prompt_budget`）统一裁决。

复杂度判断由程序根据场景数量、事件数量、正文长度及战斗/感情/反转信号自动完成；不会把整库内容注入模型。

## KnowledgeHit 统一协议（v4.5.0-dev.3）

所有外部知识来源收敛到 `v4/enhance/providers.py` 的统一协议：

- `KnowledgeHit`：统一命中结构（source/kind/asset_path/snippet/sha256/score），`source` 字段保证来源隔离；
- `IndexLibraryProvider`：本地 Markdown 索引库适配器（稳定保底），实现 `search_hits()` 统一检索入口与旧的 `search_style_cards / search_chapter_indexes / get / fingerprint / healthcheck` 协议；
- `CompositeProvider`：按顺序尝试多个 Provider，单个来源故障自动降级并写 `knowledge_provider_degraded` 审计（`audit.jsonl`），命中按 (source, asset_path) 去重；
- `rewrite` 的参考资产「选取层」经统一 Provider 协议完成，内容抽取仍经 `provider.get()`，保证审计字段与指纹一致。

## FTS 缓存与只读保证

长篇资料可用 `v4.enhance.sqlite_fts_provider.SQLiteFTSProvider` 建立零外部依赖的 SQLite FTS5 索引；中文 FTS 无命中时会回退确定性子串匹配。该索引只缓存资料副本和哈希，不改变原索引库。

v4.5.0-dev.3 起索引缓存绝不写入外部只读库目录：

- 默认落盘 `<workspace>/.cache/knowledge/fts-<root-hash>.sqlite3`（`workspace` 参数提供时）；
- 未提供 workspace 时退化为系统临时缓存目录；
- 库根目录内不产生任何 `.index` 或缓存文件。
