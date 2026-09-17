"""形态 A：确定性流水线。固定顺序语句，无「LLM 选择下一个工具」环节。

`plan_generation.py`（任务 2.12）的 6 步序列由示例测试锁定：
`load_snapshot → generate_schedule → check_constraints → evaluate_schedule
→ compute_baseline → save_proposed_plan`。
"""
