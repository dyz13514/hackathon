# 完整生产数据工作簿

下载 [`planning-demo-2026-09-26.xlsx`](outputs/01a0d936-fc4b-73e1-8ca9-459e1052b6eb/planning-demo-2026-09-26.xlsx)，在 Import 页面上方的 **Complete planning workbook** 入口上传。预览显示各表行数且无错误后，点 **Confirm complete workbook import**。此时才会出现 `PACKAGE` Import batch，Dashboard、Schedule 和后续页面才会读取这些数据。无需运行 seed。

工作簿含一个说明页 `Guide` 和以下 11 个数据页。页名与第一行列头必须保持不变；可以替换以下所有演示行，并保持跨表 ID 关联。

| 工作表 | 用途 | 必须保持的关联 |
| --- | --- | --- |
| Products | 产品名称 | `product_id` 唯一 |
| Materials | 物料库存和预留 | `material_id` 唯一，数量不得为负 |
| Machines | 机器类型、能力、状态、可用时间、加工倍率 | `machine_id` 唯一；`capabilities` 用分号分隔 |
| Workers | 工人技能和班次 | `worker_id` 唯一；`skills` 用分号分隔 |
| Operations | 每种产品的 1–3 道顺序工序、机器/工人要求和时间 | `product_id` 在 Products；`sequence` 从 1 连续编号 |
| ProductMaterials | 每产品单位用料 | 产品和物料 ID 均已定义 |
| IncomingDeliveries | 预计到货时间和数量 | 物料 ID 已定义 |
| MachineDowntime | 停机窗口与原因 | 机器 ID 已定义 |
| WorkerAbsences | 缺勤窗口 | 工人 ID 已定义 |
| ChangeoverRules | 机器切换产品的换型时间 | 引用的机器、产品 ID 已定义 |
| Orders | 订单数量、交期、优先级 | 产品 ID 已定义 |

日期建议使用 Excel 日期/时间单元格或 `YYYY-MM-DD HH:MM:SS`；布尔值用 `TRUE`/`FALSE`。上传上限 5 MB、总计 2,000 数据行。系统会在预览时检查格式、必填、重复 ID、数值、工序顺序、时间窗口与跨表引用，出错会指明工作表和行号；不会猜测缺失的配置。

演示工作簿有 89 行真实可导入数据：6 产品、10 物料、5 机器、8 工人、12 工序、18 产品用料、3 到货、1 停机、1 缺勤、11 换型规则、14 订单。它刻意保留一项资源/物料缺口，因此首次排程预计呈现 24 个已排作业、1 个不可排产作业；这是“部分可行计划”的示例，不是静态占位内容。不同日期或你改过数据后，实际结果以系统计算为准。

完整流程：

1. 在空本地数据库启动系统。若此前有旧演示库，先备份旧库并用空数据库运行；不要对需要保留的生产库直接清空。
2. Import 上传完整工作簿，核对 11 页、89 行，确认后在 **Import batches** 看见 `PACKAGE` 批次。
3. Schedule 生成提案；Approval 核对不可排产原因后审批。审批后刷新 Schedule、Dashboard、Risks、Insights、Value Ledger。
4. What-if、Quote 使用已经落库的产品与资源进行模拟；What-if 的采纳产生新待审计划。如果 Approval 已有同一天的待审缓解计划，先审批或拒绝它，再采纳新方案。
5. 日后更新订单可走 Import 下方的“Daily order or material update”列映射入口。缺料时可在 Schedule 的不可排产作业下直接填写物料的新**总库存**和修正原因；该操作会推进输入快照版本并留下审计记录。已有计划及 Trace 是历史记录，不会原地变化；如有待审计划，先在 Approval 拒绝它，再生成新计划。
6. 整包不能直接叠加到已导入的完整工作簿上。确实要替换机器、工序、班次等全部配置时，先处理待审计划，备份数据库，在 Import batches 中撤回旧 PACKAGE 批次，再导入保持相同实体 ID 的新版完整工作簿；不要把 Trace 或不可排产清单当作可编辑的源数据。

开发验证可运行 `backend/.venv/Scripts/python.exe backend/scripts/smoke_demo_workbook.py outputs/01a0d936-fc4b-73e1-8ca9-459e1052b6eb/planning-demo-2026-09-26.xlsx`。它使用临时数据库，不修改本地 `.env` 指向的业务库。
