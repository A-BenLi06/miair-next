"""日志广播背压与文件轮转回归测试。"""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

from app.core import logging as log_config


def record(message):
    return logging.LogRecord("miair", logging.INFO, __file__, 1, message, (), None)


def test_slow_subscriber_keeps_latest_logs_without_callback_errors():
    async def run():
        handler = log_config.RingBufferHandler()
        loop = asyncio.get_running_loop()
        handler.set_loop(loop)
        errors = []
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        slow = handler.subscribe()
        fast = handler.subscribe()
        for i in range(250):
            # 模拟音频工作线程产生日志, 快客户端逐条消费。
            await asyncio.to_thread(handler.emit, record(str(i)))
            assert await asyncio.wait_for(fast.get(), timeout=1) == str(i)
        assert errors == []
        assert slow.qsize() == 200
        assert [slow.get_nowait() for _ in range(200)] == [str(i) for i in range(50, 250)]
        assert len(handler.buffer) == 250

    asyncio.run(run())


def test_unsubscribed_client_does_not_receive_pending_broadcast():
    async def run():
        handler = log_config.RingBufferHandler()
        handler.set_loop(asyncio.get_running_loop())
        queue = handler.subscribe()
        handler.emit(record("pending"))
        handler.unsubscribe(queue)
        await asyncio.sleep(0)
        assert queue.empty()

    asyncio.run(run())


def test_subscribe_replays_buffered_history():
    """新订阅者应立即回放环形缓冲中的历史日志 (WS 重连不丢上下文)。"""
    async def run():
        handler = log_config.RingBufferHandler()
        handler.set_loop(asyncio.get_running_loop())
        for msg in ("old-1", "old-2"):
            await asyncio.to_thread(handler.emit, record(msg))
        queue = handler.subscribe()
        # 无需等待新日志, 订阅即可读到此前的历史
        assert queue.qsize() == 2
        assert queue.get_nowait() == "old-1"
        assert queue.get_nowait() == "old-2"
        # 订阅之后的 emit 照常实时广播
        await asyncio.to_thread(handler.emit, record("live"))
        assert await asyncio.wait_for(queue.get(), timeout=1) == "live"

    asyncio.run(run())


def test_subscribe_replay_caps_at_queue_capacity():
    """历史超过队列容量 (200) 时只保留最新的, 对齐广播丢弃策略。"""
    handler = log_config.RingBufferHandler()
    for i in range(250):
        handler.emit(record(str(i)))
    queue = handler.subscribe()
    assert queue.qsize() == 200
    assert queue.get_nowait() == "50"
    assert queue.get_nowait() == "51"


def test_logging_after_loop_shutdown_still_updates_buffer():
    handler = log_config.RingBufferHandler()
    loop = asyncio.new_event_loop()
    handler.set_loop(loop)
    loop.close()
    handler.emit(record("shutdown"))
    assert list(handler.buffer) == ["shutdown"]


def test_file_logging_rotates_and_bounds_retained_files(tmp_path, monkeypatch):
    # 独立 logger 避免改动其它 API 测试的全局日志配置。
    loggers = {}
    isolated_logging = SimpleNamespace(**vars(logging))
    isolated_logging.getLogger = lambda name: loggers.setdefault(name, logging.Logger(name))
    monkeypatch.setattr(log_config, "logging", isolated_logging)
    monkeypatch.setattr(log_config, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(log_config, "ring_handler", log_config.RingBufferHandler())
    path = tmp_path / "miair.log"
    log_config.setup_logging(False, str(path))
    handlers = loggers[log_config.LOG_NAME].handlers
    file_handler = next(h for h in handlers if isinstance(h, RotatingFileHandler))
    try:
        # 超过两次轮转容量, 确認仍然只有当前文件和一个备份。
        for i in range(1200):
            file_handler.handle(record(f"{i}: " + "x" * 1024))
        file_handler.flush()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["miair.log", "miair.log.1"]
        assert all(p.stat().st_size <= 500 * 1024 for p in tmp_path.iterdir())
        assert "1199: " in path.read_text(encoding="utf-8")
    finally:
        for handler in handlers:
            handler.close()
