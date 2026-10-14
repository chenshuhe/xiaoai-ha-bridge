#!/usr/bin/env python3
"""
小爱同学 → Home Assistant 语音控制桥接器 v2
Web 配置版 | 端口 47521

依赖：
  pip install miservice aiohttp pyyaml fastapi uvicorn[standard] python-multipart
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ──────────────── 常量 ────────────────
VERSION = "3.11.3"
CONFIG_PATH = Path("config/config.yaml")
LOG_PATH = Path("logs/bridge.log")
TOKEN_PATH = Path("config/.mi.token")
AUTH_PATH = Path("config/auth.json")
WEB_PORT = 47521

LATEST_ASK_API = "https://userprofile.mina.mi.com/device_profile/v2/conversation?source=dialogu&hardware={hardware}&timestamp={timestamp}&limit=2"
COOKIE_TEMPLATE = "deviceId={device_id}; serviceToken={service_token}; userId={user_id}"
GET_ASK_BY_MINA = ["M01"]

# QR 扫码登录状态
_qr_state: dict = {}

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# ──────────────── 日志 ────────────────
log_records: list[dict] = []  # 内存中保留最近 200 条，供前端实时查看


class MemHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        log_records.append(
            {
                "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "msg": self.format(record),
            }
        )
        if len(log_records) > 200:
            log_records.pop(0)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        MemHandler(),
    ],
)
log = logging.getLogger(__name__)

# 接入 miservice 库的 DEBUG 日志，便于诊断登录问题
logging.getLogger("miservice").setLevel(logging.INFO)
logging.getLogger("miservice.miaccount").setLevel(logging.INFO)

# ──────────────── 运行状态 ────────────────
bridge_state = {
    "running": False,
    "connected": False,
    "last_command": None,
    "last_command_time": None,
    "command_count": 0,
    "error": None,
    "start_time": None,
    "pending_clarify": None,   # 正在等待用户回答的澄清问题
}

_bridge_task: asyncio.Task | None = None
_bridge_session: aiohttp.ClientSession | None = None


# ──────────────── 配置 ────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        default = _default_config()
        save_config(default)
        return default
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _default_config() -> dict:
    return {
        "xiaomi": {
            "username": "",
            "password": "",
            "hardware": "L06A",
            "device_did": "",
            "tts_reply": True,
        },
        "homeassistant": {
            "url": "http://homeassistant.local:8123",
            "token": "",
        },
        "openai": {
            "api_base": "https://api.openai.com",
            "api_key": "",
            "model": "gpt-4o-mini",
        },
        "ai_mode": False,
        # ── AI 托管行为（全部在调用点用 cfg.get(key, default) 读取，
        #    即使前端整份覆盖时漏了某个键，运行时也只会静默回落默认值）──
        "ai_fallback": "native",        # native=交给小爱原生 / reply=AI生成回复 / prompt=固定话术
        "ai_fallback_text": "抱歉，我没听懂这个指令，请换个说法。",
        "ai_context_enabled": True,     # 多轮上下文（承接"把它关掉"）
        "ai_context_turns": 4,          # 注入最近几轮对话
        "ai_clarify_enabled": True,     # 缺关键信息时反问
        "ai_clarify_ttl": 30,           # 待澄清问题有效期（秒）
        "ai_device_filter": True,       # 首轮用精简设备清单（miss 时自动全量重试）
        # ── 版本更新 ──
        "update_repo": "chenshuhe/xiaoai-ha-bridge",
        "update_proxy": "",             # 访问 GitHub 的代理，形如 http://代理地址:端口（留空=直连）
        "device_aliases": {},
        "event_monitors": [],
        "poll_interval_seconds": 2,
        "intent_rules": [
            {
                "pattern": "(打开|开)(客厅灯|客厅的灯)",
                "action": {
                    "domain": "light",
                    "service": "turn_on",
                    "entity_id": "light.living_room",
                    "reply": "好的，客厅灯已打开",
                },
            },
            {
                "pattern": "(关闭|关)(客厅灯|客厅的灯)",
                "action": {
                    "domain": "light",
                    "service": "turn_off",
                    "entity_id": "light.living_room",
                    "reply": "好的，客厅灯已关闭",
                },
            },
            {
                "pattern": "把客厅灯调到{num}[%％]",
                "action": {
                    "domain": "light",
                    "service": "turn_on",
                    "entity_id": "light.living_room",
                    "brightness_pct": "{0}",
                    "reply": "好的，亮度已调整",
                },
            },
            {
                "pattern": "(打开|开)(空调|冷气)",
                "action": {
                    "domain": "climate",
                    "service": "turn_on",
                    "entity_id": "climate.living_room_ac",
                    "reply": "好的，空调已打开",
                },
            },
            {
                "pattern": "(关闭|关)(空调|冷气)",
                "action": {
                    "domain": "climate",
                    "service": "turn_off",
                    "entity_id": "climate.living_room_ac",
                    "reply": "好的，空调已关闭",
                },
            },
            {
                "pattern": "把温度设[到为]?{num}度",
                "action": {
                    "domain": "climate",
                    "service": "set_temperature",
                    "entity_id": "climate.living_room_ac",
                    "temperature": "{0}",
                    "reply": "好的，温度已设置",
                },
            },
            {
                "pattern": "(睡觉|晚安)模式",
                "action": {
                    "domain": "scene",
                    "service": "turn_on",
                    "entity_id": "scene.sleep",
                    "reply": "晚安",
                },
            },
        ],
    }


# ──────────────── 意图解析 ────────────────
class IntentParser:
    def __init__(self, rules: list[dict]):
        self.rules = rules

    def parse(self, text: str) -> dict | None:
        text = text.strip()
        for rule in self.rules:
            pattern = rule["pattern"].replace("{num}", r"(\d+)")
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                action = dict(rule["action"])
                groups = m.groups()
                for key, val in action.items():
                    if isinstance(val, str):
                        for i, g in enumerate(groups):
                            val = val.replace(f"{{{i}}}", g or "")
                        action[key] = self._convert_value(val)
                return action
        return None

    @staticmethod
    def _convert_value(val: str):
        try:
            return int(val)
        except ValueError:
            try:
                return float(val)
            except ValueError:
                return val


# ──────────────── HA 客户端 ────────────────
class HAClient:
    def __init__(self, url: str, token: str):
        self.base = url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def test_connection(self, session: aiohttp.ClientSession) -> bool:
        try:
            async with session.get(
                f"{self.base}/api/", headers=self.headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                return r.status == 200
        except Exception:
            return False

    async def call_service(
        self, domain: str, service: str, data: dict, session: aiohttp.ClientSession
    ) -> bool:
        url = f"{self.base}/api/services/{domain}/{service}"
        try:
            async with session.post(
                url, json=data, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                return r.status in (200, 201)
        except Exception as e:
            log.error("HA 调用异常: %s", e)
            return False

    async def create_automation(self, automation_id: str, config: dict,
                                session: aiohttp.ClientSession) -> bool:
        url = f"{self.base}/api/config/automation/config/{automation_id}"
        try:
            async with session.post(url, json=config, headers=self.headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status in (200, 201)
        except Exception as e:
            log.error("创建自动化异常: %s", e)
            return False

    async def reload_automations(self, session: aiohttp.ClientSession) -> bool:
        url = f"{self.base}/api/services/automation/reload"
        try:
            async with session.post(url, json={}, headers=self.headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status in (200, 201)
        except Exception as e:
            log.error("重载自动化异常: %s", e)
            return False

    async def list_automation_states(self, session: aiohttp.ClientSession) -> list[dict]:
        url = f"{self.base}/api/states"
        try:
            async with session.get(url, headers=self.headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                states = await r.json()
                return [
                    {
                        "entity_id": s["entity_id"],
                        "state": s.get("state", ""),
                        "alias": s.get("attributes", {}).get("friendly_name", ""),
                        "last_triggered": s.get("attributes", {}).get("last_triggered"),
                    }
                    for s in states
                    if s.get("entity_id", "").startswith("automation.xiaoai_bridge_")
                ]
        except Exception as e:
            log.error("获取自动化列表异常: %s", e)
            return []

    async def delete_automation(self, automation_id: str,
                                session: aiohttp.ClientSession) -> bool:
        url = f"{self.base}/api/config/automation/config/{automation_id}"
        try:
            async with session.delete(url, headers=self.headers,
                                      timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status == 200
        except Exception as e:
            log.error("删除自动化异常: %s", e)
            return False


# ──────────────── 桥接主循环 ────────────────
def _set_token(account, cfg: dict):
    """从 auth.json 或 cookie 恢复 token（参考 xiaomusic set_token）"""
    mi_cfg = cfg.get("xiaomi", {})
    if AUTH_PATH.exists():
        try:
            with open(AUTH_PATH, encoding="utf-8") as f:
                user_data = json.load(f)
            account.token = {
                "passToken": user_data["passToken"],
                "userId": user_data["userId"],
                "deviceId": user_data.get("deviceId", ""),
            }
            log.info("已从 auth.json 恢复 token")
            return
        except Exception as e:
            log.warning("auth.json 读取失败: %s", e)

    cookie_text = mi_cfg.get("cookie_text", "")
    if cookie_text:
        from http.cookies import SimpleCookie
        sc = SimpleCookie()
        sc.load(cookie_text)
        cookies_dict = {k: m.value for k, m in sc.items()}
        account.token = {
            "passToken": cookies_dict.get("passToken", ""),
            "userId": cookies_dict.get("userId", ""),
            "deviceId": cookies_dict.get("deviceId", ""),
        }


def _save_auth_and_token(account) -> None:
    """登录成功后保存 auth.json 和 .mi.token（参考 xiaomusic）"""
    if not hasattr(account, 'token') or not account.token:
        return
    try:
        AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        auth_data = {
            "passToken": account.token.get("passToken", ""),
            "userId": account.token.get("userId", ""),
            "deviceId": account.token.get("deviceId", ""),
        }
        with open(AUTH_PATH, "w", encoding="utf-8") as f:
            json.dump(auth_data, f, ensure_ascii=False)
        log.info("已保存 auth.json")
    except Exception as e:
        log.warning("保存 auth.json 失败: %s", e)
    try:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump(account.token, f, ensure_ascii=False)
        log.info("已保存 .mi.token")
    except Exception as e:
        log.warning("保存 .mi.token 失败: %s", e)


def _get_cookie(cfg: dict) -> dict | None:
    """从 .mi.token 或配置的 cookie_text 构建 Cookie（参考 xiaomusic get_cookie）"""
    mi_cfg = cfg.get("xiaomi", {})

    if mi_cfg.get("cookie_text"):
        from http.cookies import SimpleCookie
        from aiohttp import CookieJar
        sc = SimpleCookie()
        sc.load(mi_cfg["cookie_text"])
        cookies_dict = {k: m.value for k, m in sc.items()}
        return cookies_dict

    if not TOKEN_PATH.exists():
        return None

    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            user_data = json.load(f)
        user_id = user_data.get("userId")
        micoapi = user_data.get("micoapi")
        if not micoapi:
            return None
        service_token = micoapi[1]
        device_id = mi_cfg.get("device_id", "") or mi_cfg.get("device_did", "")
        cookie_string = COOKIE_TEMPLATE.format(
            device_id=device_id, service_token=service_token, user_id=user_id
        )
        from http.cookies import SimpleCookie
        sc = SimpleCookie()
        sc.load(cookie_string)
        return {k: m.value for k, m in sc.items()}
    except Exception as e:
        log.warning("读取 .mi.token 失败: %s", e)
        return None


def _has_xiaomi_credentials(cfg: dict) -> bool:
    """是否具备可用的登录凭据（账号密码 / cookie_text / auth.json / .mi.token 任一）。

    扫码登录只写 auth.json / .mi.token / cookie_text，不写账号密码，
    所以不能用"账号密码非空"来判定用户是否完成配置。

    口径有意收紧：落盘文件里**必须含非空 passToken** 才算数。
    只有 deviceId 的 .mi.token 放行没有意义——_set_token 和登录尝试 1/2 都只认
    passToken，提前放行只会把"配置错误"变成"运行期反复登录失败"。
    """
    mi = cfg.get("xiaomi") or {}
    if (mi.get("username") or "") and (mi.get("password") or ""):
        return True
    if (mi.get("cookie_text") or "").strip():
        return True
    for path in (AUTH_PATH, TOKEN_PATH):
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("passToken"):
                    return True
        except Exception:
            pass
    return False


async def _get_latest_ask_from_xiaoai(session: aiohttp.ClientSession, device_id: str,
                                      hardware: str, last_ts: int) -> tuple[list[dict], int, bool]:
    """HTTP API 方式获取最新对话（参考 xiaomusic get_latest_ask_from_xiaoai）
    返回 (records, last_ts, need_relogin)
    """
    cookies = {"deviceId": device_id}
    for i in range(3):
        try:
            url = LATEST_ASK_API.format(
                hardware=hardware, timestamp=str(last_ts)
            )
            timeout = aiohttp.ClientTimeout(total=15)
            r = await session.get(url, timeout=timeout, cookies=cookies)
            log.debug("HTTP轮询: %s → status=%s", url[:120], r.status)
            if r.status != 200:
                log.debug("HTTP 轮询返回 %s (第%d次)", r.status, i + 1)
                if r.status == 401:
                    return [], last_ts, True  # 需要重新登录
                continue
            data = await r.json()
            d = data.get("data")
            if not d:
                return [], last_ts, False
            records = json.loads(d).get("records", [])
            if records:
                return records, last_ts, False
        except Exception as e:
            log.warning("HTTP 轮询异常 (第%d次): %s", i + 1, e)
            continue
    return [], last_ts, False


async def bridge_loop():
    global _bridge_session
    cfg = load_config()
    mi_cfg = cfg.get("xiaomi", {})
    ha_cfg = cfg.get("homeassistant", {})

    # 账号密码不再是必须的：扫码登录只留下 auth.json / .mi.token / cookie_text，
    # 而 passToken 登录路径完全不依赖账号密码（见 miservice MiAccount.login）。
    if not _has_xiaomi_credentials(cfg):
        bridge_state["error"] = "小米账号未登录，请打开配置页扫码登录"
        bridge_state["running"] = False
        return

    if not ha_cfg.get("token"):
        bridge_state["error"] = "Home Assistant Token 未配置"
        bridge_state["running"] = False
        return

    try:
        from miservice import MiAccount, MiNAService
    except ImportError:
        bridge_state["error"] = "缺少依赖: pip install miservice"
        bridge_state["running"] = False
        return

    parser = IntentParser(cfg.get("intent_rules", []))
    ha = HAClient(ha_cfg["url"], ha_cfg["token"])
    poll_interval = cfg.get("poll_interval_seconds", 2)

    # 基于时间戳的去重，key=device_id — 持久化到文件，避免重启重复执行
    LAST_TS_PATH = CONFIG_PATH.parent / "last_timestamp.json"
    last_timestamp: dict[str, int] = {}
    if LAST_TS_PATH.exists():
        try:
            last_timestamp = json.loads(LAST_TS_PATH.read_text())
        except Exception:
            last_timestamp = {}
    log.info("上次记录时间戳: %s", last_timestamp)

    _bridge_session = aiohttp.ClientSession()
    try:
        from miservice.miaccount import get_random

        # ── 登录（三级尝试，参考 xiaomusic 的 passToken 注入绕过 SMS） ──
        account = MiAccount(_bridge_session, mi_cfg.get("username") or "", mi_cfg.get("password") or "",
                           str(TOKEN_PATH) if TOKEN_PATH.parent.exists() else None)
        _set_token(account, cfg)

        if not isinstance(account.token, dict):
            account.token = {'deviceId': get_random(16).upper()}

        na = None
        logged_in = False

        # 尝试1: 用 passToken 绕过密码步骤（xiaomusic 做法，不需要 SMS）
        if account.token.get('passToken'):
            log.info("尝试 passToken 登录...")
            try:
                ok = await account.login("micoapi")
                if ok:
                    logged_in = True
            except Exception as e:
                log.warning("passToken 登录异常: %s，尝试其他方式", e)

        # 尝试2: Cookie 注入到 session 再直接调 device_list
        if not logged_in:
            cookie_text = mi_cfg.get("cookie_text", "")
            if cookie_text:
                log.info("尝试 Cookie 注入登录...")
                try:
                    from http.cookies import SimpleCookie
                    sc = SimpleCookie()
                    sc.load(cookie_text)
                    cookies = {k: m.value for k, m in sc.items()}
                    if cookies.get('userId') and (cookies.get('passToken') or cookies.get('serviceToken')):
                        import yarl
                        sc2 = SimpleCookie()
                        for k, v in cookies.items():
                            try:
                                sc2[k] = v
                            except Exception:
                                pass
                        for domain in ['account.xiaomi.com', '.mina.mi.com']:
                            _bridge_session.cookie_jar.update_cookies(sc2, yarl.URL(f'https://{domain}/'))
                        account2 = MiAccount(_bridge_session, mi_cfg.get("username") or "", mi_cfg.get("password") or "", str(TOKEN_PATH))
                        account2.token = {
                            'deviceId': cookies.get('deviceId', get_random(16).upper()),
                            'userId': cookies['userId'],
                            'passToken': cookies.get('passToken', ''),
                        }
                        resp = await account2._serviceLogin('serviceLogin?sid=micoapi&_json=true')
                        if resp.get('code') != 0:
                            raise Exception(f"Cookie 验证失败: {resp.get('description','')}")
                        account2.token['userId'] = str(resp.get('userId', account2.token['userId']))
                        account2.token['micoapi'] = (
                            resp.get('psecurity', resp.get('ssecurity', '')),
                            cookies.get('serviceToken', ''),
                        )
                        account = account2
                        na = MiNAService(account)
                        _save_auth_and_token(account)
                        logged_in = True
                        log.info("Cookie 注入登录成功")
                except Exception as e:
                    log.warning("Cookie 注入失败: %s", e)

        # 尝试3: 密码登录（会触发 SMS 验证）
        if not logged_in:
            log.info("尝试密码登录...")
            try:
                ok = await account.login("micoapi")
                if ok:
                    logged_in = True
            except Exception as e:
                log.warning("密码登录异常: %s", e)

        if not logged_in:
            bridge_state["error"] = ("登录失败，请打开配置页面 → 在「Cookie 登录」输入框中粘贴浏览器 Cookie → 点测试连接。"
                                     "Cookie 获取方法：浏览器打开 account.xiaomi.com → F12 → Application → Cookies → 复制全部")
            bridge_state["running"] = False
            return

        # 保存 token
        _save_auth_and_token(account)
        if na is None:
            na = MiNAService(account)

        # 获取设备列表
        try:
            devices = await na.device_list()
            if devices:
                did = mi_cfg.get("device_did", "")
                target = None
                if did:
                    target = next((d for d in devices if d.get("deviceID") == did or d.get("did") == did), None)
                if not target:
                    target = devices[0]
                hw = target.get("hardware", "")
                name = target.get("name", "未知")
                if hw:
                    mi_cfg["hardware"] = hw
                    log.info("自动检测到设备: %s (hardware=%s)", name, hw)
                bridge_state["devices"] = devices
            else:
                log.warning("未发现小爱音箱设备")
        except Exception as e:
            log.warning("获取设备列表失败: %s，将使用配置中的 hardware", e)
            devices = []

        # 注入 cookie 到共享 session
        cookies_dict = _get_cookie(cfg)
        if cookies_dict:
            import yarl
            from http.cookies import SimpleCookie
            sc = SimpleCookie()
            for k, v in cookies_dict.items():
                try:
                    sc[k] = v
                except Exception:
                    pass
            for domain in ['account.xiaomi.com', '.mina.mi.com']:
                _bridge_session.cookie_jar.update_cookies(sc, yarl.URL(f'https://{domain}/'))

        ha_ok = await ha.test_connection(_bridge_session)
        bridge_state["connected"] = ha_ok
        if not ha_ok:
            log.warning("Home Assistant 连接失败，请检查地址和 Token")

        log.info("桥接器已启动 | 轮询间隔 %ss | HA %s", poll_interval, "✓" if ha_ok else "✗")
        bridge_state["error"] = None

        while bridge_state["running"]:
            try:
                device_id = mi_cfg.get("device_id", "") or mi_cfg.get("device_did", "")
                if not device_id and devices:
                    device_id = devices[0].get("deviceID", "")
                if not device_id:
                    await asyncio.sleep(poll_interval)
                    continue

                hardware = mi_cfg.get("hardware", "")
                cur_ts = int(time.time() * 1000)

                # 根据硬件类型选择轮询方式
                if hardware in GET_ASK_BY_MINA:
                    records = await _get_latest_ask_by_mina(na, device_id)
                else:
                    records, _, need_relogin = await _get_latest_ask_from_xiaoai(
                        _bridge_session, device_id, hardware or "L06A", cur_ts
                    )
                    if need_relogin:
                        log.info("Token 已过期，尝试重新登录...")
                        try:
                            # 清除 session 的旧 cookie，避免小米返回精简响应导致 missing nonce
                            _bridge_session.cookie_jar.clear_domain("account.xiaomi.com")
                            _bridge_session.cookie_jar.clear_domain("mina.mi.com")
                            _set_token(account, cfg)
                            ok = await account.login("micoapi")
                            if ok:
                                _save_auth_and_token(account)
                                na = MiNAService(account)
                                # 刷新 cookie
                                cookies_dict = _get_cookie(cfg)
                                if cookies_dict:
                                    import yarl
                                    from http.cookies import SimpleCookie
                                    sc = SimpleCookie()
                                    for k, v in cookies_dict.items():
                                        try:
                                            sc[k] = v
                                        except Exception:
                                            pass
                                    for domain in ['account.xiaomi.com', '.mina.mi.com']:
                                        _bridge_session.cookie_jar.update_cookies(sc, yarl.URL(f'https://{domain}/'))
                                log.info("重新登录成功")
                            else:
                                log.warning("重新登录失败")
                        except Exception as e:
                            log.warning("重新登录异常: %s", e)
                        await asyncio.sleep(poll_interval)
                        continue

                for rec in records or []:
                    # 时间戳去重
                    ts = rec.get("time", rec.get("timestamp_ms", 0))
                    if not ts:
                        log.warning("轮询记录缺少 time 字段, rec keys=%s", list(rec.keys())[:5])
                        continue
                    ts = int(ts)
                    if ts <= last_timestamp.get(device_id, 0):
                        log.debug("跳过旧记录 ts=%s <= last=%s", ts, last_timestamp.get(device_id, 0))
                        continue
                    last_timestamp[device_id] = ts
                    LAST_TS_PATH.write_text(json.dumps(last_timestamp, ensure_ascii=False))

                    # 提取用户问题和助手回复
                    query = _extract_query(rec)
                    answer = _extract_answer(rec)
                    if not query:
                        log.debug("跳过空查询记录 ts=%s answer=%s", ts, (answer or '')[:40])
                        continue
                    log.info("🎤 用户: %s", query)
                    if answer:
                        log.info("🤖 小爱: %s", answer)

                    # ── 唤醒词残片：只喊了一声"小爱同学"，不值得走一次 LLM ──
                    if _is_wake_fragment(query):
                        log.info("💤 唤醒词残片，忽略: %s", query)
                        continue

                    # 循环开头拿到的 cfg 是启动快照，这里重新读，让网页改配置能热生效
                    rt_cfg = load_config()

                    # ── 澄清回答：必须抢在智能去重之前 ──
                    # 用户回答"餐厅"时小爱自己也会产生一条回复记录，若先过
                    # _xiaoi_already_handled 就会被短路，追问永远续不上。
                    ttl = float(rt_cfg.get("ai_clarify_ttl", 30) or 30)
                    pending = await _conversation.claim_pending(ttl)
                    forced = pending is not None
                    if pending:
                        bridge_state["pending_clarify"] = None
                        if _is_cancel(query):
                            log.info("🚫 用户取消澄清: %s", query)
                            if mi_cfg.get("tts_reply", True):
                                await _speak(na, device_id, "好的", interrupt=True)
                            continue
                        clar_actions, _ = await _resolve_clarification(pending, query, rt_cfg)
                        if clar_actions:
                            results = await _execute_actions(
                                clar_actions, ha, na, _bridge_session,
                                device_id=device_id,
                                tts_reply=mi_cfg.get("tts_reply", True))
                            await _conversation.record_turn(
                                query, clar_actions, results, _cached_devices())
                            bridge_state["last_command"] = query
                            bridge_state["last_command_time"] = datetime.now().strftime("%H:%M:%S")
                            bridge_state["command_count"] += 1
                            bridge_state["connected"] = True
                            continue
                        log.info("🤔 澄清回答未映射到候选，按新指令重新解析: %s", query)

                    # ── 智能去重：小爱本机能处理的指令不再重复 ──
                    if not forced and await _xiaoi_already_handled(query, answer):
                        log.info("✅ 小爱已原生处理: %s", query)
                        continue

                    # 跳过音乐播放类指令——由 xiaomusic 处理
                    _music_kw = ["播放本地歌曲", "播放音乐", "播放歌曲", "下一首", "上一首",
                                 "暂停音乐", "继续播放", "本地音乐", "开始播放", "停止播放"]
                    if any(kw in query for kw in _music_kw):
                        log.info("🎵 音乐指令由 xiaomusic 处理，跳过 HA 桥")
                        continue

                    # ── 解析指令（正则 → AI） ──
                    diag: dict = {}
                    actions = await _parse_and_resolve_actions(
                        query, parser, rt_cfg, device_id=device_id, diag=diag)

                    # 信息不足 → 反问，并记下待答状态
                    if diag.get("clarify"):
                        c = diag["clarify"]
                        # 澄清场景**必然**和小爱的原生回答撞车（它会先说一句"没有发现…"）。
                        # 那句是废话，直接打断再反问，否则等它说完要 5-6 秒
                        if not forced and mi_cfg.get("tts_reply", True):
                            await _speak(na, device_id, c["question"], interrupt=True)
                        await _conversation.set_pending({
                            "question": c["question"],
                            "candidates": c["candidates"],
                            "original_query": query,
                            "created_at": time.time(),
                        })
                        bridge_state["pending_clarify"] = _conversation.pending_public()
                        log.info("❓ 已反问，等待用户回答: %s", c["question"])
                        bridge_state["last_command"] = query
                        bridge_state["last_command_time"] = datetime.now().strftime("%H:%M:%S")
                        continue

                    if not actions:
                        log.info("🤷 未匹配任何指令: %s", query)
                        await _handle_ai_fallback(query, rt_cfg, diag, na, device_id, mi_cfg)
                        continue

                    # ── 执行一组动作（支持多步复杂指令） ──
                    results = await _execute_actions(
                        actions, ha, na, _bridge_session,
                        device_id=device_id,
                        tts_reply=mi_cfg.get("tts_reply", True))
                    await _conversation.record_turn(
                        query, actions, results, _cached_devices())

                    bridge_state["last_command"] = query
                    bridge_state["last_command_time"] = datetime.now().strftime("%H:%M:%S")
                    bridge_state["command_count"] += 1
                    bridge_state["connected"] = True
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("轮询错误: %s", e)
                bridge_state["error"] = str(e)

            await asyncio.sleep(poll_interval)

    except Exception as e:
        log.error("桥接器启动失败: %s", e)
        bridge_state["error"] = str(e)
    finally:
        bridge_state["running"] = False
        bridge_state["connected"] = False
        if _bridge_session and not _bridge_session.closed:
            await _bridge_session.close()
        log.info("桥接器已停止")


async def _delayed_call_standalone(ha: HAClient, domain: str, service: str,
                                   data: dict, delay_sec: float):
    """独立的延迟执行（用于测试页面），自带 session"""
    log.info("[DEBUG] _delayed_call_standalone 开始: %s/%s, data=%s, delay=%ss", domain, service, data, delay_sec)
    await asyncio.sleep(delay_sec)
    try:
        async with aiohttp.ClientSession() as s:
            ok = await ha.call_service(domain, service, data, s)
            log.info("⏰ 定时任务完成: %s/%s → %s", domain, service, "成功" if ok else "失败")
    except Exception as e:
        log.error("⏰ 定时任务异常: %s", e)


async def _get_latest_ask_by_mina(na, device_id: str) -> list[dict]:
    """Mina 服务方式获取对话（原有逻辑，适配新格式）"""
    try:
        msgs = await na.get_latest_ask(device_id)
        records = []
        for msg in msgs or []:
            answers = msg.get("response", {}).get("answer", [])
            if not answers:
                continue
            records.append({
                "time": msg.get("timestamp_ms", 0),
                "query": answers[0].get("question", ""),
                "answers": answers,
            })
        return records
    except Exception as e:
        log.warning("Mina 轮询异常: %s", e)
        return []


async def _delayed_call(ha: HAClient, domain: str, service: str,
                       data: dict, delay_sec: float, session: aiohttp.ClientSession):
    """延迟执行 HA 服务调用（用于定时关闭等场景）"""
    log.info("[DEBUG] _delayed_call 开始: %s/%s, data=%s, delay=%ss", domain, service, data, delay_sec)
    await asyncio.sleep(delay_sec)
    try:
        ok = await ha.call_service(domain, service, data, session)
        log.info("⏰ 定时任务完成: %s/%s → %s", domain, service, "成功" if ok else "失败")
    except Exception as e:
        log.error("⏰ 定时任务异常: %s", e)


_AUTOMATION_PREFIX = "xiaoai_bridge_"
_WEEKDAY_MAP = {
    "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
    "fri": "fri", "sat": "sat", "sun": "sun",
    "周一": "mon", "周二": "tue", "周三": "wed", "周四": "thu",
    "周五": "fri", "周六": "sat", "周日": "sun",
}


async def _create_ha_automation(
    ha: HAClient, domain: str, service: str, data: dict,
    schedule_time: str, schedule_days: str | None,
    session: aiohttp.ClientSession,
) -> tuple[str, bool]:
    """在 HA 中创建定时自动化"""
    if not re.match(r"^\d{1,2}:\d{2}$", schedule_time):
        return "", False
    # 补齐 HH:MM:SS 格式
    parts = schedule_time.split(":")
    at_time = f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"

    automation_id = f"{_AUTOMATION_PREFIX}{int(time.time())}"
    entity_id = data.get("entity_id", "")

    # 构建 action
    action_item = {"service": f"{domain}.{service}", "target": {"entity_id": entity_id}}
    extra = {k: v for k, v in data.items() if k != "entity_id"}
    if extra:
        action_item["data"] = extra

    config = {
        "alias": f"小爱桥接-{at_time[:5]} {entity_id}",
        "description": "由小爱语音桥接器自动创建",
        "trigger": [{"platform": "time", "at": at_time}],
        "action": [action_item],
        "mode": "single",
    }

    # 星期条件
    days = (schedule_days or "daily").lower().strip()
    if days in ("weekdays", "工作日"):
        config["condition"] = [{"condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]}]
    elif days in ("weekends", "周末"):
        config["condition"] = [{"condition": "time", "weekday": ["sat", "sun"]}]
    elif days not in ("daily", "每天", ""):
        weekdays = []
        for d in re.split(r"[,，、\s]+", days):
            d = d.strip()
            if d in _WEEKDAY_MAP:
                weekdays.append(_WEEKDAY_MAP[d])
        if weekdays:
            config["condition"] = [{"condition": "time", "weekday": weekdays}]

    ok = await ha.create_automation(automation_id, config, session)
    if ok:
        ok = await ha.reload_automations(session)
    return automation_id, ok


def _extract_answer(rec: dict) -> str:
    """从记录中提取小爱助手的回复"""
    answers = rec.get("answers", [])
    if answers:
        return answers[0].get("tts", {}).get("text", "").strip() or ""
    return ""


def _extract_query(rec: dict) -> str:
    """从记录中提取用户问题（兼容两种 API 格式）"""
    # HTTP API 格式: 优先取 query 字段（用户问题）
    q = rec.get("query", "").strip()
    if q:
        return q
    # Mina 格式: answer[0].question
    answers = rec.get("answers", [])
    if answers:
        q = answers[0].get("question", "").strip()
        if q:
            return q
    return ""


# ──────────────── HA WebSocket 事件监听 ────────────────
EVENT_LOG_PATH = Path("logs/event_monitor.log")
_event_logger = logging.getLogger("event_monitor")
_event_logger.setLevel(logging.INFO)
if not _event_logger.handlers:
    _event_logger.addHandler(logging.FileHandler(EVENT_LOG_PATH, encoding="utf-8"))

# 事件监听 TTS 的缓存实例（避免每次播报都重新登录/获取设备）
_tts_na = None          # MiNAService 实例
_tts_na_session = None  # 共享 aiohttp session
_tts_device_id = ""     # 音箱 deviceID
_tts_lock = asyncio.Lock()


async def _xiaomi_tts(text: str) -> bool:
    """用小爱音箱播报文字（复用 /api/tts 的逻辑，带实例缓存与互斥锁）"""
    global _tts_na, _tts_na_session, _tts_device_id
    cfg = load_config()
    mi_cfg = cfg.get("xiaomi", {})
    if not _has_xiaomi_credentials(cfg):
        log.error("事件监听 TTS 失败: 小米账号未登录，请扫码登录")
        return False
    async with _tts_lock:
        try:
            from miservice import MiAccount, MiNAService
            from miservice.miaccount import get_random

            if _tts_na is None:
                if _tts_na_session is None or _tts_na_session.closed:
                    _tts_na_session = aiohttp.ClientSession()
                token_path = CONFIG_PATH.parent / ".mi.token"
                if token_path.exists():
                    token = json.loads(token_path.read_text())
                else:
                    token = {"deviceId": get_random(16).upper()}
                account = MiAccount(_tts_na_session, mi_cfg.get("username") or "",
                                    mi_cfg.get("password") or "", str(token_path))
                account.token = token
                _tts_na = MiNAService(account)

            # 获取/复用音箱 device_id（优先用配置指定）
            if not _tts_device_id:
                device_id = mi_cfg.get("device_id", "") or mi_cfg.get("device_did", "")
                if not device_id:
                    devices = await _tts_na.device_list()
                    if devices:
                        device_id = devices[0].get("deviceID", "")
                _tts_device_id = device_id

            await _tts_na.text_to_speech(_tts_device_id, text)
            log.info("🎙️ TTS 播报: %s", text)
            _event_logger.info("TTS 播报: %s", text)
            return True
        except Exception as e:
            log.error("事件监听 TTS 播报失败: %s", e)
            _event_logger.error("TTS 播报失败: %s", e)
            # 失败时重置缓存，下次调用重新初始化
            _tts_na = None
            _tts_device_id = ""
            return False


class EventMonitorEngine:
    """监听 Home Assistant 的 state_changed 事件，按规则用小爱音箱自动播报

    独立于 bridge_loop 轮询循环，通过 HA WebSocket 实时接收状态变化。
    断开后自动重连（指数退避，最大间隔 60 秒）。
    """

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._stop = False
        self._retry_delay = 5               # 当前重连间隔（秒），指数退避
        self._cooldowns: dict = {}          # (entity_id, trigger_state) -> 上次播报时间戳
        self._trigger_log: list[dict] = []  # 最近 20 条触发记录，供前端查询
        self.state = {
            "running": False,
            "connected": False,
            "last_error": None,
            "last_connect_time": None,
            "retry_count": 0,
            "next_reconnect_seconds": None,
        }

    # ── 生命周期 ──
    def start(self):
        """启动后台事件循环任务"""
        if self._task and not self._task.done():
            return
        self._stop = False
        self.state["running"] = True
        self._task = asyncio.create_task(self._run())
        log.info("事件监听引擎已启动")

    def stop(self):
        """停止后台事件循环任务"""
        self._stop = True
        self.state["running"] = False
        self.state["connected"] = False
        if self._task and not self._task.done():
            self._task.cancel()
        log.info("事件监听引擎已停止")

    # ── 主循环：连接 + 自动重连 ──
    async def _run(self):
        while not self._stop:
            try:
                await self._connect_and_subscribe()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state["connected"] = False
                self.state["last_error"] = str(e)[:200]
                log.warning("事件监听连接异常: %s", e)
                _event_logger.warning("连接异常: %s", e)
            if self._stop:
                break
            # 指数退避重连（最大 60 秒）
            self.state["retry_count"] += 1
            self.state["next_reconnect_seconds"] = self._retry_delay
            log.info("事件监听将在 %s 秒后重连 (第 %s 次)", self._retry_delay, self.state["retry_count"])
            await asyncio.sleep(self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, 60)

    async def _connect_and_subscribe(self):
        """连接 HA WebSocket → 认证 → 订阅 state_changed → 处理事件"""
        cfg = load_config()
        ha_cfg = cfg.get("homeassistant", {})
        url = ha_cfg.get("url", "")
        token = ha_cfg.get("token", "")
        if not url or not token:
            self.state["last_error"] = "HA URL 或 Token 未配置"
            log.warning("事件监听: HA 未配置，稍后重试")
            return

        # http(s) → ws(s)，并拼接 /api/websocket
        ws_url = (
            url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
            + "/api/websocket"
        )
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        async with self._session.ws_connect(
            ws_url, heartbeat=30,
            timeout=aiohttp.ClientWSTimeout(ws_close=10),
        ) as ws:
            # 连接成功后重置退避间隔
            self._retry_delay = 5
            self.state["retry_count"] = 0
            self.state["next_reconnect_seconds"] = None
            self.state["connected"] = True
            self.state["last_connect_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.state["last_error"] = None
            log.info("✅ 事件监听已连接: %s", ws_url)
            _event_logger.info("已连接: %s", ws_url)

            await self._auth(ws, token)
            await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
            log.info("已订阅 state_changed 事件")
            _event_logger.info("已订阅 state_changed 事件")

            # 持续读取消息直到断开
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except (ValueError, TypeError):
                        continue
                    if data.get("type") == "event":
                        event = data.get("event", {})
                        if event.get("event_type") == "state_changed":
                            await self._handle_state_changed(event.get("data") or {})
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    log.warning("事件监听 WebSocket 连接关闭: %s", msg.type)
                    break
        self.state["connected"] = False

    @staticmethod
    async def _auth(ws, token: str):
        """HA WebSocket 认证握手：auth_required → auth → auth_ok"""
        msg = await ws.receive()
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError(f"WS 握手失败: {msg.type}")
        data = json.loads(msg.data)
        if data.get("type") != "auth_required":
            raise ConnectionError(f"意外握手消息: {data.get('type')}")
        await ws.send_json({"type": "auth", "access_token": token})
        msg = await ws.receive()
        data = json.loads(msg.data)
        if data.get("type") != "auth_ok":
            raise ConnectionError(f"HA 认证失败: {data.get('type')} {data.get('message', '')}")

    # ── 事件处理 ──
    async def _handle_state_changed(self, data: dict):
        entity_id = data.get("entity_id", "")
        new_state = data.get("new_state") or {}
        state = new_state.get("state", "")
        if not entity_id:
            return
        for rule in self._matching_rules(entity_id, state):
            await self._trigger(rule, state)

    def _matching_rules(self, entity_id: str, state: str) -> list[dict]:
        cfg = load_config()
        matched = []
        for r in (cfg.get("event_monitors", []) or []):
            if r.get("entity_id") != entity_id:
                continue
            # 兼容 YAML 把 on/off 解析成布尔值的情况
            want = r.get("trigger_state")
            if isinstance(want, bool):
                want = "on" if want else "off"
            if str(want) == str(state):
                matched.append(r)
        return matched

    async def _trigger(self, rule: dict, state: str):
        entity_id = rule.get("entity_id", "")
        trigger_state = rule.get("trigger_state", state)
        if isinstance(trigger_state, bool):
            trigger_state = "on" if trigger_state else "off"
        trigger_state = str(trigger_state)
        cooldown = int(rule.get("cooldown_seconds", 60) or 60)
        key = (entity_id, trigger_state)
        now = time.time()

        # 冷却去重：同一实体同一状态在 cooldown 内不重复播报
        last = self._cooldowns.get(key, 0)
        if now - last < cooldown:
            log.info("⏳ 事件监听跳过（冷却中 %ss）: %s=%s", cooldown, entity_id, trigger_state)
            return
        self._cooldowns[key] = now

        message = rule.get("tts_message", "")
        if not message:
            return
        ok = await _xiaomi_tts(message)
        self._trigger_log.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "entity_id": entity_id,
            "state": trigger_state,
            "message": message,
            "success": ok,
        })
        if len(self._trigger_log) > 20:
            self._trigger_log.pop(0)
        _event_logger.info("触发播报: %s=%s → %s (%s)", entity_id, trigger_state, message, "成功" if ok else "失败")
        log.info("🎙️ 事件触发播报: %s=%s → %s %s", entity_id, trigger_state, message, "✓" if ok else "✗")

    def prune_cooldowns(self):
        """规则变更后清理已不存在的冷却记录"""
        cfg = load_config()
        rules = cfg.get("event_monitors", []) or []
        keys = {(r.get("entity_id"), str(r.get("trigger_state", ""))) for r in rules}
        for k in list(self._cooldowns):
            if k not in keys:
                self._cooldowns.pop(k, None)


# 全局事件监听引擎实例（lifespan 与 API 路由共用）
event_monitor_engine = EventMonitorEngine()


# ──────────────── FastAPI 应用 ────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器启动后自动开始轮询
    await start_bridge()
    # 启动 HA WebSocket 事件监听（独立于轮询循环）
    event_monitor_engine.start()
    log.info("Web 配置面板已启动 → http://0.0.0.0:%d", WEB_PORT)
    yield
    global _bridge_task
    if _bridge_task and not _bridge_task.done():
        bridge_state["running"] = False
        _bridge_task.cancel()
    event_monitor_engine.stop()


app = FastAPI(title="小爱HA桥接器", lifespan=lifespan)

# 静态文件
static_dir = Path("web/static")
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ──────────────── API 路由 ────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path("web/index.html")
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Web 文件缺失，请确保 web/index.html 存在</h1>")


@app.get("/api/status")
async def get_status():
    uptime = None
    if bridge_state["start_time"]:
        uptime = int(time.time() - bridge_state["start_time"])
    return {**bridge_state, "uptime": uptime}


@app.get("/api/version")
async def get_version():
    return {"version": VERSION}


@app.get("/api/config")
async def get_config():
    cfg = load_config()
    # 脱敏
    if cfg.get("xiaomi", {}).get("password"):
        cfg["xiaomi"]["password"] = "••••••••"
    if cfg.get("homeassistant", {}).get("token"):
        cfg["homeassistant"]["token"] = "••••••••"
    if cfg.get("openai", {}).get("api_key"):
        cfg["openai"]["api_key"] = "••••••••"
    return cfg


class SaveConfigRequest(BaseModel):
    config: dict[str, Any]


@app.post("/api/config")
async def post_config(req: SaveConfigRequest):
    cfg = req.config
    existing = load_config()
    # 脱敏字段保留原值
    masked = "••••••••"
    if cfg.get("xiaomi", {}).get("password") == masked:
        cfg["xiaomi"]["password"] = existing.get("xiaomi", {}).get("password", "")
    if cfg.get("homeassistant", {}).get("token") == masked:
        cfg["homeassistant"]["token"] = existing.get("homeassistant", {}).get("token", "")
    if cfg.get("openai", {}).get("api_key") == masked:
        cfg["openai"]["api_key"] = existing.get("openai", {}).get("api_key", "")
    save_config(cfg)
    log.info("配置已保存")
    return {"ok": True}


@app.post("/api/bridge/start")
async def start_bridge():
    global _bridge_task
    if bridge_state["running"]:
        return {"ok": False, "msg": "桥接器已在运行"}
    bridge_state["running"] = True
    bridge_state["start_time"] = time.time()
    bridge_state["command_count"] = 0
    bridge_state["error"] = None
    _bridge_task = asyncio.create_task(bridge_loop())
    log.info("桥接器已启动")
    return {"ok": True}


@app.post("/api/bridge/stop")
async def stop_bridge():
    global _bridge_task
    bridge_state["running"] = False
    if _bridge_task and not _bridge_task.done():
        _bridge_task.cancel()
    log.info("桥接器已停止")
    return {"ok": True}


@app.get("/api/logs")
async def get_logs(n: int = 80):
    return log_records[-n:]


class TestXiaomiRequest(BaseModel):
    username: str = ""
    password: str = ""
    use_saved: bool = False
    cookie_text: str = ""


# 70016 错误码的友好提示映射
_ERROR_HINTS = {
    70016: "可能是密码错误、账号被风控拦截，或需要手机验证码验证。建议：①确认密码正确 ②在浏览器中登录 https://account.xiaomi.com 解除风控后再试 ③如账号开启二步验证则暂不支持",
    87001: "需要验证码，请在浏览器中先登录一次小米账号后再试",
}


@app.post("/api/test/xiaomi")
async def test_xiaomi(req: TestXiaomiRequest):
    try:
        from miservice import MiAccount, MiNAService
    except ImportError:
        return {"ok": False, "msg": "缺少依赖: pip install miservice"}

    username = req.username
    password = req.password
    cfg = load_config()
    mi = cfg.get("xiaomi", {})
    masked = "••••••••"
    if req.use_saved or not username or not password or password == masked:
        if not username:
            username = mi.get("username") or ""
        if not password or password == masked:
            password = mi.get("password") or ""

    # 账号密码不再是必须的：扫码登录只留下 cookie_text / auth.json / .mi.token。
    # 三者都没有才拦下，否则一律继续尝试。
    has_saved = bool((mi.get("cookie_text") or "").strip()) or _has_xiaomi_credentials(cfg)
    if not (username and password) and not req.cookie_text and not has_saved:
        return {"ok": False, "msg": "尚未登录，请先扫码登录"}

    try:
        from miservice.miaccount import get_random

        # ── 方式0：已存 passToken（auth.json / .mi.token）──
        # 顺序与 bridge_loop 的三级尝试保持一致：passToken → Cookie → 密码。
        # 实测 store 里的 passToken 能登录成功，而 Cookie 分支可能因 serviceToken
        # 缺失等原因失败，所以 passToken 必须排在前面。
        try:
            s0 = aiohttp.ClientSession()
            try:
                acct0 = MiAccount(s0, username, password, str(TOKEN_PATH))
                _set_token(acct0, cfg)
                if acct0.token.get("passToken"):
                    log.info("尝试用已保存的 passToken 登录...")
                    if await acct0.login("micoapi"):
                        _save_auth_and_token(acct0)
                        na0 = MiNAService(acct0)
                        devices0 = await na0.device_list()
                        if not devices0:
                            return {"ok": True, "msg": "登录成功，但未发现小爱音箱设备", "devices": []}
                        return {"ok": True, "msg": f"登录成功，发现 {len(devices0)} 台设备",
                                "devices": devices0}
            finally:
                await s0.close()
        except Exception as e:
            log.warning("已存 passToken 登录失败: %s", e)

        # ── 方式1：浏览器 Cookie 直接登录 ──
        cookie_text = req.cookie_text or mi.get("cookie_text", "")
        if cookie_text and not cookie_text.startswith("https://"):
            log.info("尝试用 Cookie 直接登录...")
            cs = aiohttp.ClientSession()
            try:
                from http.cookies import SimpleCookie
                sc = SimpleCookie()
                sc.load(cookie_text)
                cookies = {k: m.value for k, m in sc.items()}
                if cookies.get('userId') and cookies.get('passToken'):
                    import yarl
                    sc2 = SimpleCookie()
                    for k, v in cookies.items():
                        try: sc2[k] = v
                        except Exception: pass
                    for domain in ['account.xiaomi.com', '.mina.mi.com']:
                        cs.cookie_jar.update_cookies(sc2, yarl.URL(f'https://{domain}/'))
                    acct = MiAccount(cs, username, password, str(TOKEN_PATH))
                    acct.token = {
                        'deviceId': cookies.get('deviceId', get_random(16).upper()),
                        'userId': cookies['userId'],
                        'passToken': cookies['passToken'],
                    }
                    resp = await acct._serviceLogin('serviceLogin?sid=micoapi&_json=true')
                    if resp.get('code') != 0:
                        raise Exception(f"Cookie 验证失败: {resp.get('description','')}")
                    acct.token['userId'] = str(resp.get('userId', acct.token['userId']))
                    acct.token['micoapi'] = (
                        resp.get('psecurity', resp.get('ssecurity', '')),
                        cookies.get('serviceToken', ''),
                    )
                    _save_auth_and_token(acct)
                    na = MiNAService(acct)
                    devices = await na.device_list()
                    if not mi.get("cookie_text"):
                        save_config({**cfg, "xiaomi": {**mi, "cookie_text": cookie_text}})
                    await cs.close()
                    if not devices:
                        return {"ok": True, "msg": "Cookie 登录成功，但未发现小爱音箱设备", "devices": []}
                    return {"ok": True, "msg": f"Cookie 登录成功，发现 {len(devices)} 台设备", "devices": devices}
            except Exception as e:
                log.warning("Cookie 直接登录失败: %s", e)
            finally:
                if not cs.closed:
                    await cs.close()

        # ── 方式2：纯密码登录（不加载缓存 token，避免 passToken 干扰） ──
        # 只有配置里本来就有账号密码的老用户才会走到这里
        if not username or not password:
            return {"ok": False, "msg": "已保存的凭据不可用，请重新扫码登录"}
        s = aiohttp.ClientSession()
        account = MiAccount(s, username, password, None)
        if not account.token or not isinstance(account.token, dict):
            account.token = {'deviceId': get_random(16).upper()}

        log.info("正在密码登录 %s ...", username)
        resp = await account._serviceLogin('serviceLogin?sid=micoapi&_json=true')
        if resp.get('code') != 0:
            data = {
                '_json': 'true',
                'qs': resp['qs'],
                'sid': resp['sid'],
                '_sign': resp['_sign'],
                'callback': resp['callback'],
                'user': username,
                'hash': __import__('hashlib').md5(password.encode()).hexdigest().upper(),
            }
            resp = await account._serviceLogin('serviceLoginAuth2', data)
            if resp.get('code') != 0:
                desc = resp.get('description', resp.get('desc', ''))
                code = resp.get('code', '')
                hint = _ERROR_HINTS.get(code, '')
                msg = f"{desc} (code:{code})"
                if hint:
                    msg += f"\n{hint}"
                await s.close()
                return {"ok": False, "msg": msg}

        # 需要短信验证：短信流程已随"只保留扫码"一并移除，这里给明确出路
        if resp.get('securityStatus', 0) & 16:
            await s.close()
            return {
                "ok": False,
                "msg": "该账号登录被要求短信验证，已不再支持。请改用「扫码登录」。",
            }

        # 登录成功
        if not resp.get('ssecurity') or not resp.get('location'):
            await s.close()
            return {"ok": False, "msg": f"登录响应缺少必要字段: {list(resp.keys())}"}

        account.token['userId'] = resp['userId']
        account.token['passToken'] = resp['passToken']
        serviceToken = await account._securityTokenService(
            resp['location'], resp['nonce'], resp['ssecurity']
        )
        account.token['micoapi'] = (resp['ssecurity'], serviceToken)

        _save_auth_and_token(account)
        na = MiNAService(account)
        devices = await na.device_list()
        await s.close()
        if not devices:
            return {"ok": True, "msg": "登录成功，但未发现小爱音箱设备", "devices": []}
        return {
            "ok": True,
            "msg": f"登录成功，发现 {len(devices)} 台设备",
            "devices": devices,
        }
    except Exception as e:
        log.exception("test_xiaomi 异常: %s", e)
        return {"ok": False, "msg": f"请求失败: {e}"}


# ──────────────── HA 设备列表 ────────────────

@app.get("/api/devices")
async def get_ha_devices():
    """获取 HA 所有可控设备（精简列表，供 AI 使用）"""
    cfg = load_config()
    ha_cfg = cfg.get("homeassistant", {})
    if not ha_cfg.get("token"):
        return {"ok": False, "devices": [], "msg": "HA 未配置"}

    ha_headers = {
        "Authorization": f"Bearer {ha_cfg['token']}",
        "Content-Type": "application/json",
    }
    base = ha_cfg["url"].rstrip("/")

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/api/states", headers=ha_headers,
                            timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return {"ok": False, "devices": [], "msg": f"HA 返回 {r.status}"}
                all_states = await r.json()
    except Exception as e:
        return {"ok": False, "devices": [], "msg": f"无法连接 HA: {e}"}

    # 只保留可控设备（排除 sensor、binary_sensor 等只读类）
    controllable = {"light", "switch", "climate", "scene", "media_player", "script",
                    "automation", "input_boolean", "fan", "cover", "lock", "vacuum", "button"}

    devices = []
    for s in all_states:
        eid = s.get("entity_id", "")
        domain = eid.split(".")[0] if "." in eid else ""
        if domain not in controllable:
            continue
        attrs = s.get("attributes", {})
        name = attrs.get("friendly_name", eid)
        devices.append({
            "name": name,
            "entity_id": eid,
            "domain": domain,
            "state": s.get("state", ""),
        })

    # 附加别名
    aliases = cfg.get("device_aliases", {})
    for d in devices:
        if d["entity_id"] in aliases:
            d["alias"] = aliases[d["entity_id"]]

    devices.sort(key=lambda d: (d["domain"], d["name"]))
    return {"ok": True, "devices": devices, "count": len(devices)}


# ──────────────── 设备别名管理 ────────────────

@app.get("/api/aliases")
async def get_aliases():
    cfg = load_config()
    return {"ok": True, "aliases": cfg.get("device_aliases", {})}


class SaveAliasRequest(BaseModel):
    entity_id: str
    alias: str


@app.post("/api/aliases")
async def save_alias(req: SaveAliasRequest):
    cfg = load_config()
    aliases = cfg.setdefault("device_aliases", {})
    alias = req.alias.strip()
    if alias:
        aliases[req.entity_id] = alias
    else:
        aliases.pop(req.entity_id, None)
    save_config(cfg)
    return {"ok": True}


# ──────────────── AI 命令解析 ────────────────

# ──────────────── 智能去重 ────────────────
# 注意：这里是**子串**匹配，新增词必须避免过宽。
# 曾经的 "关闭" 会让小爱已成功执行的回复（"正在关闭窗帘。"）被判为失败，
# 导致桥接器重复执行同一条指令（实测 488 条问答对中 49 条命中误判）。
# 因此只保留**明确表达失败**的短语。
_FAILURE_RESPONSES = [
    "没有找到", "没有听懂", "暂不支持", "我不明白", "无法",
    "不会", "不知道", "没有相关", "小爱没有学会", "没有发现",
    "还不支持", "没有相关的设备", "没有这个", "听不懂", "无能为力",
    "无法关闭", "关闭不了", "关闭失败", "无法打开", "打开不了", "打开失败",
    "没有权限", "换个说法",
]

# 唤醒词残片：用户只喊了一声"小爱同学"就被抓成一条记录，白走一次 LLM。
# 必须 fullmatch，否则会误伤 "小爱同学打开客厅灯"（日志里真实存在）。
_WAKE_FRAGMENT_RE = re.compile(r"^[小晓][爱艾](同学?)?$")


def _is_wake_fragment(text: str) -> bool:
    """判断是否为纯唤醒词残片（去标点后精确匹配）。"""
    cleaned = re.sub(r"[\s。！？!?，,、~～]+", "", text or "")
    return bool(_WAKE_FRAGMENT_RE.fullmatch(cleaned))


async def _xiaoi_already_handled(query: str, answer: str) -> bool:
    """判断小爱本机是否已经处理了这条指令。
    如果小爱回复了非失败类内容，说明它已原生处理，桥接器跳过。
    """
    if not answer:
        return False  # 没有回复 → 桥接器来处理
    # 如果回复包含失败关键词 → 小爱没处理成功
    for kw in _FAILURE_RESPONSES:
        if kw in answer:
            return False
    # 小爱给出了有意义回复 → 已原生处理
    return True


# ──────────────── AI 会话上下文（多轮承接 + 澄清待答） ────────────────

_HISTORY_TTL = 300.0   # 超过这个时间的轮次不再注入 prompt
_HISTORY_MAX = 8       # 内存里最多保留几轮


class ConversationStore:
    """进程内的会话状态：最近几轮对话、最近操作的设备、待澄清的问题。

    bridge_loop 是单个 asyncio 任务，但 /api/test/rule 是并发 handler，
    两边都会读写这里。asyncio 单线程下字段读写本身是原子的，只有
    "读 pending → 判定 → 清除"这种跨 await 的复合操作才需要锁。
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._history: list[dict] = []          # 新 → 旧
        self._pending: dict | None = None
        self._last_entity: str = ""
        self._last_name: str = ""
        self._clarify_guard: tuple | None = None  # 防止同一个问题反复追问
        self._clarify_guard_ts: float = 0.0

    # ── 待澄清问题 ──
    async def set_pending(self, pending: dict) -> None:
        async with self._lock:
            self._pending = pending
            self._clarify_guard = (pending.get("original_query"),
                                   tuple(pending.get("candidates", [])))
            self._clarify_guard_ts = time.time()

    async def claim_pending(self, ttl: float) -> dict | None:
        """读取并立即清除（原子操作），已过期则返回 None。"""
        async with self._lock:
            p, self._pending = self._pending, None
        if p and (time.time() - p.get("created_at", 0)) > ttl:
            return None
        return p

    async def clear_pending(self) -> None:
        async with self._lock:
            self._pending = None

    def pending_public(self) -> dict | None:
        """给 /api/status 看的只读快照"""
        p = self._pending
        if not p:
            return None
        return {
            "question": p.get("question", ""),
            "candidates": list(p.get("candidates", [])),
            "age": round(time.time() - p.get("created_at", 0), 1),
        }

    def is_repeat_clarify(self, query: str, candidates: list) -> bool:
        """同一个问题、同一批候选，20 秒内不再追问，避免陷入循环。"""
        key = (query, tuple(candidates))
        return (self._clarify_guard == key
                and (time.time() - self._clarify_guard_ts) < 20)

    # ── 对话历史 ──
    async def record_turn(self, query: str, actions: list[dict],
                          results: list[dict], devices: list[dict]) -> None:
        reply = next((r.get("reply") for r in results if r.get("reply")), "")
        entities = [a.get("entity_id") for a in actions if a.get("entity_id")]
        name_by_id = {d["entity_id"]: d.get("name", "") for d in devices}
        async with self._lock:
            self._history.insert(0, {
                "ts": time.time(),
                "query": query,
                "reply": reply,
                "entities": entities,
            })
            del self._history[_HISTORY_MAX:]
            self._last_entity = entities[0] if entities else ""
            self._last_name = name_by_id.get(self._last_entity, "")

    def snapshot(self, turns: int) -> dict | None:
        """构建给 prompt 用的只读快照，顺带丢弃过期轮次。"""
        now = time.time()
        fresh = [h for h in self._history if now - h["ts"] <= _HISTORY_TTL]
        if len(fresh) != len(self._history):
            self._history = fresh
        turns = max(0, min(int(turns or 0), _HISTORY_MAX))
        if not turns and not self._last_entity:
            return None
        return {
            "turns": [dict(h) for h in fresh[:turns]],
            "last_entity": self._last_entity,
            "last_name": self._last_name,
        }


