# job_agent —— 会记事的求职助手

一个用 **LangGraph** 写的找工作的 Agent。三个需求对应三件事：

| 需求 | 实现方式 | 代码位置 |
|---|---|---|
| 1. 能记住找过的工作 | SQLite 长期记忆（岗位去重入库 + 状态 + 时间线） | `store.py` |
| 2. 能交互 | 流式命令行对话 + 斜杠命令 | `cli.py` |
| 3. 用 LangGraph | `agent ↔ tools` 循环图 + 对话检查点 | `graph.py` |

### 每个文件是干什么的

| 文件 | 作用 |
|---|---|
| `__main__.py` | 入口，让 `python -m job_agent` 能启动（也支持被直接运行） |
| `config.py` | 读环境变量 / `.env`，决定用哪个模型、数据存哪儿 |
| `store.py` | 长期记忆：岗位去重、状态流转、时间线、偏好（SQLite） |
| `tools.py` | 模型能调的 10 个工具，外加网页结果清洗 |
| `graph.py` | LangGraph 主循环 + 对话检查点 |
| `cli.py` | 交互界面 + 斜杠命令 |
| `memory_view.py` | 把记忆渲染成人能看的网页 / 终端界面（`/memory` 走这里） |
| `forget.py` | 清除记忆：分范围清空、先备份（`/forget` 和 `clear_memory` 走这里） |
| `selftest.py` | 离线自测（内含测试专用的假模型） |
| `_bootstrap.py` | 入口自举：让任何启动方式都能找到 `.venv` 里的依赖 |
| `requirements.txt` | 依赖清单 |
| `README.md` | 本文件 |

另外项目根目录（`my_project/`）下有个 `启动.bat`，双击即可运行——它不是代码，只是个启动器。

---

## 一、安装（复制粘贴即可）

在项目根目录 `C:\Users\谢力\Desktop\my_project` 下打开终端：

```bash
# 1) 装依赖（装进项目自带的 .venv，不污染全局）
.venv\Scripts\python.exe -m pip install -r job_agent\requirements.txt

# 2) 验证装好了
.venv\Scripts\python.exe -c "import langgraph, langchain_openai; print('OK')"
```

> 不想装 `tavily-python` 也行，那只影响联网搜岗位，其余功能照常。

## 二、配置 API Key

Key 放在两个地方之一，配一处就够。读取顺序（**谁在前谁说了算**）：

```
系统环境变量  >  项目根目录的 .env 文件  >  代码默认值
```

### 方式 A：系统环境变量（一次配好，所有项目通用）

**图形界面**：`Win + R` → 输入 `sysdm.cpl` → 「高级」→「环境变量」→
在上半部分「用户变量」里新建或编辑 `DEEPSEEK_API_KEY` / `TAVILY_API_KEY` → 一路确定。

**命令行**（等价，改的是用户级）：

```bash
setx DEEPSEEK_API_KEY "sk-你的key"
setx TAVILY_API_KEY "tvly-你的key"        # 可选，去 tavily.com 免费领
```

> 两种方式改完都必须**重开终端**（已经开着的窗口不会自动更新）。

### 方式 B：项目根目录建一个 `.env`

路径就是 `C:\Users\谢力\Desktop\my_project\.env`（和 `job_agent` 文件夹同级，**不是**放在 `job_agent` 里面）：

```ini
DEEPSEEK_API_KEY=sk-你的key
TAVILY_API_KEY=tvly-你的key
JOB_AGENT_MODEL=deepseek-chat
JOB_AGENT_BASE_URL=https://api.deepseek.com
```

> `.env` 已写进 `.gitignore`，不会被提交。

### ⚠️ 最容易踩的坑：`.env` 改不动系统里已有的 Key

因为**系统环境变量优先级更高**。如果你 `DEEPSEEK_API_KEY` 已经在系统变量里配过，
再往 `.env` 里写一个 `DEEPSEEK_API_KEY=新key` 是**不生效的**，程序还是用系统那个。

想在项目里单独用另一个 Key，就写 **`JOB_AGENT_API_KEY`**——它的优先级最高，专门用来盖过系统变量：

```ini
JOB_AGENT_API_KEY=sk-这个才是真正生效的
```

### 验证配置读到了没（含「每个值是从哪读来的」）

```bash
.venv\Scripts\python.exe -m job_agent.config
```

输出会告诉你每个配置项的实际来源，例如：

