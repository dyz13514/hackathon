"""分层规则的静态断言（design.md Architecture §1「分层规则」，tasks.md 1.8）。

## 为什么在内核开工前就写

这五条断言保护的是**结构**，而结构一旦被违反，代码通常还是能跑的：内核里 import 一次
`Session` 不会让任何测试变红，只会让「同样输入必得同样输出」（R5.7）悄悄不再成立——因为
那个函数现在能读库了，而库的内容不在入参里。同理，Agent 直接 import 一个 handler 不会
报错，只会绕过 `Tool_Registry` 的白名单、schema 校验与投影截断，让 R22.10 的权限隔离退化
成「大家都记得走 registry」的约定。

这类退化没有失败的瞬间，只有逐渐变松的现状。所以断言必须**先于**被约束的代码存在。

## 今天大部分被扫的模块还不存在

`app/core/` 与 `app/agents/` 目前只有 `__init__.py`，`app/llm/adapter.py` 与
`app/core/autonomy.py` 还没落地（任务 5.3、7.3）。扫描因此对空包**空过**——这是有意的：
「没有违反」在空包上是真话。为了让空过不至于变成永久的假绿，另有五条元测试兜底：

- `test_scan_reaches_the_real_app_tree`：确认路径解析没歪，扫描真的看到了现有源码；
- `test_absent_scan_targets_are_declared_pending`：缺席的扫描目标必须登记在
  `PENDING_SCAN_TARGETS` 里，改名或挪目录会让这条立刻变红；
- `test_scanner_detects_synthetic_violations`：用一段合成源码证明扫描器**确实会**抓到
  违反，包括相对 import 这种最容易漏掉的写法；
- `test_file_level_scan_flags_a_real_violating_module`：在真实文件上跑通整条管道；
- `test_reference_scan_finds_calls_but_not_definitions` 与
  `test_gateway_pattern_matches_realistic_endpoints`：第 ③、④ 条各自的判定逻辑自证。

换言之，「这些断言现在是绿的」这件事本身是被测试过的。

## 扫描是 AST 而不是正则

`import` 语句的形态太多（`import a.b`、`from a import b`、`from ..db import session`、
`from . import x`），正则会同时漏报和误报——尤其漏掉相对 import，而那正是内核里最可能出现
的写法（`from ..db.session import get_session` 比 `import sqlalchemy` 更像"顺手的一行"）。
AST 把这些形态统一成绝对点分名再比对。

唯一的例外是网关 URL（第 ③ 条）：那检查的是字面量而不是 import，用文本扫描。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
APP_ROOT = BACKEND_ROOT / "app"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"

#: 目录遍历时跳过的名字：缓存、虚拟环境、依赖树。它们里面的 import 不是我们写的。
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".venv",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
    }
)

# --------------------------------------------------------------------------------------
# 扫描目标。缺席者登记在 PENDING_SCAN_TARGETS，落地后集合自然收缩，扫描逻辑无需改动。
# --------------------------------------------------------------------------------------

CORE_PACKAGE = APP_ROOT / "core"
AGENTS_PACKAGE = APP_ROOT / "agents"
AUTONOMY_MODULE = CORE_PACKAGE / "autonomy.py"
ADAPTER_MODULE = APP_ROOT / "llm" / "adapter.py"

#: 尚未落地的扫描目标，及其归属任务。缺席**必须**在此登记：这样「包被改名了所以扫描
#: 什么也没扫到」与「包还没写」两种情况不会长得一样。
PENDING_SCAN_TARGETS: dict[str, str] = {
    "app/core/autonomy.py": "任务 7.3 Autonomy_Policy_Engine",
}

# --------------------------------------------------------------------------------------
# ① 内核禁入清单
# --------------------------------------------------------------------------------------

#: 内核不得 import 的第三方包。前四个来自 tasks.md 1.8 与 design.md 分层规则第 1 条。
#:
#: `botocore` / `starlette` 是 `boto3` / `fastapi` 的传输层实体：绕开门面直接 import 它
#: 们，禁的那件事一样做成了，因此一并列入。
#:
#: `importlib` 在列的理由不同：AST 看不见 `import_module("app.db.session")`。纯函数内核
#: 没有任何动态加载的正当需求，禁掉它就把这个盲区一起关上了。
CORE_FORBIDDEN_RUNTIME = (
    "sqlalchemy",
    "fastapi",
    "httpx",
    "boto3",
    "botocore",
    "starlette",
    "importlib",
)

#: 内核不得 import 的本仓库层。这条把「沙箱拿不到会话」做成**包依赖**而不是约定
#: （tasks.md 1.8 第 ① 条注解）：`Session` 只能从 `app.db` 取，内核不 import `app.db`，
#: 于是沙箱里的计算在语言层面就没有拿到会话的路径，无需依赖任何人的自觉。
CORE_FORBIDDEN_APP_LAYERS = (
    "app.db",
    "app.api",
    "app.llm",
    "app.agents",
    "app.orchestrator",
    "app.services",
    "app.tools",
)

# --------------------------------------------------------------------------------------
# ② Agent 禁入清单
# --------------------------------------------------------------------------------------

#: Agent 只能经 `Tool_Registry.invoke()` 触达能力（design.md 分层规则第 2 条）。
#:
#: `app.tools.registry` / `app.tools.models` **不**在禁入之列——它们正是那条唯一通路和
#: 它的契约模型。禁的是绕过 registry 的四类直连：内核、handler 实现、应用服务、持久层。
AGENTS_FORBIDDEN = (
    "app.core",
    "app.tools.handlers",
    "app.services",
    "app.db",
    "sqlalchemy",
)

# --------------------------------------------------------------------------------------
# ③ 网关 URL 唯一出现点
# --------------------------------------------------------------------------------------

#: 形似 Bedrock 网关端点的 URL 字面量。命中任一关键片段即算网关地址。
GATEWAY_URL_PATTERN = re.compile(
    r"""https?://[^\s"'`]*(?:bedrock|amazonaws\.com|invoke-model|converse)[^\s"'`]*""",
    re.IGNORECASE,
)

