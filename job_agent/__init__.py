"""job_agent —— 一个「会记事」的求职助手（LangGraph 版）。

模块地图（按阅读顺序）：
    config.py         读 .env / 环境变量，决定用哪个模型、数据存哪儿
    store.py          SQLite 长期记忆：找过的岗位、投递状态、时间线、偏好
    tools.py          交给模型的工具（搜索 / 保存 / 查询 / 改状态 / 记事）
    graph.py          LangGraph 主循环：agent <-> tools，带对话检查点
    cli.py            交互式命令行界面
    memory_view.py    把记忆渲染成人能看的网页 / 终端界面
    forget.py         清除记忆：分范围清空 + 先备份（/forget 和 clear_memory 走这里）
    selftest.py       离线自测：一条命令验证全部功能（内含测试专用的假模型）
    _bootstrap.py     入口自举：让任何启动方式都能用上 .venv 里的依赖

启动方式：在项目根目录执行
    .venv\\Scripts\\python.exe -m job_agent
看它记住了什么：
    /memory          （对话里）
    -m job_agent.memory_view
忘掉记忆：
    /forget 范围      （对话里，会先备份再问你）
    -m job_agent.forget jobs --yes
"""

__version__ = "0.1.0"
