"""SOAP 参数校验回归测试 (P1-01)

缺陷形态:
    soap_handler 直接 int(params.get("DesiredVolume", "50")), 无鉴权的 DLNA
    控制端点收到 DesiredVolume="abc" 时抛未捕获 ValueError, 经 aiohttp 变成
    HTTP 500 + 堆栈日志。同类问题还有 SetMute 不校验取值、Seek 对非法产物
    静默返回 200 (renderer._parse_time 解析失败返回 0.0, 等价于"跳转到开头"
    却告诉控制点成功)。

期望行为 (UPnP AV / RenderingControl 规范):
    参数缺失或非法 -> 402 Invalid Args 的 SOAP Fault (HTTP 500 承载 Fault 体)
    动作执行异常   -> 501 Action Failed 的 SOAP Fault
    合法请求       -> 200 + 正常响应
"""

import asyncio
import re
import xml.etree.ElementTree as ET

import pytest

from app.engine.const import (
    AVTRANSPORT_URN,
    RENDERING_CONTROL_URN,
    UPNP_ERROR_ACTION_FAILED,
    UPNP_ERROR_INVALID_ARGS,
)
from app.engine.dlna.renderer import DLNARenderer
from app.engine.dlna.soap_handler import handle_soap_request

UPNP_NS = "urn:schemas-upnp-org:control-1-0"


class FakeSpeaker:
    """记录调用, 便于断言非法参数不会真的下发音箱"""

    did = "did-fake"

    def __init__(self):
        self.volume_calls: list[int] = []
        self.mute_calls: list[bool] = []

    async def set_volume(self, volume: int) -> bool:
        self.volume_calls.append(volume)
        return True

    async def get_volume(self) -> int:
        return 50

    async def set_mute(self, mute: bool) -> bool:
        self.mute_calls.append(mute)
        return True

    async def play_url(self, url, audio_id=None) -> bool:
        return True

    async def stop(self) -> bool:
        return True

    async def pause(self) -> bool:
        return True

    async def get_status(self) -> dict:
        return {"status": 0, "volume": 50}


@pytest.fixture
def renderer():
    return DLNARenderer("udn-test", "test", FakeSpeaker())


