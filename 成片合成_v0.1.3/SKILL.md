---
name: media-assembly
author: 柠檬才不萌
license: MIT
user-invocable: true
description: >
  当用户手上已经是分镜/镜头组（storyboard.json 或逐镜清单），要「合成成片」「出片」「跑成片」「分镜转成片」「视频渲染」「TTS 配音对齐」「剪映草稿」「口型同步」「逐镜工单」「render_plan」「重跑失败镜头」「短剧最后一英里」时使用本技能：交付物是逐镜工单 render_plan.json 与成片，本技能并接管 storyboard 让渡的口型决策（lip_sync_owner: 成片合成）。
  症状词「BLOCKED-CRED」「failed_items」「succeeded=0 当故障」「桩产物当真片」「storyboard 退出码 2」「TTS 时长漂移」「画外音+字幕降级」同样应触发。
  不适用：还没有分镜/镜头组之前的写戏改台词与剧本质量门（归小说转短剧）、分镜拆解（上游 novel-storyboard）、资产提取（资产提取技能）、H3/分镜等视频提示词编写（MiniMaxH3提示词）、真实平台视频生成（需外部凭据）。
metadata:
  display_name: 成片合成

  version: "0.1.3"
---

# 成片合成 v0.1.2

> **作者：柠檬才不萌 · 版本：v0.1.2**
> 本目录是成片合成技能根。所有命令在技能根执行，工作目录用 `PYTHONIOENCODING=utf-8`。

## 概述

本技能补上 AI 短剧产线断点 D1/D2 的「最后一公里」：吃 storyboard.json 分镜契约 + 投产包/资产总账 + 配音对齐单，先推导逐镜工单 `render_plan.json`，再分阶段跑视频渲染、TTS 配音、剪映合成、口型同步。

核心原则：三档 stage 各自独立可跑且幂等；单镜失败只隔离到 `failed_items.json`，不污染全局；口型决策由本技能接管（storyboard 已声明 `lip_sync_owner: 成片合成`），默认 `lip-sync=off` 时画内对白降级为「画外音 + 字幕」；适配器可注入——离线用桩跑通全链，真实渲染走注入的 HTTP 层。台账如实登记，`SKIPPED` / `NOT_RUN` / `PARTIAL` 一律不算绿灯。

## 速查

| 我要做什么 | 命令/入口 | 产出 |
|---|---|---|
| 推导逐镜工单 | `python -m renderplan plan --storyboard … --dubbing … --asset-ledger … --out .lemon/render_plan.json` | `.lemon/render_plan.json` |
| 逐档执行（video/tts/lip-sync/assemble 各跑一次） | `python -m renderplan run --plan .lemon/render_plan.json --stage <档> --out .lemon/run` | `.lemon/run/` 产物 + 追加一行 `.lemon/run-ledger.jsonl` |
| 只重跑失败镜头 | 上条 run 命令加 `--retry-failed` | 失败项隔离在 `failed_items.json`，只重跑其中镜头 |
| 口型（默认降级不驱动） | `--stage lip-sync`（默认 `off`） | 画内对白→「画外音 + 字幕」，工单记 `SKIPPED` |

## 目录结构

```
renderplan/
  __main__.py cli.py       # 命令行入口：python -m renderplan {plan,run}
  plan.py                  # storyboard 契约校验 + 逐镜工单推导
  stages.py                # video / tts / assemble / lip-sync 四档阶段
  adapters.py              # 可注入适配器：桩 + 真实 HTTP 层（凭据门）
  ledger.py                # .lemon/run-ledger.jsonl 运行台账
tests/                     # 覆盖 1~7 号行为；tmp_path、无网络、无真实剪映
```

## 一条命令

先出工单，再逐档执行（全部离线用桩跑通，产物落在 `.lemon/`）：

```bash
PYTHONIOENCODING=utf-8 python -m renderplan plan \
  --storyboard tests/fixtures/storyboard.json \
  --dubbing tests/fixtures/dubbing.json \
  --asset-ledger tests/fixtures/asset_ledger.json \
  --out .lemon/render_plan.json && \
PYTHONIOENCODING=utf-8 python -m renderplan run --plan .lemon/render_plan.json --stage video     --out .lemon/run && \
PYTHONIOENCODING=utf-8 python -m renderplan run --plan .lemon/render_plan.json --stage tts        --out .lemon/run && \
PYTHONIOENCODING=utf-8 python -m renderplan run --plan .lemon/render_plan.json --stage lip-sync   --out .lemon/run && \
PYTHONIOENCODING=utf-8 python -m renderplan run --plan .lemon/render_plan.json --stage assemble   --out .lemon/run
```