_conversation = ConversationStore()


def _safe_question(raw: str) -> str:
    """澄清问题会被 TTS 直接念出来，必须保证是一句短问句，不能把 JSON 念出来。"""
    q = (raw or "").strip()
    if not q or len(q) > 60 or not q.endswith(("？", "?")):
        return "您指的是哪一个设备？"
    return q


# ──────────────── 多步动作解析 ────────────────
async def _parse_and_resolve_actions(query: str, parser: IntentParser, cfg: dict,
                                     device_id: str = "",
                                     diag: dict | None = None) -> list[dict]:
    """解析指令，返回动作列表（正则 → AI）。

    device_id 为空表示无状态调用（网页测试页），不读写会话上下文。
    diag 是可选出参，回报 ai_used / clarify / ai_none 等诊断信息。
    返回空列表时，调用方应参考 diag 判断是"非设备指令"还是"没解析出来"。
    """
    d = diag if diag is not None else {}

    # 先试正则
    action = parser.parse(query)
    if action:
        d["ai_used"] = False
        return [action]

    if not (cfg.get("ai_mode") and cfg.get("openai", {}).get("api_key")):
        d["ai_used"] = False
        return []

    ctx = None
    if device_id and cfg.get("ai_context_enabled", True):
        ctx = _conversation.snapshot(cfg.get("ai_context_turns", 4))

    ai_result = await ai_parse_command(query, cfg=cfg, ctx=ctx, diag=d)
    d["ai_used"] = True
    if not ai_result:
        return []

    # 缺关键信息 → 反问澄清
    clarify = ai_result.get("clarify")
    if isinstance(clarify, dict):
        cands = clarify.get("candidates") or []
        if (len(cands) >= 2
                and cfg.get("ai_clarify_enabled", True)
                and not _conversation.is_repeat_clarify(query, cands)):
            d["clarify"] = {
                "question": _safe_question(clarify.get("question")),
                "candidates": list(cands),
                "original_query": query,
            }
            log.info("❓ 需要澄清: %s → 候选 %s", query, cands)
        else:
            d["clarify_rejected"] = True
        return []

    # 多步
    if isinstance(ai_result.get("actions"), list):
        actions = ai_result["actions"]
        log.info("🤖 AI 多步解析: %s → %d 个动作", query, len(actions))
        d["source"] = "ai"
        return actions

    # 单步
    if "domain" in ai_result and "service" in ai_result:
        log.info("🤖 AI 单步解析: %s → %s/%s", query,
                 ai_result.get("domain"), ai_result.get("service"))
        d["source"] = "ai"
        return [ai_result]

    # 明确判定为非设备指令 / 没匹配上
    d["ai_none"] = True
    d["none_reason"] = ai_result.get("reason", "unmatched")
    return []


