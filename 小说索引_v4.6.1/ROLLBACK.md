# 回滚

- 弃用本候选目录即可回到 20260930 批次的冻结候选；本任务未替换任何安装链接。
- 本仓源码基线为 F 盘独立 Git 仓（`products/novel-legacy`），回滚只作用于该目录：
  ```
  git -C products/novel-legacy checkout 607acb2daae2
  ```
- 合成库演练失败时保留原始副本与日志，不反向写真实库。

