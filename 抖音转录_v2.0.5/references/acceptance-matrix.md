# 验收矩阵（v2）

> 基线：Git 提交 `03c978f` 之后、本次 Task 14 的验收状态。
> 隐私约定：真实链接与输出只留在本机未跟踪的 `state/acceptance/`，
> 本文档只写匿名编号与指标。

## 1. 自动质量门

| 项目 | 命令 | 结果 |
|---|---|---|
| 全量测试 | `python -m unittest discover -s tests -t .` | **695 项通过** |
| 语法编译 | `python -m compileall -q scripts tests` | 零错误 |
| 占位符扫描 | `rg -n -i 'TO[D]O\|TB[D]\|FIXM[E]' SKILL.md scripts references tests` | 仅命中 `tests/test_skill_contract.py` 里扫描占位符自身的字符串 |
| 环境体检 | `python scripts/doctor.py --json` | 12 项全绿（含 `database: schema v2`） |
| Git 状态 | `git status --short` | 干净，无未跟踪脏文件（除本次文档） |

## 2. 数据库迁移演练

| 项目 | 结果 |
|---|---|
| 副本连续迁移两次 | `migrated=1` → `skipped=1`（幂等） |
| `PRAGMA integrity_check` | `ok` |
| 真实库迁移 | 备份 `.db/backups/transcripts-before-v2-20260916-035730.db`，`migrated=1`，`integrity=ok` |
| v1 记录可读 | 标题/作者/发布日期/原稿/摘要/文件路径全部原样 |
| 迁移后复用判定 | `render_only`（legacy 版本缺证据文件 → 只重渲染，不重下不重转） |
| 纯 DB 双文件重建 | `repair-artifacts` 一次重建成功；第二次为 no-op（`ok=1, repaired=0`） |
| v1 表保留 | `transcripts` 1 行未删；v2 新增 `transcript_versions` 1 行 + `artifacts` 1 行 |

## 3. fake 破坏性注入（`tests/test_acceptance_injection.py`，9 项全通过）

| 注入点 | 期望 | 实际 |
|---|---|---|
| 采集失败 | 终结该条，旧 current 与两个旧文件不变 | `failed/ingest_contract_changed`，旧版与旧文件保持，媒体 0 残留 |
| 第一轮 ASR 异常 | 终结该条，旧版本不变 | `failed/asr_failed`，旧版与旧文件保持 |
| 发布事务失败 | 回滚文件、旧 current 不变 | `failed/publish_failed`，旧版与旧文件保持 |
| 认证失效 | 暂停而不是失败 | `login_required`，旧版本不变 |
| 视频素材下载失败 | 只禁用 OCR | `completed` + 降级 `video_unavailable_for_ocr`，新版本发布 |
| OCR 失败 | 局部降级 | `completed` + 降级 `ocr_failed` |
| 校正失败 | 交付原始稿 | `awaiting_ai` + 降级 `correction_failed`，主文件标注“未经 AI 校正原始稿” |
| 摘要失败 | 交付原始稿 | `awaiting_ai` + 降级 `summary_failed` |
| 每个注入点后 | 无媒体残留 | `state/jobs` 下无任何媒体文件 |

## 4. 真实样本矩阵

| 编号 | 类别 | 样本 | 结果 |
|---|---|---|---|
| S-01 | 清晰口播 + 连续硬字幕 + 强背景音乐（单人） | 真实抖音作品（1 分 10 秒） | **真实跑通**：10 阶段全过、1 轮 ASR、`passed/100.0`、校正 12 段、摘要 normal、无降级、媒体 0 残留 |
| S-02 | 音频增强与降噪（-22 dB 衰减样本） | 同一音频人工衰减后测量 | 真实 FFmpeg：正常语音 `-10.9 LUFS`（不增强）、衰减后 `-36.5 LUFS`（生成增强音轨） |
| S-03 | 人声分离（UVR） | sherpa 官方语音样例（26.1 秒） | 真实推理：2 条 44.1 kHz 音轨，耗时 35.9 秒 |
| S-04 | 匿名说话人分离（单人） | 同 S-03 | 真实推理：`no_speakers`，0 标签（保守规则生效） |
| S-05 | 底部字幕 / 中部参数 / 场景切换 | 合成视频（`tests/fixtures/ocr/`） | 真实 FFmpeg + RapidOCR：3 条观察，「端午粽子」「不分南北」为 `speech_subtitle`，「参数 2026-06-19 1.2 GB」为 `static_info` |
| S-06 | 多人对白 | 单元测试（两个可比聚类） | 覆盖 A/B 映射、短聚类丢弃、重叠留空、覆盖不足留空；**缺真实样本** |
| S-07 | 无旁白图文 | 单元测试（`visual_text_only`） | 覆盖主文件不出现“完整语音原稿”、摘要走画面模式；**缺真实样本** |
| S-08 | 中英混合 | — | **未覆盖**，需要用户提供样本 |
| S-09 | 较长视频（>20 分钟） | 单元测试（抽帧上限、降采样到 2 fps） | 覆盖帧预算与长视频降级；**缺真实样本** |