def _fault_code(xml: str) -> int | None:
    """从 SOAP Fault 体里取出 UPnP errorCode"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    node = root.find(f".//{{{UPNP_NS}}}errorCode")
    if node is None or node.text is None:
        return None
    return int(node.text)


def _call(renderer, urn: str, action: str, params: dict):
    return asyncio.run(handle_soap_request(renderer, urn, action, params))


# ---------- SetVolume ----------


@pytest.mark.parametrize("bad", ["abc", "", "   ", "1.5", "50abc", "nan"])
def test_set_volume_rejects_non_integer(renderer, bad):
    """非整数必须回 402, 不得抛 ValueError 变成 HTTP 500 空响应"""
    xml, status = _call(
        renderer, RENDERING_CONTROL_URN, "SetVolume", {"DesiredVolume": bad}
    )
    assert _fault_code(xml) == UPNP_ERROR_INVALID_ARGS
    assert status == 500  # UPnP 惯例: Fault 体以 HTTP 500 承载
    assert renderer.speaker.volume_calls == [], "非法参数不应下发音箱"


@pytest.mark.parametrize("bad", ["-1", "-100", "101", "1000"])
def test_set_volume_rejects_out_of_range(renderer, bad):
    """越界值回 402 而非静默截断 (SCPD 声明 Volume 范围 0-100)"""
    xml, _ = _call(
        renderer, RENDERING_CONTROL_URN, "SetVolume", {"DesiredVolume": bad}
    )
    assert _fault_code(xml) == UPNP_ERROR_INVALID_ARGS
    assert renderer.speaker.volume_calls == []


def test_set_volume_missing_arg_is_invalid(renderer):
    """SCPD 将 DesiredVolume 声明为必填, 缺失回 402 (而非静默设为 50)"""
    xml, _ = _call(renderer, RENDERING_CONTROL_URN, "SetVolume", {})
    assert _fault_code(xml) == UPNP_ERROR_INVALID_ARGS
    assert renderer.speaker.volume_calls == []


@pytest.mark.parametrize("value,expected", [("0", 0), ("50", 50), ("100", 100)])
def test_set_volume_accepts_boundaries(renderer, value, expected):
    """边界值 0 / 100 必须放行"""
    xml, status = _call(
        renderer, RENDERING_CONTROL_URN, "SetVolume", {"DesiredVolume": value}
    )
    assert status == 200
    assert _fault_code(xml) is None
    assert renderer.speaker.volume_calls == [expected]


def test_set_volume_tolerates_whitespace(renderer):
    """控制点常带空白, 应 strip 后接受"""
    _, status = _call(
        renderer, RENDERING_CONTROL_URN, "SetVolume", {"DesiredVolume": " 42 "}
    )
    assert status == 200
    assert renderer.speaker.volume_calls == [42]


# ---------- SetMute ----------


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("TRUE", True),
                                            ("yes", True), ("0", False),
                                            ("false", False), ("no", False)])
def test_set_mute_accepts_documented_values(renderer, value, expected):
    """静音在渲染器层是「音量置 0 / 恢复原音量」实现的, 故断言 mute 状态"""
    renderer.volume = 40
    _, status = _call(
        renderer, RENDERING_CONTROL_URN, "SetMute", {"DesiredMute": value}
    )
    assert status == 200
    assert renderer.mute is expected
    if expected:
        assert renderer.speaker.volume_calls == [0]
    else:
        # 未静音时再取消静音属幂等, 不应触碰音量
        assert renderer.speaker.volume_calls == []


@pytest.mark.parametrize("bad", ["xx", "2", "on", "maybe"])
def test_set_mute_rejects_invalid(renderer, bad):
    """原实现用 `in ("1","true","True")` 判断, 任意垃圾值一律当 False (取消静音),
    属静默误动作; 现应回 402"""
    xml, _ = _call(renderer, RENDERING_CONTROL_URN, "SetMute", {"DesiredMute": bad})
    assert _fault_code(xml) == UPNP_ERROR_INVALID_ARGS
    assert renderer.mute is False
    assert renderer.speaker.volume_calls == []


# ---------- Seek ----------


@pytest.mark.parametrize(
    "bad",
    ["garbage", "00:01:2x", "", "1:2:3:4", "-00:00:10", "00:-1:00", "00:1.5:30"],
)
def test_seek_rejects_malformed_target(renderer, bad):
    """原实现静默返回 200 (等价于跳转到 0 秒), 控制点误以为成功"""
    xml, _ = _call(
        renderer, AVTRANSPORT_URN, "Seek", {"Unit": "REL_TIME", "Target": bad}
    )
    assert _fault_code(xml) == UPNP_ERROR_INVALID_ARGS


@pytest.mark.parametrize("bad_unit", ["BYTES", "rel_time", "", "TRACK"])
def test_seek_rejects_unknown_mode(renderer, bad_unit):
    """SCPD 只声明 REL_TIME / TRACK_NR"""
    xml, _ = _call(
        renderer, AVTRANSPORT_URN, "Seek",
        {"Unit": bad_unit, "Target": "00:00:10"},
    )
    assert _fault_code(xml) == UPNP_ERROR_INVALID_ARGS


@pytest.mark.parametrize("target", ["00:00:10", "0:10", "01:02:03", "00:00:00.5"])
def test_seek_accepts_valid_target(renderer, target):
    xml, status = _call(
        renderer, AVTRANSPORT_URN, "Seek", {"Unit": "REL_TIME", "Target": target}
    )
    assert status == 200
    assert _fault_code(xml) is None


# ---------- 兜底 ----------


def test_unexpected_error_becomes_action_failed_fault(renderer, monkeypatch):
    """协议层未预期异常也要转成规范 Fault, 不能冒泡成空响应"""

    async def boom(*args, **kwargs):
        raise RuntimeError("模拟内部故障")

    monkeypatch.setattr(renderer, "play", boom)
    xml, status = _call(renderer, AVTRANSPORT_URN, "Play", {})
    assert _fault_code(xml) == UPNP_ERROR_ACTION_FAILED
    assert status == 500


def test_fault_body_is_wellformed_xml(renderer):
    """Fault 体必须是可解析的 XML 且描述被转义 (防注入)"""
    xml, _ = _call(
        renderer, RENDERING_CONTROL_URN, "SetVolume",
        {"DesiredVolume": "<evil/>&x"},
    )
    assert _fault_code(xml) == UPNP_ERROR_INVALID_ARGS
    # 非法输入中的标签不得作为元素出现在响应里
    assert not re.search(r"<evil\s*/?>", xml)
