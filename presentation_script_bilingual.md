# AI 生产计划助手｜30 分钟汇报与录屏脚本

**配套 PPT：** `hackathon_showcase_storyboard_cn.pptx`  
**主叙事：** 车间发生 `CNC-01` 停机事件后，系统如何把自然语言描述转化为经计算、审批和追踪的计划决策。  
**录制原则：** 前半段用业务问题引出技术架构；后半段只沿同一事件推进。技术名词只在对应画面出现时解释。

## 总体节奏

| 阶段 | 幻灯片 | 建议时长 | 目标 |
| --- | --- | ---: | --- |
| 背景、需求与目标 | 01–04 | 6 分钟 | 说明真实问题和产品边界 |
| 架构与运行逻辑 | 05–08 | 10 分钟 | 讲清数据、上下文、Agent、工具和控制点 |
| 车间突发事件主线演示 | 09–14 | 11 分钟 | 用一次停机串起系统能力 |
| 技术回扣、价值和收束 | 15–17 | 3 分钟 | 把演示证据连接到落地价值 |

---

## 01｜封面｜0:45

**翻页与动画：** 标题、主张、赛事名称依次淡入。停留两秒后开始。

**中：** 今天我们展示的项目是 AI 生产计划助手。它面向制造车间中高频发生的变化：订单变化、缺料、设备故障和交期压力。我们的目标是让计划员能够更快理解影响、运行推演，并基于可验证的证据完成计划决策。

**EN:** Today we present the AI Production Planning Assistant. It supports manufacturing planners facing frequent changes such as order changes, shortages, machine failures and delivery pressure. The system helps planners understand impact, run simulations and make decisions from verifiable evidence.

## 02｜项目背景：一次车间突发事件｜1:45

**画面：** 停机灯和右侧车间图片。指向左侧三张卡片。

**中：** 我们从一个具体事件开始。2026 年 3 月 3 日早上八点，关键设备 CNC-01 停机六小时。计划员要立刻回答三个问题：哪些订单受到影响，是否存在可行的调整方案，正式计划应当由谁、依据什么来修改。这三类问题同时涉及事实、计算和决策权限。

**EN:** We start with a concrete event. At 8 AM on March 3, 2026, the critical machine CNC-01 is unavailable for six hours. The planner must immediately answer three questions: which orders are affected, whether a feasible adjustment exists, and who may change the formal plan based on what evidence.

## 03｜项目需求｜1:45

**动画：** 四张卡片按从左至右顺序出现。

**中：** 这次停机让项目需求变得非常明确。系统需要看清订单、物料、机器和工人的当前状态；需要在硬约束下计算可行方案；需要把推演与正式计划隔离；还需要记录完整的决策证据。速度很重要，每一步的解释、验证和审计同样重要。

**EN:** This incident makes the requirements clear. The system needs a reliable view of orders, materials, machines and workers. It needs to compute feasible options under hard constraints, separate simulations from formal plans, and retain decision evidence. Speed, explanation, validation and auditability all matter.

## 04｜项目目标：一条受控的处理流程｜1:30

**动画：** 五个节点随讲解依次出现。

**中：** 因此我们把用户体验设计成一条明确的流程：识别变化，确认结构化场景，运行沙盒推演，生成提案并审批，最后追踪和复盘。计划员始终能看到当前状态和下一步权限。系统在每个节点给出建议和证据，正式激活计划由计划员完成。

**EN:** We designed a clear workflow: identify the change, confirm a structured scenario, run a sandbox simulation, generate and approve a proposal, then trace and review the result. The planner can see the current state and authority at every step. The system supplies recommendations and evidence; the planner activates the formal plan.

## 05｜总体架构：数据流与控制流｜3:00

**画面讲法：** 先沿上方青色箭头解释数据流，再沿下方琥珀色箭头解释控制流。