_AI_DOMAIN_ACTIONS: dict[str, list[str]] = {
    "light": ["turn_on 打开", "turn_off 关闭", "toggle 切换"],
    "switch": ["turn_on 打开", "turn_off 关闭", "toggle 切换"],
    "climate": ["turn_on 打开", "turn_off 关闭", "set_temperature 设置温度",
                "set_hvac_mode 设置模式(cool/heat/dry/fan_only/auto)"],
    "scene": ["turn_on 激活"],
    "media_player": ["turn_on 打开", "turn_off 关闭", "media_play 播放", "media_pause 暂停"],
    "script": ["turn_on 执行"],
    "automation": ["turn_on 启用", "turn_off 禁用"],
    "input_boolean": ["turn_on 打开", "turn_off 关闭", "toggle 切换"],
    "fan": ["turn_on 打开", "turn_off 关闭", "set_speed 设置风速"],
    "cover": ["open_cover 打开", "close_cover 关闭", "stop_cover 停止"],
    "lock": ["lock 锁定", "unlock 解锁"],
    "vacuum": ["start 开始清扫", "stop 停止", "return_to_base 回充"],
    "button": ["press 按下"],
}


def _build_ai_prompt(query: str, devices: list[dict], aliases: dict,
                     ctx: dict | None = None) -> tuple[str, str]:
    """构建发给 OpenAI 的 system + user prompt"""
    import json as _json
    device_list = []
    for d in devices:
        domain = d["domain"]
        actions = _AI_DOMAIN_ACTIONS.get(domain, [])
        entry = {
            "name": d["name"],
            "id": d["entity_id"],
            "type": domain,
            "state": d["state"],
            "actions": actions,
        }
        cur = _device_cur(d)
        if cur:
            entry["cur"] = cur
        eid = d["entity_id"]
        if eid in aliases:
            alias_val = aliases[eid]
            if isinstance(alias_val, list):
                entry["alias"] = "、".join(alias_val)
            else:
                entry["alias"] = alias_val
        device_list.append(entry)

    system = (
        "你是 Home Assistant 语音助手。根据用户的自然语言指令，从设备列表中选择最匹配的设备并返回操作指令。\n"
        "规则：\n"
        "1. 优先使用设备的 alias（别名）来匹配用户指令\n"
        "2. 如果用户提到房间名(主卧/客厅/餐厅/次卧/厨房/阳台/书房)，优先匹配名称含该房间的设备\n"
        "3. 单步指令（如\"打开客厅灯\"）返回单条 action，格式见旧格式\n"
        "4. 多步/复杂指令（如\"打开餐厅空调温度设置为二十四度\"=打开空调+设置温度）返回 actions 数组：\n"
        '   {\"actions\":[{\"domain\":\"climate\",\"service\":\"turn_on\",\"entity_id\":\"...\",\"reply\":\"好的，已打开餐厅空调\"},{\"domain\":\"climate\",\"service\":\"set_temperature\",\"entity_id\":\"...\",\"temperature\":24,\"reply\":\"温度已设为24度\"}]}\n'
        "5. reply 只填在最后一个 action 或每个明显需要回复的 action 上，中间的可以不填\n"
        "6. 如果用户说百分比，对应 brightness_pct 或 volume_level\n"
        "7. 如果这不是控制设备的指令（闲聊/问答/天气/百科），返回 {\"action\":\"none\",\"reason\":\"non_device\"}；"
        "如果是设备指令但列表里找不到对应设备，返回 {\"action\":\"none\",\"reason\":\"unmatched\"}\n"
        "8. 只返回 JSON，不要任何其他文字。\n"
        "9. 如果用户指令包含定时/计划/每天/每周/每小时/几点/半/分等时间相关词汇（如\"每天七点半打开热水器\"），"
        "这是定时指令，返回格式增加 type、schedule_time、schedule_days 字段：\n"
        "   {\"type\":\"schedule\",\"entity_id\":\"...\",\"domain\":\"...\",\"service\":\"...\","
        "\"schedule_time\":\"07:30\",\"schedule_days\":\"daily\",\"reply\":\"回复文字\"}\n"
        "   - schedule_time 格式为 HH:MM（24小时制）\n"
        "   - schedule_days: \"daily\"=每天, \"weekdays\"=工作日, \"weekends\"=周末, 或具体星期如 \"mon,wed,fri\"\n"
        "   - 普通即时指令不需要 type 字段\n"
        "10. 如果用户指令包含\"分钟后\"、\"分钟之后\"、\"之后\"、\"秒后\"等延迟词汇（如\"三分钟后关闭餐厅空调\"、\"5分钟后关闭左炉灶\"、\"一分钟之后关闭燃气灶\"），"
        "这是延迟执行指令，在 JSON 中增加 delay_minutes 字段（单位：分钟，可以是小数）。注意：\n"
        "   - 把中文数字(一二三)转换为阿拉伯数字(123)\n"
        "   - delay_minutes 放在 JSON 顶层，与 domain/service/entity_id/reply 同级\n"
        "   示例：{\"domain\":\"climate\",\"service\":\"turn_off\",\"entity_id\":\"climate.211106251060256_climate\","
        "\"delay_minutes\":3,\"reply\":\"好的，3分钟后关闭餐厅空调\"}\n"
        "   - 如果用户说\"秒\"而非分钟，自动换算（如\"30秒后\"→delay_minutes=0.5，\"10秒后\"→delay_minutes=0.17）\n"
        "   - \"一分钟之后\"→delay_minutes=1，\"之后\"等同于\"后\"\n"
        "   - 不要给 delay 指令加上 type 字段，它不属于定时自动化\n"
        "11.【承接上一句】用户的话可能是上一轮的延续：\"它/那个/这个\"指代【最近操作设备】；"
        "\"再暗一点/亮一点/大一点/小声点/冷一点/热一点/调高/调低\"是相对调整，"
        "必须参照该设备 cur 字段里的当前值算出目标绝对值再返回，不要反问：\n"
        "   - 灯的亮度：brightness_pct = 当前 ±20（范围 5~100）；当前是 off 就按 30 打开\n"
        "   - 音量：volume_level = 当前 ±0.1（范围 0.1~1.0）；没有当前值时用 0.3\n"
        "   - 温度：temperature = 当前设定值 ±1；没有当前值时用 26\n"
        "12.【缺信息时先推断，推不出才反问】用户没说清是哪个设备时：\n"
        "   - 若【最近操作设备】能合理推断（同房间/同类型）→ 直接返回动作，不要反问\n"
        "   - 若同类候选 ≥2 且完全无法推断 → 返回 {\"clarify\":{\"question\":\"口语化中文问句，≤30字，"
        "把候选的房间或名称说出来\",\"candidates\":[\"entity_id1\",\"entity_id2\"]}}\n"
        "   - candidates 必须逐字来自下面的设备列表，2~5 个，不得编造\n"
        "   - 只有 1 个候选时直接执行，不要反问\n"
        "   示例：{\"clarify\":{\"question\":\"您说的是哪个空调？主卧、餐厅还是次卧？\","
        "\"candidates\":[\"climate.208907226818174_climate\",\"climate.211106251060256_climate\"]}}\n"
        "13.【禁止编造】所有 entity_id 必须逐字来自下面的设备列表，不得推测或拼接。\n"
        "14. 设备条目字段：id=entity_id，name=HA 里的名称，type=设备类型，state=当前状态，"
        "cur=当前可调值（灯的 brightness_pct、空调的 temperature/fan_mode、音箱的 volume_level 等），"
        "alias=用户配置的别名（可信度最高），actions=该类型支持的操作。\n"
        "返回格式（单步旧格式）：{\"entity_id\":\"...\",\"domain\":\"...\",\"service\":\"...\",<额外参数>, \"reply\":\"回复文字\"}\n"
        "返回格式（多步新格式）：{\"actions\":[{\"domain\":\"...\",\"service\":\"...\",\"entity_id\":\"...\",...}, {...}]}"
    )

    ctx_lines = []
    if ctx:
        last_entity = ctx.get("last_entity") or ""
        last_name = ctx.get("last_name") or ""
        if last_entity:
            ctx_lines.append(f"【最近操作设备】{last_name}（{last_entity}）")
        for h in (ctx.get("turns") or []):
            ents = "、".join(h.get("entities") or []) or "-"
            line = f"  用户：“{h.get('query', '')}” → 操作了 {ents}"
            if h.get("reply"):
                line += f"，回复“{h['reply']}”"
            ctx_lines.append(line)

    prefix = ""
    if ctx_lines:
        prefix = "【对话上下文】\n" + "\n".join(ctx_lines) + "\n\n"

    # 用紧凑序列化：indent=2 会让同一份设备清单凭空多出两成体积
    user = (f"{prefix}用户指令: {query}\n\n可用设备:\n"
            f"{_json.dumps(device_list, ensure_ascii=False, separators=(',', ':'))}")
    return system, user


