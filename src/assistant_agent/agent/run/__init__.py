"""Run 聚合、预算、恢复与 checkpoint。"""

# 子模块刻意不在包初始化时 eager import，避免 execution control 实现导入端口时形成环。
