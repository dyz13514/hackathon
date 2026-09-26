# AI Production Planning Agent

面向中小制造车间的排产演示系统。它把订单、物料、机器和人员放进同一份生产快照，生成可解释的排产提案；计划必须经人工审批才能成为 `ACTIVE`。遇到停机、缺料或加急需求，可以先在沙箱里推演影响，再决定是否提交新提案。

**第一次了解项目：**先看下面的[功能地图](#功能地图)，再按 [DEMO.md](DEMO.md) 实际走一遍。需求、架构和实施任务的完整依据分别在 [requirements.md](.kiro/specs/production-planning-agent/requirements.md)、[design.md](.kiro/specs/production-planning-agent/design.md)、[tasks.md](.kiro/specs/production-planning-agent/tasks.md)。

## 快速启动

需要 Python 3.11+ 和 Node.js 18+。Windows 直接双击项目根目录的 `start.cmd`，或在 PowerShell 中运行：

```powershell
.\start.ps1
```

首次运行会创建虚拟环境、安装依赖并迁移数据库，**不会自动填充演示数据**。启动器确认前后端就绪后才打开 <http://localhost:5173>；后端 API 文档在 <http://127.0.0.1:8000/api/docs>。后端与前端各占一个窗口，关闭窗口即可停止相应服务。若 `8000` 或 `5173` 端口被占用，启动器会报错；先关掉旧服务再运行。

macOS、Linux 或 WSL 可在项目根目录运行：

```bash
cp .env.example .env   # 首次使用；先设置 SESSION_SHARED_PASSWORD 和 SESSION_SECRET_KEY
make dev
```

配置入口是根目录的 `.env`。浏览器首次执行写操作时会弹出登录框，输入 `SESSION_SHARED_PASSWORD`。排产、审批、结构化 What-if 等核心流程不依赖模型；模型能力的真实演示需 `LLM_MODE=LIVE`，会调用配置的外部网关并产生用量。`STUB`/`REPLAY` 仅供离线测试或回放，页面会标明非 LIVE；LIVE 失败不会再以演示文本冒充成功。

排产按钮是确定性计算，不调用外部模型；若已有同一天的 `PENDING_APPROVAL` 计划，页面会显示该计划并引导到 Approval 审批或拒绝，不会重复创建。当前 LLM 接口走 `BEDROCK_GATEWAY_URL` 指定的 HTTP 网关（`BEDROCK_API_KEY` 为 Bearer 密钥），不是仅凭 AWS 账号就自动接入。需将 `.env` 设为 `LLM_MODE=LIVE` 并填好与网关匹配的地址、密钥和 `LLM_API_STYLE`，再重启后端；这只证明已配置，真实连通性还需单独验证。

本地真实数据模式（`APP_ENV=LOCAL`）在排程、审批、What-if、报价、风险与洞察等运行入口使用电脑当前时间。只有显式使用 `APP_ENV=DEMO` / 测试模式时，“今天”才固定为 **2026-03-02 08:00**。新的可上传演示工作簿使用 2026-09-26 起的日期；完整操作见 [DEMO.md](DEMO.md#录制前准备)。

## 功能地图

| 页面 / 入口 | 做什么 | 演示时看什么 |
| --- | --- | --- |
| Dashboard `/` | 汇总订单、物料、机器、工人和计划 | `CNC-01` 瓶颈、`MAT-STEEL-01` 缺口、订单备注的 `untrusted` 标记 |
| Schedule `/schedule` | 生成确定性排产提案 | 甘特图、不可排产原因与解锁建议、和先来先服务（FCFS）基线的对比 |
| Approval `/approval` | 审批、拒绝或修改提案 | 目标分项、输入版本、人工审批闸门；陈旧提案会被拒绝 |
| What-if `/whatif` | 五类结构化场景的只读推演；可选自然语言翻译 | 推演与当前 `ACTIVE` 的差值；“生成正式提案”后仍须审批 |
| Import & Mapping `/import` | 上传 CSV/XLSX、查看列映射、确认和整批回滚 | 原始列到目标字段的对应、归一化预览、批次来源 |
| Risks `/risks` | 风险雷达及重新扫描 | 严重度、受影响对象、模板或模型叙述来源 |
| Preferences `/preferences` | 人工创建、启用和查看软偏好；可选模型蒸馏候选 | 偏好有惩罚分，但不能突破硬约束；候选需人工启用 |
| Quote `/quote` | 新询单的可承诺交期推演 | 最早可行完成时间、对既有订单的影响；不写入正式计划 |
| Insights `/insights` | 瓶颈机器、产能和技能缺口 | `CNC-01` 的利用率及无替代能力；需要先有 `ACTIVE` 计划 |
| Value Ledger `/value` | KPI、节省步骤、执行分级和成本口径 | 当前值与基线值、实测与预测的区别；可导出 CSV |
| Trace Viewer `/traces` | 查看运行步骤、工具调用、模式和审计关联 | 为什么做出某个决策、是否用了模型、失败/降级是否留痕 |

另有计划对比视图 `/plans/{a}/compare/{b}`。当前前端没有直达按钮，需要拿两个真实计划 ID 填入网址。**扰动登记与重排、计划解释、计划导出、演示重置**已经有 API，但没有对应的完整前端操作入口；可在 API 文档或脚本中展示。不要把 API 能力讲成现有页面按钮。

### 一条最短的产品路径

1. Dashboard 看资源与订单现状。
2. Schedule 生成 `PENDING_APPROVAL` 提案，读甘特图、基线和不可排产原因。
3. Approval 审批成为 `ACTIVE`。系统允许 `PARTIAL` 计划，但会把未排产作业和原因摆出来；审批前仍会重新校验硬约束和输入版本。
4. What-if 模拟停机或缺料：先看影响，不修改生产数据；确有需要再生成新提案并审批。
5. Risks、Insights、Value Ledger、Traces 解释风险、瓶颈、业务价值及决策过程。

## 演示数据与 CSV 样例

完整演示工作簿：[`planning-demo-2026-09-26.xlsx`](outputs/01a0d936-fc4b-73e1-8ca9-459e1052b6eb/planning-demo-2026-09-26.xlsx)。它含 11 个数据工作表、89 行数据：6 种产品、14 个订单、10 种物料、5 台机器、8 名工人，并包含工序、物料清单、到货、停机、缺勤和换型规则。在空数据库的 Import 页面上方选择该 `.xlsx`，预览校验通过后点击 **Confirm complete workbook import**，然后到 Schedule 生成计划、在 Approval 审批。普通启动不会写入这批数据；排产只读取上传后落库的业务数据，不按演示 ID 偷填资源。逐表字段和操作顺序见 [导入说明](IMPORT_WORKBOOK.md)。

旧固定 seed 仍留作测试/显式重置使用，不能作为本地真实运行的依赖。业务中换成真实工作簿时，保持这 11 个 sheet 的列头与关联 ID 即可；不必修改排产代码。

可上传的样例在 [`backend/app/seed/samples/`](backend/app/seed/samples/README.md)：

| 样例 | 展示点 | 使用方式 |
| --- | --- | --- |
| `demo_material_shortage.csv` | 钢材和焊丝库存下降 | 导入后观察 Dashboard / Risks / 新计划；建议独立重置后演示 |
| `demo_material_restock.csv` | 钢材补货到 520 kg | 导入前后比较新计划的不可排产项；可演示整批回滚 |
| `demo_material_chinese_columns.csv` | 中文表头与额外 ERP 备注列 | 将原 CSV 与映射表并排看，展示备注列未成为目标字段，再看来源与回滚 |
| `demo_new_workers.csv` | 旧版工人导入示例 | 当前不支持完整班次/技能导入，不能用于真实排产 |
| `dirty_orders.csv` | 混合日期、空表头、不可解析数量 | 只演示映射和问题暴露；不要点“确认导入” |
| `malicious_orders.csv` | 不可信单元格与注入防护 | 用于对抗评估；录屏时可展示防护思路 |

**导入范围要讲准确：**新“完整工作簿”入口支持一次性落库所有排程基础配置与订单，且逐表校验类型、关联和工序顺序；现有“每日更新”入口仍只支持 `ORDER` 和 `MATERIAL` 的 LLM 列映射。两种入口都是预览不落库、确认才生成 Import batch。完整工作簿要求空工作区；旧批次或混合数据不会被悄悄覆盖。订单增量导入会检查产品是否已配置；不合法单元格需修改原文件后重传。物料导入会保留明确给出的单位，不会把 `kg` 当成 `pcs`。

## 设计思路和可证明的创新点

1. **决策可复现。**排产、硬约束、评分、FCFS 基线、沙箱和自主等级由确定性 Python 内核计算。相同快照与参数产生相同结果；业务指标与正式计划不以 LLM 的自由文本为权威。
2. **建议与执行分开。**模型可用于列映射、自然语言 What-if 翻译、解释、风险叙述和受限的重排编排；其输出经过结构化校验。场景先模拟，计划先待审，只有审批路径能激活正式计划。
3. **把“不行”解释清楚。**部分可行计划列出每项阻塞与解锁建议；方案与 FCFS 用同一口径比较；What-if 给出迟交与延期的变化量。
4. **安全边界能看到。**上传内容和订单备注被当作不可信数据，工具调用受白名单与预算限制；Trace 与审计记录保留决策路径。降级模式仍可运行确定性核心。
5. **价值可核对。**Value Ledger 区分实测累计与预测成本，记录人工步骤、决策分级和同口径基线，避免只给一段“AI 帮你优化”的叙述。

四层实现是 React/Vite 前端 → FastAPI 与会话认证 → 编排与 Agent / 工具闸门 → 纯 Python 排产内核及 SQLite 持久层。详细接口、状态机、数据结构与约束见 [design.md](.kiro/specs/production-planning-agent/design.md)。

## 验证与部署

POSIX/WSL：`make test` 跑后端基础测试与前端 Vitest，`make eval` 用 REPLAY 跑固定评估套件，`make lint` 跑 Ruff、mypy 和前端类型检查。Windows PowerShell 可分别在 `backend` 中运行 `.venv\Scripts\python -m pytest tests --ignore=tests/eval`，在 `frontend` 中运行 `npm.cmd run test`。评估和真实调用的成本边界见 [SETUP.md](SETUP.md) 与 [测试说明](backend/tests/eval/README.md)。

部署目标是单台 AWS Lightsail、Caddy、单 worker Uvicorn 与 SQLite WAL；脚本和前置条件见 [deploy/README.md](deploy/README.md)。本仓库的开发启动器用于本地演示，不是生产部署入口。
