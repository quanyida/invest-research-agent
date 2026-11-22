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

## 目录结构

| 目录 | 内容 |
|---|---|
| `app/` | FastAPI 应用、Agent 运行时、工具与持久化 |
| `tests/` | pytest 测试 |
| `static/` | 网页端静态资源 |
| `infra/sql/` | PostgreSQL 建表、RLS、队列等初始化脚本 |
| `docs/` | 设计文档、版本方案与交接文档 |
| `scripts/` | 运维与辅助脚本 |
| `skills/` | 内置领域说明（非可执行工具） |
| `samples/` | 示例数据 |

## 快速开始

1. 使用 Python 3.12，创建并激活虚拟环境。
2. 安装依赖：

   ```powershell
   pip install -r requirements.txt
   ```

3. 复制配置并填写密钥：

   ```powershell
   Copy-Item .env.example .env
   ```

   至少需要配置 `LLM_API_KEY` 与 `IFIND_MCP_TOKEN`。`.env` 已被忽略，不要提交。

4. 初始化 PostgreSQL，按顺序执行 `infra/sql/` 下的 SQL 文件。
5. 启动服务：

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

请限定 `tests` 目录，避免误收集第三方源码。

## 安全约定

- `.env`、API Key、iFinD Token、数据库密码一律不入库。
- 业务表启用 RLS，新代码必须通过 `app/db.py` 的 `tenant_scope()` 访问。
- `chat_runs` 不启用 RLS，查询必须显式带 `tenant_id`。

完整设计、数据模型、事务边界与运维说明见 `docs/交接文档.md`。