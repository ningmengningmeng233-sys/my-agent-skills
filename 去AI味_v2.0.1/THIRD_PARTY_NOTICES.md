# 第三方来源与许可声明（THIRD_PARTY_NOTICES）

本文件是「去 AI 味」（`lieflat-less-ai-tone`）v1.2 的必需运行文件，记录 Editorial 层与
小说层所吸收的第三方内容、其上游来源链、许可条款与版权行。上游许可证要求在所有副本或实质
部分中保留版权声明与本许可全文，因此本文件随包发布，不由构建开关决定带不带。

## 1. 直接上游：Humanizer-zh

| 项目 | 事实 |
| --- | --- |
| 项目 | Humanizer-zh（中文文本润色 Skill） |
| 仓库 | https://github.com/op7418/Humanizer-zh |
| 本机取证副本 | `Humanizer-zh/SKILL.md`、`LICENSE`、`README.md`、`CHANGELOG.md`、`tests/` |
| 修订日期 | 2026-09-23（`SKILL.md` frontmatter `metadata.revision`） |
| 许可 | MIT License |
| 版权行（逐字照抄上游 `LICENSE` 第 3 行） | `Copyright (c) 2026 歸藏` |

被吸收的内容：`SKILL.md`「模式的使用方式」小节的 31 项编辑检查点（原文自述「以下 31 条沿用
PR 39 的分类」），分 A–F 六类；以及「编辑约束与优先级」「文体与作者声音」「交付前核对」中
关于保留事实、确定程度与作者声音的判断尺度。本地渲染结果见第 5 节。

## 2. 直接上游：novel-craft（小说伪文艺腔病灶分类）

v1.2 FP 专项迭代新增的上游来源，用于小说伪文艺腔规则层（FP01–FP08）的病灶分类、
逐处判断问题与正反例组织方式。

| 项目 | 事实 |
| --- | --- |
| 项目 | novel-craft（中文长篇小说创作与润色 skill/agent 集合） |
| 仓库 | https://github.com/chaserr/novel-craft |
| 固定提交 | `cd9d308d46b4d19286db03e1082f2e2c3064b1b2` |
| 许可 | MIT License |
| 版权行 | `Copyright (c) 2026 chaser` |

被吸收的内容（仅限分类与判断方式）：叙事病灶分类——命名式情绪复述、空洞诗意与通感比喻、
模式化身体反应、章末感悟收束、说教化与总结式对白、静态形容词与抽象状态堆积、文艺化残句
与统一节奏、视角越界与动机代述；以及每处「先核对上下文与功能再动手」的判断问题、
正反例对照的组织方式。主要参考其 `skills/zh-novel-polish/SKILL.md`，辅助参考
`agents/novel-polisher.md`、`agents/novel-dialogist.md`、`agents/novel-scene-refiner.md`
与 `skills/novel-write/reference/` 的 04-心理、06-信息密度、07-章末结构。

**明确未吸收**（按本仓保真合同排除）：修饰语字数上限、「的」字数量限制、段落句数限制、
固定章尾感悟配额、主动补写感官/动作/物件/身体反应、把模糊时间具体化、完整小说创作
工作流及其项目目录体系。本层全部例文为自写合成材料，不是上游文本的搬移。

## 3. 上游 MIT 许可全文

### 3.1 novel-craft 的 LICENSE

```text
MIT License

Copyright (c) 2026 chaser

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### 3.2 Humanizer-zh 的 LICENSE（取自其上游仓库）

```text
MIT License

Copyright (c) 2026 歸藏

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

本仓库自身的 `LICENSE` 同为 MIT License，版权行 `Copyright (c) 2026 shiujan`；三份 MIT 条款
互相兼容，衍生内容按各自上游要求保留上面的声明。

## 4. 上游自己的来源链（按 Humanizer-zh 文件自述）

Humanizer-zh 的 `SKILL.md`「来源」小节与 `CHANGELOG.md` 2026-09-23 条目记录了它的上游：

- **blader/humanizer v3.0.0** — https://github.com/blader/humanizer/blob/v3.0.0/SKILL.md
  上游自述为「A–E 分类、声音校准和文件模式的参考来源」。
- **Humanizer-zh PR 39** — https://github.com/op7418/Humanizer-zh/pull/39
  （上游 `CHANGELOG.md` 写作「PR #39」）上游自述为「中文分类与 F 组检查点的基础；本修订
  重写条件、边界及示例」。31 条分类的实际出处即此。
- **Humanizer-zh PR 34** — https://github.com/op7418/Humanizer-zh/pull/34
  仅见于上游 `CHANGELOG.md` 的致谢列表；上游明确写明「本次整合没有直接合并这些 PR」。
- **hardikpandya/stop-slop** — https://github.com/hardikpandya/stop-slop
  上游自述为「简洁表达与编辑检查的参考来源」。
- **Wikipedia: Signs of AI writing** — https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing
  上游自述为「原项目的观察来源，不作为作者身份判断或检测准确率的保证」。

