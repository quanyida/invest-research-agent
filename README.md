# 投研 Agent

面向 C 端用户的垂直投研 Agent：用户在会话中提出 A 股 / 港股 / 美股、新闻、公告、宏观、
指数、选股等问题，系统调用 iFinD MCP 与公开网页搜索，按固定四段式输出回答。

四段式依次为：**结论 / 依据 / 关键数据 / 待确认**。

## 主要能力

- 多租户注册登录、Cookie 会话、会话列表与历史消息
- AgentScope ReAct Agent，单会话串行提问，避免并发覆盖
- iFinD 原始返回直传：不抽 fact、不二次计算业务数字
- 引用编号校验、无效引用移除与降级标记
- 工具调用与模型调用全量审计落库
- PostgreSQL 行级安全（RLS）租户隔离；Redis 可选，不可用时回退进程内缓存
- SSE 流式回放，支持断线续看

## 环境要求

- **Python 3.12**
- **Docker Desktop**（推荐，用于 PostgreSQL 16 + Redis 7）
- 或者：自备 PostgreSQL 15+，并知道一个可建角色/建库的管理员账号

## 快速开始（Windows）

前置：Docker Desktop 已启动，Python 3.12 已加入 PATH。

```powershell
git clone <repo-url>
cd invest-research-agent
.\scripts\dev.ps1
```

`dev.ps1` 会依次完成：启动容器、创建 `.venv`、安装依赖、初始化数据库、启动服务。

打开 <http://127.0.0.1:8000/ui/>，在 `dev` 环境可以直接注册账号。

需要生成真实回答时，编辑 `.env` 填入 `LLM_API_KEY`；需要 iFinD 数据时再填
`IFIND_MCP_TOKEN`。没有密钥也能启动并打开页面，只是问答无法取到数据。

### 手动启动（等价步骤）

```powershell
docker compose up -d
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe scripts\bootstrap_db.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

macOS / Linux：

```bash
docker compose up -d
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/bootstrap_db.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 数据库初始化做了什么

`scripts/bootstrap_db.py` 是幂等的，可以反复执行：

1. 用管理员连接创建 `invest_auth`（NOLOGIN + BYPASSRLS，auth 函数所有者）和
   `invest_app`（登录角色，应用实际连接使用）；
2. 创建数据库 `invest`（UTF8），所有者是 `invest_app`；
3. 按文件名顺序执行 `infra/sql/001` ～ `007`；
4. 校验 RLS 已强制开启、`auth_*` 函数归 `invest_auth`、应用账号不能切换到该角色。

默认连接参数与 `docker-compose.yml` 对齐：

| 项 | 默认值 |
|---|---|
| PostgreSQL 地址 | `127.0.0.1:55432` |
| 管理员 | `invest_admin` / `invest_admin_dev` |
| 应用角色 | `invest_app` / `ira_dev_9f3c` |
| 数据库 | `invest` |

这些密码**只用于本地开发**，部署前必须更换。使用外部 PostgreSQL 时，设置
`DATABASE_URL` 和 `PG_ADMIN_URL`（或 `PG_ADMIN_USER` / `PG_ADMIN_PASSWORD`）后
再运行初始化脚本。

重置本地数据（会删除容器数据卷）：

```powershell
docker compose down -v
```

## 配置

配置从 `.env` 读取，模板见 `.env.example`。最常用的几项：

- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`：OpenAI 兼容接口
- `IFIND_MCP_BASE` / `IFIND_MCP_TOKEN`：iFinD 官方 MCP
- `WEB_SEARCH_API_KEY`：Tavily，留空则网页搜索工具不可用
- `REDIS_URL`：不填或连不上时自动回退进程内缓存

没有 `IFIND_MCP_TOKEN` 时**没有内置假数据**，iFinD 工具会调用失败；这是预期行为。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

请限定 `tests` 目录，避免误收集第三方源码。

## 目录结构

| 目录 | 内容 |
|---|---|
| `app/` | FastAPI 应用、Agent 运行时、工具与持久化 |
| `tests/` | pytest 测试 |
| `static/` | 网页端静态资源 |
| `infra/sql/` | PostgreSQL 建表、RLS、队列等初始化脚本 |
| `docs/` | 设计文档、版本方案与交接文档 |
| `scripts/` | 初始化、启动与运维脚本 |
| `skills/` | 内置领域说明（非可执行工具） |
| `samples/` | 示例数据 |

## 安全约定

- `.env`、API Key、iFinD Token、数据库密码一律不入库。
- 业务表启用 RLS，新代码必须通过 `app/db.py` 的 `tenant_scope()` 访问。
- `chat_runs` 不启用 RLS，查询必须显式带 `tenant_id`。
- 不要修改数据库认证配置（`pg_hba.conf`）来绕过权限；初始化脚本已处理角色和授权。

完整设计、数据模型、事务边界与运维说明见 `docs/交接文档.md`。

## 常见问题

**端口 55432 已被占用。** 本机已有 PostgreSQL 时，改 `docker-compose.yml` 的端口映射，
同时把 `.env` 的 `DATABASE_URL` 改成同一端口，再运行 `scripts/bootstrap_db.py`。

**没有 Docker。** 安装 PostgreSQL 15+，用管理员账号创建角色和库后，设置
`DATABASE_URL` 与 `PG_ADMIN_URL`，再运行 `scripts/bootstrap_db.py`。

**`/readyz` 返回 `llm: missing_key`。** 服务本身可用；填 `LLM_API_KEY` 后重启即可问答。

**Windows 中文路径下自建 PostgreSQL 报编码错误。** 这是 PostgreSQL `initdb` 的已知限制。
使用 Docker 不受影响；必须本机自建时，先用 `-E SQL_ASCII --locale=C` 初始化集群，
再用 `template0` 创建 UTF8 数据库（`scripts/bootstrap_db.py` 已按此方式建库）。