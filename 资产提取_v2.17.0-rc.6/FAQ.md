# FAQ：常见报错与处理

| 现象 | 原因 | 处理 |
|---|---|---|
| `❌ LLM 认证失败（401/403）` | API key 无效或接口地址错误 | 检查 `.env` 的 `OPENAI_API_KEY` 与 `SUMMARY_API_URL`；真实 `.env` 推荐 `~/.asset-extract/.env` |
| 大量 `429` 后仍失败 | 限流 | 引擎自动指数退避（最高 60s）；仍失败则减小 `ASSET_WORKERS`，重跑同一命令断点续传 |
| `--yes` 报错退出 | 自动化场景未显式传 `--style` / `--genre` | 按报错补齐：`--genre <题材> --style <画风> --yes`（禁止静默默认） |
| `⚠️ --style「X」未命中画风库卡片` | 名称拼写不符 | 用卡片编号（如 H7）、卡片全名或别名重试；46 张卡清单见 `references\画风库.md` |
| check_schedule 报「缺身份锁 / 画风锁定段-XX / 视图定性段」 | 生成器输出缺锁定段 | `generate_prompts.py --base/--deriv ... --force` 重生成对应类型；不要手工糊弄检查 |
| check_schedule 报「场景层次雷同 / 内容整节雷同」 | 多个资产正文套话雷同 | 对应资产按各自视觉描述 `--force` 重生成；模板已强制层次来自各场景描述 |
| 拆段报「同名段内容冲突」 | 源文件已变更或分卷重编号 | 确认后 `--force` 覆盖；分卷重编号请先合并源文件 |
| 裁头版命令报错阻断 | 未先生成 full 版 | 先 `--base --view-mode full`，再补 `--view-mode crop` |
| 服饰设计校验有阻断项 | 剧种黑名单命中/配色越界 | 按 `服饰设计\输出\<剧名>\校验报告.md` 逐条修；阻断清零才交付 |
| check_schedule 报「AI味措辞 / 顿号罗列过密」 | 生成正文出现 AI 腔或罗列过密 | `--force` 重新生成对应文件；避免「说白了/总的来说」等起手式与「不是…而是」句式 |
| check_schedule 报「负面段含画风负面」 | 负面段漏掉画风负面并集 | `--force` 重新生成；勿手工删负面词 |
| 中断后想继续 | — | 重跑同一命令即断点续传；或看步骤 0 开始界面的「库内状态面板」/`maintenance.py status --json`（含流水线状态与下一命令） |
| 想回滚本次全部产物 | — | 提前 `maintenance.py backup`，出问题 `maintenance.py restore --zip ... --force` |
| 输出里有英文被判不合规 | 纯中文铁律（色值除外） | `--force` 重新生成对应文件；自由描述模式不要传英文画风描述 |
| check_asset_index 报「关系目标不存在 / 事实等级非法 / 母版漂移 / P3 缺说明」 | 索引关系悬空、越级事实或母版被手工改动 | 悬空绑定改待确认后重跑聚合；无出处事实降为强推断/制作提案；母版漂移以索引为准回写母版；P3 补故事功能说明 |
| verify 后无交付清单 | 有检查失败或缺画风 | 看 `.process/验证报告.md` 逐项修复后重跑 `verify --style`；失败不覆盖历史有效清单 |
| 视觉导出包提示「本任务无参考图」 | rc.1 的参考图路由没有接入真实链路 | 升级到 v2.15.0-rc.2；正脸导出必须带权威包风格参考图（`参考图/` 目录内有实际图片） |
| `verify-visual` 在空库返回 0 | rc.1 把「视觉未启用」当成通过 | v2.15.0-rc.2 起空库退出 3 并给 `VISUAL_NOT_ENABLED`；只有 DELIVERED 退出 0 |
| 质检报模型不存在 | rc.1 默认质检模型 `gemini-3-pro` 不在官方目录 | 默认改为 `gemini-3.1-pro-preview`，或用 `GEMINI_QC_MODEL` 覆盖 |
| 视觉命令读不到 `.env` 里的密钥 | 视觉 CLI 只读进程环境 | 已统一为 `ASSET_ENV_PATH` → 技能 `.env` → `~/.asset-extract/.env`；进程环境优先级最高 |
| 任务包发给别人后打不开参考图 | 旧包只写三份文字文件 | 新包自带 `参考图/` 与 `任务包清单.json`（逐文件哈希），复制到异目录仍可用 |
| 风格包参考图缺失导致生成失败 | Git 检出不含被忽略的 32 张参考图 | 用 `install_stylepack.py` 从本地私有 zip 安装，再用 `--verify` 逐文件校验 |
| verify 报「拒绝签发（SOURCE_UNVERIFIABLE）」 | 缺 `.start-state.json`，或状态里 `input_source` 指向的原始文件被移动/删除 | 确认原始素材已在原位，然后 `python scripts\maintenance.py init --lib <资产库> --input-source <原始txt/md> --genre <题材> --style <画风>` 重建状态，再重跑 `verify`。素材须与资产库长期共存，**不要用临时文件当源** |
| verify 报「拒绝签发（STYLE_MISMATCH）」 | 显式 `--style` 与启动状态画风不是同一张卡 | 统一为同一张卡：传与 `init`/步骤 0 一致的画风（`A2` 与「仙侠奇幻风」等价，不算冲突；`A2` 与 `H12` 冲突）。确需换画风就重跑受影响生成步骤后再 `verify` |
| verify 报「拒绝签发（STYLE_UNVERIFIABLE）」 | 状态文件没写画风（手工拼的 `.start-state.json`） | 用 `maintenance.py init` 重写状态文件（字段齐全：`input_source`/`genre`/`style`/`emphasis_hash`/`output_root`/`last_confirmed_at`） |
| status 显示 `NEEDS_REVIEW` + `SOURCE_UNVERIFIABLE` | 已签发的库丢了状态文件或源文件 | 同上一行的 `init` 修复；状态机不会把「源不可核验」当成 `DELIVERED` |
| `init` 报「已存在且参数不同」 | 防止静默覆盖配置 | 确认要换源/换画风后加 `--force`；建议先 `maintenance.py backup`（覆盖会改变源证据，旧交付清单随之失效） |
| 老库想升级 v2.13.0 | 缺索引与母版 | `maintenance.py migrate --lib <资产库>`（自动备份，旧表不动）；悬空绑定进待确认人工处理。老库缺原始输入证据时**不签发** `DELIVERED`，补来源后重验 |
| 状态机显示 EXTRACTED 但反推表存在 | 缺资产索引 | 重跑聚合生成索引，或老库跑 `migrate`；只看文件存在不判完成 |
| 拆段报「输入规范化失败（UNSUPPORTED_SOURCE_FORMAT）」 | 传入的是 PDF 或不支持的后缀 | 本技能**不支持 PDF**（也不建设文字识别）。先把素材转成 TXT / Markdown / DOCX / EPUB，再重跑同一命令 |
| 拆段报「输入规范化失败（SOURCE_DECODE_FAILED）」 | 文本不是 UTF-8 或 GB18030 | 转码为 UTF-8 后重跑。引擎只做显式编码探测，**不会用忽略式解码把乱码字吞掉** |
| 拆段报「SOURCE_ORDER_MISSING / DUPLICATE / OUTSIDE_ROOT」 | 顺序清单与源文件不是一一对应、有重复行、或条目越出清单所在目录 | 顺序清单每行写一个相对路径（相对清单所在目录），必须覆盖全部源文件且不重复、不越界 |
| 用 DOCX/EPUB/多文件后，`.process/normalized_sources/` 是什么 | 规范化中间产物（正式设计，不是垃圾） | `规范化原文.md` 是真实拆段输入，`来源清单.json` 记录原文件哈希与顺序。原文件只读，可随时用 `normalize_sources.py` 重建；**不要手工编辑**这两个文件 |
| DOCX/EPUB 项目改了原始素材后没重跑规范化 | 源指纹覆盖原文件本身 | `status` 会立刻显示 `SOURCE_CHANGED`、脱离 `DELIVERED`；重跑 `init`（或 `normalize_sources.py`）→ 拆段 → 后续步骤 → `verify` 重新签发 |
| verify 报「拒绝签发（SOURCE_UNVERIFIABLE）」但用 TXT 时正常 | 规范化项目的**原始**源或顺序清单被移动/删除 | 规范化项目在 `.start-state.json` 里额外记录 `source_manifest_path`：原始源与顺序清单都必须留在原位，删掉任何一个都会拒签 |
| 剧本用「第一场/第二场」没有切分 | 场次标记需要分隔符 | `第一场 茶楼` 会被识别为场次标题（等价「章」）；`第一场雪落了下来` 是散文，不会被误判。若剧本用的是其他标记（如「场景一」），请改写为「第一场」形式 |
| 想把法术、天气、特效单独列成一类资产 | 顶层类型只有四类 | 规格明确四类顶层资产不变：法术/天气/特效作为**视觉需求**挂在已有资产或场景上（`[视觉需求:天候=雨]`），不会生成第五类 ID，索引校验器会拒绝第五类 |
| `status --json` 里的 `visual_delivery` 是什么 | 第二条交付状态线 | `data_delivery`（资产资料）与 `visual_delivery`（视觉资产）互不阻塞。未创建视觉计划时显示 `NOT_ENABLED`（「视觉生产：未启用」）——这是**合法状态，不是失败**，资料交付照常 |

