"""Agent Service 入口（:8001）。

只做一件事：后台消费 chat_runs 队列，用 RunWorker 执行任务、写 session_events。
API 层不直接调用这里，两者只通过数据库通信。

只绑定内网地址（``127.0.0.1:8001``），不对外暴露。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent.run_worker import RunWorker
from app.api.health import router as health_router
from app.config import get_settings

logging.basicConfig(
    level=getattr(logging, get_settings().log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    worker = RunWorker(get_settings())
    await worker.start()
    try:
        yield
    finally:
        await worker.stop()


app = FastAPI(
    title="投研 Agent Service",
    version="1.0.0",
    docs_url="/docs" if get_settings().is_dev else None,
    lifespan=lifespan,
)
app.include_router(health_router)
