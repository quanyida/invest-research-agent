-- 来源渠道：产品粒度归属（ifind / web / unknown）
-- channel 只能由工具注册信息推导，禁止从上游响应或模型输出取值。
begin;

alter table evidence add column if not exists channel text not null default 'unknown';

-- evidence 启用了 FORCE RLS，应用账号直接 UPDATE 只能看到当前租户（迁移时通常 0 行）。
-- 迁移在同一事务内临时关闭 FORCE，回填完成后再恢复；ACCESS EXCLUSIVE 锁保证其他会话不会在窗口内看到未受保护的表。
alter table evidence no force row level security;

update evidence
set channel = case
  when tool like 'ifind.%' then 'ifind'
  when tool = 'web.search' or tool like 'web.%' then 'web'
  else 'unknown'
end
where channel = 'unknown';

alter table evidence force row level security;

create index if not exists evidence_channel_idx on evidence (channel, fetched_at desc);

commit;