#: 网关配置项名。读到它的人就是能调网关的人，所以读取点与 URL 字面量一样受控。
GATEWAY_SETTING_TOKENS = ("bedrock_gateway_url", "BEDROCK_GATEWAY_URL")

#: 允许出现网关地址的文件（相对 `backend/`）。
#:
#: `app/settings.py` 在列只因为它**声明**这个配置字段——「缺失即拒绝以 LIVE 启动」的校验
#: 必须知道字段名。声明与使用是两回事：真正把 URL 发出去的只有 adapter。
GATEWAY_ALLOWED_FILES = ("app/llm/adapter.py", "app/settings.py")

#: URL 字面量则更严：连 settings 也不该有，默认值不存在，只能来自环境变量（R23.11）。
GATEWAY_URL_ALLOWED_FILES = ("app/llm/adapter.py",)

FRONTEND_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte")

# --------------------------------------------------------------------------------------
# ④ 计划状态写入口
# --------------------------------------------------------------------------------------

#: 仓储层唯一能改 `production_plans.status` 的方法（design.md Components §4.1）。
#: R11.8 的绕过防护有三道：REST 模型无 `status` 字段、显式 403 路由、以及**这一条**——
#: 状态迁移的调用点集合受控。前两道挡外部请求，这一道挡内部代码。
STATUS_WRITER = "update_plan_status_if_version"

#: 当前允许触及该方法的位置（相对 `backend/`，精确到文件）。
#:
#: 任务 3.3 把这里从「按 `app/services/` 目录放宽」**收紧为恰好一处**——
#: `app/services/approval.py`（`Approval_Service`），它既定义又调用
#: `update_plan_status_if_version`。design.md §8 指定的另一处调用点是 P1 的
#: `AutoAppliedChange.revert`（一键回滚经 `Approval_Service.activate_internal()` 重新激活
#: 一个计划）。那段代码尚未落地，其文件登记在 `PENDING_STATUS_WRITER_SITES` 里；一旦落地，
#: 把它的真实路径加进本元组即可，收紧的范围仍然排除 api / agents / tools / orchestrator /
#: core 全部五层，以及 `app/services/` 下除 `approval.py` 之外的任何模块。
#:
#: 为什么现在能精确到 `approval.py` 而不再需要整个 `app/services/` 目录：`Approval_Service`
#: 的落地（任务 3.1）已经确定了 `update_plan_status_if_version` 就住在 `approval.py`，文件名
#: 不再是「尚未确定」。目录级放宽的唯一理由（文件名未知）已消失，因此按 §8 的两点清单收紧。
STATUS_WRITER_ALLOWED = ("app/services/approval.py",)