| 视觉生产报「未配置 GEMINI_API_KEY」 | 自动生图需要密钥（核心反推不需要） | 两条路：在包外 `.env` 配 `GEMINI_API_KEY` 后重跑 `run`；或用 `visual_production.py export` 导出手工任务包，生成后用 `import` 回传 |
| 视觉任务卡在「需要人工决定」 | 已自动返工 3 轮仍未通过 | 人工查看 `视觉资产\任务\<task_id>\attempt-NN\` 下的图片与 `qc.toml` 问题码，按 `repair_instruction` 人工处理或接受现状；不会再发请求 |
| `verify-visual` 报「风格包内容哈希与登记表不一致」 | 母版更新后登记表哈希变了，项目仍固定旧哈希 | 这是有意的：母版更新只提示、不自动改变项目。确认要升级就重跑 `create_visual_plan.py` 让任务重新固定新版本与哈希 |
| 想删掉一个不用的风格 | 生命周期单向 | 依次 `manage_styles.py deprecate` → `disable` → 手动改 `removable`；`scan-usage` 确认零引用后 `remove` 只产出**待删除清单**，物理删除需人工执行 |
| 旧的三格/裁头图片还能用吗 | 旧视觉产物默认不可作为参考 | 跑 `maintenance.py migrate-visual --lib <资产库> --style <画风>` 非破坏登记（先备份、旧文件不改），新计划只依据资产索引；旧图需通过新版检查才可复用 |

仍无解：跑 `python scripts\maintenance.py status --lib <资产库> --json` 把输出贴给编排器，按「流水线状态机」继续。

## v2.17.0 新增能力

| 问题 | 原因 | 处理 |
|---|---|---|
| 升级后旧库 `status` 不再是「已交付」了？ | 签发契约由 3 项扩为 9 项，旧签发没核验数量守恒与图片审核 | 重跑一次 `python scripts\maintenance.py verify --lib <资产库> --style <画风>` 重新签发；上一份自动留档到 `交付清单历史\` |
| 历史交付清单能当当前状态用吗？ | 历史目录只作快照留档 | 不能。`status` / `verify` 只读 `<资产库>\交付清单.json`，放进历史目录的清单永不生效 |
| 数量守恒是什么？为什么阻断我签发？ | 守恒式「正式资产 = 已产提示词 + 明确待审 + 明确阻断」 | 有资产既没提示词、又没写明豁免理由时，补提示词或在 `提示词\基础\跳过清单.md` 写清「非资产/用户取消」 |
| 提示词要换成编译器产出的版本，会覆盖我的产物吗？ | `compile-prompts` 默认 shadow | 默认只写 `.process\shadow-提示词差异.md`。确认差异后再 `--apply`：先备份、只改已存在节的正文、保留编号、缺节追加，总账里没有的旧节原样保留并在 `orphans` 里点名 |
| `run-visual` 能不能指定只出某几个视图？ | 视图规格由 Omni 标准定义 | 不能，也不该能：命令只提供「这批同时生成几张」（`--batch 1-4`） |
| 生成中途失败了，`status` 为什么立刻不再说已交付？ | 事件日志是写前日志，最新一条失败即撤销 | 这是有意为之：计划文件可能还没更新，但事实已经失败。按 `visual_delivery.stage=REWORK` 走返工或重跑 |
| 图片没过审，`verify` 会拦吗？ | 选了「直接生成图片」时图片审核属于产品结果的一部分 | 会拦（`check_visual_review`）。走 `review-export` → 主模型读图 → `review-import` → `publish` 后重跑 `verify` |

## v2.16.0 新增能力

| 问题 | 原因 | 处理 |
|---|---|---|
| 批量生成怎么跑？还要一个个 `--task` 吗？ | v2.16.0 起有批量入口 | `python scripts/visual_production.py run-ready --lib <资产库>`；只看状态用 `status`，中断后用 `resume` |
| 生成到一半断了，会重复消耗额度吗？ | 调度器用写前事件日志 | 不会：`resume` 重放事件补齐状态，已保存的图片不重复请求 |
| 429/超时会不会把三次机会用掉？ | 传输错误与画面失败分开计 | 不会：传输失败走有上限退避重试，只有画面被审核否决才增加轮次 |
| 为什么 `run-ready` 没有密钥也退 0？ | 没有可执行任务时不触网 | 正确行为：真要有任务可跑而没有密钥时它会明确失败并指路 |
| 图片是谁审核的？ | 终审只认当前主模型 | `review-export` 导出请求 → 主模型读图后写结果 → `review-import` 导入 → `publish` 发布；Gemini 不终审 |
| 「通过但有备注」什么时候不能用？ | 16 个硬门码一票否决 | 存在任一硬门问题（手部/比例/身份/画风/裁切/站位/视角/视线/轴线/遮挡/道具数量/场景结构/文字水印/参考链）时只能「需要返工」 |
| 旧的第一集图片要重做吗？ | 非破坏迁移 | 先 `migrate_visual_library.py scan`（只读）核对提案，再 `apply`（只复制唯一匹配、不自动通过），最后 `verify` 与迁移前清单比对 |
| 提示词任务包在没有安装 Omni 的机器上能用吗？ | 任务包自包含 | 可以：包内带实际参考图与逐文件哈希清单，手工生成后 `import` 回来走同一条审核链 |