```
  API Key    : sk-820...（长度 35）
               <- 系统环境变量 DEEPSEEK_API_KEY
  Base URL   : https://api.deepseek.com
               <- 默认值（没配置过）
```

改完配置不生效时，先跑这条命令，看箭头指的到底是哪一份。

> 程序只负责读配置，**不做任何兜底**：Key 读不到就直接表现为调用失败（401）。
> 所以改完配置先跑上面那条命令确认来源，别等提问时才踩坑。

## 三、启动（三种方式任选）

**方式 1 —— 双击（最省事）**

双击项目根目录的 `启动.bat`。它会自己切到项目目录、自动用 `.venv` 里的解释器，
所以不存在"用了错的那个 Python"的问题。

**方式 2 —— 命令行**

```bash
cd C:\Users\谢力\Desktop\my_project

# 先自测：不联网、不花钱，用临时数据库跑 7 组共 63 项检查
.venv\Scripts\python.exe -m job_agent.selftest

# 启动对话
.venv\Scripts\python.exe -m job_agent
```

> **PowerShell 用户注意**：必须写成 `.\.venv\Scripts\python.exe -m job_agent`，
> 前面那个 `.\` 不能省。不写会提示「无法将...项识别为 cmdlet」。
> 想省事就用方式 1 或方式 3。

**方式 3 —— IDE 里点运行按钮**

直接对 `job_agent/cli.py` 或 `job_agent/__main__.py` 点运行即可。这两个文件开头有三层保护：

1. **包路径自救**：被直接执行时自动补上包名和项目根目录，绕开相对导入的限制；
2. **借用 `.venv` 的依赖**：IDE 用的解释器往往不是项目的 `.venv`（这是最常见的报错原因）。
   如果两个 Python **版本一致**，就把 `.venv` 的包目录挂进当前进程——**不换进程**，
   所以 IDE 控制台的输入输出完全正常；
3. **换解释器兜底**：只有版本不一致时才换成 `.venv` 的解释器重新执行
   （跨大版本加载 C 扩展会崩，没有别的办法）。代价是调试器会断开。

> **为什么不是「总是换解释器」？** 因为替换进程之后，VSCode 的调试控制台
> 就收不到键盘输入了，表现就是「程序能跑，但打不了字」。

**VSCode 用户建议**：用 VSCode 打开 **`my_project`** 这个文件夹，
而不是里面的 `my_project\job_agent`。当项目根是 `job_agent` 时，
`my_project\.vscode\settings.json` 里的解释器设置**不会被读取**。
（`job_agent\.vscode\` 下也放了一份等效配置，所以不换也能用，
但打开外层目录更省事——`启动.bat` 也在那里。）

**要在 IDE 里调试**：项目自带 `job_agent\.vscode\launch.json`，
F5 时选「运行 job_agent（用 .venv）」，它已经配好了 `.venv` 解释器和
`integratedTerminal`（这样调试时也能输入）。

### 想手动把 IDE 解释器指到 `.venv`

**VSCode**：`Ctrl + Shift + P` → 输入 `Python: Select Interpreter` → 选
`./.venv/Scripts/python.exe`（项目里那个带 `.venv` 前缀的）。

**PyCharm**：`File` → `Settings` → `Project: my_project` → `Python Interpreter`
→ 齿轮 → `Add Local Interpreter` → `Existing` → 选
`C:\Users\谢力\Desktop\my_project\.venv\Scripts\python.exe`。

### 启动不了时按这个顺序查

| 症状 | 原因 | 解决 |
|---|---|---|
| `[致命错误] 图构建失败：ModuleNotFoundError: No module named 'langchain_openai'` | IDE 点运行用的解释器不是 `.venv`（依赖装在 `.venv`） | **已自动修复**：入口检测到后会自己切到 `.venv`。仍失败就按上面手动选解释器 |
| `ModuleNotFoundError: No module named 'langgraph'` | 同上，但 `.venv` 可能不存在 | 建环境：`python -m venv .venv`，再 `pip install -r job_agent\requirements.txt` |
| `attempted relative import with no known parent package` | 直接跑了包内的文件（旧版本才会） | 已修：现在直接运行也可以 |
| `No module named job_agent` | 当前目录不对（比如已经 cd 进了 job_agent 里面） | `cd` 回项目根目录 `my_project` |
| `无法将".venv\Scripts\python.exe"项识别为...` | PowerShell 缺 `.\` 前缀 | 写成 `.\.venv\Scripts\python.exe` |
| 窗口一闪就没了 | 双击 `.py` 文件、报错来不及看 | 用 `启动.bat`（它不会自动关窗） |
| **程序跑起来了，但打不了字** | 旧版本会「换解释器重启进程」，而替换进程会让 IDE 收不到键盘输入 | 已修：版本一致时改成「借用包目录」，不再换进程。用 F5 调试则走 `launch.json`（已配 `integratedTerminal`） |
| 提示 `Can't find a default Python` | 用了 `py` 命令，但你的 Python 是微软商店版，`py` 启动器认不出 | 改成 `.venv\Scripts\python.exe`，或直接双击 `启动.bat` |