#: design.md §8 指定但尚未落地的状态写入调用点。P1 的 `AutoAppliedChange.revert` 落地后，
#: 把其真实路径从这里移入 `STATUS_WRITER_ALLOWED`。登记在此使「P1 回滚还没写」与「它被
#: 挪到了别处」不会长得一样（与 `PENDING_SCAN_TARGETS` 同一用意）。
PENDING_STATUS_WRITER_SITES: dict[str, str] = {
    "AutoAppliedChange.revert": "任务 13.x（P1）L4 自动应用回滚",
}

# --------------------------------------------------------------------------------------
# ⑤ 自治策略引擎的隔离
# --------------------------------------------------------------------------------------

#: `core/autonomy.py` 不得 import 的模块（tasks.md 1.8 第 ⑤ 条、7.3）。
#:
#: 「影响分级不可被 LLM 影响」这条性质的载体是 `ImpactInput` 的 7 个数值字段——没有字符串
#: 字段，就没有可注入的入口。这条断言守的是它的前提：分级逻辑与 LLM/Agent 之间不存在
#: import 边，因此不可能出现「顺手把模型输出传进来」的重载。
AUTONOMY_FORBIDDEN = (
    "app.llm",
    "app.agents",
    "app.orchestrator",
    "boto3",
    "botocore",
    "httpx",
)


# --------------------------------------------------------------------------------------
# 扫描器
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportedName:
    """一条 import 解析出的绝对点分模块名及其行号。"""

    module: str
    lineno: int


def _python_files(root: Path) -> list[Path]:
    """`root` 下的全部 `.py`。`root` 不存在时返回空列表（空包 = 无违反）。"""
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*.py")
        if not _SKIP_DIRS.intersection(path.parts)
    )