上述项目在本仓库里只是**被引用的来源说明**，其文件没有被复制进来；本仓库实际吸收过规则
内容的上游以第 1 节（Humanizer-zh）与第 2 节（novel-craft）的登记为准。若将来直接吸收
其它来源的文本，需在本文件追加各自的许可与版权行。

## 5. 本地承载改编内容的文件

下表按**文件名**列出（参考目录路径由发布清单登记，本文件不重复抄一份路径，
免得构建器的引用扫描拿去比一个尚未随包发布的文件）：

| 本地文件 | 与上游的关系 |
| --- | --- |
| `editorial-rules-zh.md`（参考目录） | 31 项检查点渲染为 ED01–ED31，每条重写为「触发证据／允许修改／保留条件／反例」四件；示例文本按上游教学例改写，判断尺度沿用上游「命中不等于授权」「不是词语黑名单」；新增本仓特有的 E1/E2/E3 与 E3 不可绕过条款。 |
| `fiction-zh.md`（参考目录） | 只借用上游的保真优先级与声音校准尺度作为约束起点；三档力度、对白七不变、真实引文锁定、A/B/C 三项验收、「不是 X，而是 Y」的全书分布处理、账本与 L0–L3 口径、`needs_review` 降级处置均为本项目自创新内容，不来自上游。 |
| `fiction-prose-patterns-zh.md`（参考目录） | FP01–FP08 的病灶分类、逐处判断问题与正反例组织方式参考 novel-craft（见第 2 节，MIT）；触发门、聚集证据要求、三档 decision 边界、全部例文与禁止条款为本项目自创内容，不复制上游的固定字数、句数、配额与补写规则。 |
| `SKILL.md`（主入口） | 编辑约束与交付口径的措辞受上游「编辑约束与优先级」「交付前核对」影响，非逐字复制。 |

上游的 `tests/fixtures/cases.json`（18 条短案例）与 `tests/check_structure.py` **未**被搬入
本仓库；本仓的评测资产独立维护。

## 6. 致谢

感谢 歸藏 与 Humanizer-zh 的贡献者，chaser 与 novel-craft 的贡献者，以及 blader、
hardikpandya 与 Wikipedia 相关编辑者对本主题的先行整理。

## 7. v2.0.0 第三方运行依赖（alpha.1 P01 登记，alpha.2 P06/P07 扩表）

v2.0.0（alpha.1）起引入的外部运行依赖，alpha.2 的 P06/P07 扩了 datasets、huggingface_hub
与 BGE 模型资产。采用方式全部为**依赖＋适配**（复制无、fork 无）；
本节登记直接依赖及其许可，完整传递依赖树以 `requirements-v2.lock`（Python）与
`runtime/textlint/package-lock.json`（Node）为权威，不在此逐条抄录。上表许可均为各
发行件随包声明；「初审备注」来自 v2 研究阶段的许可初审（`参考项目清单.json`），不是
完整法律意见，接入新上游组件或模型资产前须继续核验。

### Python 侧（lock：`requirements-v2.lock`）

| 包（import 名） | 发行包 | 锁定版本 | 许可 | 来源与备注 |
| --- | --- | --- | --- | --- |
| transitions | transitions | 0.9.4 | MIT | PyPI 无 0.9.4 发行件；锁与 pyproject 固定公开提交 `bd42b38f3627e6bca7274fb4d9af2e105f75da7c`（git+https://github.com/pytransitions/transitions.git），不静默换版，不依赖开发机本地路径。 |
| keyring | keyring | 25.7.0 | MIT（pyproject 声明） | 初审备注：上游检出未发现独立 LICENSE 文件；发行 wheel 随包声明 MIT，后续升级前复核发行件许可文本。 |
| typesafe_sdk | typesafe-sdk | 0.7.2 | MIT | 初审备注：上游 LICENSE 版权字段有占位；官方 Python SDK，提供 Jev 客户端、认证、RetryPolicy 与异常类型。 |
| text_dedup | text-dedup | 0.4.1 | Apache-2.0 | 仅使用 fingerprint/cluster 产生候选；不调用 remove_duplicates/save_dataset 进入正文通路。 |
| semhash | semhash | 0.5.0 | MIT | 经 from_embeddings/custom encoder 接入，仅提取相似候选，原稿保持完整；ANN 由其依赖 vicinity 承担。 |
| FlagEmbedding | FlagEmbedding | 1.4.2 | MIT | 仅用 `FlagModel.encode` 做推理，不改其源码；模型资产许可见下面「模型资产」。 |
| numpy | numpy | 2.5.3 | BSD-3-Clause | Embedding/相似度向量运算底座（上游依赖，按直接依赖登记）。 |
| datasets | datasets | 5.0.1 | Apache-2.0 | alpha.2 P06 起被 `scripts/v2_similarity.py` 直接 import（`Dataset.from_list` 承载内存数据交给上游 fingerprint/cluster）；不调用其导出/落盘通路。 |
| huggingface_hub | huggingface_hub | 1.33.0 | Apache-2.0 | alpha.2 P07 起被 `scripts/v2_embeddings.py` 直接 import，只用 `snapshot_download(local_files_only=True)` 取钉定本地快照，禁隐式下载。 |

