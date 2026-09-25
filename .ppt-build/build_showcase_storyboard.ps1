param(
  [string]$Source = "D:\Hackathon\hackathon\hackathon_demo_deck.pptx",
  [string]$Output = "D:\Hackathon\hackathon\hackathon_showcase_storyboard_cn.pptx"
)

$ErrorActionPreference = 'Stop'
if(Test-Path Alias:H){Remove-Item Alias:H -Force}
$Build = Join-Path $PSScriptRoot 'story-work'
if(Test-Path $Build){Remove-Item $Build -Recurse -Force}
New-Item -ItemType Directory $Build | Out-Null
$Zip = Join-Path $PSScriptRoot 'story-source.zip'
Copy-Item $Source $Zip -Force
Expand-Archive $Zip $Build -Force
Remove-Item $Zip -Force

function E($s){ [Security.SecurityElement]::Escape([string]$s) }
function T($id,$s,$x,$y,$w,$h,$size=1800,$color='FFFFFF',$bold='0',$align='l'){
  '<p:sp><p:nvSpPr><p:cNvPr id="{0}" name="text{0}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{2}" y="{3}"/><a:ext cx="{4}" cy="{5}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr><p:txBody><a:bodyPr wrap="square"/><a:lstStyle/><a:p><a:pPr algn="{9}"/><a:r><a:rPr sz="{6}" b="{8}"><a:solidFill><a:srgbClr val="{7}"/></a:solidFill><a:latin typeface="Aptos"/><a:ea typeface="Microsoft YaHei"/></a:rPr><a:t>{1}</a:t></a:r></a:p></p:txBody></p:sp>' -f $id,(E $s),$x,$y,$w,$h,$size,$color,$bold,$align
}
function R($id,$x,$y,$w,$h,$fill='183A57',$radius='roundRect'){
  '<p:sp><p:nvSpPr><p:cNvPr id="{0}" name="shape{0}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{1}" y="{2}"/><a:ext cx="{3}" cy="{4}"/></a:xfrm><a:prstGeom prst="{6}"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="{5}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>' -f $id,$x,$y,$w,$h,$fill,$radius
}
function AH($id,$x,$y,$w,$color='42C7B5'){
  '<p:sp><p:nvSpPr><p:cNvPr id="{0}" name="arrow{0}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{1}" y="{2}"/><a:ext cx="{3}" cy="1"/></a:xfrm><a:prstGeom prst="line"><a:avLst/></a:prstGeom><a:ln w="18000"><a:solidFill><a:srgbClr val="{4}"/></a:solidFill><a:tailEnd type="triangle"/></a:ln></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>' -f $id,$x,$y,$w,$color
}
function AV($id,$x,$y,$h,$color='42C7B5'){
  '<p:sp><p:nvSpPr><p:cNvPr id="{0}" name="arrow{0}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm rot="5400000"><a:off x="{1}" y="{2}"/><a:ext cx="{3}" cy="1"/></a:xfrm><a:prstGeom prst="line"><a:avLst/></a:prstGeom><a:ln w="18000"><a:solidFill><a:srgbClr val="{4}"/></a:solidFill><a:tailEnd type="triangle"/></a:ln></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>' -f $id,$x,$y,$h,$color
}
function P($id,$rid,$x,$y,$w,$h){
  '<p:pic><p:nvPicPr><p:cNvPr id="{0}" name="image{0}"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="{1}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr><a:xfrm><a:off x="{2}" y="{3}"/><a:ext cx="{4}" cy="{5}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>' -f $id,$rid,$x,$y,$w,$h
}
function Card($id,$title,$body,$x,$y,$w,$h,$accent='42C7B5'){
  if($title -is [array]){$pair=$title;$accent=[string]$h;$h=$w;$w=$y;$y=$x;$x=$body;$body=[string]$pair[1];$title=[string]$pair[0]}
  elseif($body -is [int] -or $body -is [long]){$accent=[string]$h;$h=$w;$w=$y;$y=$x;$x=$body;$body=''}
  (R $id $x $y $w $h '183A57')+(R ($id+1) $x $y 65000 $h $accent 'rect')+(T ($id+2) $title ($x+180000) ($y+150000) ($w-320000) 260000 1700 'FFFFFF' '1')+(T ($id+3) $body ($x+180000) ($y+520000) ($w-340000) ($h-600000) 1250 'B8CAD7')
}
function Node($id,$title,$body,$x,$y,$w,$h,$fill='183A57'){
  if($title -is [array]){$pair=$title;$fill=[string]$h;$h=$w;$w=$y;$y=$x;$x=$body;$body=[string]$pair[1];$title=[string]$pair[0]}
  (R $id $x $y $w $h $fill)+(T ($id+1) $title ($x+130000) ($y+130000) ($w-260000) 230000 1450 'FFFFFF' '1' 'ctr')+(T ($id+2) $body ($x+130000) ($y+440000) ($w-260000) ($h-500000) 1050 'B8CAD7' '0' 'ctr')
}
function Header($title,$sub,$n){
  (T 3 $title 620000 350000 10800000 520000 2800 'FFFFFF' '1')+(T 4 $sub 620000 950000 10500000 280000 1350 '8FA7B8')+(T 5 ('NUS-ISS SHOW ME YOUR AGENT  |  '+$n.ToString('00')) 620000 6420000 4200000 120000 850 '7891A4')+(R 6 620000 6000000 1800000 30000 '42C7B5' 'rect')
}
Set-Alias -Name H -Value Header -Option AllScope -Force
function S($body){
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/><p:sp><p:nvSpPr><p:cNvPr id="2" name="background"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="12192000" cy="6858000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="10243A"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>{0}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr><p:transition spd="med" advClick="1"><p:fade/></p:transition></p:sld>' -f $body
}
function Rect { & (Get-Command R -CommandType Function) @args }
function Text { & (Get-Command T -CommandType Function) @args }
function Picture { & (Get-Command P -CommandType Function) @args }
function SlideXml { & (Get-Command S -CommandType Function) @args }
Set-Alias -Name R -Value Rect -Option AllScope -Force
Set-Alias -Name T -Value Text -Option AllScope -Force
Set-Alias -Name P -Value Picture -Option AllScope -Force
Set-Alias -Name S -Value SlideXml -Option AllScope -Force