**中：** 这张图说明技术如何服务于业务链路。订单、物料、机器和工人进入 React 界面和 FastAPI 服务。编排层根据 Intent 路由任务，创建 Trace 并管理执行过程。Agent 调用 LLM 理解语言和选择受允许的工具。工具注册表对每次调用执行白名单、输入 Schema、输出 Schema、投影、截断和记账。确定性排产核心完成约束校验、排程、风险和模拟。最后，计划治理层将提案置于待审批状态，由计划员决定是否激活。

**EN:** This diagram shows how the technology serves the business chain. Orders, materials, machines and workers enter the React UI and FastAPI service. The orchestrator routes each intent, creates a trace and manages execution. Agents use the LLM to interpret language and choose permitted tools. The Tool Registry applies the whitelist, input schema, output schema, projection, truncation and accounting to every call. The deterministic core calculates constraints, schedules, risks and simulations. Governance places a proposal into approval for the planner to activate.

## 06｜上下文如何保持连续｜2:30

**画面讲法：** 从左侧三类输入，指向 Context Manager，再指向一次 LLM 请求。

**中：** 多个模型调用保持连续，依赖的是显式运行状态和工具观察记录。`SessionState` 保存当前激活计划、待审批计划、最近扰动、已启用偏好和降级状态。更早的工具结果压缩成一行摘要，最近两条结果保留完整结构化内容。Context Manager 以固定顺序组合运行状态、历史摘要、最近观察和任务块，形成一次模型请求。外部文件等不可信内容会包裹在 `untrusted` 标记中，模型将其当作数据处理。这里可以提到：RAG 通常用于检索文档，本项目的主链路采用运行状态和工具观察来维持上下文。

**EN:** Continuity across model calls comes from explicit runtime state and tool observations. `SessionState` holds the active plan, pending plan, latest disruption, enabled preferences and degraded mode. Earlier tool results become one-line summaries, while the latest two retain their full structured content. The Context Manager assembles runtime state, history, recent observations and the task block in a fixed order. Untrusted external content is wrapped with an `untrusted` marker and treated as data. RAG is commonly used to retrieve documents; this project’s main path maintains context through runtime state and tool observations.

## 07｜三个 Agent 的职责和交接｜2:00

**动画：** 三张 Agent 卡片依次出现，最后出现 Handoff Contract。

**中：** Agent 的划分来自输入可信度和权限边界。Ingestion Agent 处理外部表格，只能预览、映射、校验和创建导入批次，映射提案先由人确认。Planning Agent 读取业务事实、调用计算工具、形成重排提案。Risk Monitor Agent 读取事实、触发风险扫描、生成风险归因叙述。每个 Agent 的输出由 Pydantic 契约校验，声明字段之外的内容会被拒绝；自由文本在跨边界时标记为不可信。工具权限由 Tool Registry 的不可变白名单在调用入口执行。

**EN:** Agent boundaries follow input trust and authority. The Ingestion Agent previews, maps and validates external files before creating an import batch. The Planning Agent reads business facts, calls computation tools and prepares replanning proposals. The Risk Monitor Agent reads facts, triggers risk scans and writes risk narratives. Pydantic contracts validate each output. Undeclared fields are rejected, while free text is marked untrusted across boundaries. The Tool Registry enforces each agent’s immutable tool whitelist at the invocation point.

## 08｜LLM API 如何参与决策｜2:15

**画面讲法：** 沿横向闭环讲解，最后停在底部两行职责划分。

**中：** 每次模型调用都在受限 ReAct 循环中运行。输入进入模型后，模型只能输出一个 JSON 动作，例如调用某个工具及其参数，或返回一个符合契约的最终结果。Tool Registry 通过七道闸门验证调用，然后将确定性工具返回的结构化事实送回上下文。下一轮模型调用基于这些最新事实选择下一步。LLM 负责自然语言翻译、解释、工具选择和受限格式输出；确定性服务负责数值、约束检查和状态变迁。

