-- /chat 与 /research 分流所需的最小数据变更。
-- 全部幂等，可重复执行。
begin;

-- 会话列表摘要（/chat 与 /research 落库时更新，避免列表页 join messages）
alter table conversations
  add column if not exists last_message_preview text,
  add column if not exists message_count int not null default 0;

-- 孤儿检测需要知道 running 状态最后一次变化的时间
alter table messages
  add column if not exists updated_at timestamptz not null default now();

-- 只有 running 需要按时间扫，部分索引足够
create index if not exists messages_orphan_idx
  on messages (status, updated_at)
  where status = 'running';

-- research 事件的清理标记
alter table session_events
  add column if not exists expires_at timestamptz;

create index if not exists session_events_expiry_idx
  on session_events (expires_at)
  where expires_at is not null;

commit;