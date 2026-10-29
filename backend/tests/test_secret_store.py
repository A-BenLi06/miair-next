"""配置敏感字段加密 (secret_store) 回归测试"""

import json
import os

from app.engine.config import Config
from app.engine.secret_store import decrypt_value, encrypt_value, load_or_create_key


def _key(tmp_path):
    return load_or_create_key(str(tmp_path))


class TestSecretStorePrimitives:
    def test_roundtrip(self, tmp_path):
        key = _key(tmp_path)
        token = encrypt_value(key, "pass1234")
        assert token.startswith("enc:v1:")
        assert "pass1234" not in token
        assert decrypt_value(key, token) == "pass1234"

    def test_empty_value_passthrough(self, tmp_path):
        key = _key(tmp_path)
        assert encrypt_value(key, "") == ""
        assert decrypt_value(key, "") == ""

    def test_legacy_plaintext_passthrough(self, tmp_path):
        """无前缀的旧明文不加密、原样通过 (平滑迁移的基础)"""
        key = _key(tmp_path)
        assert decrypt_value(key, "legacy-plain-cookie") == "legacy-plain-cookie"

    def test_wrong_key_returns_original_with_warning(self, tmp_path, caplog):
        """enc.key 丢失/轮换后解密失败: 保留原值告警, 不抛异常中断启动"""
        key = _key(tmp_path)
        token = encrypt_value(key, "pass1234")
        other_key = load_or_create_key(str(tmp_path / "other"))
        assert decrypt_value(other_key, token) == token

    def test_key_file_created_with_0600(self, tmp_path):
        _key(tmp_path)
        path = tmp_path / "enc.key"
        assert path.exists()
        assert (os.stat(path).st_mode & 0o777) == 0o600

    def test_corrupted_key_file_regenerated(self, tmp_path):
        (tmp_path / "enc.key").write_text("nothex")
        key = _key(tmp_path)
        assert len(key) == 32


class TestConfigEncryptionIntegration:
    def test_save_writes_ciphertext_to_disk(self, tmp_path):
        cfg = Config(
            conf_path=str(tmp_path),
            account="13800000000",
            password="mi-pass-123",
            cookie="userId=1; passToken=abc",
            notify_feishu_webhook="https://open.feishu.cn/hook/secret-token",
            spotify_api_key="spotify-key",
        )
        cfg.save()
        with open(tmp_path / "config.json", encoding="utf-8") as f:
            data = json.load(f)
        # 落盘内容为密文: 明文子串不可见, 字段带版本前缀
        for field, plain in (
            ("account", "13800000000"),
            ("password", "mi-pass-123"),
            ("cookie", "passToken=abc"),
            ("notify_feishu_webhook", "https://open.feishu.cn/hook/secret-token"),
            ("spotify_api_key", "spotify-key"),
        ):
            assert data[field].startswith("enc:v1:")
            assert plain not in data[field]
        # 非敏感字段仍为明文
        assert data["conf_path"] == str(tmp_path)

    def test_save_then_load_roundtrip(self, tmp_path):
        cfg = Config(
            conf_path=str(tmp_path),
            account="13800000000",
            password="mi-pass-123",
            cookie="userId=1; passToken=abc",
            notify_feishu_webhook="https://open.feishu.cn/hook/secret-token",
            spotify_api_key="spotify-key",
        )
        cfg.save()
        loaded = Config.load(str(tmp_path))
        # 运行时对象始终为明文, 业务代码无感知
        assert loaded.account == "13800000000"
        assert loaded.password == "mi-pass-123"
        assert loaded.cookie == "userId=1; passToken=abc"
        assert loaded.notify_feishu_webhook == "https://open.feishu.cn/hook/secret-token"
        assert loaded.spotify_api_key == "spotify-key"

    def test_legacy_plaintext_config_upgrades_on_save(self, tmp_path):
        """旧版明文配置: 正常加载 → 保存后自动升级为密文"""
        legacy = {
            "account": "13800000000",
            "password": "legacy-pass",
            "cookie": "userId=1; passToken=old",
        }
        (tmp_path / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
        loaded = Config.load(str(tmp_path))
        assert loaded.password == "legacy-pass"
        loaded.save()
        with open(tmp_path / "config.json", encoding="utf-8") as f:
            data = json.load(f)
        assert data["password"].startswith("enc:v1:")
        assert "legacy-pass" not in data["password"]
        # 升级后再加载, 明文还原
        assert Config.load(str(tmp_path)).password == "legacy-pass"

    def test_empty_sensitive_fields_stay_empty(self, tmp_path):
        cfg = Config(conf_path=str(tmp_path))
        cfg.save()
        with open(tmp_path / "config.json", encoding="utf-8") as f:
            data = json.load(f)
        assert data["account"] == ""
        assert data["cookie"] == ""