**EN:** Each model call runs inside a bounded ReAct loop. The model can only output a JSON action, such as a permitted tool and arguments, or a final result that matches a contract. The Tool Registry validates the call through seven gates. The structured facts from deterministic tools return to context, and the next model turn selects the next step from those facts. The LLM handles language translation, explanation, tool selection and constrained output; deterministic services handle numbers, constraints and state transitions.

## 09｜演示主线预告｜0:45

**中：** 现在进入演示。我们始终围绕同一个事件：CNC-01 明天上午停机六小时。录像将按这五步推进：先建立已审批的基线计划，再输入停机、确认结构化场景、运行沙盒模拟，最后生成提案并审批。每一步都能对应到刚刚介绍的一层技术。

**EN:** We now enter the demo. We will keep one event throughout: CNC-01 is unavailable for six hours tomorrow morning. We will establish an approved baseline, enter the outage, confirm the structured scenario, run a sandbox simulation, then create and approve a proposal. Each step maps to a technical layer we have just introduced.

## 10｜演示步骤 1：建立可信基线｜3:00

**录屏操作：**

1. 可选前置镜头，45 秒：在 Import & Mapping 上传 `demo_material_chinese_columns.csv`，展示映射建议和 Confirm and import。强调该批次可回滚、带来源。
2. 打开 Dashboard，扫过订单、物料、机器、工人；停在 `CNC-01`、`MAT-STEEL-01` 和订单备注。
3. 打开 Schedule，点击 **Generate today’s plan**。
4. 展示甘特图、`PARTIAL` 或不可排产作业、`blocking_reason`、`unblock_suggestion` 和 FCFS 对比。
5. 打开 Approval，批准该计划，使状态变为 `ACTIVE`。

**中：** 我们先得到一个正式的基线。排程内核从同一个生产快照计算计划，将每个作业明确归入已排产或不可排产，并为不可排产作业给出原因和解除条件。通过审批后，这份计划成为 ACTIVE。后续推演会始终与这个基线比较。

**EN:** We first establish the formal baseline. The scheduling core calculates from one production snapshot, classifies every job as scheduled or unschedulable, and gives a reason and unblock condition for each unschedulable job. After approval, the plan becomes ACTIVE. Every later simulation compares against this baseline.

## 11｜演示步骤 2：用自然语言描述停机｜2:00

**录屏操作：**

1. 打开 What-if。
2. 在自然语言入口输入：`CNC-01 明天上午停机 6 小时`。
3. 在 LIVE 模式下展示翻译结果；将 `machine_id`、开始时间和结束时间逐项与画面确认。
4. 点击确认。模型不可用时展示系统的降级提示，然后使用结构化表单完成同一场景。

**中：** 这一步展示 LLM 的具体价值。它把计划员的口语描述转换为结构化场景变更。模型输出只表达候选场景，计划员在执行前确认设备和时间窗口。系统没有在此刻修改生产计划。

**EN:** This step shows the specific value of the LLM. It turns the planner’s natural-language description into a structured scenario change. The model outputs a candidate scenario, and the planner confirms the machine and time window before execution. The formal production plan remains unchanged at this point.

## 12｜演示步骤 3：在沙盒计算影响｜2:00

**录屏操作：**

1. 点击 **Run simulation**。
2. 展示相对于 ACTIVE 基线的延迟、不可排产或目标差值。
3. 返回 Dashboard 或计划状态区域，确认原 ACTIVE 计划仍然存在。

**中：** 确认场景后，确定性核心运行 `run_scenario`、评估和计划比较。沙盒把停机窗口临时加入模拟输入，输出可量化影响。画面上最重要的证据是：我们看到了差异，同时正式 ACTIVE 计划保持原样。

**EN:** After confirmation, the deterministic core runs `run_scenario`, evaluation and plan comparison. The sandbox adds the outage window only to simulated inputs and returns quantified impact. The key evidence is the visible difference while the formal ACTIVE plan remains unchanged.

## 13｜演示步骤 4：从模拟到提案与审批｜2:00

**录屏操作：**

