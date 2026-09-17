"""静态提示词常量，每 Agent 一个模块（任务 5.6）。

提示词是**静态字符串常量**，无运行时插值，固定 6 段
`[ROLE] [AUTHORITY] [PROTOCOL] [TOOLS] [DATA_RULES] [OUTPUT]`；段 2/3/5 共享常量。
这保证静态前缀在同一 Agent 各轮逐字节相同，从而 `assemble_messages` 是纯函数、
输出可逐字节断言（design.md §2.1）。
"""
