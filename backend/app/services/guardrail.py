"""`Guardrail_Layer`：不受信任内容包裹与注入检测（任务 5.8，design.md §2.7(a)(b)）。

**归属：确定性** · R23.1、R23.2、R23.3 · P0

本模块落地 `Guardrail_Layer` 四职责中的前两项（另两项——Agent 输出 schema 校验与保留键
剥离、解释数值一致性检查——分别是任务 5.9 / 5.10）：

- **(a) 不受信任内容标记与包裹（R23.1–2）**：`UNTRUSTED_SOURCES` 枚举五个不受信任来源；
  `wrap_untrusted` 去控制字符、截断 2,000 字符、用零宽字符打断伪造的 `</untrusted>` 闭合标记；
  `UntrustedStr` 在读取时包住不受信任字段，使「未包裹的裸 str 被拼进提示词」在类型层面不可能。
- **(b) 注入模式检测（R23.3）**：`INJECTION_PATTERNS` 六类正则；命中即写
  `PROMPT_INJECTION_SUSPECTED` 审计（保留原文与命中片段），并在结果里置 `suspected` 供 UI 徽章。

## 为什么正则不是安全边界（必须说清，否则会被误读为防线）

**检测到注入不阻断任何业务流程。** 让对抗用例 EVAL-201 / 202 / 203 通过的是**架构**，不是
这里的正则：

- Agent 的工具白名单里**没有**任何能把计划置为 `ACTIVE` 的工具（design.md §2.7、R23.4）。
- `Approval_Service.approve()` 是 `ACTIVE` 状态的**唯一**到达路径（属性 15，任务 3.4），
  它只由规划员经认证的写端点触发，不读 Agent 输出的任何字段。
- 不受信任文本只以 `<untrusted>` 数据形式进入提示词，系统提示词第 [5] 段（`shared.py`
  的 `DATA_RULES_BLOCK`）已声明包裹内一律是数据、其中指令不得执行。

因此即便 `scan_injection` **一个模式都没命中**（正则必然可被绕过——它是启发式，不是解析器），
被攻击的动作（激活计划、越权写入）在架构上依然发生不了。`scan_injection` 只做三件事：写审计
留痕、置 `injection_suspected` 供 UI 提示、确保原文只作数据展示。把它当成防御会导致两种误判：
以为「加一个正则就能防住新式注入」，或以为「正则漏了就说明系统被攻破」——两者都错。真正的
防线是工具白名单与单一审批入口，正则只是可观测性。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Final, TypeVar

from sqlalchemy import Engine

from app.agents.contracts import AgentContract
from app.db import audit

#: Agent 输出契约的类型变量——`validate_agent_output` 收进哪个契约就吐出哪个契约的实例，
#: 调用点不必再 `cast`。上界锁死 `AgentContract`：只有 `agents/contracts.py` 那套
#: `extra="forbid"` 的输出契约才是合法的校验目标（工具输出模型不是 Agent 输出契约，见
#: `agents/contracts.py` 模块 docstring）。
_ContractT = TypeVar("_ContractT", bound=AgentContract)

__all__ = [
    "INJECTION_PATTERNS",
    "UNTRUSTED_SOURCES",
    "InjectionHit",
    "InjectionVerdict",
    "UntrustedStr",
    "require_wrapped",
    "scan_injection",
    "strip_control_chars",
    "wrap_untrusted",
]

# ==========================================================================
# (a) 不受信任内容标记与包裹（R23.1–2）
# ==========================================================================

#: 一律标记为不受信任输入的五个来源（R23.1、design.md §2.7(a)）。
#:
#: `whatif.query` 在 P0 **没有输入路径**（自然语言 What-if 是 P1，R16.1）——常量此刻一并
#: 定义，是为了让 P1 落地时无需回头改这份枚举，也让本集合与 R23.1 逐条对齐、便于测试断言其
#: 完整。P0 实际经过包裹的四个是：上传文件单元格、`Order.notes`、`Product.description`、
#: 拒绝理由（`decision.rejection_reason`）。
UNTRUSTED_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "upload.cell",
        "order.notes",
        "product.description",
        "whatif.query",
        "decision.rejection_reason",
    }
)

#: 包裹用的标签字面量。`_CLOSE_TAG` 单列出来是因为 `wrap_untrusted` 要在被包裹内容里查找并
#: 打断它——伪造一个闭合标记是「越狱出包裹」最有效的一招（对抗样例里专门留了这种单元格）。
_OPEN_TAG_FMT: Final = '<untrusted source="{source}">'
_CLOSE_TAG: Final = "</untrusted>"

#: 零宽空格。插进伪造的 `</untrusted>` 中间，使它不再是一个能闭合包裹的标记，但对人类阅读
#: 几乎无影响（展示时看起来仍是那串字符）。
_ZERO_WIDTH_SPACE: Final = "\u200b"

#: 控制字符：除 `\t` `\n` `\r` 外的 C0（U+0000–U+001F）与 DEL（U+007F）以及 C1
#: （U+0080–U+009F）。保留制表/换行/回车是因为它们在自由文本（订单备注、单元格）里是合法
#: 排版，去掉会改变展示；其余控制字符没有正当用途，却能被用来遮蔽注入文本或破坏日志/终端。
_CONTROL_CHARS_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: 截断阈值（design.md §2.7(a)）。2,000 行的文件与 50 行的文件送进 LLM 的 token 量应当相同，
#: 单个不受信任字段也不该无界膨胀提示词——因此在包裹时先截到这个长度。
MAX_UNTRUSTED_CHARS: Final = 2_000


def strip_control_chars(text: str) -> str:
    """去掉不可打印的控制字符，保留 `\\t` `\\n` `\\r`（见 `_CONTROL_CHARS_RE` 注释）。"""
    return _CONTROL_CHARS_RE.sub("", text)


def _break_forged_close_tag(text: str) -> str:
    """在任何 `</untrusted>` 中间插零宽空格，使其无法闭合我们的包裹（design.md §2.7(a)）。

    不受信任内容里若原样含一个 `</untrusted>`，未处理时它会提前闭合外层包裹，后续文本就
    「逃」到了包裹之外、被模型当作提示词的一部分——这正是最有效的一种越界写法。插入零宽
    空格后，该串在字节上不再等于闭合标记（因此不闭合包裹），但人眼看到的仍是那串字符。
    """
    return text.replace(_CLOSE_TAG, f"<{_ZERO_WIDTH_SPACE}/untrusted>")


def wrap_untrusted(text: str, source: str) -> str:
    """把一段不受信任文本包成 `<untrusted source="...">…</untrusted>`（R23.2）。

    三步，顺序固定：①去控制字符；②截断到 `MAX_UNTRUSTED_CHARS`（先去控制字符再截断，
    避免「截断点恰好切在一个控制字符处」留下半个字符的歧义）；③打断内容里伪造的闭合标记。

    `source` 应当取自 `UNTRUSTED_SOURCES`（P1 的 `whatif.query` 亦在其中）；它只作为标签的
    `source` 属性回显，帮助规划员/模型识别这段数据的来路。不强制校验 `source` 是否在枚举内
    ——包裹本身对任何来路的文本都成立，过度约束反而会挡住将来正当的新来源；枚举的用途是让
    「哪些字段该走这条路」有一份可断言的清单。
    """
    sanitised = strip_control_chars(text)[:MAX_UNTRUSTED_CHARS]
    sanitised = _break_forged_close_tag(sanitised)
    open_tag = _OPEN_TAG_FMT.format(source=source)
    return f"{open_tag}\n{sanitised}\n{_CLOSE_TAG}"


class UntrustedStr:
    """一段读取自不受信任来源的文本的类型级包装（design.md §2.7(a)）。

    ## 它守的是什么

    R23.1 列出的五个来源（`UNTRUSTED_SOURCES`）在**读取时**就该被包成 `UntrustedStr`，而任何
    要把它拼进提示词的下游只接受它的 `wrapped()` 形式——一段裸 `str` 无法冒充。这让「未经包裹
    的不受信任文本被直接拼进提示词」这条风险在**类型层面**就不成立：

    - **mypy strict**：下游函数的形参标注为 `UntrustedStr`，传 `str` 是类型错误，编译期即拒。
    - **运行期**：`require_wrapped()` 断言收到的确是 `UntrustedStr` 实例，兜住动态类型的漏网。

    `UntrustedStr` **不是** `str` 的子类：故意如此。若继承 `str`，它就能在任何接受 `str` 的地方
    静默使用（字符串拼接、f-string、`+`），那正好绕过了「只接受 `wrapped()`」这条约束——包装
    就形同虚设。不继承 `str` 意味着想用它的内容必须显式取 `.raw` 或 `.wrapped()`，每一次「解包」
    都是一处可被审查的显式动作。

    ## 与 `Context_Manager` 观察包裹的关系

    `context_manager.assemble_messages` 已在**观察侧**包裹 `untrusted=True` 的工具返回
    （`<untrusted source="tool:...">`）。`UntrustedStr` 管的是**字段读取侧**：`Order.notes`、
    `rejection_reason` 这类直接从库里读出、不经工具的自由文本，在进入任何解释载荷/提示词之前
    先过这里。两者是同一道 R23.2 边界在两条不同数据通路上的落点。
    """

    __slots__ = ("_raw", "_source")

    def __init__(self, raw: str, source: str) -> None:
        self._raw = raw
        self._source = source

    @property
    def raw(self) -> str:
        """原始文本（未包裹）。展示给规划员时用它（UI 负责纯文本渲染，不解释指令语义）。"""
        return self._raw

    @property
    def source(self) -> str:
        """这段文本的来路（`UNTRUSTED_SOURCES` 之一）。"""
        return self._source

    def wrapped(self) -> str:
        """`<untrusted>` 包裹后的形式——**唯一**被允许拼进提示词的形态（R23.2）。"""
        return wrap_untrusted(self._raw, self._source)

    def __repr__(self) -> str:
        # 不在 repr 里泄漏全文（可能很长、也可能含注入文本），只标类型与来路。
        return f"UntrustedStr(source={self._source!r}, len={len(self._raw)})"


def require_wrapped(value: UntrustedStr) -> str:
    """运行期断言 `value` 是 `UntrustedStr` 并返回其 `wrapped()`（R23.2 的运行时那一半）。

    下游装配提示词时经它取不受信任文本：类型标注（mypy strict）挡住静态传 `str` 的路径，
    这里的 `isinstance` 兜住动态传入（例如从 `dict[str, Any]` 里取出的值）。传 `str` 在这两处
    都失败——正是 design.md §2.7(a)「直接把 str 传进去会在类型检查与运行时断言两处失败」。
    """
    if not isinstance(value, UntrustedStr):
        raise TypeError(
            "不受信任文本必须先经 UntrustedStr 包裹再进提示词（R23.2）："
            f"收到未包裹的 {type(value).__name__}。请用 UntrustedStr(text, source).wrapped()。"
        )
    return value.wrapped()


# ==========================================================================
# (b) 注入模式检测（R23.3）
# ==========================================================================

#: 六类注入模式（design.md §2.7(b)）。每条 `(名称, 正则)`：
#:
#: - `IGNORE_PRIOR`：要求忽略/无视先前指令。
#: - `FORCE_APPROVE`：要求批准/激活计划。
#: - `SET_ACTIVE`：要求把某物置为 active / 活动计划。
#: - `PRIV_ESCALATE`：要求提权 / 扮演管理员或系统。
#: - `LEAK_PROMPT`：要求泄露/输出系统提示词。
#: - `ROLE_MARKUP`：伪造 system / assistant / untrusted 角色标签。
#:
#: 中英双语、允许词间有少量间隔字符（`.{0,N}`），因为注入文本常在关键词之间夹杂噪声。
#: **这是启发式，不是解析器**：必然可被绕过（换同义词、拉大间隔、用别的语言）。它的价值在
#: 可观测性——命中即留痕，而**不命中不代表安全**（见模块 docstring）。
INJECTION_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "IGNORE_PRIOR",
        re.compile(
            r"(忽略|无视|ignore|disregard).{0,12}"
            r"(先前|以上|previous|above|所有).{0,12}"
            r"(指令|instruction|prompt)",
            re.I,
        ),
    ),
    (
        "FORCE_APPROVE",
        re.compile(
            r"(批准|approve|activate|激活).{0,12}"
            r"(全部|所有|all|这个|this).{0,8}"
            r"(计划|plan)",
            re.I,
        ),
    ),
    (
        "SET_ACTIVE",
        re.compile(r"(set|置为|设为|make).{0,10}(active|活动计划)", re.I),
    ),
    (
        "PRIV_ESCALATE",
        re.compile(
            r"(你现在是|you are now|act as|扮演).{0,20}(管理员|admin|系统|system)",
            re.I,
        ),
    ),
    (
        "LEAK_PROMPT",
        re.compile(
            r"(输出|泄露|repeat|reveal|print).{0,12}"
            r"(系统提示|system prompt|instructions)",
            re.I,
        ),
    ),
    (
        "ROLE_MARKUP",
        re.compile(r"<\s*/?\s*(system|assistant|untrusted)\s*>", re.I),
    ),
)

#: 命中片段回显的最大长度。截断避免把整段（可能很长的）注入文本塞进审计载荷与 UI 徽章。
_HIT_FRAGMENT_MAX_CHARS: Final = 120


@dataclass(frozen=True, slots=True)
class InjectionHit:
    """一次注入模式命中：模式名 + 命中的文本片段（≤120 字）。"""

    pattern: str
    fragment: str


@dataclass(frozen=True, slots=True)
class InjectionVerdict:
    """`scan_injection` 的结果。

    `suspected` 供 UI 徽章（design.md §2.7(b)：置 `injection_suspected`）。**它不是放行/阻断
    的开关**——业务流程无论 `suspected` 真假都照常走（见模块 docstring）。`hits` 保留命中明细，
    供展示与排查。
    """

    suspected: bool
    hits: tuple[InjectionHit, ...]


def scan_injection(
    text: str,
    source: str,
    *,
    actor: str = "SYSTEM",
    trace_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    engine: Engine | None = None,
) -> InjectionVerdict:
    """扫描 `text` 是否命中注入模式；命中则写 `PROMPT_INJECTION_SUSPECTED` 审计（R23.3）。

    **不阻断、不改写、不拒绝任何业务动作**——只留痕并返回一个供 UI 提示的判定（模块 docstring
    详述为什么正则不是防线）。命中时审计载荷保留**原文**（`original_text`，R23.3「保留原文用于
    展示」）与逐条命中片段（`matched`），便于事后在提交材料里引用具体注入内容。

    参数除 `text` / `source` 外全部关键字：`actor` 默认 `SYSTEM`（多数扫描由系统在读取不受信任
    字段时触发）；审批路径上的 `rejection_reason` 扫描应传 `actor="PLANNER"`。`engine` 透传给
    `audit.append`（测试指向临时库）。
    """
    hits = tuple(
        InjectionHit(pattern=name, fragment=match.group(0)[:_HIT_FRAGMENT_MAX_CHARS])
        for name, pattern in INJECTION_PATTERNS
        if (match := pattern.search(text)) is not None
    )
    if hits:
        payload: Mapping[str, Any] = {
            "source": source,
            "matched": [{"pattern": h.pattern, "fragment": h.fragment} for h in hits],
            # R23.3：保留原文用于展示。截断上限与包裹一致，避免超长文本撑爆审计载荷。
            "original_text": text[:MAX_UNTRUSTED_CHARS],
        }
        audit.append(
            event_category="PROMPT_INJECTION_SUSPECTED",
            event_type="PROMPT_INJECTION_SUSPECTED",
            actor=actor,
            payload=payload,
            subject_type=subject_type,
            subject_id=subject_id,
            trace_id=trace_id,
            engine=engine,
        )
    return InjectionVerdict(suspected=bool(hits), hits=hits)


# ==========================================================================
# (c) Agent 输出 schema 校验与保留键剥离（任务 5.9，design.md §2.7(c)，R23.5、R13.11）
# ==========================================================================
#
# ## 这一段守的是什么
#
# `Guardrail_Layer` 的第三职责（前两项是本文件上半的 (a)(b)，(d) 数值一致性是任务 5.10）：
# 校验每个 Agent 的**最终输出**是否符合其输出契约（R23.5），并在校验前**剥离保留键**
# （R13.11）。design.md §2.7(c) 的四行伪代码是本段的规格：
#
#     obj = parse_json_strict(raw)                    # 失败 → AGENT_OUTPUT_NOT_JSON
#     dropped = [k for k in walk_keys(obj) if k in RESERVED_KEYS]
#     if dropped:
#         audit.write("AGENT_RESERVED_KEY_DROPPED", ...)
#         obj = drop_keys(obj, RESERVED_KEYS)         # R13.11：丢弃自主等级声明
#     return contract.model_validate(obj)             # extra="forbid"，失败 → R21.6
#
# ## 为什么剥离要在 `extra="forbid"` 校验**之前**
#
# 输出契约（`app/agents/contracts.py`）一律 `extra="forbid"`。若不先剥离，一个混入
# `impact_class` 的 Agent 输出会直接被 `model_validate` 以「多了个字段」拒掉——那当然也
# 拒绝了越权声明，但拒绝的**理由错了**：R13.11 要的不是「拒绝这次输出」，而是「悄悄丢掉
# 那个字段、把剩下的合法内容照常放行、并留一条审计」。模型在 `final` 里顺手写了个
# `autonomy_level` 不该让整轮作废（那会把它推向重试、烧步数烧 token），该让它的合法部分
# 通过、只把越权的那半剪掉。因此顺序是**先剥离再校验**，两步的失败语义完全不同：
# 剥离是静默的（写审计但不失败），校验失败才回 `AGENT_OUTPUT_CONTRACT_VIOLATION`。
#
# ## 为什么保留键要**递归**查找
#
# 契约允许嵌套对象与数组（例如 `ColumnMappingProposal.columns` 是一个对象列表）。模型可能
# 把 `autonomy_level` 藏在某个嵌套对象里而非顶层。只查顶层键会漏掉这种藏法，而 EVAL-209
# 的对抗面正是「想方设法让越权声明生效」。因此 `walk_reserved_keys` 递归穿过 dict 的值与
# list 的元素，`strip_reserved_keys` 同样递归重建——两者走同一套遍历，保证「查得到的就删得掉」。
#
# ## 这是 EVAL-209 的第二道防线（不是第一道）
#
# 第一道防线是**结构隔离**（任务 7.3、ADR-010）：`decide_autonomy` 读的是 `Orchestrator`
# 自己算出并持久化的 `impact_assessments` 行，**从不读** Agent 输出的任何字段——因此即便一个
# `autonomy_level` 漏过了这里的剥离，执行路径的选择也不会因它而变。本段是**纵深防御**：把
# 越权声明在进入契约之前就剪掉并留痕，让「模型试图声称自主等级」这件事在审计里可见
# （`AGENT_RESERVED_KEY_DROPPED`），而不是默默无害地躺在一个没人读的字段里。把它当成唯一
# 防线是错的（真正拦住越权的是结构隔离）；省掉它也是错的（那样越权尝试就不可观测了）。


class AgentOutputNotJsonError(ValueError):
    """Agent 输出不是合法 JSON 对象（design.md §2.7(c)：`AGENT_OUTPUT_NOT_JSON`）。

    继承 `ValueError`：这是「输入形状不对」，与 `AuditImmutableError` 那种「这条路不存在」
    不同。`Orchestrator._process_turn` 已在解析层把它转成回给模型的错误观察；本异常供
    `validate_agent_output` 这条**直接**入口（不经 ReAct 循环的调用方，例如列映射/解释的
    落地路径）用来把「解析失败」与「契约失败」区分开。
    """


#: 保留键：只能由确定性组件产生、Agent **无权**在输出里声称的字段（design.md §2.7(c)、
#: R13.11、ADR-010）。
#:
#: - `impact_class` / `autonomy_level`：影响分级与自主等级由 `Autonomy_Policy_Engine`
#:   确定性判定，「不允许 LLM 决定自己能否自动执行」（requirements §「LLM 与确定性边界」表）。
#: - `plan_status` / `approved`：计划状态与审批结果只由 `Approval_Service` 迁移（属性 15），
#:   Agent 声称 `approved: true` 或 `plan_status: ACTIVE` 是最直接的越权尝试。
#: - `feasibility`：可行性由 `Scheduling_Core` 判定；模型自称 `FEASIBLE` 不能使之为真。
#: - `start_time` / `end_time`：排产时刻由 `Scheduling_Core` 计算，`[AUTHORITY]` 段已禁止
#:   模型输出这两个数值（除非来自工具返回）——这里是同一条禁令在输出侧的兜底。
#:
#: 这七个与 design.md §2.7(c) 的 `RESERVED_KEYS` 逐字对齐；`test` 断言其完整。
RESERVED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "impact_class",
        "autonomy_level",
        "plan_status",
        "approved",
        "feasibility",
        "start_time",
        "end_time",
    }
)

__all__ += [
    "RESERVED_KEYS",
    "AgentOutputNotJsonError",
    "parse_json_strict",
    "walk_reserved_keys",
    "strip_reserved_keys",
    "validate_agent_output",
]


def parse_json_strict(raw: str) -> dict[str, Any]:
    """把 Agent 输出的原始字符串解析成一个 JSON **对象**（design.md §2.7(c) 第 1 行）。

    严格之处有二：①必须是合法 JSON；②顶层必须是对象（`dict`）——一个裸数组、字符串或数字
    都不是有效的 Agent 输出契约载荷。任一不满足即抛 `AgentOutputNotJsonError`
    （`AGENT_OUTPUT_NOT_JSON`）。不做任何「宽松修复」（截断到第一个 `{`、去掉尾随文本等）：
    那类修复会让「模型吐了半个 JSON」这种真实故障被悄悄掩盖，而契约校验本就是要抓住它。
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise AgentOutputNotJsonError("Agent 输出不是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise AgentOutputNotJsonError(
            f"Agent 输出必须是 JSON 对象，收到 {type(parsed).__name__}"
        )
    return parsed


