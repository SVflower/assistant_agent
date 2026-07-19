"""Agent 内核：循环、上下文、提示词。

子模块不在包初始化时 eager import，避免 execution adapter 导入 run ports 时形成环。
"""