## 四、怎么用

直接用中文聊：

```
帮我搜一下武汉的桥梁设计岗位
我之前看过哪些工作？
#3 改成已投递，备注：9/29 投的
统计一下我的求职进展
记住：只看武汉和宜昌的岗位，薪资不低于 12k
```

斜杠命令不消耗 token，直接操作数据库：

```
/help             帮助
/list applied     列出已投递的岗位
/find 中交        在记忆库里搜
/detail 3         看 #3 的完整信息和时间线
/status 3 interview
/stats            统计
/history          最近操作记录
/remember 目标城市=武汉
/export           导出 CSV（Excel 可打开）
/memory           看它到底记住了什么（终端版）
/memory html      生成一个网页，双击就能看（推荐）
/forget jobs      清空记忆：jobs / profile / conversations / all
                  会先备份到 data/backup/，再要你敲 yes 确认
/new              开一段新对话（岗位记忆保留）
/quit
```

### 怎么让它忘掉

记忆不能只增不减。清空走 `/forget`，**分范围**，因为多数时候你只是想换个话题重新聊，
并不想丢掉投递履历：

```bash
/forget conversations   # 只清聊天记录，岗位和偏好都留着
/forget jobs            # 清空岗位记忆 + 操作时间线
/forget profile         # 清掉长期偏好
/forget all             # 全清
```

两步保护，缺一不可：

1. **先备份**：范围内的内容先导成一份 JSON 落到 `data/backup/memory_backup_<时间戳>.json`，
   反悔了能人工翻回来、也能自己照着重建。
2. **再确认**：屏幕会列出"将要删掉 N 条"，要你敲 `yes` 才执行（输别的字符就是取消）。

不想进对话也能清：

```bash
.venv\Scripts\python.exe -m job_agent.forget jobs         # 只提示，不动手
.venv\Scripts\python.exe -m job_agent.forget jobs --yes   # 真删
```

你也可以直接用中文说「把之前搜的岗位忘掉」——模型会调 `clear_memory`，
但**它必须先把清单念给你、等你说"确认"才动手**（这条写进了 system prompt，也实测过）。

## 五、三层记忆是怎么设计的

1. **对话记忆**：LangGraph 的 checkpointer（`SqliteSaver`）按 `thread_id` 保存消息，
   关掉程序再打开还能接着聊。`/new` 换 `thread_id` 就是新对话。
2. **岗位记忆**：所有搜到的岗位自动写入 `job_memory.sqlite3`，去重分**两级**：
   - 精确命中「公司 + 职位 + 城市」→ 只累加 `seen_count`，**同一个岗位看十次也只有一条记录**；
   - 精确没命中但**职位标题相同** → 合并到已有那条。

   第二级是为联网搜索准备的：网页搜索只能拿到标题（公司往往为空），
   之后你用 `save_job` 把公司、城市、薪资补全时，会落到**同一条记录**上，
   而不会变成"同一岗位两条记录"。

   每次状态变化都往 `events` 表写一条，形成时间线（何时发现、何时投递、何时约面）。
3. **偏好记忆**：`profile` 表存键值对，每次组装 system prompt 时注入，
   所以它「记得你说过只看武汉」。

数据默认放在 `job_agent/data/`，想换位置就设环境变量 `JOB_AGENT_DATA_DIR`。

### 怎么看它到底记住了什么

三层记忆默认都躺在 `.sqlite3` 文件里，双击打不开。所以有个专门的可视化入口：

```bash
# 方式一：对话里直接敲（终端版，简洁）
/memory

# 方式二：生成网页（推荐，有排版、能直接双击打开）
/memory html

# 方式三：不启动对话，单独跑
.venv\Scripts\python.exe -m job_agent.memory_view
```

