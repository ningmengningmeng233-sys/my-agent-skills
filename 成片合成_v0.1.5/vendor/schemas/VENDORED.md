统一运行台账内核的包内副本（C8 跨技能共享件）

为什么要 vendor：`renderplan/plan.py` 与 `tests/test_ledger.py` 都要吃 `schemas/ledger.py` 的 `validate()`
做台账校验。以前解析只认「工作区同级的 `schemas/`」——那是开发柜特权，装到别人机器上那里什么都没有，
收件人跑自带测试直接报「缺统一台账内核」。这不许改成静默跳过：内核收下才算真对齐。

| 项目 | 事实 |
|---|---|
| 来源 | 技能柜共享内核 `schemas/ledger.py` 与 `schemas/run-ledger.schema.json` |

| 副本 sha256（storyboard.schema.json） | 89fd837994913599b542343f0e8dd24490fc5039ce9feed30b621f8435616621 |
| 副本 sha256（asset-ledger.schema.json） | 352ffc40a91bddd263b3801c7fba606c4e8ece233e1e618ba4e6001b6a1a6153 |
| 参考合同说明 | storyboard/asset-ledger 两件为参考合同：plan.py 内联校验与之逐字段对齐，运行期不加载文件；运行期加载的内核仍只有 ledger.py + run-ledger.schema.json |
| 参考合同来源 | W/schemas（2026-10-11 字节，与 E3 schemas 基线逐字节一致；改上游须同步重算） |
| 来源侧最近提交 | d6691f2 chore(P56-n): 代码瘦身（并表 twin 键切分、去掉无人用的 _as_num default 形参）+ 同步 twin 登记哈希 |
| 副本 sha256（ledger.py） | 5493897bb3449f90b3d8d88c24093c67856a3b30297643065ad7fd5739c485db |
| 副本 sha256（run-ledger.schema.json） | a587eb93e300e2c85c4a5ac81c7b8d61d659ceb998a5ff2bcca2d6e2832c065e |
| 副本与来源是否逐字节一致 | 是（构建时以 `src == vendor` 摘要断言把住） |
| 许可 | 与本包自有内容同为 MIT、同一作者，不存在跨作者覆盖许可问题 |
| 解析顺序 | 环境变量 `UNIFIED_LEDGER_PY` → 本包 `vendor/schemas/` → 工作区同级 `schemas/` |
| 漂移检查 | `tests/test_vendored_ledger.py`：解析必须落在包内、副本必须匹配本文件登记的摘要、内核必须真能 `validate()` |
