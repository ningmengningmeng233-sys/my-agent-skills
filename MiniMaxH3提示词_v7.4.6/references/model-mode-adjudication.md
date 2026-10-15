# SKILL.md §1.1 规范生成模式与兼容别名

> 归属：`MiniMaxH3提示词_v7.4.2` 技能仓 SKILL.md 挪出段全文，governance 第2轮 REFACTOR 自 SKILL.md 整体逐字挪入（一字未改），SKILL.md 原位留指针。

---

## 原文：SKILL.md §1.1 规范生成模式与兼容别名

5 大规范模式（Canonical Modes）：`t2va` 文生视频、`i2va` 图生/首帧生视频、`fl2va` 首尾帧（强制起始帧+结束帧两张关键帧）、`l2va` 尾帧生视频、`ref2va` 多模态参考。外部别名（`t2v`、`i2v`、`fl2v`、`first-last-frame`、`l2v`、`multimodal`、`multimodal-ref`）统一在路由阶段归一化，映射表唯一事实源是 `scripts/router.py:18-24`，正文不抄录。
## 原文：SKILL.md §1.2 生产模型能力约束

各模型支持的模式、分辨率、比例、时长现场读 `config/model-capabilities.json`，公开 API 边界另见 `config/provider-capabilities.json`。两条常驻红线：**H3-Max-Turbo 为内部条目、公开 V2 未列出，不得当可调用**；**H3-Max 的单图 `i2va`/`l2va` 在路由层阻断**。能力核对记录：`references/official/external-verification-log.md`；`references/native/model-capabilities.md` 为人工维护镜像，发现漂移以配置为准回改文档（原再生脚本已随历史套件不可用）。
