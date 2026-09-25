# AI 生产计划助手｜30 分钟中英双语汇报配音稿

对应文件：`hackathon_demo_deck_cn.pptx`。中文为主讲版本，英文用于国际评委场景。整套采用淡入转场；录屏页按右侧提示讲解，操作时避免念大段文字。

## 01 封面｜1:00

**动画：** 标题、主张、赛事名称依次淡入。

**中：** 生产现场的变化从来不会等待计划员准备好。订单插单、物料短缺和设备停机，都可能在几分钟内让原计划失效。我们的 AI 生产计划助手不直接替人做决定，而是理解变化、模拟影响，并给出计划员能够核验和批准的行动建议。

**EN:** Change never waits for a planner to be ready. Rush orders, material shortages and machine downtime can invalidate a plan within minutes. Our assistant understands the change, simulates the impact and produces an action a planner can verify and approve.

## 02 业务问题｜1:40

**动画：** 四个挑战词依次出现，每出现一个配一个真实场景。

**中：** 生产计划的难点不只是排出一张甘特图。一个订单变化会同时挤压设备产能、物料可用性和交期承诺。计划员需要快速判断，但每次修改又都必须保留可行性、可解释性和责任边界。

**EN:** Planning is more than generating a Gantt chart. One order change affects capacity, material availability and delivery commitments at the same time. Planners need speed, but every change must remain feasible, explainable and accountable.

## 03 项目目标与边界｜1:40

**动画：** 先出现“理解、模拟、建议”，再出现红线边界。

**中：** 我们把产品定位为计划协作助手。它可以理解需求、运行模拟、提出建议；它不能编造业务事实，不能绕开生产约束，也不能跳过人工审批直接激活计划。这样的边界让 AI 能够安全地进入真实运营场景。

**EN:** We designed a planning copilot. It can understand requests, run simulations and propose actions. It cannot invent facts, bypass production constraints or activate a plan without approval. These boundaries make AI safe for real operations.

## 04 从输入到决策的分层架构｜2:30

**动画：** 背景架构图先出现，再按从左到右顺序讲解，讲到审批治理时停顿。

**中：** 这不是技术清单，而是一条受控的决策链。界面收集意图，API 负责身份和业务边界，编排层管理任务，LLM 从被允许的工具中选择下一步，确定性核心负责计算，最后治理层将提案送入审批。每一层都把模糊输入变得更明确、更安全，才交给下一层。

**EN:** This is not a technology list. It is a controlled decision chain. The UI captures intent. The API enforces boundaries. Orchestration manages the task. The LLM selects a permitted tool. The deterministic core computes the result. Governance sends the proposal to approval.

## 05 上下文与智能体交接｜2:30

**动画：** 四类上下文信息逐项出现，最后显示 Context Manager。

**中：** 多个智能体保持上下文，并不是不断把全部聊天记录塞进模型。每次调用前，系统基于业务状态、当前任务、最近验证的工具结果和压缩后的历史摘要，重新组织任务上下文。智能体之间交接的是经过 Schema 校验的结构化状态，而不是含糊的自然语言猜测。

**EN:** Agents do not maintain context by repeatedly sending an unlimited chat transcript to the model. Before each call, the system rebuilds context from business state, active task, verified tool results and a compact history summary. Agents hand over schema-validated structured state.

## 06 LLM 的职责与边界｜2:00

**动画：** 左右两侧分别淡入，最后出现“分工协作”。

**中：** LLM 负责理解自然语言、解释权衡、选择下一项获准工具。排程、约束检查、风险识别和影响计算由确定性核心完成。我们不把概率性的语言输出当作生产事实，而是让模型调用能够复现、能够校验的计算工具。

**EN:** The LLM interprets language, explains trade-offs and selects the next approved tool. The deterministic core handles scheduling, constraints, risks and impact. We do not treat probabilistic language output as operational truth.

## 07 录屏一：受治理的数据导入｜2:00

**操作：** 播放导入与字段映射录屏。停在映射建议和确认按钮各一秒。

**中：** 系统先提出字段映射建议，计划员确认后才创建导入批次。这个批次可追溯、可回滚，因此原始表格不会未经确认就影响生产计划。

**EN:** The system proposes a field mapping. The planner confirms it before an import batch is created. The batch is traceable and reversible, so raw data cannot silently change the plan.

