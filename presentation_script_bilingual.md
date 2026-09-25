# AI Production Planning Assistant — 30-minute demo script

Use the English line for an international judging audience; the Chinese line is the matching rehearsal/reference version. Slides 7–11 contain video placeholders: replace each with the recorded screen capture, keep the narration live or record it as voice-over.

## Slide 1 — Opening (1:00)

**EN:** Production planning is full of changes, but high-impact changes should never be made by an opaque AI. Our AI Production Planning Assistant understands the request, simulates the consequence, and proposes an action that a planner can verify and approve.

**中：** 生产计划每天都在变化，但高影响决策不应该交给不可解释的 AI。我们的智能生产计划助手会理解请求、模拟影响，并提出可由计划员核验和批准的行动建议。

## Slide 2 — Problem (1:30)

**EN:** A planner has to respond to order changes, shortages, machine outages and due-date pressure at once. The difficult part is not only producing a schedule; it is preserving feasibility, traceability and accountability while changes happen.

**中：** 计划员必须同时处理订单变化、物料短缺、设备故障和交期压力。难点不只是排出计划，更是在变化发生时维持可行性、可追溯性和责任边界。

## Slide 3 — Goal and boundary (1:30)

**EN:** We designed a planning copilot, not an autonomous factory controller. It may understand, simulate and propose. It may not fabricate data, bypass constraints, or activate a material plan without human approval.

**中：** 我们设计的是计划协作助手，而不是自动化工厂控制器。它可以理解、模拟和提出建议；但不能编造数据、绕过约束，也不能未经人工批准就激活计划。

## Slide 4 — Connected architecture (2:30)

**EN:** This diagram is the key to the technical stack. The UI gathers intent. The API applies identity and business boundaries. The orchestrator manages the task. The LLM selects from controlled actions. The tool layer invokes deterministic planning services. Finally, governance records the proposal and routes it to approval. Each layer exists because the next layer needs a smaller, safer and more explicit input.

**中：** 这张图才是技术栈的核心。界面收集意图，API 施加身份和业务边界，编排层管理任务，LLM 从受控动作中选择，工具层调用确定性的计划服务，最后治理层记录提案并送入审批。每一层的存在，都是为了让下一层获得更小、更安全、更明确的输入。

## Slide 5 — Context and handoff (2:30)

**EN:** The agents do not rely on an unlimited conversation transcript. A Context Manager rebuilds a task-focused context for every model call from the current business state, the active task, recent verified tool results and a short summary of history. Agent-to-agent handoffs use schema-validated contracts. In plain language: the next agent receives the facts and decision status it needs, rather than guessing from chat text.

**中：** 智能体并不依赖无限增长的对话记录。每次调用模型前，Context Manager 会用当前业务状态、正在处理的任务、最近已验证的工具结果和简短历史摘要，重新构建与任务相关的上下文。智能体之间通过经过 Schema 校验的契约交接。通俗地说，下一个智能体拿到的是完成工作所需的事实和决策状态，而不是从聊天文本里猜。

## Slide 6 — LLM versus deterministic core (2:00)

**EN:** The LLM is not the scheduling engine. It translates natural language, explains trade-offs and chooses the next approved tool. The deterministic core calculates constraints, schedules, risks and impact. This separation gives us natural interaction without turning probability into operational truth.

**中：** LLM 不是排产引擎。它负责理解自然语言、解释权衡，并选择下一项获准调用的工具。确定性核心计算约束、排程、风险和影响。这种分工既保留了自然交互，又不会把概率性的输出当成生产事实。

## Slide 7 — Video 1: governed ingestion (2:00)

**ACTION:** Play the import-and-mapping recording. Pause after the mapping proposal.

**EN:** We start with an incoming material update. The system proposes a mapping, but the user confirms it before a reversible batch is created. This is the first trust boundary: source data is made explicit before it can affect a plan.

**中：** 我们从一份新到的物料更新开始。系统提出字段映射建议，但用户确认后才会创建可回滚的导入批次。这是第一道可信边界：源数据必须先被显式确认，才能影响计划。

## Slide 8 — Video 2: constrained schedule (2:30)

