# 官方资料外部核对记录（External Verification Log）

本文件记录每次针对 MiniMax 官方公开资料的联网核对：核对日期、来源、核对点、结论。
规则：**能力配置（config/*.json）以官方可核验来源为准；核对发现差异时先改配置与测试，再再生文档。**
不能靠旧文档推断当前接口能力；每次发布前应重新核对并在此追加记录。
**公开 API 口径与内部口径分离**：`config/provider-capabilities.json`（能否用 API key 调用）≠ `config/model-capabilities.json`（技能内部构建能力）。内部保留条目不构成官方公开支持；下文的"未发现差异"只覆盖逐条列出的具体字段。

## 公开 API 边界（由 config/provider-capabilities.json 派生）

<!-- public-api-boundary
# 由 config/provider-capabilities.json 派生；文档必须与本块一致
# supported: 公开 V2 API 已列出的模式; denied: 明确不支持; not-listed: 未列入公开模型表
supported: H3=fl2va,i2va,l2va,ref2va,t2va
supported: H3-Max=fl2va,i2va,l2va,t2va
denied: H3-Max=ref2va
not-listed: H3-Max-Turbo
-->

## 2026-09-14 复核（v7.3.0 非人工工程完善批次）

- 来源（本次联网重新获取）：
  - 视频生成指南：https://platform.minimax.io/docs/guides/video-generation
  - V2 创建接口参考：https://platform.minimax.io/docs/api-reference/video-generation-v2-create
- 逐字段核对结果：
  1. 公开模型表：指南与创建接口的 `model` 枚举都只有 `MiniMax-H3` 与 `MiniMax-H3-Max`；**页面中不出现任何 Turbo 变体**。
  2. H3 生成模式：文生视频、图生视频（首帧/尾帧）、参考生成（参考图/视频/音频）——与本矩阵 H3 五模式 = SUPPORTED 一致。
  3. H3-Max 生成模式：指南只列文生视频与图生视频，参考生成标注 "coming soon"；创建接口明确写 "reference-to-video (reference image / video / audio) is not supported"——与 `H3-Max ref2va = NOT_SUPPORTED_IN_PUBLIC_API` 一致；**不得由通用 H3 的 Ref2VA 推断 H3-Max 可调用**。
  4. 规格字段：H3 为 768P/2K、4–15s；H3-Max 为 480P/768P、5–15s——与两个配置文件的 `resolutions` / `duration_seconds` 一致。
  5. 接口形态：/v2/video_generation 创建 + /v2/query/video_generation/{task_id} 查询——与 `scripts/h3_v2_client.py` 一致（当时的假服务器契约测试覆盖；该测试已随历史套件不可用——2026-09-19 瘦身复核注记，接口形态现以 `scripts/h3_v2_client.py` 源码为准）。
- 差异：**在上述五个可核验字段上**，本次没有发现需要修改 `config/*.json` 的差异；该结论不外推到未列出的字段。
- 仍未证实（不得推断）：
  - `H3-Max-Turbo`：公开模型表未列出该模型。`config/model-capabilities.json` 中对它的 `VERIFIED_OFFICIAL` / `UNVERIFIED` 是**技能内部构建与阻断口径**，不代表官方公开 API 支持，也不得据此编造 model id。
  - 官方 Agent 通道与开源权重（HuggingFace 等）的可用性、版本与参数能力本次未重新取证，保持未证实状态。
  - H3-Max `i2va` / `l2va` 的内部状态仍为 `UNVERIFIED`：创建接口把它们并入图生视频（首帧/尾帧）描述，逐字段确认需要一次真实调用，属 MODEL_RENDER_E2E（当前 NOT_RUN），本文不构成渲染门禁证据。

## 2026-09-13 核对（v7.3.0 门禁修复批次）

- 来源：
  - 官方 API 文档：https://platform.minimax.io/docs/guides/video-generation （MiniMax 官方，视频生成指南）
  - 开源权重页：https://huggingface.co/MiniMaxAI/MiniMax-H3 （T2VA / FL2VA / Ref2VA 复现用例）
  - 官方集成仓库：https://github.com/MiniMax-AI/awesome-minimax-h3-integration （原生立体声、2K/15s 上限）
- 核对点与结论：
  1. 模型家族：官方公开文档列出 MiniMax H3（开源通用多模态视频模型）与 MiniMax H3 Max（托管增强变体）；**未列出 Turbo 变体**。`config/model-capabilities.json` 的 H3 / H3-Max / H3-Max-Turbo 三条目是技能内部口径，其中 Turbo 条目不因本核对获得公开支持；
  2. 生成模式：T2VA / I2VA / FL2VA / Ref2VA 为 H3 家族主模式——与配置 `canonical_modes` 一致（H3-Max 的 Ref2VA 与 Turbo 各模式不在此列）；
  3. 规格：H3 最长 15s、最高 2K——与配置 `duration_seconds.max=15`、`resolutions=["768P","2K"]` 一致；H3-Max 的 480P/768P、5–15s 与配置一致（"极速定位"为公开页面的营销表述，不再用作能力依据）；
  4. 接口形态：/v2/video_generation 创建 + /v2/query/video_generation/{task_id} 查询——与 `scripts/h3_v2_client.py` 一致（当时的假服务器契约测试覆盖；该测试已随历史套件不可用——2026-09-19 瘦身复核注记，接口形态现以 `scripts/h3_v2_client.py` 源码为准）。
- 差异：在上述五个具体字段上未发现需要修改 `config/*.json` 的差异（原文"未发现差异"按本条限定理解）。
- 限制：在线核对只能确认公开文档口径；额度内的真实调用验证仍属 MODEL_RENDER_E2E（NOT_RUN），本记录不构成渲染门禁证据。
