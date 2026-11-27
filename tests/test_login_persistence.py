# -*- coding: utf-8 -*-
"""登录态持久化 / 保存配置不冲掉凭据的回归测试。

运行方式（容器内，避免本机缺依赖）：
    docker cp bridge.py xiaoai-ha-bridge:/tmp/t/bridge.py
    docker cp tests xiaoai-ha-bridge:/tmp/t/tests
    docker exec -w /tmp/t xiaoai-ha-bridge sh -c 'mkdir -p logs && python -m unittest discover -s tests -v'
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class _Account:
    """最小化的 MiAccount 替身：只需要 token 属性。"""

    def __init__(self, token=None):
        self.token = token


def _cookie(pass_token: str) -> str:
    return f"userId=123; passToken={pass_token}; serviceToken=svc-old"


class TestSetTokenMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old = (bridge.AUTH_PATH, bridge.TOKEN_PATH)
        bridge.AUTH_PATH = self.root / "auth.json"
        bridge.TOKEN_PATH = self.root / ".mi.token"

    def tearDown(self):
        bridge.AUTH_PATH, bridge.TOKEN_PATH = self._old
        self.tmp.cleanup()

    def test_keeps_micoapi_session_when_restoring_pass_token(self):
        bridge.TOKEN_PATH.write_text(json.dumps({
            "passToken": "P-old", "userId": "42", "deviceId": "DEV1",
            "micoapi": ["ss", "service-token"],
        }), encoding="utf-8")
        bridge.AUTH_PATH.write_text(json.dumps({
            "passToken": "P-new", "userId": "42", "deviceId": "DEV1",
        }), encoding="utf-8")

        acct = _Account({"passToken": "P-old", "userId": "42",
                         "deviceId": "DEV1", "micoapi": ["ss", "service-token"]})
        bridge._set_token(acct, {"xiaomi": {}})

        # auth.json 的 passToken 更新，必须覆盖
        self.assertEqual(acct.token["passToken"], "P-new")
        # .mi.token 里的 micoapi 会话必须保留（旧实现会整份丢掉）
        self.assertEqual(acct.token["micoapi"], ["ss", "service-token"])
        self.assertTrue(bridge._has_mina_session(acct.token))

    def test_restores_session_from_file_even_if_account_token_empty(self):
        bridge.TOKEN_PATH.write_text(json.dumps({
            "passToken": "P", "userId": "42", "deviceId": "DEV1",
            "micoapi": ["ss", "svc"],
        }), encoding="utf-8")
        acct = _Account(None)
        bridge._set_token(acct, {"xiaomi": {}})
        self.assertEqual(acct.token["passToken"], "P")
        self.assertEqual(acct.token["micoapi"], ["ss", "svc"])

    def test_has_mina_session_rejects_broken_shapes(self):
        self.assertFalse(bridge._has_mina_session(None))
        self.assertFalse(bridge._has_mina_session({"micoapi": []}))
        self.assertFalse(bridge._has_mina_session({"micoapi": ["ss", ""]}))
        self.assertTrue(bridge._has_mina_session({"micoapi": ["ss", "svc"]}))


class TestMergeLoginCredentials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old = bridge.AUTH_PATH
        bridge.AUTH_PATH = self.root / "auth.json"

    def tearDown(self):
        bridge.AUTH_PATH = self._old
        self.tmp.cleanup()

    def test_empty_cookie_text_does_not_wipe_saved_credentials(self):
        existing = {"xiaomi": {"username": "u", "cookie_text": _cookie("P1"),
                               "device_did": "DID1", "hardware": "L06A"}}
        incoming = {"xiaomi": {"username": "u", "device_did": "", "hardware": ""}}
        bridge._merge_login_credentials(incoming, existing)
        self.assertEqual(incoming["xiaomi"]["cookie_text"], _cookie("P1"))
        self.assertEqual(incoming["xiaomi"]["device_did"], "DID1")
        self.assertEqual(incoming["xiaomi"]["hardware"], "L06A")

    def test_stale_cookie_from_old_page_is_rejected(self):
        bridge.AUTH_PATH.write_text(json.dumps({"passToken": "P-new"}), encoding="utf-8")
        existing = {"xiaomi": {"username": "u", "cookie_text": _cookie("P-new")}}
        incoming = {"xiaomi": {"username": "u", "cookie_text": _cookie("P-old")}}
        bridge._merge_login_credentials(incoming, existing)
        self.assertEqual(incoming["xiaomi"]["cookie_text"], _cookie("P-new"))

    def test_switching_account_is_not_blocked(self):
        bridge.AUTH_PATH.write_text(json.dumps({"passToken": "P-new"}), encoding="utf-8")
        existing = {"xiaomi": {"username": "old", "cookie_text": _cookie("P-new")}}
        incoming = {"xiaomi": {"username": "new", "cookie_text": _cookie("P-old")}}
        bridge._merge_login_credentials(incoming, existing)
        self.assertEqual(incoming["xiaomi"]["cookie_text"], _cookie("P-old"))


class TestConfigEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old = (bridge.CONFIG_PATH, bridge.AUTH_PATH, bridge.TOKEN_PATH,
                     bridge._active_parser)
        bridge.CONFIG_PATH = self.root / "config.yaml"
        bridge.AUTH_PATH = self.root / "auth.json"
        bridge.TOKEN_PATH = self.root / ".mi.token"
        bridge.AUTH_PATH.write_text(json.dumps({"passToken": "P-new"}), encoding="utf-8")
        bridge.save_config({
            "xiaomi": {"username": "u", "password": "pw", "cookie_text": _cookie("P-new"),
                       "device_did": "DID1", "hardware": "L06A"},
            "homeassistant": {"url": "http://ha", "token": "t"},
            "intent_rules": [{"pattern": "开灯", "action": {"domain": "light"}}],
        })

    def tearDown(self):
        (bridge.CONFIG_PATH, bridge.AUTH_PATH, bridge.TOKEN_PATH,
         bridge._active_parser) = self._old
        self.tmp.cleanup()

    def test_save_rules_only_touches_rules_and_hot_reloads(self):
        bridge._active_parser = bridge.IntentParser([{"pattern": "旧", "action": {}}])
        rules = [{"pattern": "打开客厅灯", "action": {"domain": "light", "service": "turn_on"}}]
        out = asyncio.run(bridge.save_rules(bridge.SaveRulesRequest(rules=rules)))

        cfg = bridge.load_config()
        self.assertTrue(out["ok"])
        self.assertEqual(cfg["intent_rules"], rules)
        self.assertEqual(cfg["xiaomi"]["cookie_text"], _cookie("P-new"))
        self.assertEqual(cfg["xiaomi"]["password"], "pw")
        self.assertEqual(bridge._active_parser.rules, rules)

    def test_post_config_keeps_saved_login_credentials(self):
        payload = {
            "xiaomi": {"username": "u", "password": "••••••••", "device_did": "", "hardware": ""},
            "homeassistant": {"url": "http://ha", "token": "••••••••"},
            "intent_rules": [],
        }
        asyncio.run(bridge.post_config(bridge.SaveConfigRequest(config=payload)))
        cfg = bridge.load_config()
        self.assertEqual(cfg["xiaomi"]["cookie_text"], _cookie("P-new"))
        self.assertEqual(cfg["xiaomi"]["device_did"], "DID1")
        self.assertEqual(cfg["xiaomi"]["password"], "pw")


if __name__ == "__main__":
    unittest.main()