$slides = @()

# 01 Cover
$b=(P 50 'rId2' 0 0 12192000 6858000)+(R 51 0 0 5700000 6858000 '10243A' 'rect')+(T 52 'AI 生产计划助手' 620000 1670000 4700000 720000 3500 'FFFFFF' '1')+(T 53 '面对车间突发变化的可信决策智能体' 650000 2560000 4300000 330000 1850 'C4D9E7')+(R 54 650000 3260000 3700000 680000 '17344E')+(T 55 '理解变化  ·  推演影响  ·  生成可审计提案' 860000 3450000 3300000 240000 1450 'FFFFFF' '1' 'ctr')+(T 56 'NUS-ISS  |  Show Me Your Agent' 650000 4300000 3500000 200000 1200 'FFC857' '1')
$slides += S $b

# 02 Background
$b=(P 50 'rId2' 0 0 12192000 6858000)+(R 51 0 0 5500000 6858000 '10243A' 'rect')+(T 52 '从一次真实的车间突发事件开始' 650000 670000 4300000 600000 2750 'FFFFFF' '1')+(T 53 '2026-03-03 08:00：CNC-01 停机，预计持续 6 小时' 650000 1470000 4000000 260000 1500 'FFC857' '1')+(Card 60 '订单承诺','哪些订单会延迟？哪些还能按时完成？' 650000 2150000 3850000 800000 '42C7B5')+(Card 70 '资源约束','CNC-01 是深孔钻削能力的关键资源。' 650000 3100000 3850000 800000 'FFB547')+(Card 80 '决策压力','计划员需要在有限时间内提出可执行的调整方案。' 650000 4050000 3850000 800000 '8E7CFF')
$slides += S $b

# 03 Requirements
$b=(Header '项目要解决的四个关键问题' '从业务需求推导出系统的能力边界' 3)+(T 10 '一次停机发生后，计划员需要在同一条决策链里获得四类答案。' 850000 1650000 10300000 300000 1600 'D3E3EC')+(Card 20 '看清事实','订单、库存、机器、工人处于哪一种状态？' 900000 2350000 2450000 1350000 '42C7B5')+(Card 30 '找到可行方案','硬约束下有哪些作业能排、哪些必须说明原因？' 3600000 2350000 2450000 1350000 'FFB547')+(Card 40 '控制变更风险','推演结果如何与正式计划隔离？谁有权生效？' 6300000 2350000 2450000 1350000 '8E7CFF')+(Card 50 '留下决策证据','输入、工具调用、结果和审批如何追溯？' 9000000 2350000 2450000 1350000 'FF6B6B')+(T 60 '项目需求：快，但每一步都能被解释、验证和审计。' 1550000 4650000 9000000 400000 2050 'FFFFFF' '1' 'ctr')
$slides += S $b

