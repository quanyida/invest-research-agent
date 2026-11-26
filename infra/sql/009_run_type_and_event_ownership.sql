-- 009: 运行类型、事件归属、抢占优先级
--
-- 目的：
--   1. chat_runs.run_type 区分普通聊天与深度研究，供 worker 分队列抢占
--   2. chat_runs.priority 让短任务插队
--   3. session_events.run_id 区分同一消息的多次尝试（重试时事件不能混）
--
-- 回填说明：
--   历史上 /chat 不写 chat_runs，所以 session_events 里有一部分事件的
--   run_id 会保持 NULL。这些是同步聊天的遗留事件，按 message_id 仍可读。

begin;

alter table chat_runs
  add column if not exists run_type text not null default 'research',
  add column if not exists last_seq integer not null default 0,
  add column if not exists priority integer not null default 100;

alter table session_events
  add column if not exists run_id uuid;

update session_events e
   set run_id = r.id
  from chat_runs r
 where e.message_id = r.message_id
   and e.run_id is null;

create index if not exists session_events_run_seq_idx
  on session_events (run_id, id)
  where run_id is not null;

create index if not exists chat_runs_pick_chat_idx
  on chat_runs (priority, created_at)
  where status = 'queued' and run_type = 'chat';

create index if not exists chat_runs_pick_research_idx
  on chat_runs (priority, created_at)
  where status = 'queued' and run_type = 'research';

commit;
