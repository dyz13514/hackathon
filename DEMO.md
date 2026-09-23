# 演示素材与讲解顺序

## 1. 导入演示

在 Import 页面上传 `backend/app/seed/samples/demo_material_update.csv`。

它会被识别为 `MATERIAL`，三列都会自动映射。确认导入后，三个物料的可用库存会更新；可在批次列表中回滚这次导入。

`backend/app/seed/samples/demo_new_workers.csv` 是第二份干净样例，会识别为 `WORKER`，适合演示新增员工与批次回滚。

`backend/app/seed/samples/dirty_orders.csv` 刻意带有混合日期、空表头和不可解析数值，适合演示系统要求人工确认而不是猜测。

## 2. 排产与审批演示

1. 在 Schedule 页点击生成计划。
2. 在 Approval 页批准计划，使它成为 ACTIVE。
3. 打开 What-if 页面进行模拟，再从模拟结果生成待审批提案。

排产与模拟由确定性内核计算，因此相同数据会得到相同结果；它们不是调用 AWS 模型的入口。

## 3. What-if 输入示例

本地 `LLM_MODE=STUB` 下，以下表达可用于完整演示，不会访问 AWS：

- `把 ORD-009 的优先级改为紧急`
- `CNC-01 明天上午停机 6 小时`
- `将 MAT-STEEL-01 的可用库存改为 35`

翻译结果会先显示为结构化变更，点击确认后才会运行模拟。其他自由表达需要把 `.env` 中的 `LLM_MODE` 改为 `LIVE`，并重启 `make dev`；只有这一模式会调用你配置的 Bedrock 网关并产生用量。