## 08 录屏二：约束下生成计划｜2:30

**操作：** 播放仪表盘与排程录屏，明确指向 CNC-01、物料约束、甘特图和阻塞原因。

**中：** 这里生成的是受真实约束检验的计划。我们可以追问某个订单为何被阻塞、哪个资源是瓶颈，以及需要满足什么条件才能解除阻塞。计划员获得的是依据，而不仅仅是答案。

**EN:** The system generates a plan checked against real constraints. We can ask why an order is blocked, which resource is the bottleneck and what would unblock it. The planner receives evidence, not only an answer.

## 09 录屏三：审批与责任｜1:30

**操作：** 播放从 PENDING_APPROVAL 到 APPROVE、MODIFY、REJECT 的状态转移。

**中：** 提案和正式计划之间有清晰的状态边界。智能体可以收集证据并准备建议，但计划员必须明确选择批准、修改或拒绝。运营影响发生之前，决策人和决策依据都保持可见。

**EN:** A proposal and an active plan have a clear state boundary. The agent prepares evidence and recommendations, while the planner explicitly approves, modifies or rejects it.

## 10 录屏四：风险雷达｜1:30

**操作：** 播放风险扫描，展示瓶颈、物料耗尽、零缓冲订单和产能风险。

**中：** 风险雷达把隐藏在计划中的脆弱点转为按优先级排列的待处理事项。它不替计划员下结论，而是帮助计划员从影响最大的风险开始调查和行动。

**EN:** The risk radar turns hidden plan fragility into prioritized issues. It helps the planner investigate and act on the highest-impact risk first.

## 11 录屏五：What-if 情景推演｜3:30

**动画与操作：** 输入“CNC-01 明天上午停机 6 小时”，播放翻译、确认、沙盒模拟和方案对比。

**中：** 这是智能体能力最集中的一段。计划员用自然语言描述设备故障，LLM 将其转为结构化场景；信息不足时，系统请求确认；随后确定性核心在沙盒中模拟影响。最终呈现的是带替代方案和影响说明的正式提案，而不是对生产环境的直接修改。

**EN:** This is the most agentic part. A planner describes a machine outage in natural language. The LLM translates it into a structured scenario and requests confirmation when needed. The deterministic core simulates the impact in a sandbox. The result is a formal proposal, not an uncontrolled production change.

## 12 追踪与审计｜1:30

**操作：** 可播放 Trace Viewer 45 秒；没有录屏时停留在结论。

**中：** 每次运行都保留任务、组装后的上下文、工具调用、验证结果、提案和审批结论。这条追踪记录回答“系统为什么这样建议”，也让一次 AI 对话成为可审计的运营记录。

**EN:** Every run retains the task, assembled context, tool calls, verified results, proposal and approval outcome. This trace answers why the system made a recommendation.

## 13 可信性证据｜2:00

**动画：** 六道防线逐项淡入。

**中：** 可信来自架构。输入验证避免错误数据进入系统；确定性计算保证核心结果可复现；沙盒隔离变化；审批阻止越权；版本和审计记录保证可回看。某一层不确定时，它不会悄悄变成生产决策。

**EN:** Trust comes from architecture. Input validation protects data quality. Deterministic computation makes results reproducible. Sandboxes isolate changes. Approval prevents overreach. Versioning and audit preserve accountability.

## 14 价值与下一步｜2:00

**动画：** 先显示当下价值，再出现 ERP／MES 与多工厂路径，最后停留在收束句。

**中：** 即时价值是缩短计划重排时间，同时提升决策的可解释性与可治理性。下一步，系统可以连接 ERP 和 MES，并扩展到跨产线、跨工厂的场景。变化频繁的地方，智能体要足够有用；后果真实的地方，智能体必须值得信任。谢谢。

**EN:** The immediate value is faster replanning with clearer, more governable decisions. Next, the system can connect with ERP and MES and scale to multi-site planning. Where change is frequent, agents must be useful. Where consequences are real, agents must be trustworthy. Thank you.

## 录制节奏

第 1–6 页约 11 分钟；第 7–12 页连同录屏与停顿约 12 分钟；第 13–14 页约 4 分钟；余下 3 分钟留给切换、停顿和问答缓冲。