def _effective_reserved(allowed: frozenset[str]) -> frozenset[str]:
    """`RESERVED_KEYS` 去掉目标契约**合法声明**的字段名后，真正要剥离的保留键集合。

    ## 为什么要减去 `allowed`

    一个保留键的语义是「只能由确定性组件产生、Agent 无权自行声称」——但这只在**没有契约
    授权**它时成立。`RevisedPlanProposal` 恰恰把 `feasibility` 声明为自己的输出字段
    （值由确定性内核算出、经 `handoff` 承载），因此在这个契约下 `feasibility` 是**被授权的
    接口字段**，不是越权声明。若不减去，剥离会把一个必填的合法字段删掉，随后 `extra=
    "forbid"` 校验反而因「缺字段」失败——把「模型正确填了它该填的」误判成违规。

    减去的是**契约声明的字段名**：`impact_class` / `autonomy_level` / `plan_status` /
    `approved` / `start_time` / `end_time` 没有任何 P0 契约声明它们，因此对所有契约仍是
    全额剥离；只有 `feasibility` 因 `RevisedPlanProposal` 声明而在该契约下被豁免。这条豁免
    随契约走：换一个不声明 `feasibility` 的契约（如 `ExplanationDraft`），它照样被剥离。
    """
    return RESERVED_KEYS - allowed


