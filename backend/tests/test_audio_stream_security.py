"""音频流会话令牌与并发连接限制测试。"""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from app.engine.audio_stream import _MAX_STREAM_CLIENTS, AudioStreamServer, with_cache_buster


def test_audio_stream_requires_random_session_token():
    stream = AudioStreamServer("127.0.0.1", 0)
    stream._active = True
    token = stream._session_id
    assert len(token) >= 40

    rejected = stream._authorize_stream_request(SimpleNamespace(query={}))
    assert rejected.status == 404
    assert stream._client_count == 0

    rejected = stream._authorize_stream_request(
        SimpleNamespace(query={"sid": "guessable-timestamp"})
    )
    assert rejected.status == 404
    assert stream._client_count == 0

    assert stream._authorize_stream_request(SimpleNamespace(query={"sid": token})) is None
    assert stream._client_count == 1
    stream._release_stream_client()
    assert stream._client_count == 0


def test_audio_stream_limits_concurrent_clients():
    stream = AudioStreamServer("127.0.0.1", 0)
    stream._active = True
    request = SimpleNamespace(query={"sid": stream._session_id})

    for _ in range(_MAX_STREAM_CLIENTS):
        assert stream._authorize_stream_request(request) is None
    rejected = stream._authorize_stream_request(request)
    assert rejected.status == 429
    assert stream._client_count == _MAX_STREAM_CLIENTS

    for _ in range(_MAX_STREAM_CLIENTS):
        stream._release_stream_client()


def test_airplay_flush_keeps_current_stream_token_but_record_rotates_it():
    stream = AudioStreamServer("127.0.0.1", 0)
    stream.start_streaming()
    record_token = stream._session_id

    # FLUSH / FLUSHBUFFERED 重置音频队列，但不会通知音箱改用新 URL。
    stream.start_streaming(new_session=False)
    assert stream._session_id == record_token
    assert stream._authorize_stream_request(
        SimpleNamespace(query={"sid": record_token})
    ) is None
    stream._release_stream_client()

    # 新 RECORD 会通知音箱新的 stream URL，可以安全轮换会话令牌。
    stream.start_streaming()
    assert stream._session_id != record_token
    rejected = stream._authorize_stream_request(
        SimpleNamespace(query={"sid": record_token})
    )
    assert rejected.status == 404


def test_cache_buster_preserves_audio_stream_token():
    source = "http://speaker.local:8000/spotify/stream.wav?sid=random-secret"
    updated = with_cache_buster(source)
    parsed = urlsplit(updated)

    assert parse_qs(parsed.query)["sid"] == ["random-secret"]
    assert parse_qs(parsed.query)["_"]