失败重跑只碰失败镜头：`python -m renderplan run --plan .lemon/render_plan.json --stage video --out .lemon/run --retry-failed`

## 输入契约

- `storyboard.json`：严格对齐 `vendor/schemas/storyboard.schema.json` 真字段名（`episodes[].segments[].{id,sceneIndex,cuts[].{beats,seconds,size,camera,characters,props,frame}}`）。镜号按 `x-shot-table` 推导为「段号+f<段内切序>」（如 `E01-01-f2`）。不合契约直接拒绝并给纯中文错误、退出码 2（无 traceback）。
- 投产包/资产总账：`vendor/schemas/asset-ledger.schema.json`（`entries[].asset_id/ledger_status`）。
- 配音对齐单：novel-storyboard 产出的 `{segment,cut,ep,speaker,line,seconds}` 列表，`seconds` 即该镜计划时长。

## 阶段与降级

- **video**：调用注入的 `VideoAdapter.generate(shot)`；`StubVideoAdapter` 产出占位 clip 供离线跑通。
- **tts**：调用注入的 `TtsAdapter`，比对产物音频时长与配音对齐单计划时长，|Δ| > 阈值（默认 0.35s）时告警并建议文档化降级「画外音 + 字幕」。
- **assemble**：`jianying-editor` 可导入时经其集成点生成草稿；否则写出 `draft/draft_desc.json` + `assemble.json`，并记录「剪映导出未验证（缺 jianying-editor 运行环境）」。
- **lip-sync**：默认 `off`。接管分镜让渡的口型决策（`lip_sync_owner: 成片合成`）；`off` 时画内对白降级为「画外音 + 字幕」，工单里记录原因；VO 画外音本不需口型，不降级。`off` 记 `SKIPPED`（一镜口型都没做，`succeeded=0`），`on` 因引擎未接入记 `NOT_RUN`——两种模式都不算绿灯。
- 每档跑完向 `.lemon/run-ledger.jsonl` 追加一条运行台账（`--out .lemon/...` 时就近写，不会套成 `.lemon/.lemon/`）。

## 常见错误 / 红旗

- 把 `SKIPPED` / `NOT_RUN` / `PARTIAL` 当绿灯汇报：台账红线，只有真实产物到位才 `SUCCEEDED`（`renderplan/ledger.py`，测试 `test_skipped_is_not_reported_green`、`test_lipsync_on_is_not_reported_green`）。
- 把桩产物当真片交付：本机仅桩渲染验证，`StubVideoAdapter` 产出的是占位字节流。
- 凭据 key 还是 `REPLACE` 占位时硬凑真实渲染：`HttpVideoAdapter`/`HttpTtsAdapter` 构造即抛 `BLOCKED-CRED`，应停下报告等凭据，不外圈绕过（`renderplan/adapters.py`，测试 `test_http_layer_never_built_without_credentials`）。
- storyboard 不合契约时手改字段名硬喂：`plan` 直接退出码 2 给纯中文错误（`renderplan/plan.py::StoryboardError`），应按错误修正输入再跑（测试 `test_cli_bad_storyboard_exit_code_2`）。
- 忽略 TTS 时长漂移告警：|Δ| 超阈值（默认 0.35s，`--drift-threshold` 可调）必须按建议走「画外音 + 字幕」降级或返修配音（测试 `test_tts_stage_warns_on_duration_drift`）。
- 把 lip-sync `off` 的 `succeeded=0` 当故障：`off` 记 `SKIPPED` 是正常态；`on` 因引擎未接入记 `NOT_RUN`，两种都不算绿也不算错。
- 用模型自评顶替真人盲评结论：盲评包回收前结论字段一律留空。
- 忘记 `PYTHONIOENCODING=utf-8` 直接跑：Windows 默认代码页会把中文输出搞乱，错误信息读不见。
- 现象：用户笼统说「最后一英里/跑产线」就被本技能接走。成因：与「小说转短剧」触发词互相吸引（本轮实测碰撞）。处置：可观察谓词——输入已是分镜/镜头组（storyboard.json 或逐镜清单）才进本技能；还停在写戏改台词与剧本质量门阶段的归小说转短剧。
- 现象：改 frontmatter 时把「以 renderplan 命令吃…离线桩可跑通全链」这类流程摘要写回 description。成因：SDO 明文——description 写流程会让代理照描述干活不读正文（本轮微测实测判红）。处置：description 只写触发词与输入前置条件，链路与阶段细则留在正文「概述」「阶段与降级」；闸门为 tests/test_skill_md.py 的 test_sdo_* 用例。