# ──────────────── AI 设备清单 / 请求 / 结果校验 ────────────────

_CONTROLLABLE_DOMAINS = {
    "light", "switch", "climate", "scene", "media_player", "script",
    "automation", "input_boolean", "fan", "cover", "lock", "vacuum", "button",
}

# 对"再暗一点"这类相对指令有用的当前值。只带这些，避免 prompt 体积膨胀。
_CUR_ATTRS: dict[str, tuple[str, ...]] = {
    "light": ("brightness",),
    "climate": ("temperature", "hvac_mode", "current_temperature", "fan_mode"),
    "media_player": ("volume_level",),
    "fan": ("percentage",),
    "cover": ("current_position",),
}

# 首轮精简清单：这些类型永远保留
_TIER1_DOMAINS = {"light", "climate", "scene", "script", "fan", "cover",
                  "lock", "vacuum", "media_player", "input_boolean"}
# 这些类型数量巨大又不常被点名（switch 133 / button 60 / automation 10），
# 只在被关键词或别名命中时才带上；漏掉时靠"全量重试"兜底。
_TIER2_DOMAINS = {"switch", "button", "automation"}
_DEVICE_CAP = 60
_EXTRA_TOKENS = ("空调", "热水器", "洗衣机", "干衣机", "洗碗机", "窗帘", "电脑")

