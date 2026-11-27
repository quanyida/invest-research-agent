# 投研 Agent 事件存储与消息分发架构方案

**版本：** V1.0  
**状态：** 第一阶段实施方案  
**适用范围：** Chat / Deep Research  
**核心技术：** FastAPI + PostgreSQL + SSE  
**后续扩展：** Redis Streams

---

## 一、背景与问题

当前投研 Agent 使用 PostgreSQL 同时承担任务队列和事件存储职责。

核心流程：

1. API 创建 Chat 或 Deep Research 任务。
2. Worker 从 PostgreSQL 队列中领取任务。
3. Worker 执行 Agent，持续产生执行事件。
4. Worker 将事件写入 `session_events` 表。
5. API 通过 SSE 接口读取事件。
6. API 将事件持续推送给前端。
7. 前端根据事件类型更新消息内容、研究进度和工具执行状态。

当前事件表存在以下问题：

- `delta` 事件产生频率较高，持续写入会导致表数据不断增长。
- `ping`、工具执行状态等过程性事件也会增加存储量。
- 当前缺少完善的事件清理和生命周期管理。
- API SSE 通过数据库轮询读取事件，存在无效查询开销。
- 如果未来用户量增加，事件表可能影响 PostgreSQL 的查询、索引、备份和维护成本。

因此，需要明确任务调度、实时事件分发和事件持久化的职责边界。

## 二、核心架构决策

采用分阶段演进策略。

### 第一阶段：继续使用 PostgreSQL

暂时不引入 Redis、Kafka、RabbitMQ 等额外基础设施。

PostgreSQL 继续承担：

- 任务状态管理。
- 任务队列。
- 事件持久化。
- 最终消息和研究报告存储。
- 断线恢复和历史事件回放。

通过 PostgreSQL LISTEN / NOTIFY 优化 SSE 事件读取机制。

### 第二阶段：引入 Redis Streams

当事件量、并发量和数据库负载达到一定规模后，再引入 Redis Streams。

Redis 主要负责：

- 高频事件流。
- 实时事件分发。
- 任务消息分发（视实际需求决定）。

PostgreSQL 继续作为核心业务数据库，保存任务状态、消息、研究报告、证据等业务数据。

### 第三阶段：按业务规模演进

如果未来出现大规模事件流、多消费者订阅、跨系统数据分发等需求，再评估 Kafka 或其他分布式事件平台。

不提前引入复杂基础设施。

---

## 三、第一阶段架构

### 3.1 整体架构

```text
                 Frontend
                    |
                    | HTTP / SSE
                    v
               API Service
                    |
                    | 创建任务
                    | SSE 事件转发
                    v
                PostgreSQL
                    ^
                    |
             INSERT Events
             pg_notify()
                    |
                    |
               Agent Worker
                    |
                    v
                 Agent
```

说明：

- 前端只访问 API Service。
- API Service 负责业务接口和 SSE 转发。
- Agent Worker 负责 Agent 执行。
- PostgreSQL 保存任务和事件。
- Worker 写入事件后，通过 NOTIFY 通知 API。
- API 收到通知后查询事件表，再通过 SSE 推送前端。

### 3.2 LISTEN / NOTIFY 机制

当前方案：

```text
API SSE
   |
   | 每隔 50ms 查询数据库
   v
session_events
```

优化后：

```text
Agent Worker
   |
   | INSERT session_events
   | pg_notify('run_events', run_id)
   v
PostgreSQL
   |
   | NOTIFY
   v
API Listener
   |
   | 查询 session_events
   v
SSE
   |
   v
Frontend
```

通知只传递 `run_id`，不传递事件内容。

PostgreSQL 事件表仍然是事件的事实来源。

### 3.3 Worker 端实现

在现有事件批量写入方法中增加通知。

建议修改：

`app/agent/run_queue.py`

核心伪代码：

```python
async with session.begin():
    await insert_events(session, events)

    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {
            "channel": "run_events",
            "payload": run_id,
        },
    )
```

要求：

1. 事件写入和 NOTIFY 必须处于同一个事务。
2. 事务提交成功后，PostgreSQL 才会真正发送通知。
3. 一批事件只需要发送一次通知。
4. 不要将完整 delta 文本放入 NOTIFY。
5. 保留当前 worker_id + attempt fencing 机制。

### 3.4 API 端实现

API 应用启动时建立一个专用 PostgreSQL Listener 连接：

```sql
LISTEN run_events;
```

收到通知后：

```text
payload = run_id
```