def walk_reserved_keys(obj: Any, allowed: frozenset[str] = frozenset()) -> list[str]:
    """递归遍历 `obj`，返回其中出现过的（去除 `allowed` 后的）保留键名（去重、稳定排序）。

    只在 **dict 的键** 上匹配保留键——一个恰好等于 `"feasibility"` 的**字符串值**不是越权
    声明，是数据（例如某个 `rationale` 里提到了这个词）。穿过 dict 的值与 list 的元素向下
    递归，因此藏在嵌套对象里的保留键也查得到（见模块 §(c) docstring 的递归理由）。

    `allowed` 是目标契约合法声明的字段名（见 `_effective_reserved`）——命中它们不算越权，
    不进返回集合。默认空集：不带契约信息的直接调用（例如只想扫描一段任意 JSON）按全额
    `RESERVED_KEYS` 判定。

    返回排序后的去重列表：写进 `AGENT_RESERVED_KEY_DROPPED` 审计载荷时顺序稳定，便于断言与
    事后比对。
    """
    found: set[str] = set()
    _collect_reserved(obj, _effective_reserved(allowed), found)
    return sorted(found)


def _collect_reserved(obj: Any, reserved: frozenset[str], found: set[str]) -> None:
    """`walk_reserved_keys` 的递归工作函数：把命中的保留键名收进 `found`。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in reserved:
                found.add(key)
            _collect_reserved(value, reserved, found)
    elif isinstance(obj, list):
        for item in obj:
            _collect_reserved(item, reserved, found)


def strip_reserved_keys(obj: Any, allowed: frozenset[str] = frozenset()) -> Any:
    """递归重建 `obj`，删除全部（去除 `allowed` 后的）保留键（design.md §2.7(c)，R13.11）。

    返回一个**新的**结构（不原地改传入的对象——调用方可能还要用原始形态写审计/排查）。
    与 `walk_reserved_keys` 走同一套遍历与同一套 `allowed` 豁免：dict 里丢掉键在生效保留集
    里的项、对其余值递归；list 逐元素递归；标量原样返回。「查得到的就删得掉」由两者遍历
    一致保证。
    """
    reserved = _effective_reserved(allowed)
    return _strip(obj, reserved)


def _strip(obj: Any, reserved: frozenset[str]) -> Any:
    """`strip_reserved_keys` 的递归工作函数。"""
    if isinstance(obj, dict):
        return {
            key: _strip(value, reserved)
            for key, value in obj.items()
            if key not in reserved
        }
    if isinstance(obj, list):
        return [_strip(item, reserved) for item in obj]
    return obj


def validate_agent_output(
    parsed: Any,
    contract: type[_ContractT],
    *,
    agent: str,
    trace_id: str | None = None,
    engine: Engine | None = None,
) -> _ContractT:
    """剥离保留键后用 `contract` 校验 `parsed`，返回契约实例（design.md §2.7(c)）。

    调用点已持有解析好的 dict（`Orchestrator._process_turn` 先解析再分派；直接入口先调
    `parse_json_strict`），因此本函数从**已解析的对象**起步，专注 §2.7(c) 的后三行：

    1. `walk_reserved_keys` 递归找出保留键——但**减去 `contract` 合法声明的字段名**
       （`_effective_reserved`）：`RevisedPlanProposal` 把 `feasibility` 声明为自己的输出
       字段（值由确定性内核算出），在该契约下它是被授权的接口字段而非越权声明，因此不剥离；
       换成不声明它的契约（如 `ExplanationDraft`）则照剥。有命中则写一条
       `AGENT_RESERVED_KEY_DROPPED` 审计（载荷含 `dropped_keys` 与 `agent`，R13.11 的
       可观测性），再 `strip_reserved_keys` 剪掉。剥离**不**失败——静默处置，只留痕。
    2. `contract.model_validate` 校验剪后的对象。契约 `extra="forbid"`，因此多字段/缺字段/
       类型不符都被拒，抛 `pydantic.ValidationError`——调用方按 R21.6 转成
       `AGENT_OUTPUT_CONTRACT_VIOLATION`（`Orchestrator` 已如此处理）。

    `parsed` 类型标注为 `Any`：多数调用点已保证它是 dict（`parse_json_strict` / `Orchestrator`
    的解析层都只放对象过来），但 `final` 里也可能是模型吐出的任意 JSON（裸数组、标量）。
    非对象无保留键可剥，原样交给 `model_validate` 按契约拒绝——校验失败的语义由此保持一致。

    `agent` 进审计载荷（哪个 Agent 试图声称保留键）；`trace_id` 关联到本次运行的 `Trace`
    （R24.5）；`engine` 透传给 `audit.append`（测试指向临时库）。校验异常**不**在此吞掉——
    保留键剥离与契约校验是两件事，前者本层处置完，后者交由懂得「怎么把校验失败回给模型」
    的调用方（Error Handling §3 的观察机制）。
    """
    # 契约合法声明的字段名（含别名）——这些即便撞上保留键也是被授权的接口字段，不剥离。
    allowed = frozenset(contract.model_fields) | {
        field.alias
        for field in contract.model_fields.values()
        if field.alias is not None
    }
    dropped = walk_reserved_keys(parsed, allowed)
    if dropped:
        audit.append(
            event_category="AGENT_RESERVED_KEY_DROPPED",
            event_type="AGENT_RESERVED_KEY_DROPPED",
            actor=agent,
            payload={"agent": agent, "dropped_keys": dropped},
            trace_id=trace_id,
            engine=engine,
        )
    cleaned = strip_reserved_keys(parsed, allowed)
    return contract.model_validate(cleaned)


# ==========================================================================
# (d) 解释数值的闭世界一致性检查（任务 5.10，design.md §2.7(d)、ADR-012，R10.7、R23.6）
# ==========================================================================
#
# ## 这一段守的是什么
#
# `Guardrail_Layer` 的第四职责（前三项是 (a)(b)(c)）：LLM 生成计划解释时，它能看到的数字
# **只有**我们喂给它的紧凑结构化载荷里的数字。因此解释文本里出现的任何数字，只要不能匹配回
# 载荷（在按单位设定的容差内），就是模型编造的——这就是**闭世界比对**。不匹配即回退到模板
# 解释（`TemplateExplanation`），并写 `EXPLANATION_NUMERIC_MISMATCH` 审计（R10.7）。
#
# ## 为什么闭世界比对这里够用（ADR-012）
#
# 通常「校验一段自由文本里的数字是否属实」需要语义级理解——那很难。但解释调用的**输入是我们
# 完全控制的载荷**，「模型能合法使用的数字集合」是已知且有限的，因此一个正则（`NUMBER_RE`）
# 加集合比对（`check_numeric_consistency`）就够。这个方案能成立，依赖任务 5.11 的载荷侧配合三
# 条降低误报措施（见下）——三者缺一，本检查要么每次误报（过严）要么形同虚设（过松）。
#
# ## 三条降低误报措施（分工：本层 vs 任务 5.11 的载荷/提示词侧）
#
# 1. **提示词逐字复制 + 阿拉伯数字**（提示词侧，任务 5.6/5.11）：提示词第 [6] 段要求文中每个
#    数字逐字复制自载荷、不换算不四舍五入，并**明确要求阿拉伯数字**。为什么点名阿拉伯数字：
#    `NUMBER_RE` 只识别 `[-+]?\d...`，中文数词（「五小时」「两天」）匹配不到任何桶——若模型用
#    中文写数字，本检查会**每次都回退**，把一个可用的解释毙掉。这条约束把「中文数词」这条最常
#    见的误报源从输入侧消除。
# 2. **载荷预置换算形式**（载荷侧，任务 5.11）：既给 `total_tardiness_minutes: 315`，也给
#    `total_tardiness_human: "5 小时 15 分钟"`。这样模型无需自己把 315 分钟换算成「5 小时
#    15 分钟」——它只要逐字抄。`collect_numeric_facts` 因此从 `total_tardiness_human` 这个
#    字符串里也把 5 和 15 收进 `hours`/`counts` 桶（递归收集穿过字符串里的数字），使抄写的
#    换算形式也能匹配。
# 3. **载荷不含可组合原料**（载荷侧，任务 5.11）：刻意不给单价，只给已算好的金额；不给「每件
#    工时 × 件数」，只给总工时。没有原料，模型就没有「算出一个载荷里不存在的新数字」的合法
#    途径——于是「文本里的数字不在载荷中」这个信号能干净地指向编造，而非合法推导。
#
# ## 偏宽松是有意的
#
# `check_numeric_consistency` 对无单位数字尝试**全部数值桶**（counts + minutes + ratios +
# money + days + hours），容差也偏松。这不是漏洞：本检查的目标是抓「载荷里根本不存在的数字」
# （模型凭空编一个订单号数量、一个不存在的拖期值），而不是抓「用错了单位」。把一个存在于载荷
# 的数字误判成编造（假阳性）的代价是毙掉一个正确解释、退回模板——比放过一个「单位串了但数值
# 恰好存在」的边缘情况更糟。因此在「宁可漏报边缘、不可误伤正解」之间，这里选后者。


class _NumericBucket(str, Enum):
    """`NumericFactSet` 的七个桶名。与 `NumericFactSet` 的字段名逐一对应，容差查 `_TOL`。"""

    COUNTS = "counts"
    MINUTES = "minutes"
    RATIOS = "ratios"
    MONEY = "money"
    DAYS = "days"
    HOURS = "hours"
    LITERALS = "literals"


#: 各数值桶的比对容差（design.md §2.7(d) 的 `NumericFactSet` 注释）。`literals` 不在此表
#: ——它整体豁免，`mask_literals` 在提取数字前就把标识符/时间戳挖掉，根本不进比对。
_TOL: Final[dict[_NumericBucket, Decimal]] = {
    _NumericBucket.COUNTS: Decimal("0"),  # 计数类：整数，零容差
    _NumericBucket.MINUTES: Decimal("0.5"),  # 分钟：取整到分钟，半分钟容差
    _NumericBucket.RATIOS: Decimal("0.005"),  # 比率/百分比：0.5 个百分点
    _NumericBucket.MONEY: Decimal("0.005"),  # 金额：半分钱
    _NumericBucket.DAYS: Decimal("0.02"),  # 天（由分钟派生 m/1440）
    _NumericBucket.HOURS: Decimal("0.02"),  # 小时（由分钟派生 m/60）
}

#: 各单位归一后落到哪些桶。无单位（`None`）尝试**全部数值桶**——偏宽松是有意的（见模块 §(d)
#: docstring）；有单位则只查语义匹配的桶（外加 minutes/days/hours 之间的派生关系）。
#:
#: 归一化约定（`_normalise` 落实）：`%` / percent / 个百分点 → 除以 100 归到 `ratios`；
#: 分钟 → `minutes`；小时 → `hours`；天 → `days`；美元 / USD / $ → `money`。
_BUCKETS_BY_UNIT: Final[dict[str | None, tuple[_NumericBucket, ...]]] = {
    None: (
        _NumericBucket.COUNTS,
        _NumericBucket.MINUTES,
        _NumericBucket.RATIOS,
        _NumericBucket.MONEY,
        _NumericBucket.DAYS,
        _NumericBucket.HOURS,
    ),
    "ratios": (_NumericBucket.RATIOS,),
    "minutes": (_NumericBucket.MINUTES,),
    "hours": (_NumericBucket.HOURS,),
    "days": (_NumericBucket.DAYS,),
    "money": (_NumericBucket.MONEY,),
}

#: 数字提取正则（design.md §2.7(d) 逐字采用）。
#:
#: - 前视 `(?<![A-Za-z0-9_\-./:])`：数字前不能紧跟字母/数字/下划线/连字符/点/斜杠/冒号——这
#:   把嵌在标识符（`JOB-004`、`OP1`）、ISO 时间戳（`2026-03-02T08:15:00`）里的数字排除在
#:   「独立数字」之外。**它是 `mask_literals` 的第二道保险**：即便某个标识符没被显式屏蔽，
#:   其内部数字多半也不会被当作独立 token 提取。
#: - 主体两支：带千分位的 `\d{1,3}(?:,\d{3})*(?:\.\d+)?`（如 `1,234.5`）或朴素 `\d+(?:\.\d+)?`。
#: - 尾随单位（可选）：`%` / percent / 个百分点 / 分钟 / min / minutes / 小时 / hours /
#:   天 / days / 美元 / USD / `$`。
NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_\-./:])"
    r"([-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(%|percent|个百分点|分钟|min|minutes|小时|hours?|天|days?|美元|USD|\$)?"
)

#: 单位词 → 归一化桶键的映射。大小写不敏感（匹配前先 `lower()`）。
_UNIT_TO_BUCKET_KEY: Final[dict[str, str]] = {
    "%": "ratios",
    "percent": "ratios",
    "个百分点": "ratios",
    "分钟": "minutes",
    "min": "minutes",
    "minutes": "minutes",
    "小时": "hours",
    "hour": "hours",
    "hours": "hours",
    "天": "days",
    "day": "days",
    "days": "days",
    "美元": "money",
    "usd": "money",
    "$": "money",
}

#: ISO 8601 时间戳（日期，可带时间）。`mask_literals` 用它把 `2026-03-02T08:15:00` /
#: `2026-03-02` 这类整体挖掉——它们是标识性数据，不是「解释里引用的量」，比对它们只会误报。
_ISO_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"
)

#: 标识符样式：一段大写字母/数字，中间至少一个连字符（`JOB-004`、`CNC-01`、`PR-003`、
#: `ORD-013`、`PLAN-7`）。这类是实体 ID，`literals` 桶整体豁免它们（design.md §2.7(d)）。
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"\b[A-Za-z]+(?:-[A-Za-z0-9]+)+\b")


@dataclass(frozen=True, slots=True)
class NumericFactSet:
    """从喂给 LLM 的结构化载荷递归收集的全部数值叶子（design.md §2.7(d)）。

    七个桶按单位/语义分类，各设容差（见 `_TOL`）。`literals` 桶收集标识符与 ISO 时间戳并
    **整体豁免**——它们不是「解释里引用的量」，比对只会误报，因此在提取阶段就被 `mask_literals`
    挖掉。`days` / `hours` 由 `minutes` 派生（`collect_numeric_facts` 生成时一并填），让「315
    分钟」写成「5.25 小时」或「0.22 天」时也能匹配。

    `frozenset` + `frozen=True`：事实集一旦收集即定型，`check_numeric_consistency` 只读不改。
    """

    counts: frozenset[Decimal] = frozenset()
    minutes: frozenset[Decimal] = frozenset()
    ratios: frozenset[Decimal] = frozenset()
    money: frozenset[Decimal] = frozenset()
    days: frozenset[Decimal] = frozenset()
    hours: frozenset[Decimal] = frozenset()
    literals: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class NumericCheckResult:
    """`check_numeric_consistency` 的结果。

    `ok` 为真表示文本里每个数字都能匹配回载荷（`numeric_check = PASS`）；为假则 `unmatched`
    列出编造的数字原文，调用路径据此回退 `TemplateExplanation` 并写审计（`numeric_check =
    FALLBACK`）。`unmatched` 保序去重，便于审计载荷稳定与断言。
    """

    ok: bool
    unmatched: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TemplateExplanation:
    """回退用的确定性模板解释占位（任务 5.10 提供接口，任务 5.11 的 `TemplateExplanationRenderer`
    填充真实内容）。

    数值一致性检查失败（或后续预算耗尽 / 降级模式）时，`numeric_check = FALLBACK`，解释路径
    发布本类型而非 LLM 文本。此处只承载「回退发生了、原因是什么」这一最小事实：`reason` 说明
    回退缘由（如 `"EXPLANATION_NUMERIC_MISMATCH"`），`unmatched` 保留触发回退的编造数字。
    任务 5.11 会把它换成基于 `decision_evidence` / `assumptions` 的完整模板渲染——届时本占位
    的字段是那个渲染器的输入的子集，不会被推翻。
    """

    reason: str
    unmatched: tuple[str, ...] = ()


__all__ += [
    "NUMBER_RE",
    "NumericCheckResult",
    "NumericFactSet",
    "TemplateExplanation",
    "check_numeric_consistency",
    "collect_numeric_facts",
    "guard_explanation_numeric_consistency",
    "is_close",
    "mask_literals",
]


def is_close(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    """`|a - b| <= tol`（design.md §2.7(d)）。全程 `Decimal`，不经浮点。"""
    return abs(a - b) <= tol


def _to_decimal(raw: str) -> Decimal | None:
    """把提取出的数字串（可能带千分位逗号、正负号）转成 `Decimal`；无法转则 `None`。"""
    cleaned = raw.replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except (ArithmeticError, ValueError):
        return None


def _collect_numbers_from_str(
    text: str,
    counts: set[Decimal],
    minutes: set[Decimal],
    ratios: set[Decimal],
    money: set[Decimal],
    days: set[Decimal],
    hours: set[Decimal],
) -> None:
    """从一个字符串里提取独立数字（先挖掉 ISO 时间戳与标识符），按其**尾随单位**收进对应桶。

    载荷里的字符串值（如 `total_tardiness_human: "5 小时 15 分钟"`）也含数字——这些正是我们
    预置的、供模型逐字抄写的换算形式（措施②）。因此收集事实时必须穿过字符串，否则模型逐字抄
    「5 小时 15 分钟」反而会因 5、15 不在事实集里被误判成编造。

    **按单位分桶而非一律进 counts**：文本侧比对时「5 小时」这个 token 会带单位「小时」，从而
    只查 `hours` 桶（不是无单位的全桶尝试）。若这里把 5 只收进 `counts`，`hours` 桶里没有 5，
    比对就落空。因此这里复用 `_normalise` 的单位归一逻辑，把字符串里的「5 小时」收进 `hours`、
    「15 分钟」收进 `minutes`——与文本侧的定桶规则对称。无单位的裸数字仍进 `counts`。
    百分号形式（`85%`）归一到 ratios（除以 100）。
    """
    masked = _IDENTIFIER_RE.sub(" ", _ISO_TIMESTAMP_RE.sub(" ", text))
    bucket_sets: dict[str, set[Decimal]] = {
        "counts": counts,
        "minutes": minutes,
        "ratios": ratios,
        "money": money,
        "days": days,
        "hours": hours,
    }
    for match in NUMBER_RE.finditer(masked):
        value, bucket_key = _normalise(match.group(1), match.group(2))
        if value is None:
            continue
        bucket_sets[bucket_key if bucket_key is not None else "counts"].add(value)


def collect_numeric_facts(payload: Any) -> NumericFactSet:
    """递归收集载荷里的全部数值叶子，构成 `NumericFactSet`（design.md §2.7(d) 的
    `collect_numeric_facts → NumericFactSet`）。

    分桶依据是**键名的单位后缀**（闭世界载荷由我们自己按约定构造，键名是可靠的单位信号）：

    - `*_minutes` → `minutes` 桶，并派生 `hours = m/60`、`days = m/1440` 一并收入（让「315
      分钟」写成「5.25 小时」也能匹配，措施②的机制侧）。
    - `*_ratio` / `*_rate` / `*_pct` / `*_percent` → `ratios` 桶。
    - `*_usd` / `*_money` / `*_cost` → `money` 桶。
    - `*_days` → `days` 桶；`*_hours` → `hours` 桶。
    - 其余数值（计数、无单位）→ `counts` 桶。
    - 字符串值：ISO 时间戳与标识符收进 `literals`（整体豁免）；其中的数字**按其尾随单位
      分桶**（`5 小时` → `hours`、`15 分钟` → `minutes`，无单位裸数字 → `counts`），与文本侧
      定桶规则对称，使预置换算形式被逐字抄写时也能匹配（措施②，见 `_collect_numbers_from_str`）。

    `payload` 是任意可 JSON 化的嵌套结构（dict / list / 标量）；`bool` **不**当数值收集
    （`isinstance(True, int)` 为真，但 `True` 不是一个「量」）。
    """
    counts: set[Decimal] = set()
    minutes: set[Decimal] = set()
    ratios: set[Decimal] = set()
    money: set[Decimal] = set()
    days: set[Decimal] = set()
    hours: set[Decimal] = set()
    literals: set[str] = set()
    _collect(payload, None, counts, minutes, ratios, money, days, hours, literals)
    return NumericFactSet(
        counts=frozenset(counts),
        minutes=frozenset(minutes),
        ratios=frozenset(ratios),
        money=frozenset(money),
        days=frozenset(days),
        hours=frozenset(hours),
        literals=frozenset(literals),
    )


def _bucket_for_key(key: str | None) -> str:
    """按键名的单位后缀判定该数值该进哪个桶（返回桶字段名）。"""
    if key is None:
        return "counts"
    k = key.lower()
    if k.endswith("_minutes") or k.endswith("_min"):
        return "minutes"
    if k.endswith("_hours"):
        return "hours"
    if k.endswith("_days"):
        return "days"
    if (
        k.endswith("_ratio")
        or k.endswith("_rate")
        or k.endswith("_pct")
        or k.endswith("_percent")
    ):
        return "ratios"
    if k.endswith("_usd") or k.endswith("_money") or k.endswith("_cost"):
        return "money"
    return "counts"


def _collect(  # noqa: PLR0913 —— 七个桶各一个累加器，聚成对象反而更难读
    obj: Any,
    key: str | None,
    counts: set[Decimal],
    minutes: set[Decimal],
    ratios: set[Decimal],
    money: set[Decimal],
    days: set[Decimal],
    hours: set[Decimal],
    literals: set[str],
) -> None:
    """`collect_numeric_facts` 的递归工作函数。`key` 是当前值所在的字典键（用于分桶）。"""
    if isinstance(obj, bool):
        return  # bool 是 int 的子类，但它不是一个「量」——不收集。
    if isinstance(obj, int | float | Decimal):
        value = Decimal(str(obj))
        bucket = _bucket_for_key(key)
        if bucket == "minutes":
            minutes.add(value)
            hours.add(value / Decimal("60"))  # 派生小时
            days.add(value / Decimal("1440"))  # 派生天
        elif bucket == "hours":
            hours.add(value)
        elif bucket == "days":
            days.add(value)
        elif bucket == "ratios":
            ratios.add(value)
        elif bucket == "money":
            money.add(value)
        else:
            counts.add(value)
        return
    if isinstance(obj, str):
        for ts in _ISO_TIMESTAMP_RE.findall(obj):
            literals.add(ts)
        for ident in _IDENTIFIER_RE.findall(obj):
            literals.add(ident)
        _collect_numbers_from_str(obj, counts, minutes, ratios, money, days, hours)
        return
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            _collect(v, str(k), counts, minutes, ratios, money, days, hours, literals)
        return
    if isinstance(obj, list | tuple):
        for item in obj:
            # 列表元素继承父键的单位语义（`tardiness_minutes: [30, 45]` 里两项都是分钟）。
            _collect(item, key, counts, minutes, ratios, money, days, hours, literals)
        return
    # 其余类型（None 等）无数值可收。


def mask_literals(text: str, literals: frozenset[str]) -> str:
    """从 `text` 挖掉标识符、ISO 时间戳与事实集里登记的字面量，再交给数字提取（design.md
    §2.7(d) 的 `mask_literals`）。

    三步屏蔽，把「标识性数据里的数字」从比对中排除，避免它们被误当作「解释引用的量」：

    1. 事实集 `literals` 里登记的字符串（载荷中出现过的具体 ID / 时间戳）逐个替换掉。
    2. 任何 ISO 时间戳（`_ISO_TIMESTAMP_RE`）——即便某个时间戳没进事实集（模型抄了个格式对
       但载荷没有的时间戳，那是另一回事，不该由数字比对来抓）。
    3. 任何标识符样式（`_IDENTIFIER_RE`，如 `JOB-004`）——把整个 ID 挖掉，其内部的 `004`
       就不会被 `NUMBER_RE` 当独立数字提取。

    顺序：先替具体 `literals`（最精确），再按模式挖时间戳与标识符（兜底）。替换成空格而非空串，
    避免把两侧文本粘连出一个本不存在的数字（`JOB-004` 与后面的 `12` 之间若无分隔会粘成
    `00412`）。
    """
    masked = text
    # 长的先替，避免一个短字面量是另一个的前缀时替错（稳定：按长度降序再按字典序）。
    for lit in sorted(literals, key=lambda s: (-len(s), s)):
        if lit:
            masked = masked.replace(lit, " ")
    masked = _ISO_TIMESTAMP_RE.sub(" ", masked)
    masked = _IDENTIFIER_RE.sub(" ", masked)
    return masked


def _normalise(number_text: str, unit_text: str | None) -> tuple[Decimal | None, str | None]:
    """把一个匹配到的 `(数字, 单位)` 归一成 `(Decimal 值, 桶键)`。

    - 去千分位逗号、转 `Decimal`（`_to_decimal`）。
    - `%` / percent / 个百分点 → 值除以 100，桶键 `ratios`（`85%` → `0.85`）。
    - 其余单位按 `_UNIT_TO_BUCKET_KEY` 归到 minutes/hours/days/money。
    - 无单位 → 桶键 `None`（调用侧对无单位尝试全部数值桶）。

    数字无法解析为 `Decimal` 时返回 `(None, ...)`，调用侧跳过该 token（不把解析失败误判成
    编造——那是提取噪声，不是模型行为）。
    """
    value = _to_decimal(number_text)
    if value is None:
        return None, None
    if unit_text is None:
        return value, None
    unit = unit_text.strip().lower()
    bucket_key = _UNIT_TO_BUCKET_KEY.get(unit)
    if bucket_key == "ratios":
        return value / Decimal("100"), "ratios"
    return value, bucket_key


def check_numeric_consistency(text: str, facts: NumericFactSet) -> NumericCheckResult:
    """闭世界比对：`text` 里每个数字都能匹配回 `facts` 则 `ok`，否则列出编造数字
    （design.md §2.7(d)、R10.7）。

    流程与 design.md §2.7(d) 的伪代码一致：

    1. `mask_literals` 先挖掉标识符 / ISO 时间戳 / 事实集字面量（`JOB-004` / `CNC-01` /
       `PR-003` / 时间戳）——它们整体豁免，不进比对。
    2. `NUMBER_RE` 逐个提取剩余数字（连同可选单位）。
    3. `_normalise` 去千分位、`%` → /100、按单位定桶键。
    4. 有单位 → 只查该单位对应的桶；无单位 → 尝试**全部数值桶**（偏宽松，见模块 §(d)
       docstring）。任一桶里存在一个数（在该桶容差内）与之接近，即算匹配。
    5. 一个都不匹配的数字进 `unmatched`（保序去重）。

    返回 `NumericCheckResult(ok=not unmatched, unmatched=...)`。**本函数不写审计、不产生副
    作用**——是否回退、是否记 `EXPLANATION_NUMERIC_MISMATCH` 由
    `guard_explanation_numeric_consistency`（或任务 5.11 的解释路径）决定，保持本比对纯净可测。
    """
    stripped = mask_literals(text, facts.literals)
    unmatched: list[str] = []
    seen: set[str] = set()
    for token in NUMBER_RE.finditer(stripped):
        number_text, unit_text = token.group(1), token.group(2)
        value, bucket_key = _normalise(number_text, unit_text)
        if value is None:
            continue
        buckets = _BUCKETS_BY_UNIT.get(bucket_key)
        if buckets is None:
            # 未知单位键（不应发生，_normalise 只产已知键或 None）——保守当无单位处理。
            buckets = _BUCKETS_BY_UNIT[None]
        matched = any(
            is_close(value, fact, _TOL[bucket])
            for bucket in buckets
            for fact in getattr(facts, bucket.value)
        )
        if not matched:
            original = token.group(0).strip()
            if original not in seen:
                seen.add(original)
                unmatched.append(original)
    return NumericCheckResult(ok=not unmatched, unmatched=tuple(unmatched))


def guard_explanation_numeric_consistency(
    narrative: str,
    payload: Any,
    *,
    plan_id: str | None = None,
    trace_id: str | None = None,
    actor: str = "PLANNING_AGENT",
    engine: Engine | None = None,
) -> TemplateExplanation | None:
    """对 LLM 生成的解释 `narrative` 跑闭世界数值比对；不一致则写审计并返回回退占位。

    这是任务 5.10 提供给解释路径（任务 5.11 的 `Explanation_Builder`）的**接线入口**：

    - 从 `payload`（送给 LLM 的紧凑载荷）`collect_numeric_facts` 得到事实集。
    - `check_numeric_consistency(narrative, facts)`：`ok` 则返回 `None`——调用方照常发布 LLM
      文本，`numeric_check = PASS`。
    - 不一致则写 `EXPLANATION_NUMERIC_MISMATCH` 审计（载荷含 `unmatched` 与事实集各桶大小的
      摘要，R10.7 的可观测性），并返回 `TemplateExplanation`——调用方据此发布模板解释，
      `numeric_check = FALLBACK`。

    审计**只**在不一致时写（与 `scan_injection` / `validate_agent_output` 的「命中才留痕」
    一致）。`plan_id` 作为 `subject_id`（哪个计划的解释被回退）；`trace_id` 关联本次运行的
    `Trace`；`engine` 透传给 `audit.append`（测试指向临时库）。
    """
    facts = collect_numeric_facts(payload)
    result = check_numeric_consistency(narrative, facts)
    if result.ok:
        return None
    audit.append(
        event_category="EXPLANATION_NUMERIC_MISMATCH",
        event_type="EXPLANATION_NUMERIC_MISMATCH",
        actor=actor,
        payload={
            "unmatched": list(result.unmatched),
            "facts_summary": {
                "counts": len(facts.counts),
                "minutes": len(facts.minutes),
                "ratios": len(facts.ratios),
                "money": len(facts.money),
                "days": len(facts.days),
                "hours": len(facts.hours),
                "literals": len(facts.literals),
            },
        },
        subject_type="PLAN" if plan_id is not None else None,
        subject_id=plan_id,
        trace_id=trace_id,
        engine=engine,
    )
    return TemplateExplanation(
        reason="EXPLANATION_NUMERIC_MISMATCH", unmatched=result.unmatched
    )