# 04 Goals
$b=(Header '目标体验：把“处理突发事件”变成一条受控流程' '计划员始终看得见当前状态、可选方案与决策权限' 4)+(AH 20 1650000 3200000 8400000 '42C7B5')+(Node 30 '01 识别变化','停机、缺料、插单' 800000 2400000 1700000 1100000 '183A57')+(Node 40 '02 结构化确认','把意图变成可校验场景' 3000000 2400000 1700000 1100000 '183A57')+(Node 50 '03 沙盒推演','计算影响并保留原计划' 5200000 2400000 1700000 1100000 '183A57')+(Node 60 '04 提案审批','人决定是否激活' 7400000 2400000 1700000 1100000 '183A57')+(Node 70 '05 追踪复盘','留下完整证据链' 9600000 2400000 1700000 1100000 '183A57')+(T 80 '系统提供建议与证据；计划员拥有正式计划的决定权。' 1900000 4550000 8200000 400000 1950 'FFC857' '1' 'ctr')
$slides += S $b

# 05 Architecture flow
$b=(H '总体架构：数据流与控制流如何连接' '技术栈嵌入业务决策链，而不是单独罗列' 5)+(T 10 '数据流' 750000 1600000 1200000 220000 1350 '42C7B5' '1')+(T 11 '控制流' 9500000 1600000 1200000 220000 1350 'FFC857' '1')+(AH 12 2450000 2600000 650000 '42C7B5')+(AH 13 5000000 2600000 650000 '42C7B5')+(AH 14 7550000 2600000 650000 '42C7B5')+(AH 15 10000000 2600000 650000 '42C7B5')+(Node 20 '业务数据','订单｜物料｜机器｜工人' 650000 2100000 1800000 1050000 '17344E')+(Node 30 'React 界面 + FastAPI','收集意图｜返回可解释结果' 3100000 2100000 1800000 1050000 '17344E')+(Node 40 '编排层','路由 Intent｜管理 Trace 与预算' 5650000 2100000 1800000 1050000 '197D79')+(Node 50 'Agent / LLM','理解语言｜选择获准工具｜输出结构化提案' 8200000 2100000 1800000 1050000 '197D79')+(Node 60 '工具注册表','白名单｜Schema｜执行｜投影｜截断｜记账' 10600000 2100000 1250000 1050000 '17344E')+(AV 70 6550000 3450000 800000 'FFC857')+(Node 80 '确定性排产核心','约束校验｜排程｜风险扫描｜情景模拟｜影响比较' 4650000 4300000 3400000 900000 '183A57')+(AH 90 8100000 4750000 800000 'FFC857')+(Node 100 '计划治理','PENDING_APPROVAL → APPROVE / MODIFY / REJECT → ACTIVE' 9000000 4300000 2600000 900000 '5C3D1F')+(T 110 'Trace、审计日志、版本号贯穿全流程，为每一次建议保留证据。' 1850000 5550000 8200000 250000 1500 'B8CAD7' '0' 'ctr')
$slides += S $b

# 06 Context
$b=(H '模型调用的上下文：由系统状态驱动' '每一轮都重新装配与当前任务相关的事实' 6)+(Node 10 'SessionState','active_plan｜pending_plan｜last_disruption｜已启用偏好｜degraded mode' 650000 2000000 2800000 1050000 '17344E')+(Node 20 '工具观察历史','早期结果：每条压缩为一行摘要' 650000 3450000 2800000 900000 '17344E')+(Node 30 '最近两条观察','保留完整的结构化工具结果；不可信内容加 <untrusted> 包装' 650000 4700000 2800000 900000 '17344E')+(AH 40 3700000 2600000 950000 '42C7B5')+(AH 41 3700000 3900000 950000 '42C7B5')+(AH 42 3700000 5150000 950000 '42C7B5')+(Node 50 'Context Manager','固定顺序组装：运行状态｜历史摘要｜最近观察｜任务块' 4850000 3050000 2600000 1250000 '197D79')+(AH 60 7550000 3670000 1050000 '42C7B5')+(Node 70 '一次 LLM 请求','静态提示词 + 工具 Schema；动态消息只包含本轮必要上下文' 8700000 3050000 2700000 1250000 '183A57')+(T 80 '上下文连续性来自运行状态与工具观察。RAG 未作为本项目主流程组件。' 1550000 5650000 9000000 250000 1450 'FFC857' '1' 'ctr')
$slides += S $b