关键传递依赖（完整树见 lock）：torch 2.14.1（BSD-3-Clause，CPU 轮子）、
transformers 5.18.0（Apache-2.0）、model2vec 0.9.0（MIT）、
vicinity 0.4.6（Apache-2.0，semhash 的 ANN 承载）、usearch 2.26.3（Apache-2.0，
vicinity 后端之一；本项目实测走 vicinity 的精确全近邻后端）、pydantic 2.13.5（MIT）、
tenacity 9.1.4（Apache-2.0）。

### 模型资产（alpha.2 P07 钉定）

| 资产 | 来源 | 许可 | 钉定身份 |
| --- | --- | --- | --- |
| `BAAI/bge-small-zh-v1.5` | HuggingFace 官方仓库 BAAI/bge-small-zh-v1.5 | MIT（仓库 frontmatter `license: mit`；README License 节原文「FlagEmbedding is licensed under the MIT License. The released models can be used for commercial purposes free of charge.」） | revision（commit）`7999e1d3359715c523056ef9478215996d62a620`；`model.safetensors` 95,827,648 字节，SHA256 `354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026`；加载时逐字节复核，不符即拒。 |

模型不随技能包分发（体积与许可承载方式都不适合进包），运行前需按授权显式取到独立缓存目录；
缺失时 `scripts/v2_embeddings.py` 直接报错，不会偷偷联网下载。取模型与跑推理都不上传用户正文。

### Node 侧（lock：`runtime/textlint/package-lock.json`）

| 包 | 锁定版本 | 许可 | 用途与备注 |
| --- | --- | --- | --- |
| textlint | 15.8.0 | MIT | lint 内核与 Markdown AST；仅 lint 产生结构候选，不使用自动写文件通路。要求 Node>=20.18.0（engines 已钉）。 |
| @textlint/markdown-to-ast | 15.8.0 | MIT | Markdown AST 解析；AST 只用于结构识别，不重新序列化整文覆盖。 |
| @textlint/textlint-plugin-markdown | 15.8.0 | MIT | Markdown 文本 processor 插件。 |

### 可选中文 NLP 通道（未捆绑）

`scripts/v2_nlp.py` 的默认后端是 `none`（本包自带实现，只做句子级码点切分）。
下两行是**已核对但未捆绑**的可选通道的三层许可记录：组件代码、预训练权重、服务
条款分别核对，任何一层不明即不捆绑、不默认选。核实日期 2026-10-06，方式：
PyPI JSON API（`info.license` / `requires_dist` / `classifiers`）、GitHub
`repos/<上游>`（`license` 字段）与上游 README 原文。

| 后端（import 名） | 捆绑状态 | 组件许可 | 权重许可 | 服务条款 | 来源与备注 |
| --- | --- | --- | --- | --- | --- |
| hanlp | **未捆绑**、未装、不进 lock | 已声明（Apache-2.0，README 原文「HanLP is licensed under Apache License 2.0」） | **非商业限制**：README 原文「Unless otherwise specified, all models in HanLP are licensed under CC BY-NC-SA 4.0」 | 未核对 | https://github.com/hankcs/HanLP ；PyPI `requires_dist` 含 `hanlp-downloader`（取权重走远端），故本层永不替用户选权重、不在 `analyze` 前实例化。许可确认：`license_ack` 不是必需，但商用/分发前须单独核对权重条款。 |
| ltp | **未捆绑**、未装、不进 lock | **未声明**：PyPI `info.license=null` 且 classifiers 无 License 项；GitHub `repos/HIT-SCIR/ltp` 的 `license` 字段为 `null` | 未知：README 图示模型经 HuggingFace 分发（原文「默认 huggingface 下载，可能需要代理」），逐个模型条款未核对 | 未核对：README 写明面向高校/科研机构/个人免费，「除上述机构以外的企事业单位，如申请使用该平台，需付费」 | https://github.com/HIT-SCIR/ltp ；来源不明即不捆绑。选择该后端必须显式 `license_ack=True`（用户已读本条并确认场景可用），否则 `build_provider` 直接拒。 |

- 两条都不进 `EXTERNAL_DEPENDENCIES`：扩表必须与真实安装验证同任务发生，本轮本机
  未安装任何 NLP 组件，真实组件推理为 NOT_RUN。
- NLP 输出只供切分与披露，**不构成「这是 AI 写的」证据**，也不产生改写授权；
  假后端测试证明的是适配形状，不是推理质量。

### 登记纪律

- 依赖登记的权威表是 `scripts/runtime_contract.py` 的 `EXTERNAL_DEPENDENCIES`
  （import 名 → 发行包/最低版本/可选组）；本文件只承担许可与来源声明，两处不同职能。
- `scripts/v2_contracts.validate_external_imports` 以该登记表为唯一对照源；
  每次扩表必须与真实安装验证同任务发生（总工单全局约束 10）。