API 唤醒对应的 SSE 请求。

然后执行：

```sql
SELECT *
FROM session_events
WHERE run_id = :run_id
  AND id > :last_event_id
ORDER BY id ASC
LIMIT 100;
```

读取事件后，通过 SSE 推送给前端。

注意：

- Listener 使用长期数据库连接。
- 不要将监听连接放进普通短生命周期连接池中反复借还。
- 每个 API 实例建立自己的监听连接。
- 监听断开后自动重连。
- 重连后必须重新执行 LISTEN。
- 不依赖通知补发，重新读取数据库来恢复事件。

### 3.5 SSE 读取机制

原有逻辑：

```python
while True:
    events = await read_events(run_id, after_id)

    if events:
        for event in events:
            yield format_sse(event)
            after_id = event.id

    await asyncio.sleep(0.05)
```

优化逻辑：

```python
while True:
    events = await read_events(run_id, after_id)

    for event in events:
        yield format_sse(event)
        after_id = event.id

    if run_finished:
        break

    await wait_for_notification_or_timeout(
        run_id=run_id,
        timeout=1.0,
    )
```

执行原则：

- 收到通知，立即查询事件。
- 没有通知，等待一段时间后兜底查询。
- 建议兜底间隔初始设置为 1 秒，根据监控结果调整。
- 每次查询按事件 ID 升序读取。
- 支持 Last-Event-ID 和 after_id 断线恢复。
- 每次读取设置 LIMIT，避免单次拉取过多数据。
- 不允许只依赖 NOTIFY，否则监听断开期间可能遗漏通知。

---

## 四、事件数据分类与生命周期

核心原则：

**不是所有 Agent 事件都值得长期保存。**

将事件分为三类。

| 类型 | 事件示例 | 存储策略 |
|---|---|---|
| 高频过程事件 | delta、ping | 短期保存 |
| 关键过程事件 | plan、tool_start、tool_end、error | 根据审计需求保留 |
| 最终业务产物 | 最终回答、研究报告、证据、引用 | 长期保存 |

### 4.1 高频过程事件

包括：

- delta
- ping
- 高频进度更新
- 临时状态变化

特点：

- 数量多。
- 单条业务价值较低。
- 主要服务于实时前端展示。
- 不应该无限期保存。

建议初始保留策略：

- 普通过程事件：任务结束后保留 24～72 小时。
- 运行中的任务：不清理。
- 失败任务：根据排障需要适当延长保留期。

### 4.2 关键过程事件

包括：

- plan
- tool_start
- tool_end
- evidence
- error
- done

特点：

- 有一定审计和排障价值。
- 可以帮助还原 Agent 执行过程。
- 适合按业务需要保留。

建议：

- tool_start、tool_end：保留 7～30 天。
- error：保留必要的错误摘要和诊断信息。
- evidence：独立持久化到证据表，不依赖事件日志长期保存。

### 4.3 最终业务产物

包括：

- 最终助手消息。
- 研究报告。
- 研究证据。
- 引用来源。
- 任务最终状态。

必须独立保存到 PostgreSQL 业务表。

不能依赖 delta 事件恢复最终回答。

用户刷新页面时，应该先读取持久化的消息和研究报告，再根据需要获取任务执行进度。

---

## 五、session_events 清理策略

第一阶段继续使用 PostgreSQL 时，需要建立定期清理机制。

### 5.1 基础清理原则

清理必须满足：

1. 只清理已结束的任务。
2. 运行中的任务绝不清理。
3. 先确认最终消息、研究报告、证据已独立持久化。
4. 保留时间可配置。
5. 删除操作需要分批执行。
6. 清理任务必须有日志和监控。

### 5.2 初始保留策略

建议先采用：

| 事件类别 | 初始保留期限 |
|---|---|
| delta、ping | 任务结束后 24～72 小时 |
| plan、tool_start、tool_end | 7～30 天 |
| error | 30 天或按排障要求 |
| 最终回答、报告、证据 | 长期保存 |

注意：如果不同事件类型需要不同保留时间，清理任务必须根据 event_type 和任务终态分别判断。

### 5.3 分批清理

不建议一次性删除大量数据。

建议每次删除有限数量：

```sql
WITH expired AS (
    SELECT id
    FROM session_events
    WHERE created_at < NOW() - INTERVAL '3 days'
      AND event_type IN ('delta', 'ping')
      AND run_id IN (
          SELECT id
          FROM chat_runs
          WHERE status IN ('done', 'error', 'cancelled')
      )
    ORDER BY id
    LIMIT 5000
)
DELETE FROM session_events
WHERE id IN (SELECT id FROM expired);
```