# 07 Agent boundaries
$b=(H '三个 Agent：按输入可信度与权限划分职责' '交接通过 Pydantic 输出契约进行校验与留痕' 7)+(Card 10 'Ingestion Agent','面对外部表格。只能预览、映射、校验、创建导入批次；映射结果进入人工确认。' 700000 2050000 3450000 1700000 '42C7B5')+(Card 20 'Planning Agent','面对扰动与计划。读取业务事实、调用计算工具、形成重排提案；没有激活计划的权限。' 4350000 2050000 3450000 1700000 'FFB547')+(Card 30 'Risk Monitor Agent','读取事实并触发风险扫描，生成风险归因叙述；不持有写入工具。' 8000000 2050000 3450000 1700000 '8E7CFF')+(AH 40 2500000 4150000 7800000 '42C7B5')+(Node 50 'Handoff Contract','声明字段｜extra=forbid｜不可变实例｜自由文本标记为 untrusted' 2800000 4550000 6500000 820000 '197D79')+(T 60 '权限不是写在说明文档里，而是由 Tool Registry 的不可变白名单在调用入口执行。' 1450000 5650000 9200000 250000 1450 'FFC857' '1' 'ctr')
$slides += S $b

# 08 LLM decision mechanics
$b=(H 'LLM API 在流程里承担什么工作' '以受限 ReAct 循环选择下一步，每轮工具结果成为下一轮观察' 8)+(Node 10 '输入','自然语言请求或文件预览' 550000 2350000 1500000 900000 '17344E')+(AH 11 2150000 2800000 550000 '42C7B5')+(Node 20 'LLM 输出 JSON 动作','tool + arguments，或 final 契约' 2800000 2350000 1800000 900000 '197D79')+(AH 21 4750000 2800000 550000 '42C7B5')+(Node 30 'Tool Registry 七道闸门','白名单 → 输入 Schema → 执行 → 输出 Schema → 投影 → 截断 → 记账' 5400000 2200000 2400000 1200000 '183A57')+(AH 31 8550000 2800000 550000 '42C7B5')+(Node 40 '确定性工具结果','排程、约束、风险、模拟等结构化事实' 9200000 2350000 1800000 900000 '17344E')+(T 60 '循环控制：工具结果返回 Context Manager → 下一轮只基于最新事实决定下一步。' 2500000 4200000 7600000 260000 1550 'FFC857' '1' 'ctr')+(T 70 'LLM 负责：翻译、解释、选择工具、生成受限格式。   确定性服务负责：数值、约束、状态变迁。' 1100000 5100000 10000000 340000 1750 'FFFFFF' '1' 'ctr')
$slides += S $b

# 09 Demo story map
$b=(H '进入主线演示：CNC-01 明天上午停机 6 小时' '用一条“车间突发事件”串起前面的技术层' 9)+(T 10 '基线状态：当前计划已处于 ACTIVE，订单、物料、机器和工人组成一个可重放的生产快照。' 900000 1650000 10200000 300000 1500 'D3E3EC')+(AH 20 1300000 3300000 9800000 '42C7B5')+(Node 30 '① 基线计划','Dashboard / Schedule' 750000 2650000 1600000 1000000 '17344E')+(Node 40 '② 描述停机','What-if 自然语言' 2800000 2650000 1600000 1000000 '17344E')+(Node 50 '③ 确认场景','结构化结果确认' 4850000 2650000 1600000 1000000 '197D79')+(Node 60 '④ 沙盒模拟','比较影响，原计划不变' 6900000 2650000 1600000 1000000 '183A57')+(Node 70 '⑤ 提案审批','计划员决定是否生效' 8950000 2650000 1600000 1000000 '5C3D1F')+(T 80 '录像中每一个页面只承担这条主线的一个环节。' 2200000 4700000 7800000 330000 1900 'FFC857' '1' 'ctr')
$slides += S $b

