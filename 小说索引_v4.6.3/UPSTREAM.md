# UPSTREAM — 来源与归属说明（由 run01 台账派生）

任务：54Lynnn参考能力同步 run01 ｜ 生成时刻见 `deltas.json.generated_at` 与各候选 `packaging-report.json`

## 已核查的上游

- `https://github.com/54Lynnn/bilibili-auto-transcript.git` @ `c73d149d458f6d680745f6b8e3027d08f99009a7`
- 另一条上游 `https://github.com/54Lynnn/knowledge-rag.git` 本批未作为改动依据（未断言二者无接口）。

实测范围（不越据立论）：2026-10-02T16:50:12 用 `git ls-remote` 复算，两仓**默认分支 HEAD** 与上述固定提交逐位一致；**未做全分支与标签清点**，因此不声称「作者不存在更晚版本」。核查命题因此是「已固定上游里已具备的能力是否被本地遗漏」。

## 本产品的关系与分类

- 关系编号：`R05` ｜ 核查项 7 条
- 裁决分布：不适用 2, 已吸收 1, 来源待确认 1, 需要更新 3
- 来源类型：indirect-inheritance 1, protocol-reference 1, source-unverified 5
- 本地基线：`607acb2daae2`（来源 N-OLD）

## 明确不得署名为作者的本地自有提交

- 本产品不涉及 knowledge-rag 自有修复；本轮改动若与本地同族实现同源，已在下方「未采用/归属」段落注明，不冒充作者代码。

## 本轮实际改动与归属

- `R05-D02` 路径归一化与历史重复行迁移（成人索引 v4.6.5 等价能力） ｜ 来源类型 `source-unverified` ｜ 上游 `无作者侧来源；能力只存在于本地同族后代 A/scripts/index_db.py:42-46,139-202` ｜ 本地 `snapshots/N-OLD/scripts/index_db.py:99-105,118-132,134-152,154-164（get` ｜ 状态 `PASS`
- `R05-D03` 删除/重命名的元字符安全（成人索引 v4.6.3 等价能力） ｜ 来源类型 `source-unverified` ｜ 上游 `无作者侧来源；能力只存在于本地同族后代` ｜ 本地 `snapshots/N-OLD/scripts/delete_novel.py:36（glob f"{novel_author}-*-idx` ｜ 状态 `PASS`
- `R05-D05` 现有测试是否覆盖上述缺口 ｜ 来源类型 `source-unverified` ｜ 上游 `不适用（缺口是本地旧版自身）` ｜ 本地 `snapshots/N-OLD/tests/test_index_db.py:1-60（5 用例）；tests/test_ops.py 无元` ｜ 状态 `PASS`

### 改动归属在哪里查

- 逐条 delta 的实现状态：任务根 `deltas.json`（含 RED/GREEN case 名与提交号）；
- 全量提交明细：本仓 `git log --oneline`（本文件不复制清单，避免自我失效）。

## 未采用与边界

- 本套改动来源是**同族本地实现的等价能力**（成人索引已修的同类缺陷），不是作者代码；作者仓库无对应物。

## 来源未闭合（不得当作已对齐）

- `R05-D07` 维护/使用状态、宿主真实使用、是否停用旧技能：本产品无 Git 身份、无作者侧提交、宿主使用未验证，维护与停用判断的证据基础不存在；抬成 ALIGNED_NO_CHANGE 会掩盖未验证事实

下一条所需证据：旧库早期版本的提交号字节，用于确认 3 条缺陷是否为继承而来。

## 分发状态

- 本轮候选 `distributable=false`：未点名发布渠道即不上传；
- 未做真实安装、宿主链接替换、原仓回灌、真实模型/媒体调用与人工画质评价。