生成的 `记忆总览.html` 放在项目根目录，页面里分五块：

| 区块 | 内容 |
|---|---|
| 1 岗位记忆 | 它记住的每个岗位（职位/公司/城市/薪资/状态/出现次数） |
| 2 求职进展 | 各状态的数量分布 |
| 3 操作时间线 | 什么时候看到的、什么时候投的 |
| 4 长期偏好 | 你让它记住的偏好 |
| 5 对话记忆 | 它记得你们聊过什么（从对话检查点里读出来的） |

> 这页是**只读快照**，看完就关掉，不会影响数据库。想刷新就再跑一次命令。

### 联网搜索结果是怎么变干净的

Tavily 返回的只是网页标题和摘要（例：`桥梁设计师招聘_武汉理工大设计研究院有限公司 - 建筑英才网`），
直接用会毁掉记忆库。所以 `search_jobs` 里做了一道清洗管线：

| 步骤 | 做什么 | 例子 |
|---|---|---|
| 1 抽公司名 | 正则找「xx有限公司 / xx设计院 / xx工程局」 | → `武汉理工大设计研究院有限公司` |
| 2 洗标题 | 去掉公司名、站点名、招聘噪音词、城市前缀、半截括号 | → `桥梁设计师` |
| 3 补字段 | 从摘要里抠城市（`工作地区：湖北-武汉市`）和薪资（`5-10K`） | → 城市=武汉，薪资=5-10K |
| 4 挡噪音 | 不含「工程师/设计师/经理/施工/技术…」的标题判为网站首页，直接丢弃 | 丢掉 `人才`、`建筑英才网` |

所以联网结果的**公司 / 城市 / 薪资是"推断值"，可能不准**——工具会主动提醒模型这一点，
并让它在跟你确认后用 `save_job` 修正（修正会合并，不会重复）。想要准确的结构化数据，
就配上 `TAVILY_API_KEY` 之后自己确认一下关键岗位。

## 六、想自己加功能？

加一个工具只要三步，在 `tools.py` 里：

```python
@tool
def my_tool(arg: str) -> str:
    """工具说明（模型靠这段话决定什么时候用它，一定要写清楚）。"""
    return "结果"
```

然后把它加进文件最后的 `ALL_TOOLS` 列表即可，图里不用改任何东西。

> **如果新工具会删数据 / 覆盖数据**，学 `clear_memory` 留两道保险：
> 加一个 `confirm: bool = False` 参数（第一次调用只返回"将要删什么"的清单，
> 让模型先跟用户确认），以及动手前先备份。删东西的工具是最容易被模型误用的，
> 光靠 system prompt 里写"要小心"不够。

几个现成的扩展方向：
- 简历匹配打分：读 `resume.pdf`，把岗位描述丢给模型算匹配度
- 定时跑一遍搜索：新增岗位用 `events` 表比对，有新的就提醒
- 自动生成投递邮件草稿

## 七、常见问题

**Q: 报 `ModuleNotFoundError: No module named 'langgraph'`**
装的解释器和运行的不是同一个。用 `.venv\Scripts\python.exe` 全程一致。

**Q: 提示 `openai.AuthenticationError` / 401**
Key 没读到或写错了。用 `python -m job_agent.config` 看它到底读到了什么——
「配置来源」那几行会直接指出每个值是来自系统环境变量还是 `.env`。
`setx` 改过的变量必须**重开终端**才生效。

**Q: 想清空记忆重新开始**
用 `/forget all`（先自动备份、再要你敲 yes）。也可以只清一部分，比如 `/forget conversations`
只清聊天记录、履历留着。直接删 `job_agent\data\` 文件夹当然更彻底，但那样连备份都没有，
下次启动得重新建库——除非你确定什么都不要了，否则不推荐。

**Q: 搜出来的公司/城市不准，或者有个别结果看着不像岗位**
联网结果的字段是从网页标题推断的，属于预期内。你可以直接说
「#3 的公司是 XX 设计院，薪资 12-18k」，它会用 `save_job` 修正并**合并到原记录**。
如果看到明显是网站首页的结果，直接说「#7 删掉」即可让它把状态改成 `archived`。

**Q: 中文在终端里乱码**
本项目已自动把 stdout 切成 UTF-8。若仍乱码，在终端执行 `chcp 65001`。
