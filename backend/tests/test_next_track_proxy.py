"""DLNA 切歌恢复媒体代理访问的回归测试。"""

import asyncio

from app.engine.dlna import renderer as renderer_module
from app.engine.dlna.renderer import DLNARenderer


class FakeSpeaker:
    did = "did-test"

    def __init__(self, proxy_state):
        self.proxy_state = proxy_state
        self.played_urls = []

    async def stop(self):
        return True

    async def play_url(self, url, audio_id=None):
        assert self.proxy_state["paused"] is False
        self.played_urls.append(url)
        return False  # 不创建后台播放状态轮询任务


def test_next_track_resumes_proxy_before_play_command(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(renderer_module.asyncio, "sleep", no_wait)
    proxy_state = {"paused": False}
    speaker = FakeSpeaker(proxy_state)
    renderer = DLNARenderer("udn-test", "speaker-test", speaker)
    renderer.next_uri = "https://media.example/song.mp3"
    renderer.proxy_url_func = lambda uri, _udn: f"http://127.0.0.1/media/{uri}"
    renderer.abort_proxy_func = lambda _udn: proxy_state.update(paused=True)
    renderer.resume_proxy_func = lambda _udn: proxy_state.update(paused=False)

    asyncio.run(renderer.next_track())

    assert proxy_state["paused"] is False
    assert speaker.played_urls == [
        "http://127.0.0.1/media/https://media.example/song.mp3"
    ]


def test_next_uri_starts_prebuffering():
    renderer = DLNARenderer("udn-test", "speaker-test", FakeSpeaker({"paused": False}))
    buffered = []
    renderer.pre_buffer_func = buffered.append

    asyncio.run(
        renderer.set_next_av_transport_uri("https://media.example/next.mp3", "metadata")
    )

    assert renderer.next_uri == "https://media.example/next.mp3"
    assert buffered == ["https://media.example/next.mp3"]
