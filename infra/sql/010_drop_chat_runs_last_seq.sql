-- 010: 删除 chat_runs.last_seq
--
-- 该列由 009 引入，但从来没有读者：
--   1. SSE 的 run_detached.last_seq 取自事件表 id，是 app/api/chat.py 的局部变量，
--      与这一列无关（前端 static/assets/app.js 读的是那个 SSE 字段）；
--   2. 全仓没有 select last_seq，也没有任何条件判断依赖它。
--
-- 而每次 append_event 都会顺带 update 这一行，等于每写一条事件都白付一次
-- 行更新 + WAL + 索引维护。既然无人读取，直接删除，不做兼容保留。
begin;

alter table chat_runs drop column if exists last_seq;

commit;