def _relative(path: Path) -> str:
    """报错信息里用的路径形式：相对 `backend/`，正斜杠。"""
    try:
        return path.relative_to(BACKEND_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _package_of(path: Path) -> str:
    """文件所在包的点分名，用于解析相对 import。

    `backend/` 之外的文件（元测试用的临时文件）没有包名：绝对 import 的判定不需要它。
    """
    try:
        relative = path.relative_to(BACKEND_ROOT).with_suffix("")
    except ValueError:
        return ""
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()  # `app/core/__init__.py` 自己就是 `app.core`
    else:
        parts.pop()  # `app/core/scheduling.py` 属于 `app.core`
    return ".".join(parts)


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    """把 `from ..db import session` 解析成 `app.db`。

    `level` 是点的个数：1 表示当前包，2 表示上一级，以此类推。越界（点比层级还多）时
    返回剩下的部分而不是抛异常——那种代码 import 期就会失败，不需要这里再报一次。
    """
    parts = package.split(".") if package else []
    if level > 1:
        parts = parts[: max(len(parts) - (level - 1), 0)]
    if module:
        parts = [*parts, *module.split(".")]
    return ".".join(part for part in parts if part)


def _imports_in_source(
    source: str, package: str = "", filename: str = "<memory>"
) -> tuple[ImportedName, ...]:
    """源码里的全部 import，一律折算成绝对点分名。

    `from app.core import scheduling` 同时产出 `app.core` 与 `app.core.scheduling`：
    禁入清单既可能写包名也可能写模块名，两种粒度都要能命中。
    """
    tree = ast.parse(source, filename=filename)
    found: list[ImportedName] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(ImportedName(alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            base = (
                _resolve_relative(package, node.level, node.module)
                if node.level
                else (node.module or "")
            )
            if not base:
                continue
            found.append(ImportedName(base, node.lineno))
            for alias in node.names:
                if alias.name != "*":
                    found.append(ImportedName(f"{base}.{alias.name}", node.lineno))
    return tuple(found)


def _imports_of(path: Path) -> tuple[ImportedName, ...]:
    return _imports_in_source(
        path.read_text(encoding="utf-8"),
        package=_package_of(path),
        filename=str(path),
    )


def _is_forbidden(imported: str, forbidden: str) -> bool:
    """`app.db.session` 命中禁入项 `app.db`；`app.database` 不命中。"""
    return imported == forbidden or imported.startswith(f"{forbidden}.")


def _forbidden_imports(files: list[Path], forbidden: tuple[str, ...]) -> list[str]:
    """逐文件比对禁入清单，返回人可读的违反清单。"""
    offences: list[str] = []
    for path in files:
        for imported in _imports_of(path):
            for name in forbidden:
                if _is_forbidden(imported.module, name):
                    offences.append(
                        f"{_relative(path)}:{imported.lineno} 里 import 了 "
                        f"{imported.module}（禁入：{name}）"
                    )
    return sorted(set(offences))


def _is_reference_to(node: ast.Attribute | ast.Name | ast.Constant, name: str) -> bool:
    """节点是否引用了 `name`。三种形态：`obj.name`、裸 `name`、字符串 `"name"`。"""
    if isinstance(node, ast.Attribute):
        return node.attr == name
    if isinstance(node, ast.Name):
        return node.id == name
    return bool(node.value == name)


def _references_to(path: Path, name: str) -> list[int]:
    """`name` 被**引用**的行号：属性访问、裸名、以及 `getattr` 用的字符串。

    函数定义本身不算引用，因此仓储层的 `def update_plan_status_if_version` 不会被算成
    调用点；`getattr(repo, "update_plan_status_if_version")` 这种绕法则会（字符串必须
    整体等于方法名，所以顺带提到它的 docstring 不会误报）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute | ast.Name | ast.Constant) and _is_reference_to(
            node, name
        ):
            lines.append(node.lineno)
    return sorted(set(lines))


def _is_allowed(relative_path: str, allowed: tuple[str, ...]) -> bool:
    """允许项以 `/` 结尾表示整个目录，否则要求路径完全相等。"""
    return any(
        relative_path.startswith(item) if item.endswith("/") else relative_path == item
        for item in allowed
    )


def _frontend_source_files() -> list[Path]:
    if not FRONTEND_SRC.is_dir():
        return []
    return sorted(
        path
        for path in FRONTEND_SRC.rglob("*")
        if path.is_file()
        and path.suffix in FRONTEND_SOURCE_SUFFIXES
        and not _SKIP_DIRS.intersection(path.parts)
    )


# --------------------------------------------------------------------------------------
# ① 确定性内核不碰 I/O
# --------------------------------------------------------------------------------------


def test_core_does_not_import_io_libraries() -> None:
    """`app/core/**` 不得 import ORM / Web 框架 / HTTP 客户端 / AWS SDK。

    这是 R5.7（同样输入必得同样输出）的结构前提。内核一旦能自己取数据，它的输出就不再只
    由入参决定，`test_deterministic_scheduling.py` 那条属性也就失去了意义——它仍然会通过，
    因为它构造的输入没变，而变的是那些不在输入里的东西。
    """
    offences = _forbidden_imports(_python_files(CORE_PACKAGE), CORE_FORBIDDEN_RUNTIME)
    assert not offences, "确定性内核出现 I/O 依赖（R5.7 前提被破坏）：\n" + "\n".join(offences)


def test_core_does_not_import_persistence_or_web_layers() -> None:
    """内核也不得 import 本仓库的持久层 / Web 层 / LLM 层 / 服务层。

    禁第三方 ORM 却允许 `from app.db.session import get_session`，等于禁了工具而留着后门。
    这条一起断言，`app.core` 的依赖面才真的收敛到「标准库 + pydantic + 自己」。

    对沙箱（任务 8.1）尤其要紧：沙箱推演全在这一层完成，「沙箱拿不到会话」因此是一个
    包依赖事实，而不是需要 code review 去维护的约定。
    """
    offences = _forbidden_imports(_python_files(CORE_PACKAGE), CORE_FORBIDDEN_APP_LAYERS)
    assert not offences, "确定性内核依赖了外层：\n" + "\n".join(offences)


# --------------------------------------------------------------------------------------
# ② Agent 只经 Tool_Registry 触达能力
# --------------------------------------------------------------------------------------


def test_agents_do_not_import_kernel_services_or_handlers() -> None:
    """`app/agents/**` 不得直连内核、handler、服务或持久层（R22.10）。

    `Tool_Registry.invoke()` 那条通路上串着五件事：白名单、输入 schema、执行、输出
    schema、投影 + 2,000 token 截断。直接 import handler 会把这五件事一次全绕过，而调用
    看起来完全正常——没有异常、没有告警，只是一个不受约束的 Agent 拿到了未截断的全量数据。

    权限隔离不能靠「记得走 registry」，只能靠「没有别的路可走」。
    """
    offences = _forbidden_imports(_python_files(AGENTS_PACKAGE), AGENTS_FORBIDDEN)
    assert not offences, "Agent 绕过 Tool_Registry 直连底层（R22.10 违反）：\n" + "\n".join(
        offences
    )


# --------------------------------------------------------------------------------------
# ③ 网关 URL 只出现在 llm/adapter.py
# --------------------------------------------------------------------------------------


def test_gateway_url_literal_appears_only_in_the_adapter() -> None:
    """全仓库只有 `llm/adapter.py` 能出现网关 URL 字面量（R21.10）。

    「唯一出口」是好几条纪律的共同支点：`DETERMINISTIC_ONLY` 旁路只需在一处生效、token
    记账不会漏账、cassette 录制回放能覆盖所有调用、凭证只经一处读取。第二个出口不会让
    任何测试变红，只会让这四条各漏一个口子。

    前端一并扫描：浏览器里出现网关地址意味着密钥也在浏览器里。
    """
    offences: list[str] = []
    for path in _python_files(APP_ROOT) + _frontend_source_files():
        relative = _relative(path)
        if _is_allowed(relative, GATEWAY_URL_ALLOWED_FILES):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in GATEWAY_URL_PATTERN.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offences.append(f"{relative}:{line} 出现网关地址 {match.group(0)!r}")
    assert not offences, "网关 URL 出现在 adapter 之外（R21.10 违反）：\n" + "\n".join(offences)


def test_gateway_setting_is_read_only_by_the_adapter() -> None:
    """`BEDROCK_GATEWAY_URL` 的读取点同样受控。

    URL 从配置读进来之后，「唯一出口」就不再由字面量位置决定，而由**谁读得到这个配置**
    决定。因此配置名的出现位置一起钉住：`settings.py` 声明字段，`adapter.py` 使用它，
    没有第三处。
    """
    offences: list[str] = []
    for path in _python_files(APP_ROOT):
        relative = _relative(path)
        if _is_allowed(relative, GATEWAY_ALLOWED_FILES):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in GATEWAY_SETTING_TOKENS:
            index = text.find(token)
            if index >= 0:
                line = text.count("\n", 0, index) + 1
                offences.append(f"{relative}:{line} 引用了 {token}")
    assert not offences, "网关配置被 adapter 之外的模块读取：\n" + "\n".join(offences)


# --------------------------------------------------------------------------------------
# ④ 计划状态迁移的调用点集合
# --------------------------------------------------------------------------------------


def test_plan_status_writer_call_sites_are_confined() -> None:
    """`update_plan_status_if_version` 只能被仓储层与应用服务层触及（R11.8、R23.4）。

    R11.8 要求「计划状态不可被绕过审批地修改」。REST 层的两道防护（`PlanUpdateIn` 无
    `status` 字段、`PATCH` 检测到 `status` 键即 403）挡的是外部请求；这一道挡的是内部
    代码——一个 handler 或 Agent 工具悄悄改状态，外部防护一个都不会触发。

    任务 3.3 已把允许集合收紧为恰好一处已落地的调用点 `app/services/approval.py`
    （`Approval_Service`）；design.md §8 指定的另一处 `AutoAppliedChange.revert` 属 P1，
    尚未落地，登记在 `PENDING_STATUS_WRITER_SITES`。此外全部排除。
    """
    offences: list[str] = []
    for path in _python_files(APP_ROOT):
        relative = _relative(path)
        if _is_allowed(relative, STATUS_WRITER_ALLOWED):
            continue
        for line in _references_to(path, STATUS_WRITER):
            offences.append(f"{relative}:{line}")
    assert not offences, (
        f"{STATUS_WRITER} 在允许集合之外被引用（R11.8 绕过防护被破坏）："
        + "\n".join(offences)
        + f"\n允许集合：{list(STATUS_WRITER_ALLOWED)}"
    )


# --------------------------------------------------------------------------------------
# ⑤ 自治策略引擎与 LLM 无 import 边
# --------------------------------------------------------------------------------------


def test_autonomy_engine_has_no_llm_or_agent_imports() -> None:
    """`core/autonomy.py` 的 import 不含任何 LLM / Agent 模块（R13、R22.10）。

    影响分级决定「这个改动能不能自动应用」。如果分级本身可以被模型输出影响，那么
    `IMPACT_MAJOR` 必须上报人工这条规则就变成了模型的建议而非系统的保证。断言的是最强
    的形式：两者之间连 import 边都没有，因此不存在能把模型输出喂进分级的调用形态。
    """
    offences = _forbidden_imports(_python_files(AUTONOMY_MODULE), AUTONOMY_FORBIDDEN)
    assert not offences, "自治策略引擎与 LLM 层产生了依赖：\n" + "\n".join(offences)


# --------------------------------------------------------------------------------------
# 元测试：证明上面的扫描不是在空过
# --------------------------------------------------------------------------------------


def test_scan_reaches_the_real_app_tree() -> None:
    """路径解析正确，扫描确实看到了现有源码。

    `_python_files()` 对不存在的目录返回空列表，这让「包还没写」能正常空过——代价是一个
    写错的根路径也会安静地空过，于是所有断言变成永久绿灯。这里用两个已知存在的文件把
    路径解析钉住。
    """
    scanned = {_relative(path) for path in _python_files(APP_ROOT)}
    assert "app/settings.py" in scanned
    assert "app/db/models.py" in scanned
    assert len(scanned) >= 5, f"app/ 下只扫到 {len(scanned)} 个文件，路径解析可能不对"


def test_absent_scan_targets_are_declared_pending() -> None:
    """缺席的扫描目标必须在 `PENDING_SCAN_TARGETS` 里登记。

    这条把「模块还没写」与「模块被改名或挪走了」区分开。后者会让对应断言从此空过，而
    空过的断言和通过的断言在报告里长得一模一样。
    """
    targets = {
        "app/core": CORE_PACKAGE,
        "app/agents": AGENTS_PACKAGE,
        "app/tools/handlers": APP_ROOT / "tools" / "handlers",
        "app/core/autonomy.py": AUTONOMY_MODULE,
        "app/llm/adapter.py": ADAPTER_MODULE,
    }
    absent = {name for name, path in targets.items() if not path.exists()}
    undeclared = absent - set(PENDING_SCAN_TARGETS)
    assert not undeclared, (
        f"扫描目标缺席且未登记：{sorted(undeclared)}。"
        "若是改名或迁移，请同步更新本文件的路径常量，而不是补登记。"
    )


def test_scanner_detects_synthetic_violations() -> None:
    """扫描器确实会抓到违反——四种 import 形态逐一验证。

    这是整个文件的自证。所有断言当前都作用在近乎空的包上，所以「全绿」既可能意味着没有
    违反，也可能意味着扫描器根本不工作。用一段合成源码把后一种可能排除掉。

    相对 import 单独列出来是因为它最容易被漏：内核里真正会出现的那一行更可能是
    `from ..db.session import get_session`，而不是显眼的 `import sqlalchemy`。
    """
    source = (
        "import sqlalchemy\n"
        "from fastapi import Depends\n"
        "from ..db.session import get_session\n"
        "from . import scheduling\n"
        "import json\n"
    )
    imported = {name.module for name in _imports_in_source(source, package="app.core")}

    assert "sqlalchemy" in imported
    assert "fastapi" in imported
    assert "app.db.session" in imported, "相对 import 未被折算成绝对名"
    assert "app.core.scheduling" in imported, "`from . import x` 未被解析"
    assert "json" in imported, "标准库 import 也该被解析出来（只是不在禁入清单里）"

    # 同一段源码在禁入清单下必须报出违反，且标准库不被误伤。
    tree_offences = [
        name.module
        for name in _imports_in_source(source, package="app.core")
        if any(
            _is_forbidden(name.module, forbidden)
            for forbidden in (*CORE_FORBIDDEN_RUNTIME, *CORE_FORBIDDEN_APP_LAYERS)
        )
    ]
    assert set(tree_offences) == {
        "sqlalchemy",
        "fastapi",
        "fastapi.Depends",
        "app.db.session",
        "app.db.session.get_session",
    }, "禁入判定漏报或误伤（`json` 不得出现在其中）"
    assert not _is_forbidden("app.database", "app.db"), "前缀匹配必须以点为界"


def test_file_level_scan_flags_a_real_violating_module(tmp_path: Path) -> None:
    """整条链路（目录遍历 → 读文件 → 解析 → 判定）在真实文件上确实报错。

    上一条元测试验的是解析器，这一条验的是它前后的管道：`_python_files()` 是否真的枚举到
    了子目录里的文件、`_forbidden_imports()` 是否把行号和禁入项都带进了报错信息。
    """
    package = tmp_path / "core_like"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "scheduling.py").write_text(
        "from __future__ import annotations\nimport httpx\n", encoding="utf-8"
    )
    (package / "pure.py").write_text("import json\n", encoding="utf-8")

    discovered = _python_files(package)
    assert len(discovered) == 3, f"目录遍历漏文件：{discovered}"

    offences = _forbidden_imports(discovered, CORE_FORBIDDEN_RUNTIME)
    assert len(offences) == 1, f"期望恰好一条违反，实际 {offences}"
    assert "scheduling.py:2" in offences[0]
    assert "httpx" in offences[0]


def test_reference_scan_finds_calls_but_not_definitions(tmp_path: Path) -> None:
    """第 ④ 条的引用扫描：抓调用与 getattr，放过定义。

    放过定义不是宽容，而是必要——方法本来就定义在仓储层，把 `def` 算成调用点会让允许集合
    永远得包含定义所在文件，掩盖真正的问题。
    """
    module = tmp_path / "probe.py"
    module.write_text(
        "class Repo:\n"
        f"    def {STATUS_WRITER}(self, plan_id, status, version):\n"
        "        return 1\n"
        "\n"
        "def caller(repo):\n"
        f"    repo.{STATUS_WRITER}(1, 'ACTIVE', 2)\n"
        f"    return getattr(repo, '{STATUS_WRITER}')\n",
        encoding="utf-8",
    )

    assert _references_to(module, STATUS_WRITER) == [6, 7]

    # 允许集合的两种写法：目录前缀与精确文件。
    assert _is_allowed("app/services/approval.py", STATUS_WRITER_ALLOWED)
    assert not _is_allowed("app/tools/handlers/write.py", STATUS_WRITER_ALLOWED)
    assert not _is_allowed("app/api/plans.py", STATUS_WRITER_ALLOWED)
    assert _is_allowed("app/llm/adapter.py", GATEWAY_URL_ALLOWED_FILES)
    assert not _is_allowed("app/llm/cassette.py", GATEWAY_URL_ALLOWED_FILES)


def test_gateway_pattern_matches_realistic_endpoints() -> None:
    """URL 正则对真实形态的端点有效，且不误伤普通地址。

    正则漏掉网关地址这件事同样是安静的：第 ③ 条会通过，而唯一出口已经不唯一了。
    """
    assert GATEWAY_URL_PATTERN.search(
        'BASE = "https://bedrock-runtime.us-east-1.amazonaws.com/model/x/converse"'
    )
    assert GATEWAY_URL_PATTERN.search("url = 'http://gw.internal/bedrock/invoke-model'")
    assert not GATEWAY_URL_PATTERN.search('origins = "http://localhost:5173"')
    assert not GATEWAY_URL_PATTERN.search('docs = "https://fastapi.tiangolo.com/"')