# 10 Demo baseline
$b=(H '演示步骤 1：先建立可信基线' '录屏 01｜Dashboard → Schedule → Approval｜约 3 分钟' 10)+(R 10 650000 1750000 7100000 3600000 '17344E')+(T 11 '插入录屏 01：查看 Dashboard，生成计划，展示甘特图与 FCFS 对比，批准为 ACTIVE。' 1000000 3400000 6400000 340000 1550 'FFFFFF' '1' 'ctr')+(Card 20 '页面上要证明什么','计划来自订单、物料、机器、工人的快照；排程内核将作业分成 scheduled 与 unschedulable，并给出阻塞原因。' 8300000 1950000 3250000 1350000 '42C7B5')+(Card 30 '此刻对应的技术层','FastAPI 接收请求；确定性 Scheduler 计算计划；Approval 状态机将提案变为 ACTIVE。' 8300000 3550000 3250000 1350000 'FFB547')+(T 40 '配音重点：先让评委看见“当前事实”和“约束下的基线”，后面的所有影响都以此为参照。' 1250000 5650000 8800000 250000 1450 'FFC857' '1' 'ctr')
$slides += S $b

# 11 Demo LLM translation
$b=(P 50 'rId2' 0 1500000 12192000 4700000)+(H '演示步骤 2：用自然语言描述突发事件' '录屏 02｜What-if 翻译与结构化确认｜约 2 分钟' 11)+(R 10 700000 2100000 4900000 650000 '17344E')+(T 11 '计划员输入：CNC-01 明天上午停机 6 小时' 1000000 2310000 4400000 190000 1500 'FFFFFF' '1' 'ctr')+(AH 12 2650000 2920000 650000 'FFC857')+(R 20 700000 3150000 4900000 1050000 '183A57')+(T 21 '结构化预览：machine_id = CNC-01；start = 2026-03-03 08:00；end = 14:00' 900000 3460000 4500000 300000 1400 'CFE6E4' '1' 'ctr')+(T 30 'LLM 的职责：从自然语言提取封闭场景变更，输出 JSON，交给计划员确认。' 700000 4550000 5000000 300000 1500 'FFC857' '1')+(T 31 'LIVE 模式下展示真实模型调用；模型不可用时，切换为结构化表单并明确标注降级状态。' 700000 5000000 5000000 250000 1250 'D3E3EC')
$slides += S $b

# 12 Demo sandbox
$b=(H '演示步骤 3：在沙盒里计算影响' '录屏 03｜Run simulation → Compare plans｜约 2 分钟' 12)+(Node 10 '当前 ACTIVE 计划','正式生产计划保持不变' 800000 2350000 2400000 1100000 '17344E')+(AH 11 3450000 2900000 850000 'FFC857')+(Node 20 'Sandbox Scenario','临时加入 CNC-01 停机窗口' 4500000 2350000 2400000 1100000 '5C3D1F')+(AH 21 7150000 2900000 850000 'FFC857')+(Node 30 '确定性核心','run_scenario + evaluate + compare' 8200000 2350000 3000000 1100000 '197D79')+(T 40 '录屏画面：先展示延迟、不可排产或目标差值，再回到 Dashboard，确认 ACTIVE 计划没有被改写。' 1300000 4150000 9300000 330000 1750 'FFFFFF' '1' 'ctr')+(Card 50 '技术解释','情景推演读写隔离。模拟结果只是提案的证据，不会污染正式计划。' 2900000 5000000 6200000 750000 '42C7B5')
$slides += S $b

# 13 Demo proposal approval
$b=(H '演示步骤 4：从模拟结论到正式提案' '录屏 04｜Generate formal proposal → Approval｜约 2 分钟' 13)+(AH 10 2100000 3250000 7600000 '42C7B5')+(Node 20 '影响评估','哪些订单、交期和资源受到影响' 900000 2550000 2100000 1150000 '17344E')+(Node 30 '重排提案','保存为 PENDING_APPROVAL' 3650000 2550000 2100000 1150000 '197D79')+(Node 40 '人工审批','Approve / Modify / Reject' 6400000 2550000 2100000 1150000 '5C3D1F')+(Node 50 '新 ACTIVE 计划','审批后才成为正式计划' 9150000 2550000 2100000 1150000 '17344E')+(T 60 '录屏画面：展示目标分项、输入版本；尝试审批时系统再次核验约束与版本。' 1700000 4450000 8600000 280000 1600 'FFC857' '1' 'ctr')+(T 70 'Agent 可以准备提案和解释；批准动作由认证后的计划员端点执行。' 2050000 5100000 8000000 300000 1750 'FFFFFF' '1' 'ctr')
$slides += S $b

