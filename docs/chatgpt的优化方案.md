# session_events 优化清单

## 一、P0：优先优化，减少数据库写入和存储量

### 1. 优化 Chat Snapshot 写入机制
- [x] 当前 Chat 每 200ms 写入一次全文 Snapshot，改为增量 Delta 批量写入。
- [x] 避免每次 Snapshot 都重复保存完整回答。
- [x] 如果需要快速恢复，单独保留低频 Snapshot 检查点。 （评估后决定不新增检查点：新写入全为 delta，恢复靠 delta 回放 + messages 终稿；旧 snapshot 仍兼容回放）
- [x] 评估 Snapshot 检查点频率，例如每 3～5 秒一次。 （随上条一并评估，不引入新检查点）
- [x] 保证最终答案完成时有完整的持久化记录。
- [x] 保证断线重连时能够通过 Snapshot + Delta 正确恢复完整内容。 （新写入无 snapshot，靠 delta 回放恢复；旧 snapshot 数据仍兼容）

### 2. 优化 Research Delta 写入机制
- [x] 保留当前 Delta 缓冲批量写入机制。
- [x] 在当前 200 字符阈值基础上，增加最大时间间隔，避免低速生成时长时间不落库。 （最大间隔 1.0s）
- [x] 非 Delta 事件到来时，继续先 Flush Delta，保证事件顺序。
- [x] 流结束、异常结束时，确保剩余缓冲内容不会丢失。
- [x] 验证 delta_reset 对缓冲区和已持久化内容的处理是否正确。

### 3. 减少数据库事务开销
- [x] 评估是否可以将一批 Delta 放在同一个数据库事务中批量 INSERT。
- [x] 保留关键事件的独立持久化能力。
- [x] 保留 Worker fencing 校验，不能为了减少 SQL 而移除所有权验证。
- [x] 避免每个普通文本增量都单独创建数据库事务。

## 二、P1：优化事件表结构与清理机制

### 4. 检查 session_events 索引
- [x] 检查 `(message_id, id)` 索引是否仍有必要。
- [x] 检查 `(run_id, id)` 索引是否覆盖主要 SSE 回放查询。
- [x] 检查 `expires_at` 部分索引的使用情况。
- [x] 使用 EXPLAIN ANALYZE 验证事件查询是否命中预期索引。
- [x] 根据实际查询计划决定是否调整索引，不盲目删除。 （EXPLAIN 命中预期索引，未调整）

### 5. 优化事件过期与清理
- [x] 明确 Chat 和 Research 的事件保留期限。
- [x] 评估当前默认 24 小时保留策略是否合理。 （保留 24h 作默认，改为可配置 EVENT_RETENTION_CHAT/RESEARCH_HOURS）
- [x] 确保任务正常结束、失败、超时、Worker 被回收时都能设置过期时间。
- [x] 确保清理任务不会删除仍在运行的任务事件。
- [x] 评估当前每 600 秒执行一次清理是否合适。 （保留 600s 作默认，改为可配置 EVENT_PURGE_INTERVAL_SECONDS）
- [x] 如果单次删除数据量较大，改为分批删除，降低长事务和锁压力。
- [x] 检查清理任务在多 Worker 环境下是否存在重复执行和资源竞争。 （pg_try_advisory_lock 保证单 Worker 执行）

### 6. 明确事件保留策略
- [x] 区分实时回放事件与长期审计数据。 （session_events 负责回放，messages/evidence/tool_calls 负责长期留存）
- [x] 对关键审计数据设置独立保留策略。 （审计数据存于独立表，不参与 session_events 清理，等同长期保留）
- [x] 不要因为 session_events 过期而影响 messages、evidence、tool_calls 等业务数据。
- [x] 确保用户历史消息不依赖已经过期的 session_events。 （messages.content 为历史真相来源）

## 三、P1：检查 SSE 回放与任务可靠性

### 7. 检查断线续传
- [x] 验证 SSE 使用 event ID 作为恢复游标。
- [x] 验证 after_seq 只读取当前 run 的事件。
- [x] 验证同一 message 多次重试时，不会混合不同 attempt 的事件。
- [x] 验证断线重连不会重复展示大量 Delta。
- [x] 验证 Snapshot + Delta 恢复后的文本与最终答案一致。 （新数据靠 delta 回放；旧 snapshot 兼容）
- [x] 验证 done、error 等终态事件不会遗漏。

### 8. 检查 Worker 并发安全
- [x] 保留 run_id、attempt、worker_id 的 fencing 机制。
- [x] 验证旧 Worker 失去租约后不能继续写事件。
- [x] 验证 Worker 崩溃后，新 Worker 可以正确接管任务。
- [x] 验证任务重试时旧事件不会污染新一轮执行。
- [x] 验证事件 ID 和事件顺序在多 Worker 环境下仍然可靠。

## 四、P2：监控与性能验证

### 9. 增加 session_events 监控
- [ ] 统计 Chat 每次请求产生的事件数量。
- [ ] 统计 Research 每次请求产生的事件数量。
- [ ] 统计 Snapshot、Delta、工具事件各自的数量。
- [ ] 统计每次请求写入的总字符数和总字节数。
- [ ] 统计 session_events 每日新增行数。
- [ ] 统计 session_events 表和索引的实际占用空间。
- [ ] 监控 PostgreSQL WAL 增长量。
- [ ] 监控事件写入耗时、回放查询耗时和清理耗时。

### 10. 建立性能测试
- [ ] 模拟大量并发 Chat 请求。
- [x] 模拟长时间 Research 请求。 （perf_events.py 合成 60s 流验证）
- [x] 模拟高频 Delta 输出。 （perf_events.py 5 delta/s 验证）
- [ ] 模拟用户频繁断线和重连。
- [x] 模拟 Worker 崩溃、租约过期和任务重试。 （test_worker_heartbeat_fence / test_run_fencing 覆盖）
- [x] 对比优化前后的数据库写入次数。 （perf_events.py：chat 事务 302→61）
- [x] 对比优化前后的数据库存储量。 （perf_events.py：chat 字符 454500→3000）
- [ ] 对比优化前后的 SSE 首次响应和恢复耗时。

## 五、改造原则

1. 不删除 session_events，它仍然承担 SSE 持久化回放职责。
2. 不将所有事件简单改成 Redis，PostgreSQL 继续作为可靠持久化层。
3. 不牺牲 run_id、attempt、fencing 和 RLS 等可靠性机制。
4. Chat 优先解决全文 Snapshot 重复写入问题。
5. Research 保留关键研究过程事件，只优化文本 Delta 的批量写入。
6. 关键事件必须可靠持久化，不能因为性能优化而丢失。
7. 每次改造都要有断线续传、任务重试和并发场景的回归测试。

## 六、建议实施顺序

第一阶段：Chat Snapshot 改造、Research Delta 时间阈值、批量写入。

第二阶段：SSE 回放兼容性、事件索引、过期清理优化。

第三阶段：数据库监控、并发压测、性能对比。

**最终目标：降低数据库写入量和存储放大，同时保证 SSE 断线续传、任务重试、Worker 接管和审计数据可靠性。**
