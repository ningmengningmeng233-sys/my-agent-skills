# 视觉金标准契约夹具（工单04-Task3）

每套夹具包含 `asset.json`、`expected_plan.toml`、`rubric.toml`：

- `asset.json`：资产事实（含原文摘句），用于生成计划；
- `expected_plan.toml`：期望任务集合、依赖链、画幅与阻断问题码；
- `rubric.toml`：该类型的检查口径与阻断码；`human_visual_review` 一律留空，
  **人工视觉结论由人工填写，自动化测试不代填**。

老罗用例的外部错误图属用户私有输入，**未入库**：`external_image_hash` 为空且
`external_image_status = NOT_AVAILABLE`（实施时该文件已不在磁盘，不采集假哈希）。

