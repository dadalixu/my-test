# my_project 项目长期笔记

## 项目构成
- `job_agent/` — LangGraph 求职助手（2026-09-29 建），根目录只剩它 + `启动.bat`
- `启动.bat` — 双击即用的启动器（自动切目录 + 用 `.venv` 解释器 + 不关窗）
- `.venv` — 项目自带虚拟环境（Python 3.12.10）
- ~~`main.py` / `11.py` / `requirements.txt`~~ — 早期练手文件，2026-09-29 已挪到 `.workbuddy/removed/2026-09-29/`

## job_agent 约定
- **运行方式**：双击根目录 `启动.bat`，或 `.venv\Scripts\python.exe -m job_agent`
  （**必须用 `.venv` 解释器**，系统 python 没装依赖，会报 `No module named 'langchain_core'`）；
  自测 `-m job_agent.selftest`；配置检查 `-m job_agent.config`
- **入口支持多种跑法**：`__main__.py` / `cli.py` / `selftest.py` 顶部有 PEP 366 自救块，
  所以 IDE 里对着文件点运行按钮也能启动（用 `Path(__file__)` 定位，不依赖 cwd）
- **解释器自举**：`cli.py` / `selftest.py` 顶部有 `_bootstrap_deps()`，三层顺序：
  ① 已是 `.venv` 解释器 → 不动；② **版本一致 → 把 `.venv\Lib\site-packages` 挂进 `sys.path`
  （不换进程，IDE 输入输出不受影响）**；③ 版本不一致才 `os.execv` 换解释器兜底。
  **别改回"总是 execv"**：换进程会让 IDE 收不到键盘输入（上一版就栽在这）。
- **IDE 配置**：`job_agent/.vscode/settings.json`（解释器指向 `../.venv`）+
  `launch.json`（F5 调试用，`console: integratedTerminal`，否则不能输入）。
  **用户 VSCode 的工作区根是 `my_project\job_agent`**，所以外层 `.vscode` 不生效
- **环境坑**：本机 `py` 启动器认不出微软商店版 Python（`py -0` 返回 No Installed Pythons Found），
  别建议用 `py -m job_agent`；`.venv` 与系统 Python **同为 3.12.10**
- **依赖清单**：`job_agent/requirements.txt`（langgraph / langgraph-checkpoint-sqlite / langchain-core / langchain-openai / tavily-python），已装在 `.venv`
- **数据落盘**：`job_agent/data/job_memory.sqlite3`（岗位）、`conversation.sqlite3`（对话检查点），已 gitignore
- **加新功能**：只在 `tools.py` 里加 `@tool` 函数并追加到 `ALL_TOOLS`，图不用改
  （目前 10 个工具；增删工具要同步改 `selftest.py` 里「注册了 N 个工具」那条断言）
- **清除记忆**：`forget.py` —— 分范围清空 + 删前自动备份。三个入口共用它：
  ① `/forget 范围`（CLI，列清单 → 敲 yes）；② `clear_memory` 工具（模型调，**必须 `confirm=True`** 才动手）；
  ③ `python -m job_agent.forget 范围 --yes`（脚本）。
  范围 jobs / profile / conversations / all，`normalize_scope()` 认中文别名（"岗位"/"对话记忆"）。
  备份落 `data/backup/memory_backup_<时间戳>.json`（随 `data/` 一起 gitignore）。
  **两个易踩点**：清 jobs 时必须连 `sqlite_sequence` 一起清，否则清空后新岗位从 #57 开始；
  `conversations` 在另一个库（`conversation.sqlite3` 的 `checkpoints`/`writes` 表），store.py 碰不到
- **看记忆（记忆可视化）**：`/memory`（终端）/ `/memory html`（生成 `记忆总览.html` 到项目根目录，双击即看），
  或直接 `-m job_agent.memory_view`。实现分两层：`collect()` 读数据 → `render_text()` / `render_html()` 渲染；
  对话记忆从 `SqliteSaver` 的检查点里读，读不到就跳过、不报错
- **新入口文件的自举**：顶部写 PEP 366 自救块，再 `from ._bootstrap import bootstrap; bootstrap(__file__)`，
  等价于 `cli.py` / `selftest.py` 里那份内联实现（换解释器兜底时会转发 `sys.argv[1:]`，
  别丢掉这行，否则命令行开关会在重启后失效）
- **状态取值**：new / interested / applied / interview / offer / rejected / archived
- **Key 来源**：系统环境变量 `DEEPSEEK_API_KEY` + `TAVILY_API_KEY`（项目内无 .env，也不需要）。
  配置优先级：**系统环境变量 > 项目根 `.env` > 默认值**；
  想在 `.env` 里覆盖系统的 Key 必须用 `JOB_AGENT_API_KEY`（读取顺序排第一）
- **配置自检**：`python -m job_agent.config` 会逐项打印「值 + 来源」（含脱敏后的 Key），
  改配置不生效时先跑它看箭头指向
- **没有兜底降级（2026-09-29 第二轮清理，用户明说"删掉"）**：早先有两套「没 Key 怎么办」的逻辑，全删了——
  ① 大模型 Key 缺失：`MISSING_KEY_MESSAGE` / `Settings.has_key` / `model_label` 分支 / `cli.py` 启动拦截（源头是最早的离线假模型 → 改报错 → 现连报错也删）；
  ② 搜索 Key 缺失：`tools.py` 的 `SAMPLE_JOBS`（10 条内置示例岗位）+ `_match_samples()` 降级。
  现在：Key 读不到 = 直接 401 或搜索返回可读失败信息，靠 `python -m job_agent.config` 查来源定位。
- **自测是改代码后的第一道关**：`selftest.py` 用临时库 + 注入假模型（`FakeToolCallingModel`），不联网不花钱。
  联网那一步用 `_fake_tavily_search` 顶替（6 条 `example.invalid` 假岗位），
  保住「搜索→清洗→入库→去重」链路；测「搜不了」时 `try/finally` 临时换回 `_REAL_TAVILY_SEARCH`。
  假岗位**只留 6 条不要减**：`test_tools` 用完 3 条后，`test_graph_loop` 还要有新的可入库，
  否则「这轮搜索写进了记忆库」会因去重而假失败。

## 用户偏好（本项目内）
- 改代码前先给方案；改动尽量落在新文件上，不覆盖已有文件
- 输出偏好表格化、结构化；插图统一「正规简约」风格、PNG+SVG 双交付、放 `outputs/`