## 边界（如实登记，勿当绿灯）

- **真实视频/TTS 渲染 NOT_RUN（BLOCKED-CRED，解锁条件＝填入可用凭据）**：凭据未配置或仍为字面量 `REPLACE` 占位。`HttpVideoAdapter` / `HttpTtsAdapter` 在凭据缺失或为 `REPLACE` 时构造即抛错，因此缺凭据环境只能跑桩链路。**缺凭据时仅桩渲染验证**——桩产物是占位字节流，不是真片；产物签发通过 `validate_rendered_artifact` 严格拦截桩流伪造真片。
- 剪映导出未验证：本机 `jy_wrapper` 不可导入，assemble 只落到降级描述，不产出真实草稿。
- 台账红线：`SKIPPED` / `NOT_RUN` / `PARTIAL` 均不算绿灯；只有真实产物到位才 `SUCCEEDED`。
- 未做：视频编码、字幕烧制、转场/调色、真实口型驱动、多集并发。

### 真实环境端到端验收 Checklist（W09.5，凭据具备后触发）

当外部凭据与硬件环境具备（`RENDER_ENDPOINT` 与 `RENDER_KEY` 解锁）时，按以下五步执行真实端到端验收，并登记凭证：
1. **平台与请求回溯**：记录调用平台名称、请求 ID、响应状态码及耗时；
2. **合法性与魔数探测**：调用 `renderplan.adapters.validate_rendered_artifact` 验证真实产物魔数（MP4/WAV/WebM），拒绝桩产物伪造；
3. **时长与采样对账**：验证真实视频、音频时长与 `render_plan.json` 误差 ≤ 0.35s；
4. **失败隔离核查**：确认 `failed_items.json` 是否收敛为 0，如有失败项仅重跑该镜头；
5. **人工观看结论**：人工检视音画同步与视觉连贯性，在验收单登记 SHA-256 与观看结论。

## 已知限制

- 未验证：真实成片画质、音画同步、剪映草稿可导入性——本机全部走桩，需在凭据解锁后于真实环境重跑。
- 台账已对齐 `vendor/schemas/run-ledger.schema.json`（C8 跨技能统一内核，`vendor/schemas/ledger.py::validate`）：每条记录带齐 `skill_version/model_node/input_units/calls/tokens_in/tokens_out/cost_cny/gate_failures/retries/artifact_paths`，桩链路无模型调用故成本字段如实记 0；内核路径可用环境变量 `UNIFIED_LEDGER_PY` 覆盖，测试直接吃 `validate()` 校验，内核缺失即测红（不静默跳过）。
- 台账 `.lemon/`、`output/` 及 `*.mp4/*.wav` 等大体量产物已在 `.gitignore` 排除，不入库。
- 未经真人验证：成片是否看得下去，本机只能证明「链路跑得通」。真人盲评包（3 集剧本 + 桩片 + 评分表 + 阈值）随维护流程发放，回收前结论字段一律留空，不得由模型自评顶替。
- 编号断点（PIPELINE.md D3/D7）：`characters` 的 `C01` 与资产总账 `CH_名` 无共享规则，工单仅并列登记两侧引用，不冒充已打通。

## 常见坑

- 五条现象级坑（lip-sync 绿灯误判 / 凭据 `REPLACE` 占位 / 桩产物当真片 / 台账 `.lemon/.lemon/` 嵌套 / 分镜契约 traceback）全文已外移至 `references/常见坑.md`（只搬不删，可反查）。