1. 点击 **Generate formal proposal from this scenario**。
2. 打开 Approval，展示目标分项、输入版本和 `PENDING_APPROVAL` 状态。
3. 选择 **Approve**、**Modify** 或 **Reject** 中的实际决策；演示批准后新计划成为 ACTIVE。

**中：** 沙盒结果为提案提供证据。保存提案后，状态机进入 PENDING_APPROVAL。审批动作再次核验约束和输入版本，计划员再决定是否让新计划生效。这个控制点把计算结果连接到真实运营决策。

**EN:** Sandbox results provide evidence for a proposal. Saving the proposal moves the state machine to PENDING_APPROVAL. Approval validates constraints and input versions again, then the planner decides whether the new plan becomes active. This control point connects computation to an operational decision.

## 14｜演示步骤 5：风险、追踪与复盘｜2:00

**录屏操作：**

1. 打开 Risks，点击 **Rescan**，展示风险严重程度、受影响对象和建议。
2. 打开 Trace Viewer，找到本次运行的 `trace_id`。
3. 展示任务、工具调用、关键结果、提案和审批结论。
4. 可打开 Value Ledger，展示项目记录哪些量化指标；只引用实际页面已经出现的数值。

**中：** 在最后一步，我们将风险和整个运行轨迹放在一起看。同一个 trace_id 可以连接任务、上下文、工具调用、结果、提案和审批结论。这样团队能够复盘系统当时看到了什么、计算了什么，以及谁做出了最终决定。

**EN:** In the final step, we view risk and the complete run together. The same trace ID connects the task, context, tool calls, results, proposal and approval outcome. The team can review what the system observed, what it computed and who made the final decision.

## 15｜从演示回看技术证据｜1:00

**中：** 现在回扣技术链路。基线排程对应确定性 Scheduler 和约束检查；自然语言停机对应 Planning Agent 和 Schema 契约；沙盒推演对应 `run_scenario` 与 `compare_plans`；审批和 Trace 对应状态机、审计和工具记账。编排层在整条路径中统一路由、创建 Trace 并管理 ReAct 回合。

**EN:** We can now map the demo back to the implementation. Baseline scheduling maps to the deterministic Scheduler and constraint checks. The natural-language outage maps to the Planning Agent and schema contract. Sandbox simulation maps to `run_scenario` and `compare_plans`. Approval and trace map to the state machine, audit and tool accounting. The orchestrator routes the full path, creates the trace and manages ReAct turns.

## 16｜落地价值｜1:00

**中：** 落地后，运营团队能更快定位可排和不可排作业，并在正式改动前看见影响。治理团队获得输入来源、版本、审批和调用日志组成的证据链。商业上，这套能力可以接入 ERP 和 MES，进一步扩展到报价承诺、风险治理和跨工厂协同。

**EN:** In deployment, operations teams can identify schedulable and unschedulable work faster and view impact before a formal change. Governance teams receive an evidence chain covering input source, version, approval and tool calls. Commercially, this capability can connect with ERP and MES and extend to quoting, risk governance and multi-site coordination.

## 17｜收束｜0:30

**中：** 这次展示的核心是一条完整的决策链：从变化发生、事实确认、可复现计算，到人工批准和可审计记录。谢谢。

**EN:** The core of this demonstration is a complete decision chain: from a change event, through fact confirmation and reproducible computation, to human approval and an auditable record. Thank you.

## 录制检查清单

- 演示前调用 `POST /api/demo/reset`，获得干净的固定种子数据。
- 在本地健康检查中确认 `LLM_MODE=LIVE`；若使用降级路径，画面和配音均明确说明。
- 录屏前记下本次生成的 `plan_id`、`trace_id` 与实际差值；不要在字幕中预写会变化的数值。
- 出现 `PENDING_PLAN_EXISTS` 时，先到 Approval 处理当前待审批提案，再继续生成下一份计划。
- 所有价值数字以录制时界面出现的实际数值为准。
