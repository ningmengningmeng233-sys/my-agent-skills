# MiniMax 视频生成模型能力矩阵 (model-capabilities)

> 本文档历史上由 `scripts/generate_capability_docs.py` 基于 `config/model-capabilities.json` 自动生成；该生成器已随历史套件不可用（2026-09-19 瘦身复核确认），本文档转为人工维护。
> `config/model-capabilities.json` 仍是唯一事实源：更新能力矩阵先改配置、再同步本文档，两者不一致以配置为准。

## 0. 公开 API 边界 (Public API Boundary)

> 本块由 `config/provider-capabilities.json` 派生，是「能否用 API key 调用」的唯一口径；
> 下方第 1、2 节是技能内部的构建能力口径。**内部保留条目不等于官方公开支持**：
> `supported` 为公开 V2 文档列出的模型与模式，`denied` 为官方明确不支持，
> `not-listed` 为未出现在公开模型表中（不得据此推断可调用）。

<!-- public-api-boundary
# 由 config/provider-capabilities.json 派生；文档必须与本块一致
# supported: 公开 V2 API 已列出的模式; denied: 明确不支持; not-listed: 未列入公开模型表
supported: H3=fl2va,i2va,l2va,ref2va,t2va
supported: H3-Max=fl2va,i2va,l2va,t2va
denied: H3-Max=ref2va
not-listed: H3-Max-Turbo
-->

## 1. 核心模型能力概览 (Official Capability Matrix)

| 型号 | 官方支持模式 | 最大时长 | 支持分辨率 | 支持画幅比例 | 适用场景与特性 | 官方验证状态 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **H3** | FL2VA, I2VA, L2VA, REF2VA, T2VA | 4-15秒 | 768P, 2K | 16:9, 9:16, 1:1, 4:3, 21:9 |  | `VERIFIED_OFFICIAL` |
| **H3-Max** | FL2VA, I2VA, L2VA, REF2VA, T2VA | 5-15秒 | 480P, 768P | 16:9, 9:16, 1:1 | Plain i2va and l2va without dual frames blocked | `VERIFIED_OFFICIAL` |
| **H3-Max-Turbo** | FL2VA, I2VA, L2VA, REF2VA, T2VA | 5-15秒 | 480P, 768P | 16:9, 9:16 | Fast generation; plain i2va, l2va, and ref2va blocked | `VERIFIED_OFFICIAL` |

## 2. 规范模式详细支持与事实源追溯 (Per-Mode Provenance)

| 型号 | 模式 | 验证状态 | 来源凭证文件 | Upstream Commit | 校验摘要 (SHA-256) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **H3** | `t2va` | `VERIFIED_OFFICIAL` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3** | `i2va` | `VERIFIED_OFFICIAL` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3** | `fl2va` | `VERIFIED_OFFICIAL` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3** | `l2va` | `VERIFIED_OFFICIAL` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3** | `ref2va` | `VERIFIED_OFFICIAL` | `references/official/source-manifest.json` | `e89c19d4` | `1e574f356716...` |
| **H3-Max** | `t2va` | `VERIFIED_OFFICIAL` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3-Max** | `i2va` | `UNVERIFIED` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3-Max** | `fl2va` | `VERIFIED_OFFICIAL` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3-Max** | `l2va` | `UNVERIFIED` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3-Max** | `ref2va` | `REQUIRES_CONFIRMATION` | `references/official/source-manifest.json` | `e89c19d4` | `1e574f356716...` |
| **H3-Max-Turbo** | `t2va` | `VERIFIED_OFFICIAL` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3-Max-Turbo** | `i2va` | `UNVERIFIED` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3-Max-Turbo** | `fl2va` | `VERIFIED_OFFICIAL` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3-Max-Turbo** | `l2va` | `UNVERIFIED` | `references/official/source-manifest.json` | `e89c19d4` | `2cfebc096a6e...` |
| **H3-Max-Turbo** | `ref2va` | `UNVERIFIED` | `references/official/source-manifest.json` | `e89c19d4` | `1e574f356716...` |

## 3. 不支持组合与阻断规则 (Fail-Closed Rules)

路由引擎 (`scripts/router.py`) 严格执行 Fail-Closed 策略，对以下未验证或官方明确不支持的组合进行硬阻断：

- **H3-Max** 阻断模式: `i2va`, `l2va`, `ref2va`（未获官方验证或未开放支持）。
- **H3-Max-Turbo** 阻断模式: `i2va`, `l2va`, `ref2va`（未获官方验证或未开放支持）。
- **时长限制**：超出各型号 `duration_seconds` 定义的时长请求一律阻断并提示安全区间。
- **分辨率限制**：超出各型号 `resolutions` 白名单的请求严格阻断。
- **画幅比例限制**：超出各型号 `supported_aspect_ratios` 白名单的请求严格阻断。

## 4. 模式兼容性别名 (Backward Compatibility Aliases)

为了保持向下兼容性，以下别名仅在输入路由解析阶段映射到 5 种规范模式（Canonical Modes），系统内部与事实源严格采用规范键：

- `first-last-frame` -> `fl2va`
- `fl2v` -> `fl2va`
- `i2v` -> `i2va`
- `l2v` -> `l2va`
- `multimodal` -> `ref2va`
- `multimodal-ref` -> `ref2va`
- `t2v` -> `t2va`
