"""缓冲回收必须优先淘汰闲置项，保护当前播放与代理请求。"""

import asyncio
from types import SimpleNamespace

from app.engine.dlna.device_server import DeviceServer
from app.engine.dlna.media_buffer import MediaBuffer


def _buffer(url: str, size: int) -> MediaBuffer:
    buf = MediaBuffer(url)
    buf.data = bytearray(size)
    buf.download_complete = True
    return buf


def test_memory_cleanup_evicts_idle_buffer_before_active_playback():
    server = DeviceServer("127.0.0.1", 8200)
    server._max_buffer_memory = 10
    active_url = "http://media.local/active.mp3"
    idle_url = "http://media.local/idle.mp3"
    server._media_buffers = {
        "active": _buffer(active_url, 8),
        "idle": _buffer(idle_url, 8),
    }
    server._url_to_buffer = {active_url: "active", idle_url: "idle"}
    server.renderers["renderer"] = SimpleNamespace(
        current_uri=active_url,
        next_uri="",
    )

    server._cleanup_by_memory()

    assert "active" in server._media_buffers
    assert "idle" not in server._media_buffers


def test_memory_cleanup_keeps_active_buffer_when_no_idle_candidate():
    server = DeviceServer("127.0.0.1", 8200)
    server._max_buffer_memory = 4
    active_url = "http://media.local/active.mp3"
    server._media_buffers = {"active": _buffer(active_url, 8)}
    server._url_to_buffer = {active_url: "active"}
    server.renderers["renderer"] = SimpleNamespace(
        current_uri=active_url,
        next_uri="",
    )

    server._cleanup_by_memory()

    assert "active" in server._media_buffers


def test_buffer_count_limit_does_not_evict_active_sources():
    server = DeviceServer("127.0.0.1", 8200)
    for index in range(10):
        url = f"http://media.local/{index}.mp3"
        server._media_buffers[str(index)] = _buffer(url, 1)
        server._url_to_buffer[url] = str(index)
        server.renderers[str(index)] = SimpleNamespace(current_uri=url, next_uri="")

    assert server._cleanup_old_buffers() is False
    assert len(server._media_buffers) == 10


def test_incomplete_downloads_reserve_worst_case_memory():
    server = DeviceServer("127.0.0.1", 8200)
    server._media_buffers = {
        "first": MediaBuffer("http://media.local/first.mp3"),
        "second": MediaBuffer("http://media.local/second.mp3"),
        "third": MediaBuffer("http://media.local/third.mp3"),
    }

    assert server._cleanup_old_buffers() is False


def test_seek_result_is_not_added_when_capacity_fills_during_processing():
    server = DeviceServer("127.0.0.1", 8200)
    source_url = "http://media.local/song.mp3"
    source = _buffer(source_url, 16)
    source.total_size = len(source.data)
    server._media_buffers = {"source": source}
    server._url_to_buffer = {source_url: "source"}
    server._cleanup_old_buffers = lambda: False

    async def fake_seek(*_args):
        return b"seeked-audio"

    server._ffmpeg_seek = fake_seek

    result = asyncio.run(server.create_seek_url(source_url, 1, 10))

    assert result is None
    assert set(server._media_buffers) == {"source"}
