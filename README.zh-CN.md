![Banner](assets/banner.png)

# Memorandum Message Collector

[![CI](https://github.com/shiryavsky/memorandum/actions/workflows/python-app.yml/badge.svg)](https://github.com/shiryavsky/memorandum/actions/workflows/python-app.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**语言:** [English](README.md) · [Deutsch](README.de.md) · [Русский](README.ru.md) · 简体中文

不用再翻五个聊天客户端去找某人上周二说过什么。Memorandum 把 **Mattermost、Telegram、Pachca 和 IMAP 邮件** 聚合到本地可搜索数据库，并通过 MCP 工具暴露 —— 这样 **Claude、Gemini、Hermes** 等 MCP 客户端就能跨所有工作对话回答你的问题。

## 像这样问 Claude

> *"总结一下平台团队这周对 `PL-15491` 的讨论。"*
>
> *"昨天有人 @ 我聊迁移的事吗？"*
>
> *"找出 Marina 周二发的那份表格，把 Q3 的数据拉出来。"*
>
> *"给客户最后一封关于上线日期的邮件写个回复草稿。"*

Memorandum 在本地运行 —— 你的消息和附件永远不会离开本机，智能体只通过 MCP 与它们交互。

## 功能特性

**数据源与同步**
- 从 **Mattermost、Telegram、Pachca、IMAP** 拉取数据 —— 每种类型支持多账号，独立命名
- 按源增量同步；并发抓取（单个源失败不影响其他源）
- 在 ingest 时**捕获文件附件** —— 对 Pachca 和 Telegram 至关重要，它们的 URL 会过期
- 每个源独立的 YAML 过滤器：跳过机器人、频道和正则模式

**搜索与检索**
- **两层存储** —— SQLite 用于结构化查询（发送者 / 频道 / 时间范围），ChromaDB 用于语义搜索
- **实时增量读取** —— `get_new_messages` 直接打到源，让智能体看到频道分钟级最新内容
- **会话重建** —— `get_thread` 返回根消息 + 所有回复，包括跨 IMAP 文件夹
- **YouTrack issue 链接** —— 从 URL 和频道名中解析 issue id；`find_by_issue` 返回所有引用某个 id 的内容
- 每条结果都附 **永久链接** —— 点击即可跳回原始消息

**人员与身份**
- **跨源别名**，可选 role / team / reports-to / responsible-for —— 智能体从第一次会话起就知道谁是谁
- **智能体可写别名** —— Claude 能把它了解到的关于人的事（角色变动、新项目）直接持久化到 `config.yaml`（round-trip 保留你的注释）
- **内部 vs 外部** 分类（源标志 → 邮箱域名 → 单别名覆盖）；外部发送者会被打上 `[external]` 标签
- **@ 关系图** —— `who_mentioned` 回答"本周谁 @ 了我 / Alice"，自动做别名解析

**运维**
- **MCP 服务器** 提供搜索、摘要、digest、会话、issue 查找和文件访问等工具
- **回发消息**（按源选择性开启）—— 支持 Telegram business 聊天；邮件回复落到你的 Drafts 文件夹供审阅
- **保留 / housekeeping** —— 自动清理旧消息和向量；内容寻址的附件清扫机制保留所有仍被引用的文件
- **CLI**: `./bin/memorandum {health, dashboard, aliases refresh, prune, reindex-chroma}` —— 实时终端 TUI 加运维工具

实现细节（架构、表结构、同步内部机制）见 [AGENTS.md](AGENTS.md)。

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/shiryavsky/memorandum.git
cd memorandum
```

### 2. 安装（macOS/Linux）

```bash
./setup.sh
```

`setup.sh` 会创建 `.venv`、安装 Python 依赖，并在首次运行时从 `config.example.yaml` 引导生成 `config.yaml`。在 Linux 上会从 [PyTorch CPU 索引](https://download.pytorch.org/whl/cpu) 预装仅 CPU 的 PyTorch（FlagEmbedding 的传递依赖），从而跳过约 1.3 GB 的 CUDA 包；macOS 上的 torch 本来就是 CPU 版本。

默认安装的是多语言通用 embedding 模型 —— 中文开箱即用。

### 3. 配置

配置分两份文件：

- **`config.yaml`**（在项目目录下，已 gitignore）—— 结构、过滤器、别名、retention。
- **`/etc/memorandum/secrets.yaml`**（`chmod 600`）—— 各源的 token / 密码。放在项目目录之外，这样沙箱在 `~/` 的 agent（Claude Desktop / Claude Code / 任何 filesystem-MCP）就读不到。详见下方 [为什么单独建 secrets 文件](#为什么单独建-secrets-文件)。

**一次性配置 secrets 文件：**

```bash
sudo mkdir -p /etc/memorandum
sudo install -m 600 -o "$USER" secrets.example.yaml /etc/memorandum/secrets.yaml
sudo "$EDITOR" /etc/memorandum/secrets.yaml
```

然后编辑 `config.yaml`（步骤 2 中从 `config.example.yaml` 生成），添加你的源：

```yaml
sources:
  company_mattermost:
    type: mattermost
    enabled: true
    url: "https://mattermost.yourcompany.com"
    # token 从 /etc/memorandum/secrets.yaml 读入
    internal: true                        # 这里的发送者视为公司员工（外部人员会被打上 [external] 标签）
    allow_send: false                     # 默认；设为 true 允许 send_message 工具在此发帖
    filters:
      skip_senders: ["github-bot"]
      skip_channels: ["off-topic"]
      skip_patterns:
        - "^Reminder:"
        - "joined the channel"

  work_telegram:
    type: telegram
    enabled: true
    # token 从 secrets.yaml 读入 —— bot 由 @BotFather 创建

  work_pachca:
    type: pachca
    enabled: true
    # token 从 secrets.yaml 读入 —— Pachca 设置中的 Automations → API
    filters:
      skip_channels: ["random"]

display_timezone: "America/New_York"   # MCP 输出中的时间戳

# 可选：将类似 "PL-15491" 的 YouTrack issue 链接和频道名做分类。
# 省略此块可禁用 issue id 检测（URL 仍会以通用方式提取）。
youtrack:
  base_url: "https://youtrack.yourcompany.com"
  project_prefixes: [PL, DEMO, MOBILE]

# 当前用户（始终视为内部）。使用裸用户名，不带前导 "@"。
my_aliases:
  - "you"
  - "you.lastname"

# 其他人的规范身份。role / team / reports_to / responsible_for
# 都是可选的，通过 MCP 工具 `get_user_aliases` 暴露。
user_aliases:
  - canonical_name: "Jane Smith"
    internal: true
    role: "Backend lead"
    team: "Platform"
    responsible_for: ["dev-pl", "PL-*"]
    aliases: ["jane", "jsmith"]
```

`/etc/memorandum/secrets.yaml` 里的源名要和上面的 config 完全对应：

```yaml
sources:
  company_mattermost:
    token: "PAT-paste-your-mattermost-token-here"
  work_telegram:
    token: "123456:AABBcc..."
  work_pachca:
    token: "your-pachca-token"
```

要覆盖默认路径（测试 / 开发 / 无 sudo 的环境），在 `config.yaml` 里设 `secrets_path:` 或导出 `MEMORANDUM_SECRETS_PATH`。文件缺失是 OK 的 —— 需要凭据的连接器会在 connect 阶段给出明确报错。

#### 为什么单独建 secrets 文件

MCP 服务器以你身份运行，能读你能读的一切。**agent**（Claude Desktop / Claude Code / 你接进来的任意 filesystem-MCP）通常被沙箱限制在项目或 home 目录里。把凭据放在 `/etc/` 下，它们就物理性地落在那条 allowlist 之外了 —— 误用或将来出现的 filesystem 工具 grep 不到，agent 自己的 path traversal 也跳不出沙箱根。这不是 UNIX 权限边界，而是沙箱边界 —— 但这正是 agent 实际遵守的那条边界。

提示：ingest 跑几周后运行 `./bin/memorandum aliases refresh` —— 它会为每个尚未在 `user_aliases` 中的发送者打印 stub 条目，按消息数排序并标注来源。复制你关心的条目，并手动补上 `role`/`team`/`internal`。

### 4. 首次 ingest

```bash
./run_ingest.sh --hours 720  # 拉取过去 30 天
```

### 5. 健康检查

首次 ingest 后，验证一切是否正常 —— 源是否连上、消息是否入库、embedding 是否生成：

```bash
./bin/memorandum health
```

注册服务器后，相同报告也可通过 MCP 工具 `get_health` 获取。

### 6. 启动调度器（每 15 分钟运行一次）

**Linux + systemd（推荐用于生产）：**
```bash
sudo cp systemd/memorandum-collect.service /etc/systemd/system/
sudo cp systemd/memorandum-collect.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memorandum-collect.timer
```

**macOS 或非 systemd 环境：**
```bash
./bin/memorandum-sync
```
配合 `cron` / `launchd` 即可周期化。脚本会取 `/tmp/memorandum-sync.lock`，所以并发触发也是安全的。

### 7. 注册 MCP 服务器

#### Claude 端：

添加到 Claude MCP 配置（`~/.config/claude/mcp_servers.json`）：

```json
{
  "memorandum": {
    "command": "/path/to/memorandum/.venv/bin/python",
    "args": ["/path/to/memorandum/mcp_server/server.py"],
    "cwd": "/path/to/memorandum",
    "timeout": 120
  }
}
```

#### Hermes 端：

添加到 Hermes 配置（`~/.hermes/config.yaml`）：

```yaml
mcp_servers:
  memorandum:
    command: /path/to/memorandum/.venv/bin/python
    args:
      - /path/to/memorandum/mcp_server/server.py
      - --config
      - /path/to/memorandum/config.yaml
    timeout: 120
```

`--config` 参数确保即使工作目录失效，服务器也能找到 `config.yaml`。

### 8.（可选）实时仪表盘

ingest 进入定时运行后，终端 TUI 可以一屏展示存储、ingest 健康度、@ 提及、发送活动以及 MCP 工具使用情况 —— 放在 tmux 面板里很方便：

```bash
./bin/memorandum dashboard
```

每 5 秒刷新；按 `q` 退出。

![Dashboard Screenshort](assets/dashboard.png)

## 项目结构

```
memorandum/
├── config.yaml              # 非敏感设置（源、过滤器、别名）—— gitignored
├── config.example.yaml      # 配置示例
├── secrets.example.yaml     # /etc/memorandum/secrets.yaml 的模板（chmod 600；纳入 git —— 不含真凭据）
├── requirements.txt         # Python 依赖
├── requirements-dev.txt     # 开发依赖（pytest, pytest-cov, responses）
│
├── connectors/                  # 源连接器
│   ├── CONTRIBUTING.md          # ★ 如何添加新连接器 —— 扩展本目录前必读
│   ├── _common.py               # 共享常量（inline 预览大小、默认文本扩展名集合）
│   ├── factory.py               # build_connector —— ingest 与 MCP 共用的单一构造点
│   ├── mattermost_connector.py  # Mattermost REST API（按频道同步）
│   ├── telegram_connector.py    # Telegram Bot API（群组、频道、business 消息；跳过与机器人的私聊）
│   ├── pachca_connector.py      # Pachca REST API（按频道游标同步）
│   └── email_connector.py       # IMAP（文件夹即频道；Message-ID 线索；发送 = 草稿）
│
├── pipeline/                # ingest 引擎（在 systemd 下运行）
│   ├── ingest.py            # 编排 fetch → filter → store，每个源一个连接器
│   ├── format.py            # 规范的消息渲染器（MCP 服务器与 dashboard 共用）
│   ├── health.py            # 健康报告构建与格式化（CLI 与 MCP 共用）
│   ├── alias_resolver.py    # 从 user_aliases 配置中做规范身份解析
│   └── filter_engine.py     # 基于 YAML 的按源过滤
│
├── cli/                     # 面向用户的 CLI 工具（`python -m cli ...` / `bin/memorandum`）
│   ├── __main__.py          # argparse 调度器
│   ├── health.py            # `memorandum health` —— 封装 pipeline.health
│   ├── aliases.py           # `memorandum aliases refresh` —— append-only stub 生成器
│   ├── alias_writer.py      # 共享的 YAML round-trip 层（refresh + MCP 写入工具使用）
│   ├── prune.py             # `memorandum prune` —— retention 的 dry-run 预览 / --commit
│   ├── dashboard.py         # `memorandum dashboard` —— 实时 rich TUI（只读 DB 连接）
│   └── reindex.py           # `memorandum reindex-chroma` —— 清空并从 SQLite 重建 chroma
│
├── storage/                 # 存储层
│   ├── db.py                # SQLite 元数据存储
│   └── vector_store.py      # ChromaDB embedding
│
├── mcp_server/              # MCP 服务器
│   ├── server.py            # 应用 + 调度器 + accessors + main
│   ├── schemas.py           # Claude 自省时看到的 Tool() 声明
│   ├── projectors.py        # tool_calls 审计日志的逐工具参数脱敏
│   └── tools/               # 按领域拆分（search、digests、channels、threads、
│       │                    # identity、files、info）；平铺的 TOOL_HANDLERS 注册表
│       └── …
│
├── data/                    # 本地存储（gitignored）
│   ├── messages.db          # SQLite 数据库
│   ├── chroma/              # ChromaDB 持久化
│   └── attachments/         # 已下载的消息附件
│
├── systemd/                         # Linux 部署
│   ├── memorandum-collect.service   # Systemd oneshot 服务
│   └── memorandum-collect.timer     # Systemd 定时器（每 15 分钟）
|
├── bin/                     # 脚本
│   ├── memorandum-sync      # 带 lock 保护的主同步脚本
│   └── memorandum           # CLI 包装器 —— 在 venv 中执行 `python -m cli "$@"`
│
├── tests/                   # 单元测试（pytest）
│   ├── conftest.py          # 共享 fixture
│   ├── test_config.py
│   ├── test_filter_engine.py
│   ├── test_db.py
│   ├── test_server.py
│   ├── test_ingest.py
│   ├── test_mattermost_connector.py
│   ├── test_telegram_connector.py
│   ├── test_pachca_connector.py
│   ├── test_alias_resolver.py
│   ├── test_health.py
│   ├── test_youtrack_helpers.py
│   ├── test_cli_main.py
│   └── test_cli_aliases.py
│
├── setup.sh                 # macOS/Linux 安装脚本
├── run_ingest.sh            # 一次性 ingest 测试
└── README.md
```

## 可用工具（MCP）

| 工具                  | 描述                                                          |
| -------------------- | ----------------------------------------------------------- |
| `search_messages`    | 按关键词或语义搜索                                              |
| `summarize_channel`  | 获取某频道的消息用于总结                                          |
| `summarize_messages` | 在灵活的时间范围（小时/天）内做消息 digest                          |
| `list_channels`      | 列出数据库中已知的频道（id + 名称 + 描述）                          |
| `get_new_messages`   | 直接从源（所有源）拉取比 DB 更新的频道消息                           |
| `get_thread`         | 按 `thread_id` 重建完整会话（根 + 回复）                           |
| `get_stats`          | 每个已配置源的消息统计                                          |
| `get_attached_file`  | 按 file_id 获取文件内容（Telegram、Mattermost、Pachca）            |
| `get_user_aliases`   | 显示已配置的身份别名和当前用户别名                                 |
| `get_health`         | 最近一次 ingest 状态、每个源的新鲜度、错误                          |
| `send_message`       | 向频道发送文本消息（通过 `allow_send` 选择性开启；所有源）             |
| `find_by_issue`      | 查找引用某个 YouTrack issue id 的消息（链接 + 频道名匹配）            |
| `who_mentioned`      | 查找某人被 @ 的消息（带别名解析；`target: "me"` 可用）                |
| `upsert_user_alias`  | 把你了解到的关于人的事（role / team / aliases / `responsible_for`）持久化到稳定的记忆层 |
| `remove_user_alias`  | 删除一条 user_aliases 记录；my_aliases 目标会被拒绝                |
| `update_user_alias_strings` | 在现有条目上增删特定别名；跨规范身份的"窃取"会被拒绝             |

### send_message

向频道发送文本回复 —— read→act 循环中负责动作的一半。两道安全护栏：

- **按源 opt-in**（默认拒绝）：除非源在 `config.yaml` 中设置 `allow_send: true`，否则工具拒绝执行。发送对他人可见，所以默认关闭。
- **发送前必读**：智能体在发送前必须为该频道调用 `get_new_messages`；如果出现了新消息，发送会被取消，回复要结合新上下文重新斟酌。

参数：`source`、`channel`（来自 `list_channels` 的频道 **id**）、`text`，以及可选的 `reply_to`（Mattermost 根帖 id / Telegram 消息 id / Pachca 父消息 id）以挂入会话。尚不支持发送文件附件。

### summarize_messages 参数

| 参数            | 类型   | 默认值 | 描述                                                    |
| -------------- | ------ | ------ | ------------------------------------------------------ |
| `hours`        | int    | -      | 回看 N 小时（例如 4、24、168）。优先于 `days`             |
| `days`         | int    | 1      | 回看 N 天                                              |
| `source`       | string | -      | 按源名过滤（例如 `company_mattermost`）                  |
| `channel`      | string | -      | 按频道名过滤                                            |
| `max_messages` | int    | 100    | 每个频道最多消息数                                       |

用 `get_stats` 查看你的实例里配置了哪些源名。

## 测试

```bash
# 安装开发依赖
pip install -r requirements-dev.txt

# 运行测试
pytest tests/ -v --tb=short

# 带覆盖率报告
pytest tests/ --cov=. --cov-report=term-missing --ignore=storage/vector_store.py
```

测试套件（约 640 个用例）覆盖了配置和 secrets 加载、过滤、SQLite 存储（通过 RLock 实现的线程安全）、MCP URL 生成与工具处理器、所有四种连接器（HTTP / IMAP 已 mock）以及 `ConnectorProtocol` 契约测试、ingest 编排器（VectorStore 被 mock —— 不加载 BGE-M3 模型）、CLI 调度器，以及 `aliases refresh` 通过 `ruamel.yaml` 的 round-trip。

## Ingest 选项

```bash
# 正常同步（使用保存的频道状态）
./run_ingest.sh

# 强制从 24 小时前开始完整扫描
./run_ingest.sh --hours 24 --force

# 调试模式
./run_ingest.sh --debug
```

## CLI 工具

面向用户的工具位于 `cli/` 下。`./bin/memorandum` 包装器会自动解析 venv；否则从激活的 venv 调用 `python -m cli <动词>`。

```bash
./bin/memorandum health                          # ingest 状态 + 每源新鲜度
./bin/memorandum health --json                   # 机器可读
./bin/memorandum aliases refresh                 # 为新发送者打印 stub user_aliases 条目
./bin/memorandum aliases refresh --in-place      # 把这些 stub 追加进 config.yaml
./bin/memorandum reindex-chroma                  # 清空并从 SQLite 重建向量库
```

`reindex-chroma` 取与 `bin/memorandum-sync` 相同的 `/tmp/memorandum-sync.lock`，因此正在运行的 sync（或另一个 reindex）会干净地阻塞它，而不是抢同一份状态。适用于：chroma 目录损坏后的恢复、schema 修复后回填元数据、或切换 embedding 模型时的重建步骤。

`health` 的退出码：`0`=正常，`1`=部分/错误，`2`=从未运行 —— 可用作监控探针（`./bin/memorandum health && echo healthy || echo check logs`）。同样的数据从 Claude 端可通过 MCP 工具 `get_health` 获取。

`aliases refresh` 是 **仅追加** 的：它会用 DB 中的发送者与你现有的 `user_aliases` 条目做 diff，并为尚未覆盖的发送者发出 stub（按消息数排序）。现有条目从不被修改或重排；`--in-place` 使用 `ruamel.yaml` round-trip，保证 `config.yaml` 中的注释完整保留。

> `python -m pipeline health`（旧形式）现在只会打印一行重定向提示并以退出码 2 结束 —— 请使用 `python -m cli health`（或上面的包装器）。

## Linux 部署（systemd）

在 Linux 上用 systemd 跑生产：

```bash
# 复制 service 与 timer 文件
sudo cp systemd/memorandum-collect.service /etc/systemd/system/
sudo cp systemd/memorandum-collect.timer /etc/systemd/system/

# 安装 sync 日志的 logrotate 配置
sudo cp systemd/memorandum-sync.logrotate /etc/logrotate.d/memorandum-sync

# 编辑 service 文件中的路径
sudo vim /etc/systemd/system/memorandum-collect.service
# 把 WorkingDirectory 和 ExecStart 改成你的安装路径

# 启用并启动定时器
sudo systemctl daemon-reload
sudo systemctl enable --now memorandum-collect.timer

# 查看状态
sudo systemctl status memorandum-collect.timer
sudo systemctl list-timers

# 查看日志
journalctl -u memorandum-collect -f

# 查看 sync 日志
tail -f /var/log/memorandum-sync.log

# 手动运行（如有需要）
sudo systemctl start memorandum-collect
```

## 日志

sync 脚本（`bin/memorandum-sync`）的日志写入：
- Linux 上的 `/var/log/memorandum-sync.log`（若 /var/log 可写）
- 项目目录下的 `data/memorandum-sync.log`（fallback）

日志按天滚动并保留 7 天，由上面安装的 logrotate 配置控制。

## 环境要求

- Python 3.11+
- 虚拟环境（`.venv`）
- 一个 Mattermost Personal Access Token、Telegram Bot Token 和/或 Pachca Personal Access Token
- 模型 + 数据约需 4.5 GB 磁盘（默认 BGE-M3；用更小的模型会更少 —— 见 [更换 embedding 模型](#更换-embedding-模型)）
- BGE-M3 embedding 约需 2–2.5 GB 内存（默认；小型英文模型约 300 MB 即可）

## 更换 embedding 模型

向量存储的模型与调优参数在 `config.yaml` 的 `embedding:` 下。省略整个块即保留 BGE-M3 默认值；也可单独覆盖任一键：

```yaml
embedding:
  model: "BAAI/bge-m3"       # 任何 FlagEmbedding 支持的模型 id
  device: "cpu"              # "cpu", "cuda" 或 "mps"
  use_fp16: true
  max_length: 512
  batch_size: 1
  collection_name: "messages"
```

建议的替代模型：
- `BAAI/bge-m3` —— 多语言，约 4 GB 磁盘，**1024 维**（默认）
- `BAAI/bge-small-en-v1.5` —— 仅英文，约 130 MB，**512 维**（快、低内存）

**重要 —— 维度问题：** Chroma 每个 collection 都按固定维度存储向量。把 `model:` 指向不同的模型（或输出尺寸不同的模型）会让相似度搜索悄然失效，除非所有文档都重新 embed。如果现有 collection 维度与配置模型不匹配，Memorandum 在第一次插入时会抛出清晰的报错；但更换之前还是要选好迁移路径：

1. **保留旧向量。** 把 `collection_name:` 改成新名字（如 `messages_bge_small`）。旧 collection 仍在磁盘上，新模型填充新的。
2. **从零开始。** 执行 `./bin/memorandum reindex-chroma` —— 它会获取 sync lock，删掉 chroma 目录，并用当前配置的模型对 SQLite 里的每条消息重新 embed。

## 扩展 Memorandum

### 添加新的源连接器（Slack、Discord、Matrix……）

内置的四个连接器是个小接口面，系统其他部分自然能扩展到第五个。完整说明 —— 接口契约、消息 dict 形状、增量同步模式、文件附件、需要接入的四个 dispatch 点、要写的测试，以及现有连接器在搭建过程中踩过的坑 —— 都在 **[connectors/CONTRIBUTING.md](connectors/CONTRIBUTING.md)**。写代码前请通读一遍；契约不大，但 *顺序* 与 *不变式* 至关重要。

## 已发布与下一步

[**CHANGELOG.md**](CHANGELOG.md) 是一份决策日志 —— 每个落地的功能都附带简短的动机和它触及的文件路径。既可作为"这个构建里有什么"的索引，也能作为贡献者的"X 为什么这样做"参考。

计划中的工作和 bug 报告请走 [GitHub Issues](../../issues)（提供模板）；设计层面的问题请走 [Discussions](../../discussions)。
