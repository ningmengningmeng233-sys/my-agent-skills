# 索引库（用户自备，可移植；v4.3.0 起为默认自动发现项）

## 一句话

`rewrite` 默认会自动发现并加载本目录。**索引库不是装饰品**：
它提供的文风卡/章节索引/小说索引会进入改写 prompt，并在
`audit.jsonl` 写 `enhance_use`，在 `manifest.json` 写
`enhancements.reference_assets.enabled/used`。加载不到会明确报出
`enhance_unavailable`，绝不静默假装用过。

## 目录结构

```
索引库/
├── 小说原文/       ← 小说原文（按书分文件夹；只读检索上下文）
├── 小说索引/       ← 每书一个索引文件（frontmatter 含"包含元素"标签）
├── 章节索引/       ← 每章一个索引（含"爽点/看点/伏笔/金句"标注）
├── 文风蒸馏卡/     ← 风格卡/场景卡（含"可执行规则/锚点"）
└── 判官验证/       ← 文风卡判官验证信息
```

本包已内置最小兜底资产：
- `文风蒸馏卡/内置-网文基础风格卡.md`
- `章节索引/内置-网文基础章法-idx.md`
- `小说索引/内置-网文基础风格.md`

用户放入自己的索引后自动优先；不放入也不静默，而是用内置卡兜底。

## 加载优先级

`CLI --index-lib <路径>` ＞ 工作区 `.enhance.env`/`.env` 的
`INDEX_LIB_DIR` ＞ 环境变量 ＞ **本机自备索引库（真实数据）**
＞ skill 根 `索引库/`（内置兜底）＞ 未找到。

质量优先模式：

```bash
# 找不到索引库直接失败，防止静默降级
uv run --project v4 python -m v4.core.cli rewrite <工作区> --require-index-lib

# 显式指定
uv run --project v4 python -m v4.core.cli rewrite <工作区> --index-lib "<你的索引库路径>"

# 本次不加载（本地规则仍会注入 prompt）
uv run --project v4 python -m v4.core.cli rewrite <工作区> --no-index-lib
```

## 结构要求（用户自己的爽文索引库同此结构）

- `小说原文/<书>-<作者>/<章节段文件>.md`
- `小说索引/<书>-<作者>.md`（frontmatter 含"包含元素"）
- `章节索引/<书>-<作者>-第X章~第Y章-NNN-idx.md`（含"爽点/看点"段落）
- `文风蒸馏卡/<场景>-<书>-<作者>.md`（含 `### 1. 句子节奏` 式轴 + `**规则**` 行）

## 使用审计（防"没读假装读过"）

改写完成后查：

```bash
uv run --project v4 python -m v4.core.cli status <工作区> --verbose
```

- `manifest.json` → `enhancements.reference_assets.enabled/used`
- `audit.jsonl` → `enhance_use`（逐条含 asset_path/SHA-256/selection_reason）
- 索引库路径不存在或形态不对 → 审计写 `enhance_unavailable`

## 新书扩充

直接向 `小说原文/` 添加新书文件夹即可，skill 会自动动态扫描并纳入轮换；
运行 `scripts/extract_samples.py` 可将新书样本抽取进 `references/样本库/`；
运行 `scripts/build_demo_libs.py` 可从章节索引生成示范库候选。