**ACTION:** Play dashboard and schedule generation. Point to CNC-01, shortages, the Gantt chart and unblock conditions.

**EN:** The schedule is generated against real constraints, rather than being composed in natural language. We can show why an order is blocked, which resource is the bottleneck and what condition would unblock the plan.

**中：** 计划是在真实约束下生成的，而不是用自然语言拼出来的。我们能够展示订单为何被阻塞、哪个资源是瓶颈，以及满足什么条件才能解除阻塞。

## Slide 9 — Video 3: approval (1:30)

**ACTION:** Play the approval transition: PENDING_APPROVAL to APPROVE, MODIFY or REJECT.

**EN:** A proposal is not a plan until it is approved. The state transition makes decision authority visible: the agent can prepare evidence and recommendations, while the planner owns the final activation.

**中：** 提案在获批前不是正式计划。这个状态流转清晰呈现决策权：智能体准备证据和建议，而计划员拥有最终激活权。

## Slide 10 — Video 4: risk radar (1:30)

**ACTION:** Play risk scan; show bottleneck, material runout, zero-slack order and capacity risk.

**EN:** Risk monitoring turns hidden fragility into a prioritized list. It connects operational signals to planning decisions, so the planner can investigate the highest-impact issue first.

**中：** 风险监控把隐藏的脆弱点转化为按优先级排列的问题列表。它把运营信号连接到计划决策，让计划员优先处理影响最大的事项。

## Slide 11 — Video 5: what-if proposal (3:30)

**ACTION:** Enter “CNC-01 will be down for six hours tomorrow morning”; show translation, confirmation, sandbox simulation and proposal comparison.

**EN:** This is the most agentic moment. The LLM translates the request into a structured scenario, asks for confirmation when needed, then calls a sandbox simulation. The result is a formal proposal with impacts and alternatives—not an uncontrolled change to production.

**中：** 这是最能体现智能体能力的时刻。LLM 将请求翻译为结构化场景；需要时先请求确认，然后调用沙盒模拟。结果是带影响和备选方案的正式提案，而不是对生产计划进行不可控的直接修改。

## Slide 12 — Traceability (1:30)

**ACTION:** Optionally play Trace Viewer for 45 seconds; otherwise point to the lifecycle.

**EN:** For every run, we retain the task, assembled context, tool calls, verified results, proposal and approval outcome. This trace turns an AI interaction into an auditable operational record.

**中：** 每次运行都会保留任务、组装后的上下文、工具调用、已验证结果、提案和审批结论。这条追踪记录把一次 AI 交互变成可审计的运营记录。

## Slide 13 — Trust evidence (2:00)

**EN:** Trust is an architecture property, not a claim. Verified inputs, deterministic calculation, sandbox simulation, approval gates, versioning and traces work together. If one layer fails or is uncertain, it cannot silently become an operational decision.

**中：** 可信不是一句口号，而是一种架构属性。已验证输入、确定性计算、沙盒模拟、审批关口、版本化和追踪记录共同发挥作用。如果某一层失败或存在不确定性，它不会悄悄变成一项生产决策。

## Slide 14 — Value and close (2:00)

**EN:** The immediate value is faster replanning with a safer decision process. The longer path is a governed planning copilot integrated with ERP and MES, then scaled to multi-site planning. Our core message is simple: make the agent useful where change is frequent, and trustworthy where consequences are real.

**中：** 直接价值是更快地重排计划，同时拥有更安全的决策流程。更长远的路径是与 ERP 和 MES 集成的、受治理的计划协作助手，并逐步扩展到多工厂计划。我们的核心信息很简单：在变化频繁的地方让智能体有用，在后果真实的地方让它可信。

## Recording rhythm

**EN:** Keep Slides 1–6 at about 11 minutes, Slides 7–12 at about 12 minutes including videos and deliberate pauses, and Slides 13–14 plus Q&A buffer at about 7 minutes.

**中：** 建议第 1–6 页约 11 分钟；第 7–12 页连同视频和停顿约 12 分钟；第 13–14 页以及问答缓冲约 7 分钟。