# 14 Demo trace
$b=(H '演示步骤 5：风险、追踪与复盘' '录屏 05｜Risks → Trace Viewer → Value Ledger｜约 2 分钟' 14)+(Card 10 'Risk Scanner','展示瓶颈、物料耗尽、零缓冲订单或产能风险；风险来自确定性扫描。' 750000 2050000 3300000 1450000 'FF6B6B')+(Card 20 'Trace Viewer','展示本次任务、上下文、工具调用、结果、提案和审批记录。' 4450000 2050000 3300000 1450000 '42C7B5')+(Card 30 'Value Ledger','将运行次数、审批、人工步骤与可量化指标沉淀为价值证据。' 8150000 2050000 3300000 1450000 '8E7CFF')+(T 40 '录屏画面：用同一个 trace_id 串起“发生了什么、系统看到了什么、调用了什么、谁做了最终决定”。' 1250000 4300000 9300000 360000 1800 'FFFFFF' '1' 'ctr')+(T 50 '这一步把智能体行为从一次临时交互，变成可复盘的运营记录。' 2300000 5100000 7700000 280000 1600 'FFC857' '1' 'ctr')
$slides += S $b

# 15 proof map
$b=(H '从演示回看技术：每一段画面都对应一项系统证据' '评委看到的不是页面巡游，而是一条可验证的决策链' 15)+(T 10 '演示环节' 850000 1750000 1700000 220000 1450 '42C7B5' '1')+(T 11 '背后的实现' 3600000 1750000 2600000 220000 1450 '42C7B5' '1')+(T 12 '可见证据' 7600000 1750000 2800000 220000 1450 '42C7B5' '1')+(Card 20 '基线排程','确定性 Scheduler + 约束检查' 650000 2150000 3000000 600000 '17344E')+(Card 30 '甘特图、阻塞原因、FCFS 对比' 7350000 2150000 3800000 600000 '197D79')+(Card 40 '自然语言停机','Planning Agent + Schema 契约' 650000 2950000 3000000 600000 '17344E')+(Card 50 '结构化场景预览与人工确认' 7350000 2950000 3800000 600000 '197D79')+(Card 60 '沙盒推演','run_scenario + compare_plans' 650000 3750000 3000000 600000 '17344E')+(Card 70 '正式计划保持 ACTIVE，差异可见' 7350000 3750000 3800000 600000 '197D79')+(Card 80 '审批与 Trace','状态机 + 审计 + 工具记账' 650000 4550000 3000000 600000 '17344E')+(Card 90 '批准记录、版本、trace_id、调用日志' 7350000 4550000 3800000 600000 '197D79')+(T 100 '中间层：Orchestrator 统一路由、创建 Trace、管理预算与 ReAct 回合。' 3550000 3300000 3300000 280000 1450 'FFC857' '1' 'ctr')
$slides += S $b

# 16 Value
$b=(H '落地价值：运营效率、治理能力与扩展基础' '价值来自缩短决策链，并保留进入生产环境前的控制点' 16)+(Card 10 '运营价值','更快识别可排与不可排作业；用 What-if 先计算影响；减少反复手工比对。' 700000 2100000 3350000 1750000 '42C7B5')+(Card 20 '治理价值','审批、版本、输入来源和工具调用形成统一证据链，便于责任追踪与复盘。' 4420000 2100000 3350000 1750000 'FFB547')+(Card 30 '商业与技术扩展','连接 ERP / MES；沉淀跨产线与跨工厂的计划数据和规则资产。' 8140000 2100000 3350000 1750000 '8E7CFF')+(T 40 '可用的起点：生产计划协作。   可扩展的方向：报价承诺、风险治理、跨站点协同。' 1650000 4750000 8800000 360000 1850 'FFFFFF' '1' 'ctr')
$slides += S $b

