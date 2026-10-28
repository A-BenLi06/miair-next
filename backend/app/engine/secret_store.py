"""配置敏感字段加密 (AES-256-GCM)

config.json 含小米账号密码 / Cookie / Spotify API Key 等长期凭据,
虽有 0600 权限保护, 仍存在误拷贝 (备份 / 排障外发) 导致凭据泄露的
路径。本模块在 Config.save/load 的序列化层透明加解密: 内存中的
Config 对象始终为明文 (业务代码 / 掩码回写流程无感知), 落盘为密文。

- 密钥: conf 目录下 enc.key (首次使用时生成, 0600, hex 编码 32 字节),
  与配置同目录, 备份迁移时一起带走
- 格式: "enc:v1:" + base64(nonce[12] + tag[16] + ciphertext)
- 兼容: 无前缀的旧明文配置正常加载, 下次保存自动升级为密文
- 解密失败 (enc.key 丢失 / 密文损坏) 时保留原值并告警, 服务不中断,
  凭据需重新配置 —— 不可用优先于静默的数据损坏
- 依赖: PyCryptodome (与 AirPlay fairplay 共用既有运行时依赖)
"""

from __future__ import annotations

import base64
import logging
import os

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

log = logging.getLogger("miair")

# 密文前缀: 版本化便于未来更换算法; 旧明文无前缀, 加载时原样通过
_PREFIX = "enc:v1:"
_NONCE_SIZE = 12
_TAG_SIZE = 16
_KEY_PATH = "enc.key"


def load_or_create_key(conf_path: str) -> bytes:
    """读取 (或首次生成) conf 目录下的加密密钥, 返回 32 字节"""
    path = os.path.join(conf_path, _KEY_PATH)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                key = bytes.fromhex(f.read().strip())
            if len(key) == 32:
                return key
            log.warning(f"enc.key 长度异常, 重新生成: {path}")
        except ValueError:
            log.warning(f"enc.key 内容损坏 (非 hex), 重新生成: {path}")
    os.makedirs(conf_path, exist_ok=True)
    key = get_random_bytes(32)
    # 0600 创建 (与 config.json / secret.key 同基线), 消除全局可读窗口
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key.hex())
    return key


def encrypt_value(key: bytes, plaintext: str) -> str:
    """加密为带前缀的 base64 密文; 空串原样返回 (与未配置等价)"""
    if not plaintext:
        return plaintext
    nonce = get_random_bytes(_NONCE_SIZE)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return _PREFIX + base64.b64encode(nonce + tag + ciphertext).decode("ascii")


def decrypt_value(key: bytes, value: str) -> str:
    """解密带前缀的密文; 旧明文 (无前缀) 原样返回"""
    if not value.startswith(_PREFIX):
        return value
    try:
        raw = base64.b64decode(value[len(_PREFIX):], validate=True)
        nonce = raw[:_NONCE_SIZE]
        tag = raw[_NONCE_SIZE:_NONCE_SIZE + _TAG_SIZE]
        ciphertext = raw[_NONCE_SIZE + _TAG_SIZE:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning(f"配置字段解密失败 (enc.key 丢失或密文损坏?): {e}")
        return value