_devices_cache: dict = {"key": None, "ts": 0.0, "devices": None}
_DEVICES_CACHE_TTL = 5.0


def _device_cur(d: dict) -> dict:
    """挑出该设备当前可调值，供模型计算相对调整。"""
    attrs = d.get("attrs") or {}
    out: dict = {}
    for key in _CUR_ATTRS.get(d["domain"], ()):
        val = attrs.get(key)
        if val is None:
            continue
        if key == "brightness":
            try:
                out["brightness_pct"] = round(float(val) / 255 * 100)
            except (TypeError, ValueError):
                continue
        elif key == "volume_level":
            try:
                out[key] = round(float(val), 2)
            except (TypeError, ValueError):
                continue
        else:
            out[key] = val
    return out


def _alias_text(val: Any) -> str:
    if isinstance(val, list):
        return "、".join(str(v) for v in val)
    return str(val or "")


async def _fetch_ha_devices(cfg: dict) -> list[dict] | None:
    """拉取 HA 全部状态并过滤出可控设备。5 秒 TTL 缓存，避免过滤重试时重复拉取。"""
    ha_cfg = cfg.get("homeassistant", {})
    base = (ha_cfg.get("url") or "").rstrip("/")
    token = ha_cfg.get("token") or ""
    if not base or not token:
        return None

    cache_key = (base, token)
    now = time.time()
    if (_devices_cache["key"] == cache_key
            and now - _devices_cache["ts"] < _DEVICES_CACHE_TTL
            and _devices_cache["devices"] is not None):
        return _devices_cache["devices"]

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/api/states", headers=headers,
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    log.warning("拉取 HA 设备失败: HTTP %s", r.status)
                    return None
                all_states = await r.json()
    except Exception as e:
        log.warning("拉取 HA 设备异常: %s", e)
        return None

    devices = []
    for st in all_states:
        eid = st.get("entity_id", "")
        domain = eid.split(".")[0] if "." in eid else ""
        if domain not in _CONTROLLABLE_DOMAINS:
            continue
        attrs = st.get("attributes", {})
        devices.append({
            "name": attrs.get("friendly_name", eid),
            "entity_id": eid,
            "domain": domain,
            "state": st.get("state", ""),
            "attrs": attrs,
        })

    _devices_cache.update(key=cache_key, ts=now, devices=devices)
    return devices


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def _query_tokens(query: str) -> set[str]:
    """查询串的 2-gram + 整串 + 少量高频设备名词"""
    q = re.sub(r"\s+", "", query or "")
    tokens = {q} if q else set()
    for i in range(len(q) - 1):
        tokens.add(q[i:i + 2])
    for extra in _EXTRA_TOKENS:
        if extra in q:
            tokens.add(extra)
    return {t for t in tokens if t}