真实批次（混合输入）实测：

```
3 条输入 → submitted=3, new=1, reused=1, duplicates=1
pos=0 已发布作品      → reused（带主文件路径，未重新下载）
pos=1 modal_id 同作品 → duplicate（规范化后按 aweme_id 二次去重）
pos=2 失效作品 ID     → failed/ingest_timeout（重试 3 次后终结，不阻断批次）
顺序保持、严格串行、逐条汇报（含未处理的复用/重复项）
```

认证暂停与续跑用测试认证故障模拟（不注销真实账号）：
`tests/test_batch_resume.py::AuthPauseTests`（第二项暂停 → 第三项不执行 → `--resume` 从第二项继续 → 第一项不重跑）。

## 5. 人工质量抽查（S-01 逐段对照）

同一作品的 v1（无 OCR/无校正）与 v2（OCR + 证据融合 + 受约束校正）对照：

| 位置 | v1 原稿 | v2 校正版 | 依据 |
|---|---|---|---|
| 00:04 | 果端午 | **过端午** | 硬字幕「过端午」 |
| 00:06 | 菜子粥 | **赛龙舟** | 硬字幕「赛龙舟」 |
| 00:08 | 秋冬 | **敲大鼓** | 硬字幕「敲大鼓」 |
| 00:47 | 端午这么吃的饱 | **端午怎么吃得饱？** | 硬字幕「这怎么吃得饱」 |
| 00:56 | 还不够我塞眼红吗 | **还不够我塞牙缝呢！** | 硬字幕「还不够我塞牙缝的呢」 |
| 全篇 | 无标点 | 逗号/句号/问号/感叹号齐备 | 标点级改动无需证据 |

统计：错字/同音字修正 **5 处**（全部有硬字幕证据，`correction_changes` 可逐条追溯），
标点补齐 8 处，漏句 0 处，错说话人 0 处（单人样本，输出不加标签）。
原始稿与校正版同时保留：证据文件里 `seg-N` 原文 → 校正文 → 证据 → 置信度完整可查。

## 6. 已知限制（如实记录）

1. **OCR 会读出水印碎片**：抖音界面水印（「小抖音」「抖音号: …」「P/Q/J」）会被识别，
   其中少量被误判为 `speech_subtitle`。它们与 ASR 文本相似度过低，会被判为冲突而不参与替换；
   画面文字区会如实列出，阅读时需自行忽略。
2. **质量分衡量解码置信度，不衡量内容正确性**：语义错误但置信度高的片段仍可能得高分
   （实测音乐/歌唱样本得分 100）。内容正确性依赖硬字幕证据与人工抽查。
3. **强背景音乐下的说话人聚类不可靠**：已用静音比例与聚类数做保守抑制，
   但真正需要区分“多人对白 + 全程 BGM”的场景仍需要人工确认。
4. **未覆盖的真实类别**：多人对白、无旁白图文、中英混合、超长视频（见 S-06~S-09）。
5. **v1 遗留文件未自动删除**：`output/2026/06/` 里仍有一个 v1 时期命名的文件
   （`<标题>_<ID>.txt`，无作者前缀）。它的内容已完整保存在 SQLite（`transcript_versions`
   v1，`is_current=0`），也没有被 v2 覆盖；删除属于不可逆操作，留给用户自行决定。
   v2 交付固定为「作者_标题_ID.txt」+「..._原始记录.txt」两个文件。
6. **验证过的时间点**：以上数字来自 2026-09-16 的本机实测，参数变化后需重跑本矩阵。

## 7. 复现命令

```powershell
# 自动质量门
.venv\Scripts\python.exe -m unittest discover -s tests -t .
.venv\Scripts\python.exe scripts\doctor.py --json

# 真实单条（会走完整流水线；--force 强制重转）
.venv\Scripts\python.exe scripts\douyin_transcript.py "<链接>" --force --json

# 真实批次（含复用/重复/失效）
.venv\Scripts\python.exe scripts\batch_transcribe.py --text "<多条链接>" --json

# 破坏性注入验收
.venv\Scripts\python.exe -m unittest tests.test_acceptance_injection -v

# 迁移演练（对副本）
.venv\Scripts\python.exe scripts\migrations.py --db "<副本路径>" --json
```