以上是示意 SQL，实际执行前需要根据当前项目的 chat_runs 主键、外键和事件表字段进行调整。

清理任务可以每隔几分钟执行一次。

### 5.4 大规模情况下使用时间分区

如果事件表增长很快，可以考虑 PostgreSQL 按 created_at 做 RANGE 分区。

例如：

```text
session_events
  |
  |-- session_events_2026_11_27
  |-- session_events_2026_11_28
  |-- session_events_2026_11_29
  |-- session_events_2026_11_30
```

过期后直接删除整个分区：

```sql
DROP TABLE session_events_2026_11_27;
```

相比逐行 DELETE，删除分区通常更适合大规模数据清理。

但分区会影响主键、唯一约束、外键及索引设计，不能直接在现有表上随意改造，需要单独评估。

---

## 六、第二阶段：Redis Streams 架构

当 PostgreSQL 事件表负载明显增加，或者实时事件并发量成为瓶颈时，再考虑引入 Redis Streams。

### 6.1 目标架构

```text
                 Frontend
                    |
                    | HTTP / SSE
                    v
                API Service
                    |
                    | XREAD
                    v
                  Redis
                    ^
                    |
                    | XADD
                    |
               Agent Worker
                    |
                    v
                  Agent

                PostgreSQL
                    ^
                    |
       任务状态 / 最终消息 / 报告 / 证据
```

### 6.2 Redis Streams 的职责

Redis Streams 负责：

- 高频事件实时分发。
- 事件短期回放。
- 任务消息分发（根据需要启用）。
- 事件长度裁剪和过期管理。

PostgreSQL 负责：

- 用户和会话。
- 任务业务状态。
- 最终助手消息。
- 研究报告。
- 研究证据。
- 引用和其他核心业务数据。

### 6.3 事件流设计

可以为每个 run 创建独立 Stream：

```text
run:{run_id}:events
```

例如：

```text
run:123:events
run:456:events
run:789:events
```

Worker 写入：

```redis
XADD run:123:events * type delta text "正在分析财务数据"
```

API 读取：

```redis
XREAD BLOCK 1000 STREAMS run:123:events 0-0
```

继续读取时使用上次返回的 Stream ID。

### 6.4 事件裁剪

例如限制每个任务最多保留 5000 条：

```redis
XADD run:123:events MAXLEN ~ 5000 * type delta text "..."
```

任务结束后设置过期：

```redis
EXPIRE run:123:events 86400
```

表示保留 24 小时。

实际系统应当根据事件大小、任务时长、前端回放窗口和并发情况设置限制。

### 6.5 Consumer Group 注意事项

任务队列可以使用 Consumer Group：

```text
research:tasks
```

多个 Worker 共同消费任务，通过 ACK 确认处理完成。

但是前端事件流不建议使用 Consumer Group。

原因：

- Consumer Group 会让多个消费者分摊消息。
- 多个浏览器连接需要看到同一份事件。
- 因此前端 SSE 应使用独立读取游标，通过 XREAD 读取，而不是使用同一个消费组分配事件。

### 6.6 Redis 持久化要求

不能把 Redis 当作默认可靠的永久存储。

需要明确：

- AOF 或其他持久化策略。
- Redis 故障恢复策略。
- 数据丢失时的补偿机制。
- Stream 过期策略。
- 任务执行中断后的恢复方式。

最终业务结果仍然必须写入 PostgreSQL。

---

## 七、任务队列与事件流需要分开设计

需要明确两个概念：

### 任务队列

解决：

- 谁来执行任务。
- 任务由哪个 Worker 领取。
- 任务是否正在运行。
- 任务失败后如何重试。
- 如何防止多个 Worker 重复执行。

### 事件流

解决：

- Agent 执行过程中产生了什么事件。
- 前端如何实时收到事件。
- 如何断线恢复。
- 如何回放历史事件。

两者可以使用同一个 Redis，但逻辑上应该独立。

即使未来引入 Redis Streams，也不能直接删除 PostgreSQL 中的任务业务状态。

任务状态、执行租约、attempt、worker_id、fencing 等仍然需要有明确的持久化和一致性设计。

---

## 八、分阶段实施计划

### Phase 1：优化当前 PostgreSQL 架构

优先级最高。

- [ ] 保留当前 PostgreSQL 任务队列。
- [ ] 保留 session