def _dedupe_shadow_entities(devices: list[dict]) -> list[dict]:
    """同一台空调会同时暴露 _climate 和 _thermostat 两个实体，名字几乎一样，
    模型容易二选一选错（"打开空调"实测有 8 个候选）。同设备前缀下优先保留 _climate。"""
    groups: dict[str, list[dict]] = {}
    for d in devices:
        if d["domain"] == "climate":
            groups.setdefault(d["entity_id"].split("_", 1)[0], []).append(d)
    drop: set[str] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        if any(d["entity_id"].endswith("_climate") for d in group):
            drop.update(d["entity_id"] for d in group
                        if not d["entity_id"].endswith("_climate"))
    if drop:
        log.debug("去重影子实体: %s", sorted(drop))
    return [d for d in devices if d["entity_id"] not in drop]


def _select_devices_for_prompt(query: str, devices: list[dict], aliases: dict,
                               last_entity: str = "") -> list[dict]:
    """首轮用的精简设备清单。体积从 ~22K tokens 降到 ~3K，显著提升小模型的指令遵循。"""
    tokens = _query_tokens(query)
    alias_ids = set(aliases.keys())
    picked = []
    for d in _dedupe_shadow_entities(devices):
        eid = d["entity_id"]
        if (d["domain"] in _TIER1_DOMAINS or eid in alias_ids or eid == last_entity):
            picked.append(d)
            continue
        if d["domain"] in _TIER2_DOMAINS:
            hay = _norm_name(d.get("name", "")) + " " + _norm_name(_alias_text(aliases.get(eid)))
            if any(t in hay for t in tokens):
                picked.append(d)
    if len(picked) > _DEVICE_CAP:
        picked = picked[:_DEVICE_CAP]
    return picked


def _validate_entity(eid: str, valid_ids: set[str], devices: list[dict],
                     aliases: dict) -> str | None:
    """把模型给的 entity_id 校正到真实存在的实体上；幻觉则返回 None。

    这里刻意**不做模糊匹配**：这个家里的设备 ID 高度相似
    （30 个"苏米智能筒射灯Mesh2.0"只差几位数字），近似匹配会把幻觉 ID
    静默改到隔壁房间的另一盏灯上——控制错设备比不执行更糟。
    """
    if not eid:
        return None
    if eid in valid_ids:
        return eid
    # 大小写差异（安全）
    lowered = {v.lower(): v for v in valid_ids}
    if eid.lower() in lowered:
        return lowered[eid.lower()]
    # 别名反向匹配
    for real, val in aliases.items():
        vals = val if isinstance(val, list) else [val]
        if eid in vals:
            return real
    # 名称精确匹配用户给的名字（唯一命中才算）
    hits = [d["entity_id"] for d in devices
            if _norm_name(d.get("name", "")) == _norm_name(eid)]
    if len(hits) == 1:
        return hits[0]
    return None


def _validate_actions(actions: list[dict], valid_ids: set[str],
                      devices: list[dict], aliases: dict) -> list[dict]:
    """剔除/校正动作里的幻觉 entity_id，并让 domain 与 entity_id 前缀保持一致。"""
    out = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        fixed = _validate_entity(a.get("entity_id", ""), valid_ids, devices, aliases)
        if not fixed:
            log.warning("⚠ 丢弃幻觉 entity_id: %s（动作 %s/%s）",
                        a.get("entity_id"), a.get("domain") or "?", a.get("service") or "?")
            continue
        a = dict(a)
        a["entity_id"] = fixed
        # domain 一律以 entity_id 前缀为准，避免出现 /api/services// 这种空调用
        real_domain = fixed.split(".")[0]
        if a.get("domain") != real_domain:
            if a.get("domain"):
                log.info("🔧 修正 domain: %s → %s", a["domain"], real_domain)
            a["domain"] = real_domain
        out.append(a)
    return out