# 17 Close
$b=(P 50 'rId2' 0 0 12192000 6858000)+(R 51 0 0 5800000 6858000 '10243A' 'rect')+(T 52 '让车间变化成为可管理的决策流程' 650000 1800000 4900000 800000 3100 'FFFFFF' '1')+(T 53 '一次变化，从事实确认开始，经过可复现计算，最终由人做出可审计的决定。' 650000 2850000 4500000 500000 1750 'C4D9E7')+(R 54 650000 3750000 4000000 580000 '17344E')+(T 55 '可信智能体 = 受控模型调用 + 确定性内核 + 人在环治理' 850000 3940000 3600000 190000 1350 'FFC857' '1' 'ctr')+(T 56 'Thank you' 650000 4750000 2000000 260000 1550 'FFFFFF' '1')
$slides += S $b

$sp = Join-Path $Build 'ppt\slides'; $rp = Join-Path $sp '_rels'
$rel = Get-Content (Join-Path $rp 'slide1.xml.rels') -Raw
$media = Join-Path $Build 'ppt\media'; New-Item -ItemType Directory $media -Force | Out-Null
$assets = Join-Path (Split-Path $PSScriptRoot) 'presentation_assets'
foreach($file in @('cover-hero.png','incident-hero.png','whatif-hero.png','architecture-hero.png')){ Copy-Item (Join-Path $assets $file) (Join-Path $media $file) -Force }
$images = @{ 0='cover-hero.png'; 1='incident-hero.png'; 10='whatif-hero.png'; 16='architecture-hero.png' }
for($i=0;$i -lt $slides.Count;$i++){
  $r=$rel
  if($images.ContainsKey($i)){
    $img='<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/'+$images[$i]+'"/>'
    $r=$rel.Replace('</Relationships>',$img+'</Relationships>')
  }
  [IO.File]::WriteAllText((Join-Path $sp ('slide'+($i+1)+'.xml')),$slides[$i],[Text.UTF8Encoding]::new($false))
  [IO.File]::WriteAllText((Join-Path $rp ('slide'+($i+1)+'.xml.rels')),$r,[Text.UTF8Encoding]::new($false))
}

[xml]$ct=Get-Content -LiteralPath (Join-Path $Build '[Content_Types].xml')
if(-not ($ct.Types.Default | Where-Object {$_.Extension -eq 'png'})){$png=$ct.CreateElement('Default',$ct.DocumentElement.NamespaceURI);$png.SetAttribute('Extension','png');$png.SetAttribute('ContentType','image/png');[void]$ct.DocumentElement.AppendChild($png)}
for($i=2;$i -le $slides.Count;$i++){
  $part='/ppt/slides/slide'+$i+'.xml'
  if(-not ($ct.Types.Override | Where-Object {$_.PartName -eq $part})){$n=$ct.CreateElement('Override',$ct.DocumentElement.NamespaceURI);$n.SetAttribute('PartName',$part);$n.SetAttribute('ContentType','application/vnd.openxmlformats-officedocument.presentationml.slide+xml');[void]$ct.DocumentElement.AppendChild($n)}
}
$ct.Save((Join-Path $Build '[Content_Types].xml'))

[xml]$p=Get-Content (Join-Path $Build 'ppt\presentation.xml');$ns=New-Object Xml.XmlNamespaceManager($p.NameTable);$ns.AddNamespace('p','http://schemas.openxmlformats.org/presentationml/2006/main');$lst=$p.SelectSingleNode('//p:sldIdLst',$ns)
for($i=15;$i -le $slides.Count;$i++){$n=$p.CreateElement('p','sldId',$lst.NamespaceURI);$n.SetAttribute('id',[string](255+$i));$n.SetAttribute('id','http://schemas.openxmlformats.org/officeDocument/2006/relationships',('rId'+(10+$i)));[void]$lst.AppendChild($n)}
$p.Save((Join-Path $Build 'ppt\presentation.xml'))

[xml]$pr=Get-Content (Join-Path $Build 'ppt\_rels\presentation.xml.rels')
for($i=15;$i -le $slides.Count;$i++){$n=$pr.CreateElement('Relationship',$pr.DocumentElement.NamespaceURI);$n.SetAttribute('Id',('rId'+(10+$i)));$n.SetAttribute('Type','http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide');$n.SetAttribute('Target',('slides/slide'+$i+'.xml'));[void]$pr.DocumentElement.AppendChild($n)}
$pr.Save((Join-Path $Build 'ppt\_rels\presentation.xml.rels'))

if(Test-Path $Output){Remove-Item $Output -Force}
Compress-Archive -Path (Join-Path $Build '*') -DestinationPath ($Output+'.zip') -Force
Move-Item ($Output+'.zip') $Output -Force
