# 本目录夹具说明（SYNTHETIC）

- 全部文本为本轮自造，**不是**任何真实作品、真实用户书稿或现役资产库内容；
  生成器：`tools/c02_make_synth_fixtures.py`（RunRoot 内，维护源专用，不随包），可用同环境重跑复现。
- `最小剧本.docx`：2 个一级标题、3 段正文、1 个 2×2 表格（行 2 首格「玉牌」次格「信物」），
  页眉「剧本草稿」与页脚「页脚装饰 请勿外传」为**装饰内容**，产品规范化时必须剔除。
- `最小小说.epub`：spine 顺序刻意为 nav → chapter_b → chapter_a，用于钉住
  「按 spine 取正文，不按文件名字母序」；含 `<style>` 与 `<script>` 干扰块，必须被剔除。
- 期望契约来自 `测试/unit/test_source_normalizer.py`（顺序、定位器 `标题/段落N/行N`、
  引用可回溯、manifest 格式与哈希）。
