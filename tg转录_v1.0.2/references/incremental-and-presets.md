# 增量更新与常用方案

本文件按需加载：增量更新（在线动作）与常用方案（本地文件）各走自己的入口，
细节不进 `SKILL.md`。每句话都能指回 `scripts/updating.py`（行首 `update:`）、
`scripts/presets.py`（行首 `preset:`）或 `scripts/archive.py` 的注册与转发；
指不到的，一律落在末节「未验证与限制」，不写成能力。

入口：`archive.py update --manifest <覆盖清单> --probe <上界探测> --fetched <抓取结果>
--database <索引库>`（`archive._cmd_update` 薄转发到 `updating.main`，参数名逐字对应
`updating.build_parser`）；`archive.py preset --preset-action save/list/show/use
--preset-dir <方案目录> …`（`archive._cmd_preset` 薄转发到 `presets.main`）。
12 个子命令已全部注册（`archive.COMMANDS`），没有未实现动作。

## 增量更新（`update:`）

代码：`scripts/updating.py`；组合：`plan_update(manifest, upper_bounds, options)` →
`commit_update(database, plan, fetched)`。增量依托已验收的覆盖清单
（`kind = telegram-archive-coverage`），不只看最大消息 ID。

### 回查窗口（`update: plan_update`，`DEFAULT_LOOKBACK_DAYS = 7`）

- 默认回查上次快照末端 **7 天**，且不越过原授权范围；`options` 可显式换成别的**有界**
  区间（`recheck_since` + `recheck_until` 必须成对给，半开不接受）。
- 无界回查一律拒绝：`recheck_all`、`lookback_days=None` 直接抛错；回查天数限
  `MIN_LOOKBACK_DAYS..MAX_LOOKBACK_DAYS`（`update: _int` 校验），要更大范围请显式给
  有界的起止。
- 每轮固定新上界；`resume_round` 复跑沿用上一次计划的冻结上界，新的探测值只记
  `deferred_upper_bound`，不随重跑漂移。
- 无覆盖凭据的本地源不能直接继承在线水位（`update: _credential_of` 判定）：
  `metadata` 不足时只追加，并说明无法检测编辑的原因。

### 提交与版本（`update: commit_update`）

- 更新按 peer + chat + message 身份合并去重；被修改的消息保留可追溯旧版本并更新
  当前阅读/搜索视图（`update: revisions` 语义由 `catalog` 既有事务接口承载，
  `stage_archive` / `publish_staged` / `commit` / `abort`，不另起第二份锁）。
- 可信删除只写 tombstone + 证据（`update: update_events` 台账表，与消息行同事务
  提交），默认保留旧原档，不物理删历史。删除、撤回或无法访问**不能**仅凭某次抓取
  缺少 ID 判定：只有后端明确删除事件（`backend_delete_event`）或对确切已存消息作
  成功且权威的复查（`authoritative_recheck` + `result=absent`）才记删除
  （`update: CREDIBLE_DELETION_BASES`）；权限/会话类失败记「不可访问」，其余记
  「未核验」，两者都不写 tombstone（`update: _merge_chat`）。
- 最大 ID 仅是定位信息，不是全量凭据（`local_max_message_id` 另记，不推进水位）。
- 失败或缺段不推进完成水位；构建新版本阅读/索引失败不切换当前版本，旧可用视图
  维持、新批次可恢复；所选媒体未验收完成时不宣称该轮混合更新完成。
- 互斥：与导出/导入/检索按**归档锁**互斥（同一状态根，加锁序固定先归档后会话，
  `state.LOCK_ORDER`）；在线侧另持会话锁。锁被占报退出码 3，索引不可用
  （`catalog.CatalogError`）同样报 3，原始归档未被改动。

### 报告与退出码（`update: main`）

- 四个文件（`--manifest/--probe/--fetched/--database`）缺任何一个直接退出码 2，
  不默认、不猜；`--out` 给了就把整份报告落盘。
- 报告给出本轮回查陈述（`recheck.statement`）、已提交水位与是否推进
  （`watermark.after/advanced`）、未核验/不可访问/删除标记计数。
- 退出码：`complete` → 0，其余状态 → 1（`update: main` 尾部）。

## 常用方案（`preset:`）

代码：`scripts/presets.py`；合同：`save_preset(directory, name, selection, *,
overwrite=False)`、`resolve_preset(directory, name, overrides, now)`。
**仅在用户要求保存时保存命名方案，仅在用户主动调用时解析**；存在历史方案不自动
启动任务，不创建定时或无人值守任务。

### 保存（`preset: save_preset`）

- 存的是选定对象、内容、输出、清洗、目的地和范围表达式（含相对表达式原文），外加
  安全的本地账号引用；验证码/密码/会话库/命名空间/带 `user:pass` 的代理 URL 与
  `tdl/storage` 等运行配置**丢弃且逐项申报**（`preset: _split_fields` +
  `RUNTIME_DROP_REASONS`，返回体带 `stored_fields` / `dropped`），不落盘。
- 键名归一化精确匹配 + 值侧形态扫描：偷藏进白名单字段的凭据（密码/验证码/手机号
  形态、无 scheme 的 `user:pass@host`、会话库形态）整次拒绝，不回显值、不落盘。
- 同名方案返回 `status=exists`，覆盖必须用户明确说「覆盖保存」（`overwrite=True`），
  不自动改名、不静默覆盖；每次保存都带 `started_task=false / scheduled=false /
  executed=false / real_account_verified="NOT_RUN"`。
- 方案目录由使用者给出，**不得位于技能发布包内**；原子保存复用 `state` 唯一发布器。

### 调用（`preset: resolve_preset`）

- 绝对日期范围下次仍用该日期；相对范围在本次调用时用传入 `now` 重新解析——支持的
  表达式白名单为 `today / yesterday / this_month / last_month / last_N_days`
  （`preset: RELATIVE_FIXED_EXPRESSIONS` + `last_(\d{1,4})_days`，N 为 1–4 位数字），
  由任务时区确定；其它自然语言由代理澄清，不自行猜日期。**本模块不读真实时钟**，
  `use` 缺 `--now` 直接拒绝。
- 本轮 `overrides` 逐字段**替换**保存值，不做并集；账号/目标无法唯一解析时回问重新
  选择（label-only 重述账号直接 `account_unavailable` 回问，不兜底匹配旧目标）。
- 解析后重新 `validate_job` 和能力检查（`preset: inspect_capabilities`），展示本次
  摘要；未知方案名返回 `status=missing` + 可选方案名 + `questions`，不自动挑一个。
- CLI 只打可展示视图：`job` 里带着本轮运行配置，绝不打印（`preset: _public`）。
- 退出码：`resolved / saved` → 0，`capability_gap` → 3，其余（含需用户重选的
  `needs_choice / missing / exists`）→ 2（`preset: _status_exit`）。

## 未验证与限制（不许当承诺）

1. 真实账号上的增量更新与方案调用均为 `NOT_RUN`：合成与假后端只证明本地调度与状态
   处理，不证明 Telegram 在线兼容性。
2. 本地导入的产物恒为「用户导入的本地快照，在线覆盖未验证」，不能据此跳过在线导出，
   也不能直接继承在线水位。
3. 范围完成只表示该版本、当时账号可访问的数据导出完成；回查窗口之外的更早编辑标记
   为未核验，不宣称全历史完全同步。
4. 分享、检索、阅读各节的口径以各自参考文件为准，本文件不复述、不放宽。
5. 本文件不写任何本机真实路径；示例位置一律写 `<保存目录>` / `<方案目录>` 这类占位。