def _parse_ai_json(content: str) -> dict | None:
    """从模型输出里抠出 JSON 对象（容忍 markdown 包裹与前后缀文字）"""
    text = content.strip()
    if "```" in text:
        parts = text.split("```")
        if len(parts) > 1:
            text = parts[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
        return obj
    except (json.JSONDecodeError, ValueError):
        pass
    end = text.rfind("}")
    if end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _ai_is_none(result: Any) -> bool:
    """模型这次没给出可执行结果（既不是动作，也不是澄清请求）"""
    if not isinstance(result, dict):
        return True
    if isinstance(result.get("clarify"), dict):
        return False
    if isinstance(result.get("actions"), list) and result["actions"]:
        return False
    return not (result.get("domain") and result.get("service"))


_JSON_MODE_HOSTS = ("api.openai.com", "api.deepseek.com", "api.siliconflow.cn")


async def _ai_chat(cfg: dict, system_prompt: str, user_prompt: str,
                   max_tokens: int = 800, timeout: float = 15.0) -> str | None:
    """调用 OpenAI 兼容的 /chat/completions，返回文本内容；真正的失败返回 None。"""
    oai_cfg = cfg.get("openai", {})
    api_key = oai_cfg.get("api_key", "")
    api_base = (oai_cfg.get("api_base") or "https://api.openai.com").rstrip("/")
    model = oai_cfg.get("model", "gpt-4o-mini")
    url = f"{api_base}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    # 结构化输出只在已知支持的供应商上开，400 时自动降级重试
    use_json_mode = any(h in api_base for h in _JSON_MODE_HOSTS)

    for attempt in (0, 1):
        body = dict(payload)
        if use_json_mode and attempt == 0:
            body["response_format"] = {"type": "json_object"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=body, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    if r.status == 400 and use_json_mode and attempt == 0:
                        log.info("AI 不支持 response_format，降级重试")
                        continue
                    if r.status != 200:
                        text = await r.text()
                        log.warning("AI API 返回 %s: %s", r.status, text[:300])
                        return None
                    data = await r.json()
        except Exception as e:
            log.warning("AI 请求异常: %s", e)
            return None

        if not data.get("choices"):
            log.warning("AI 返回无 choices: %s", list(data.keys()))
            return None
        msg = data["choices"][0].get("message", {})
        content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        if not content:
            log.warning("AI 返回空内容, msg keys: %s", list(msg.keys()))
            return None
        log.info("AI raw content: %s", content[:200])
        return content
    return None


async def ai_parse_command(query: str, cfg: dict | None = None,
                           ctx: dict | None = None,
                           diag: dict | None = None) -> dict | None:
    """用 LLM 解析用户指令。

    返回：
      - 动作 / 多步 actions / {"clarify": {...}} → 可用的解析结果
      - {"action": "none", "reason": ...}        → 模型明确判定"不是设备指令"或"没匹配上"
      - None                                     → 真正的失败（未配置 / 网络 / API 错误）
    区分后两者是兜底策略的前提：只有"明确判定"才该触发兜底播报。
    """
    cfg = cfg or load_config()
    if not cfg.get("openai", {}).get("api_key"):
        return None
    if not cfg.get("homeassistant", {}).get("token"):
        return None

    devices = await _fetch_ha_devices(cfg)
    if devices is None:
        return None

    aliases = cfg.get("device_aliases", {})
    valid_ids = {d["entity_id"] for d in devices}

    # 首轮用精简清单提准确率；没命中就退回全量，保证不丢能力
    attempts = [devices]
    if cfg.get("ai_device_filter", True):
        slim = _select_devices_for_prompt(query, devices, aliases,
                                          (ctx or {}).get("last_entity", ""))
        if len(slim) < len(devices):
            attempts = [slim, devices]

    result: dict | None = None
    for idx, devs in enumerate(attempts):
        system_prompt, user_prompt = _build_ai_prompt(query, devs, aliases, ctx)
        if diag is not None:
            diag["prompt_chars"] = len(user_prompt)
            diag["prompt_devices"] = len(devs)
        content = await _ai_chat(cfg, system_prompt, user_prompt)
        if content is None:
            return None
        parsed = _parse_ai_json(content)
        if parsed is None:
            log.warning("AI 输出无法解析为 JSON: %s", content[:200])
            return None
        result = parsed
        if not _ai_is_none(parsed):
            break
        # 模型说"这根本不是设备指令"时，给它更多设备也不会改变结论，
        # 直接省下这次全量重试（闲聊类指令不必付 22K token 的代价）
        if parsed.get("reason") == "non_device":
            break
        if idx == 0 and len(attempts) > 1:
            log.info("🔎 精简清单（%d 台）未命中，改用全量（%d 台）重试",
                     len(devs), len(devices))
    assert result is not None

    if _ai_is_none(result):
        return {"action": "none", "reason": result.get("reason", "unmatched")}

    # 澄清：候选必须落在真实设备列表里，否则宁可当没解析出来
    clarify = result.get("clarify")
    if isinstance(clarify, dict):
        cands = [c for c in (clarify.get("candidates") or []) if c in valid_ids]
        dropped = len(clarify.get("candidates") or []) - len(cands)
        if dropped:
            log.warning("⚠ 澄清候选剔除 %d 个幻觉 entity_id", dropped)
        return {"clarify": {"question": clarify.get("question", ""),
                            "candidates": cands}}

    # 动作：校正 entity_id / domain
    if isinstance(result.get("actions"), list):
        return {"actions": _validate_actions(result["actions"], valid_ids, devices, aliases)}
    valid = _validate_actions([result], valid_ids, devices, aliases)
    if not valid:
        return {"action": "none", "reason": "hallucinated"}
    return valid[0]


# ──────────────── 动作执行（主循环与测试页共用） ────────────────

# 这些键是桥接器的元数据，绝不能当成 HA 服务参数发出去
# （例如 reply 漏进去会让 HA 返回 400）
_META_KEYS = {"domain", "service", "reply", "delay_minutes", "type",
              "schedule_time", "schedule_days", "action", "clarify", "reason"}


async def _wait_until_silent(na, device_id: str, timeout: float = 8.0) -> bool:
    """等小爱把当前这句说完。player_get_status 里 status==3 空闲、1 正在播。

    没这一步的话，桥接器的播报会和"小爱自己的回答"撞车——实测撞车时
    remote/ubus 会被阻塞好几秒，然后语音被丢掉（用户什么也听不到）。
    查状态失败时不阻塞，直接放行。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = await na.player_get_status(device_id)
            info = ((resp or {}).get("data") or {}).get("info") or ""
            status = json.loads(info).get("status") if info else None
            if status != 1:
                return True
        except Exception:
            return True
        await asyncio.sleep(0.6)
    log.warning("等待小爱静音超时（%.0fs），仍继续播报", timeout)
    return False


async def _stop_playback(na, device_id: str) -> bool:
    """打断小爱当前正在播的内容（只在真的在播时才动手）。

    澄清场景下小爱总会先说一句"没有发现可以操作的空调…"，等它说完要 5-6 秒；
    直接打断可以让反问基本立刻响起。

    ⚠ 这个 stop 用的 media 参数是 app_ios，但实测它能停掉 mibrain 发出的 TTS，
    所以**不排除也会停掉正在播放的音乐**。只在"刚发生一轮语音交互"时调用，
    降低误伤概率。
    """
    try:
        resp = await na.player_get_status(device_id)
        info = ((resp or {}).get("data") or {}).get("info") or ""
        status = json.loads(info).get("status") if info else None
        if status != 1:
            return False                    # 没在播，不需要打断
        await na.player_stop(device_id)
        await asyncio.sleep(0.2)            # 给音箱一点反应时间
        log.info("⏹ 已打断小爱当前播报，改播桥接器的内容")
        return True
    except Exception as e:
        log.warning("打断播报失败（忽略，继续播报）: %s", e)
        return False


async def _speak(na, device_id: str, text: str, *,
                 wait_silent: bool = False, interrupt: bool = False) -> bool:
    """播报文字并**检查小米返回的业务码**。

    miservice 的 ubus_request 不看业务码，HTTP 200 但语音被丢掉是可能的，
    所以这里必须自己判 code，失败重试一次并留下日志——否则出问题完全无从查起。

    interrupt=True 先打断小爱当前播报（快，但可能误停音乐）；
    wait_silent=True 则等它说完（慢，约 5-8 秒，但绝对安全）。
    """
    if not text:
        return False
    if interrupt:
        await _stop_playback(na, device_id)
    elif wait_silent:
        await _wait_until_silent(na, device_id)
    for attempt in (1, 2):
        try:
            resp = await na.text_to_speech(device_id, text)
            code = (resp or {}).get("code")
            dcode = ((resp or {}).get("data") or {}).get("code")
            if code == 0 and dcode in (0, None):
                log.info("🎙️ TTS 播报: %s", text)
                return True
            log.warning("TTS 返回异常(第%d次): code=%s data.code=%s resp=%s",
                        attempt, code, dcode, str(resp)[:200])
        except Exception as e:
            log.warning("TTS 请求异常(第%d次): %s", attempt, e)
        if attempt == 1:
            await asyncio.sleep(1.2)
    log.error("⚠ TTS 播报失败（重试后仍不成）: %s", text)
    return False


async def _execute_actions(actions: list[dict], ha: "HAClient", na, session,
                           device_id: str = "", tts_reply: bool = False,
                           delayed_runner=None) -> list[dict]:
    """执行一组动作并返回每个动作的结果，不修改入参。

    原实现用 action.pop() 抠掉元字段，既销毁了调用方的列表，又依赖
    "pop 干净"才能避免 reply / schedule_time 被当作 HA 服务参数发出去。
    这里改成显式构造干净 payload。

    na 为 None 时不播报（网页测试页用，避免点一下测试家里音箱突然说话）。
    """
    runner = delayed_runner or _delayed_call
    results = []
    for idx, action in enumerate(actions):
        domain = action.get("domain", "")
        service = action.get("service", "")
        reply = action.get("reply", "好的")
        delay = action.get("delay_minutes")
        schedule_type = action.get("type")
        schedule_time = action.get("schedule_time")
        schedule_days = action.get("schedule_days")
        payload = {k: v for k, v in action.items() if k not in _META_KEYS}

        if schedule_type == "schedule" and schedule_time:
            automation_id, ok = await _create_ha_automation(
                ha, domain, service, payload, schedule_time, schedule_days, session)
            kind = "schedule"
            reply = (f"好的，已创建定时任务：{schedule_time} {schedule_days or '每天'} {reply}"
                     if ok else "抱歉，创建定时任务失败了")
            if ok:
                log.info("🕐 已创建自动化 %s", automation_id)
        elif delay:
            try:
                delay_sec = float(delay) * 60
            except (ValueError, TypeError):
                delay_sec = 0
            kind = "delay"
            log.info("⏰ 定时任务: 将在 %s 分钟后执行 %s/%s", delay, domain, service)
            asyncio.create_task(runner(ha, domain, service, payload, delay_sec, session))
            ok = True
        else:
            kind = "now"
            ok = await ha.call_service(domain, service, payload, session)

        if na is not None and reply and tts_reply:
            tts = reply
            if kind == "now":
                if not ok:
                    tts = f"抱歉，第{idx+1}步操作失败了"
                elif idx < len(actions) - 1:
                    tts += "，继续执行下一步"
            await _speak(na, device_id, tts)

        results.append({"domain": domain, "service": service, "ok": ok,
                        "reply": reply, "kind": kind})
    return results


def _cached_devices() -> list[dict]:
    """取最近一次拉取的设备快照，不触发网络请求（用于记录设备友好名）。"""
    return _devices_cache.get("devices") or []


_CANCEL_WORDS = {"算了", "不用了", "不弄了", "取消", "没事", "别管了", "不要了", "不用"}


def _is_cancel(text: str) -> bool:
    """用户在回答澄清问题时改主意了"""
    cleaned = re.sub(r"[\s。！？!?，,、~～]+", "", text or "")
    return cleaned in _CANCEL_WORDS


async def _resolve_clarification(pending: dict, query: str,
                                 cfg: dict) -> tuple[list[dict], dict]:
    """把用户对澄清问题的回答解析成动作，entity_id 必须落在候选范围内。"""
    devices = await _fetch_ha_devices(cfg)
    if devices is None:
        return [], {}
    valid_ids = {d["entity_id"] for d in devices}
    cands = [c for c in pending.get("candidates", []) if c in valid_ids]
    if not cands:
        log.warning("⚠ 澄清候选设备已不存在，放弃本次澄清")
        return [], {}

    by_id = {d["entity_id"]: d for d in devices}
    lines = [f"- {by_id[c].get('name', '')}（{c}）" for c in cands]
    system = (
        "用户刚才的指令信息不足，你问了他一个问题。现在他给出了回答，"
        "请判断他选的是哪个设备并给出操作。\n"
        "只返回 JSON：{\"entity_id\":\"...\",\"domain\":\"...\",\"service\":\"...\","
        "\"reply\":\"回复文字\"}\n"
        f"entity_id 必须从这个列表里逐字选择：{cands}\n"
        "如果用户的回答和这些候选都无关（例如他改说了别的指令），返回 {\"action\":\"none\"}\n"
        "只返回 JSON，不要任何其他文字。"
    )
    user = (f"原始指令：{pending.get('original_query', '')}\n"
            f"已问过的问题：{pending.get('question', '')}\n"
            "候选设备：\n" + "\n".join(lines) +
            f"\n\n用户的回答：{query}")

    content = await _ai_chat(cfg, system, user, max_tokens=300, timeout=12)
    if not content:
        return [], {}
    parsed = _parse_ai_json(content)
    if not parsed or _ai_is_none(parsed):
        return [], {}
    actions = parsed["actions"] if isinstance(parsed.get("actions"), list) else [parsed]
    # 只接受候选范围内的实体，越界视为解析失败
    actions = _validate_actions(actions, set(cands), devices, cfg.get("device_aliases", {}))
    if not actions:
        return [], {}
    log.info("✅ 澄清解析: %s → %s", query, [a.get("entity_id") for a in actions])
    return actions, {}


async def _ai_generate_reply(query: str, cfg: dict) -> str | None:
    """闲聊兜底：让模型生成一句可直接念出来的短回复。"""
    system = ("你是一个家庭语音助手。用一句不超过 40 字的中文口语回答用户，"
              "直接给答案，不要客套话，不要 markdown，不要引号。")
    return await _ai_chat(cfg, system, query, max_tokens=120, timeout=10)


async def _handle_ai_fallback(query: str, cfg: dict, diag: dict, na,
                              device_id: str, mi_cfg: dict) -> None:
    """AI 判定"这不是设备指令"时的兜底行为，由 ai_fallback 配置决定。"""
    if not diag.get("ai_used") or not diag.get("ai_none"):
        return  # 没走到 AI，或不是"明确判定"，都不是兜底的触发条件
    if diag.get("none_reason") != "non_device":
        # 是设备指令但没匹配上：交给小爱，别冒出一句莫名其妙的闲聊
        return

    mode = (cfg.get("ai_fallback") or "native").lower()
    if mode == "native":
        log.info("↩ AI 判定非设备指令，交还小爱原生回答")
        return
    if na is None or not mi_cfg.get("tts_reply", True):
        return

    text = cfg.get("ai_fallback_text") or "抱歉，我没听懂这个指令，请换个说法。"
    if mode == "reply":
        generated = await _ai_generate_reply(query, cfg)
        if generated:
            text = generated
    log.info("📢 兜底播报(%s): %s", mode, text)
    await _speak(na, device_id, text, interrupt=True)


# ──────────────── QR 扫码登录（绕过短信验证） ────────────────

@app.post("/api/qr/login")
async def qr_login_start():
    """发起 QR 扫码登录，返回二维码链接"""
    import string, random
    try:
        device_id = ''.join(random.choices(string.ascii_letters + string.digits, k=16)).upper()
        headers = {
            "User-Agent": "MiHome/6.0.103 (com.xiaomi.mihome; build:6.0.103.1; iOS 14.4.0) Alamofire/6.0.103",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        s = aiohttp.ClientSession()
        # Step 1: serviceLogin?sid=mijia
        url = f"https://account.xiaomi.com/pass/serviceLogin?_json=true&sid=mijia&deviceId={device_id}&_locale=zh_CN"
        log.info("QR Step1: %s", url[:80])
        try:
            async with s.get(url, headers=headers, ssl=False) as r:
                raw = await r.read()
        except Exception as e:
            await s.close()
            return {"ok": False, "msg": f"无法连接小米服务器: {e}"}

        # 尝试解析响应（去掉可能的 &&& 前缀）
        text = raw.decode("utf-8", errors="replace")
        try:
            service_data = json.loads(text) if text.startswith("{") else json.loads(text[11:])
        except Exception:
            await s.close()
            return {"ok": False, "msg": f"小米返回异常: {text[:200]}"}

        log.info("QR Step1 resp: code=%s", service_data.get("code"))
        if service_data.get("code") == 0:
            await s.close()
            return {"ok": False, "msg": "服务异常，请稍后重试"}

        location = service_data.get("location", "")
        if not location:
            await s.close()
            return {"ok": False, "msg": "无法获取登录入口"}

        # Step 2: 获取二维码
        from urllib.parse import urlencode
        login_url = "https://account.xiaomi.com/longPolling/loginUrl"
        full_url = login_url + "?" + location.split("?")[1] if "?" in location else login_url + "?" + location
        try:
            async with s.get(full_url, headers=headers, ssl=False) as r:
                raw = await r.read()
        except Exception as e:
            await s.close()
            return {"ok": False, "msg": f"获取二维码网络失败: {e}"}

        text = raw.decode("utf-8", errors="replace")
        try:
            login_data = json.loads(text) if text.startswith("{") else json.loads(text[11:])
        except Exception:
            await s.close()
            return {"ok": False, "msg": f"二维码接口返回异常: {text[:200]}"}

        await s.close()
        qr_url = login_data.get("qr", login_data.get("loginUrl", ""))
        lp_url = login_data.get("lp", "")
        if not qr_url or not lp_url:
            return {"ok": False, "msg": "获取二维码失败: " + str(list(login_data.keys()))}

        _qr_state["lp_url"] = lp_url
        _qr_state["device_id"] = device_id
        _qr_state["ready"] = True
        log.info("QR 登录二维码已生成")
        return {"ok": True, "qr_url": qr_url, "msg": "请用米家 App 扫描二维码"}
    except Exception as e:
        log.exception("QR 登录异常: %s", e)
        return {"ok": False, "msg": f"QR 登录失败: {e}"}


@app.get("/api/qr/poll")
async def qr_login_poll():
    """轮询扫码结果"""
    if not _qr_state.get("ready"):
        return {"ok": False, "done": False, "msg": "请先点击扫码登录"}

    lp_url = _qr_state.get("lp_url", "")
    headers = {
        "User-Agent": "MiHome/6.0.103 (com.xiaomi.mihome; build:6.0.103.1; iOS 14.4.0) Alamofire/6.0.103",
        "Connection": "keep-alive",
    }
    s = aiohttp.ClientSession()
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with s.get(lp_url, headers=headers, ssl=False, timeout=timeout) as r:
            raw = await r.read()
        text = raw.decode("utf-8", errors="replace")
        try:
            lp_data = json.loads(text) if text.startswith("{") else json.loads(text[11:])
        except Exception:
            await s.close()
            return {"ok": False, "done": False, "msg": f"轮询异常: {text[:100]}"}
        # 检查是否扫码成功
        if lp_data.get("psecurity") and lp_data.get("userId"):
            # Step 3: 获取 serviceToken
            callback_url = lp_data.get("location", "")
            if callback_url:
                async with s.get(callback_url, headers=headers, ssl=False) as r:
                    pass
            sts_cookies = {k: v.value for k, v in
                           s.cookie_jar.filter_cookies(
                               __import__('yarl').URL('https://api2.mina.mi.com/')).items()}
            service_token = sts_cookies.get("serviceToken", "")
            if not service_token:
                service_token = lp_data.get("serviceToken", "")

            # 保存 auth 数据
            auth_data = {
                "passToken": lp_data["passToken"],
                "userId": str(lp_data["userId"]),
                "deviceId": _qr_state.get("device_id", ""),
                "ssecurity": lp_data.get("ssecurity", lp_data.get("psecurity", "")),
                "serviceToken": service_token,
            }
            AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(AUTH_PATH, "w", encoding="utf-8") as f:
                json.dump(auth_data, f, ensure_ascii=False)
            # 保存 .mi.token
            token_data = {**auth_data, "micoapi": [auth_data["ssecurity"], service_token]}
            TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                json.dump(token_data, f, ensure_ascii=False)
            # 保存 cookie_text 到配置
            cfg = load_config()
            mi_cfg = cfg.get("xiaomi", {})
            cookie_text = f"userId={auth_data['userId']}; passToken={auth_data['passToken']}; serviceToken={service_token}; deviceId={auth_data['deviceId']}"
            cfg.setdefault("xiaomi", {})["cookie_text"] = cookie_text
            save_config(cfg)
            _qr_state.clear()
            log.info("QR 扫码登录成功! userId=%s", auth_data["userId"])
            await s.close()
            return {"ok": True, "done": True, "msg": f"扫码登录成功! userId={auth_data['userId']}"}
        else:
            await s.close()
            return {"ok": True, "done": False, "msg": "等待扫码中..."}
    except asyncio.TimeoutError:
        await s.close()
        return {"ok": True, "done": False, "msg": "等待扫码中..."}
    except Exception as e:
        await s.close()
        if "401" in str(e) or "timeout" in str(e).lower():
            return {"ok": True, "done": False, "msg": "等待扫码中..."}
        log.warning("QR 轮询异常: %s", e)
        return {"ok": False, "done": False, "msg": f"轮询错误: {e}"}


class TestHARequest(BaseModel):
    url: str
    token: str


@app.post("/api/test/ha")
async def test_ha(req: TestHARequest):
    # 前端回显的是脱敏值（••••••••），此时回退到已保存的地址/令牌
    saved = load_config().get("homeassistant", {}) or {}
    url = (req.url or "").strip() or saved.get("url", "")
    token = (req.token or "").strip()
    if not token or token == "••••••••":
        token = saved.get("token", "")
    if not url or not token:
        return {"ok": False, "msg": "请先填写 HA 地址和长期令牌"}
    ha = HAClient(url, token)
    async with aiohttp.ClientSession() as s:
        ok = await ha.test_connection(s)
    return {"ok": ok, "msg": "连接成功" if ok else "连接失败，请检查地址和 Token"}


@app.post("/api/ha/services")
async def get_ha_services(req: TestHARequest):
    ha = HAClient(req.url, req.token)
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(
                f"{ha.base}/api/services",
                headers=ha.headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    services = await r.json()
                    return {"ok": True, "services": services}
                return {"ok": False, "msg": f"请求失败: {r.status}"}
        except Exception as e:
            return {"ok": False, "msg": f"请求异常: {e}"}


class TestRuleRequest(BaseModel):
    text: str
    rules: list[dict]


@app.post("/api/test/rule")
async def test_rule(req: TestRuleRequest):
    """网页"测试规则"卡片。与主循环共用同一套解析/执行逻辑，只是不播报 TTS。

    device_id 留空 = 无状态调用，不读写真实会话上下文（避免测试污染线上记忆）。
    """
    cfg = load_config()
    diag: dict = {}
    actions = await _parse_and_resolve_actions(
        req.text, IntentParser(req.rules), cfg, device_id="", diag=diag)

    source = "📋 规则" if not diag.get("ai_used") else "🤖 AI"

    if diag.get("clarify"):
        c = diag["clarify"]
        return {
            "matched": False, "executed": False, "ai_used": True,
            "clarify": c,
            "msg": f"❓ 需要澄清：{c['question']}（候选 {len(c['candidates'])} 个）",
            "action": None,
        }

    if not actions:
        if diag.get("ai_none"):
            reason = "非设备指令" if diag.get("none_reason") == "non_device" else "未找到对应设备"
        else:
            reason = "未匹配任何规则"
        return {"matched": False, "executed": False, "ai_used": bool(diag.get("ai_used")),
                "msg": reason, "action": None}

    ha_cfg = cfg.get("homeassistant", {})
    ha_url = ha_cfg.get("url", "")
    ha_token = ha_cfg.get("token", "")
    if not ha_url or not ha_token:
        return {"matched": True, "executed": False, "msg": "HA 未配置",
                "action": actions[0], "ai_used": diag.get("ai_used", False)}

    ha = HAClient(ha_url, ha_token)
    try:
        async with aiohttp.ClientSession() as s:
            results = await _execute_actions(
                actions, ha, None, s, tts_reply=False,
                delayed_runner=lambda h, d, sv, data, sec, _sess:
                    _delayed_call_standalone(h, d, sv, data, sec))
    except Exception as e:
        return {"matched": True, "executed": False, "msg": f"HA 调用异常: {e}",
                "action": actions[0], "ai_used": diag.get("ai_used", False)}

    ok_count = sum(1 for r in results if r["ok"])
    steps = len(results)
    kinds = {r["kind"] for r in results}
    if kinds == {"schedule"}:
        msg = f"{source} → 创建定时自动化 {results[0]['reply']}"
    elif "delay" in kinds:
        msg = f"{source} → 已创建延迟任务（{steps} 步），到点后执行"
    else:
        detail = "全部成功" if ok_count == steps else f"{ok_count}/{steps} 步成功"
        msg = f"{source} → {'多步执行：' if steps > 1 else 'HA '}{detail}"

    return {
        "matched": True,
        "executed": ok_count == steps,
        "msg": msg,
        "action": actions[0] if steps == 1 else {"actions": actions},
        "steps": results,
        "ai_used": bool(diag.get("ai_used")),
    }


@app.get("/api/automations")
async def list_automations():
    cfg = load_config()
    ha_cfg = cfg.get("homeassistant", {})
    if not ha_cfg.get("url") or not ha_cfg.get("token"):
        return {"ok": False, "automations": [], "msg": "HA 未配置"}
    ha = HAClient(ha_cfg["url"], ha_cfg["token"])
    async with aiohttp.ClientSession() as s:
        automations = await ha.list_automation_states(s)
    return {"ok": True, "automations": automations, "count": len(automations)}


@app.delete("/api/automations/{automation_id}")
async def delete_automation(automation_id: str):
    cfg = load_config()
    ha_cfg = cfg.get("homeassistant", {})
    if not ha_cfg.get("url") or not ha_cfg.get("token"):
        raise HTTPException(400, "HA 未配置")
    if not automation_id.startswith(_AUTOMATION_PREFIX):
        raise HTTPException(403, "只能删除桥接器创建的自动化")
    ha = HAClient(ha_cfg["url"], ha_cfg["token"])
    async with aiohttp.ClientSession() as s:
        ok = await ha.delete_automation(automation_id, s)
        if ok:
            await ha.reload_automations(s)
    if not ok:
        raise HTTPException(500, "删除失败")
    return {"ok": True}




# ──────────────── 事件监听规则管理 ────────────────
class EventMonitorRule(BaseModel):
    entity_id: str
    trigger_state: str = "on"
    tts_message: str = ""
    cooldown_seconds: int = 60


@app.get("/api/event-monitors")
async def list_event_monitors():
    """列出所有事件监听规则"""
    cfg = load_config()
    rules = cfg.get("event_monitors", []) or []
    return {"ok": True, "count": len(rules), "rules": rules}


@app.post("/api/event-monitors")
async def add_event_monitor(rule: EventMonitorRule):
    """新增事件监听规则（持久化到 config.yaml）"""
    rule.entity_id = rule.entity_id.strip()
    rule.tts_message = rule.tts_message.strip()
    if not rule.entity_id or not rule.tts_message:
        raise HTTPException(400, "entity_id 和 tts_message 不能为空")
    if rule.cooldown_seconds < 0:
        raise HTTPException(400, "cooldown_seconds 不能为负数")
    cfg = load_config()
    rules = cfg.get("event_monitors", []) or []
    new_rule = {
        "entity_id": rule.entity_id,
        "trigger_state": rule.trigger_state,
        "tts_message": rule.tts_message,
        "cooldown_seconds": rule.cooldown_seconds,
    }
    rules.append(new_rule)
    cfg["event_monitors"] = rules
    save_config(cfg)
    event_monitor_engine.prune_cooldowns()
    log.info("已添加事件监听规则: %s=%s → %s",
             rule.entity_id, rule.trigger_state, rule.tts_message)
    return {"ok": True, "count": len(rules), "rules": rules}


@app.delete("/api/event-monitors/{index}")
async def delete_event_monitor(index: int):
    """按序号删除事件监听规则（持久化到 config.yaml）"""
    cfg = load_config()
    rules = cfg.get("event_monitors", []) or []
    if index < 0 or index >= len(rules):
        raise HTTPException(404, "规则不存在")
    removed = rules.pop(index)
    cfg["event_monitors"] = rules
    save_config(cfg)
    event_monitor_engine.prune_cooldowns()
    log.info("已删除事件监听规则: %s", removed.get("entity_id", ""))
    return {"ok": True, "removed": removed, "count": len(rules), "rules": rules}


@app.get("/api/event-monitors/status")
async def event_monitor_status():
    """查看 WebSocket 连接状态与近期触发日志"""
    return {
        "ok": True,
        "state": event_monitor_engine.state,
        "recent_triggers": list(event_monitor_engine._trigger_log),
    }


# ──────────────── TTS 播报 ────────────────
class TTSRequest(BaseModel):
    text: str

@app.post("/api/tts")
async def tts_speak(req: TTSRequest):
    """让小爱音箱播报指定文字"""
    cfg = load_config()
    mi_cfg = cfg.get("xiaomi", {})
    
    if not _has_xiaomi_credentials(cfg):
        raise HTTPException(400, "小米账号未登录，请打开配置页扫码登录")
    
    try:
        from miservice import MiAccount, MiNAService
        from miservice.miaccount import get_random
        
        token_path = CONFIG_PATH.parent / ".mi.token"
        async with aiohttp.ClientSession() as session:
            if token_path.exists():
                token = json.loads(token_path.read_text())
            else:
                token = {"deviceId": get_random(16).upper()}
            
            account = MiAccount(session, mi_cfg.get("username") or "", mi_cfg.get("password") or "", str(token_path))
            account.token = token
            
            na = MiNAService(account)
            devices = await na.device_list()
            
            device_id = ""
            if devices:
                device_id = devices[0].get("deviceID", "")
            
            await na.text_to_speech(device_id, req.text)
            log.info("TTS 播报: %s", req.text)
            
            return {"ok": True, "msg": f"已播报: {req.text}"}
    except Exception as e:
        log.error("TTS 播报失败: %s", e)
        raise HTTPException(500, f"TTS 播报失败: {e}")


# ──────────────── 版本更新 ────────────────
# 从 GitHub 直接拉取文件覆盖本地（不走 git：容器里没有 git，也没挂载 .git）。
# 前提是 compose 把 bridge.py / web 挂载进了容器，否则写进去的只是容器层，重建就没了。

_UPDATE_RAW = "https://raw.githubusercontent.com/{repo}/master/{path}"
_UPDATE_FILES = ("bridge.py", "web/index.html")


def _parse_version(v: str) -> tuple:
    """'3.11.1' → (3, 11, 1)，非数字部分忽略"""
    nums = [int(x) for x in re.findall(r"\d+", v or "")][:3]
    return tuple(nums + [0] * (3 - len(nums)))


def _code_is_mounted() -> bool:
    """代码文件是否通过 bind mount 挂进容器。

    没挂载的话覆盖 /app/bridge.py 只是改了容器可写层，docker compose 一重建就丢，
    所以这种情况必须让用户走手动更新。
    """
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            return any(len(p := line.split()) > 1 and p[1] == "/app/bridge.py"
                       for line in f)
    except Exception:
        return False


def _norm_requirements(text: str) -> list[str]:
    """去掉注释/空行/版本号后的依赖名集合，避免格式差异造成误判"""
    out = set()
    for line in (text or "").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.add(re.split(r"[<>=!~\[;]", line)[0].strip().lower())
    return sorted(out)


async def _fetch_remote(cfg: dict, path: str) -> str | None:
    """下载仓库 master 分支上的文件（走 update_proxy）"""
    repo = (cfg.get("update_repo") or "").strip() or "chenshuhe/xiaoai-ha-bridge"
    url = _UPDATE_RAW.format(repo=repo, path=path)
    proxy = (cfg.get("update_proxy") or "").strip() or None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, proxy=proxy,
                             timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    log.warning("更新: %s 返回 HTTP %s", url, r.status)
                    return None
                return await r.text()
    except Exception as e:
        log.warning("更新: 下载 %s 失败: %s", url, e)
        return None


async def _check_update(cfg: dict) -> dict:
    """比对本地与远端版本，并判断能否自动更新"""
    current = VERSION
    remote = await _fetch_remote(cfg, "bridge.py")
    if not remote:
        return {"ok": False, "current": current,
                "msg": "无法访问 GitHub，请检查网络或在下方填写代理"}
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', remote, re.M)
    if not m:
        return {"ok": False, "current": current,
                "msg": "远端 bridge.py 未找到 VERSION，仓库可能已变更"}

    latest = m.group(1)
    has_update = _parse_version(latest) > _parse_version(current)

    # 依赖变化的话，光换代码文件不够，必须重建镜像。
    # 注意这里是三态：True / False / None(没能确认)。
    # 拉不到远端 requirements.txt 时必须当"未知"而不是"没变"——
    # 万一真加了依赖，更新完容器会因 ImportError 起不来，那比不更新糟得多。
    dep_changed: bool | None = None
    remote_req = await _fetch_remote(cfg, "requirements.txt")
    if remote_req is not None:
        try:
            local_req = Path("requirements.txt").read_text(encoding="utf-8")
            dep_changed = _norm_requirements(remote_req) != _norm_requirements(local_req)
        except Exception as e:
            log.warning("更新: 读取本地 requirements.txt 失败: %s", e)
            dep_changed = None

    return {"ok": True, "current": current, "latest": latest,
            "has_update": has_update, "dep_changed": dep_changed,
            "mounted": _code_is_mounted()}


@app.get("/api/update/check")
async def update_check():
    return await _check_update(load_config())


@app.post("/api/update/apply")
async def update_apply():
    """下载新版覆盖本地，然后让进程退出交给 Docker 拉起。

    任何一步失败都不动现有文件；写入过程中出错则从备份回滚。
    """
    cfg = load_config()
    info = await _check_update(cfg)
    if not info.get("ok"):
        raise HTTPException(400, info.get("msg", "检查更新失败"))
    if not info.get("has_update"):
        return {"ok": True, "restart": False, "msg": f"已是最新版本 v{info['current']}"}
    if not info.get("mounted"):
        raise HTTPException(400, "当前部署未把代码挂载进容器，无法自动更新。"
                                 "请手动执行：git pull && docker compose up -d --build")
    # 只有"确认过依赖没变"才允许自动更新；True 或 None(未确认) 都拒绝
    dep = info.get("dep_changed")
    if dep is True:
        raise HTTPException(400, "新版本的依赖有变化，自动更新装不了依赖。"
                                 "请手动执行：git pull && docker compose up -d --build")
    if dep is None:
        raise HTTPException(400, "没能确认新版本的依赖是否变化（网络或代理不稳定），"
                                 "为免更新后起不来，已中止。请稍后重试，或手动执行："
                                 "git pull && docker compose up -d --build")

    latest = info["latest"]

    # 1) 下载并预校验
    staged: dict[str, str] = {}
    for rel in _UPDATE_FILES:
        content = await _fetch_remote(cfg, rel)
        if not content:
            raise HTTPException(400, f"下载 {rel} 失败，已中止（未改动任何文件）")
        staged[rel] = content

    new_py = staged["bridge.py"]
    if not re.search(rf'^VERSION\s*=\s*"{re.escape(latest)}"', new_py, re.M):
        raise HTTPException(400, "下载内容与预期版本不符，已中止")
    if "FastAPI" not in new_py or "bridge_loop" not in new_py:
        raise HTTPException(400, "下载内容不像本项目的 bridge.py，已中止")

    # 语法校验：临时写一份编译，过了才敢覆盖线上文件
    import py_compile
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="xiaoai-update-")
    try:
        cand = Path(tmpdir) / "candidate.py"
        cand.write_text(new_py, encoding="utf-8")
        py_compile.compile(str(cand), cfile=str(Path(tmpdir) / "c.pyc"), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, f"下载的 bridge.py 语法校验失败，已中止：{str(e.msg)[:150]}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    html = staged["web/index.html"]
    if "<html" not in html.lower() or "</html>" not in html.lower():
        raise HTTPException(400, "下载的 index.html 内容异常，已中止")

    # 2) 备份 + 覆盖。用写入而非替换，保持 inode 不变，bind mount 才认得出
    backups: list[tuple[Path, Path]] = []
    try:
        for rel in _UPDATE_FILES:
            p = Path(rel)
            bak = p.with_name(p.name + f".pre-{VERSION}.bak")
            if p.exists():
                shutil.copy2(p, bak)
                backups.append((p, bak))
        for rel, content in staged.items():
            Path(rel).write_text(content, encoding="utf-8")
    except Exception as e:
        for p, bak in backups:
            try:
                shutil.copy2(bak, p)
            except Exception:
                pass
        log.error("更新写入失败，已尝试回滚: %s", e)
        raise HTTPException(500, f"写入失败，已回滚到 v{VERSION}：{e}")

    log.info("已更新到 v%s，进程即将退出，由 Docker 重启", latest)
    # 等响应发出去再退出；restart: unless-stopped 会把容器拉起来
    asyncio.get_running_loop().call_later(1.5, os._exit, 0)
    return {"ok": True, "restart": True, "latest": latest,
            "msg": f"已更新到 v{latest}，正在重启…"}


# ──────────────── 启动 ────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT, log_level="warning")
