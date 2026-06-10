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
VERSION = "3.9.0"
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
}

# 临时保存短信验证流程中的登录状态
_pending_login: dict = {}
_pending_session: aiohttp.ClientSession | None = None

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
        "device_aliases": {},
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
    mi_cfg = cfg["xiaomi"]
    ha_cfg = cfg["homeassistant"]

    if not mi_cfg.get("username") or not mi_cfg.get("password"):
        bridge_state["error"] = "小米账号未配置"
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
        account = MiAccount(_bridge_session, mi_cfg["username"], mi_cfg["password"],
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
                        account2 = MiAccount(_bridge_session, mi_cfg["username"], mi_cfg["password"], str(TOKEN_PATH))
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
                    action = parser.parse(query)
                    # AI 托管模式：正则未匹配时用 AI 解析
                    if not action and cfg.get("ai_mode") and cfg.get("openai", {}).get("api_key"):
                        ai_result = await ai_parse_command(query)
                        if ai_result:
                            action = ai_result
                            log.info("🤖 AI 解析: %s → %s/%s", query,
                                     ai_result.get("domain"), ai_result.get("service"))
                    if action:
                        domain = action.pop("domain")
                        service = action.pop("service")
                        reply = action.pop("reply", "好的")
                        delay = action.pop("delay_minutes", None)
                        schedule_type = action.pop("type", None)
                        schedule_time = action.pop("schedule_time", None)
                        schedule_days = action.pop("schedule_days", None)

                        if schedule_type == "schedule" and schedule_time:
                            # 定时自动化：在 HA 中创建定时任务
                            automation_id, ok = await _create_ha_automation(
                                ha, domain, service, action,
                                schedule_time, schedule_days, _bridge_session,
                            )
                            if ok:
                                reply = f"好的，已创建定时任务：{schedule_time} {schedule_days or '每天'} {reply}"
                                log.info("🕐 已创建自动化 %s", automation_id)
                            else:
                                reply = "抱歉，创建定时任务失败了"
                            if mi_cfg.get("tts_reply", True):
                                try:
                                    await na.text_to_speech(device_id, reply)
                                except Exception:
                                    pass
                        elif delay:
                            # 延迟执行：先回复，后定时执行
                            try:
                                delay_sec = int(delay) * 60
                            except ValueError:
                                delay_sec = 0
                            log.info("⏰ 定时任务: %s 将在 %s 分钟后执行 %s/%s",
                                     query, delay, domain, service)
                            if mi_cfg.get("tts_reply", True):
                                try:
                                    await na.text_to_speech(device_id, reply)
                                except Exception:
                                    pass
                            asyncio.create_task(_delayed_call(
                                ha, domain, service, action, delay_sec, _bridge_session
                            ))
                        else:
                            ok = await ha.call_service(domain, service, action, _bridge_session)
                            if mi_cfg.get("tts_reply", True):
                                tts = reply if ok else "抱歉，操作失败了"
                                try:
                                    await na.text_to_speech(device_id, tts)
                                except Exception:
                                    pass

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
                       data: dict, delay_sec: int, session: aiohttp.ClientSession):
    """延迟执行 HA 服务调用（用于定时关闭等场景）"""
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


# ──────────────── FastAPI 应用 ────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器启动后自动开始轮询
    await start_bridge()
    log.info("Web 配置面板已启动 → http://0.0.0.0:%d", WEB_PORT)
    yield
    global _bridge_task
    if _bridge_task and not _bridge_task.done():
        bridge_state["running"] = False
        _bridge_task.cancel()


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
    if req.use_saved or not username or not password or password == "••••••••":
        if not username:
            username = mi.get("username", "")
        if not password or password == "••••••••":
            password = mi.get("password", "")
    if not username or not password:
        return {"ok": False, "msg": "请先填写账号和密码"}

    global _pending_session
    if _pending_session and not _pending_session.closed:
        await _pending_session.close()

    try:
        from miservice.miaccount import get_random

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

        # 检查是否需要短信验证
        if resp.get('securityStatus', 0) & 16:
            notif_url = resp.get('notificationUrl', '')
            if not notif_url:
                verify_url = "https://account.xiaomi.com"
            elif notif_url.startswith("https://") or notif_url.startswith("http://"):
                verify_url = notif_url
            elif notif_url.startswith("https//"):
                verify_url = notif_url.replace("https//", "https://", 1)
            elif notif_url.startswith("//"):
                verify_url = f"https:{notif_url}"
            elif notif_url.startswith("/"):
                verify_url = f"https://account.xiaomi.com{notif_url}"
            else:
                verify_url = f"https://account.xiaomi.com/{notif_url}"

            _pending_login.update({
                'ssecurity': resp.get('ssecurity', ''),
                'nonce': resp.get('nonce', ''),
                'location': resp.get('location', ''),
                'userId': resp.get('userId', ''),
                'passToken': resp.get('passToken', ''),
                'username': username,
                'password': password,
            })
            _pending_session = s
            return {
                "ok": False,
                "msg": "密码验证通过，但需要手机验证码确认。\n①复制下方链接在浏览器中打开 ②完成短信验证 ③将跳转后页面的完整 Cookie 粘贴到下方输入框",
                "need_verify": True,
                "verify_url": verify_url,
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


class XiaomiVerifyRequest(BaseModel):
    cookie_text: str = ""


@app.post("/api/test/xiaomi/verify")
async def xiaomi_verify(req: XiaomiVerifyRequest):
    """处理短信验证回调：支持 STS URL 或浏览器 Cookie"""
    global _pending_session
    try:
        from miservice import MiAccount, MiNAService
    except ImportError:
        return {"ok": False, "msg": "缺少依赖: pip install miservice"}

    if not _pending_login.get('username'):
        return {"ok": False, "msg": "请先点击「测试连接」触发验证流程"}

    username = _pending_login['username']
    password = _pending_login['password']
    pending = dict(_pending_login)

    if not req.cookie_text:
        return {"ok": False, "msg": "请粘贴短信验证完成后的跳转 URL"}

    # ── 方式A：短信验证回调 URL（自动获取 serviceToken）──
    if req.cookie_text.startswith("https://api2.mina.mi.com/sts"):
        log.info("检测到 STS 回调 URL，自动获取 serviceToken...")
        s = aiohttp.ClientSession()
        try:
            # 用 pending session 的 cookies 请求 STS 端点
            if _pending_session and not _pending_session.closed:
                import yarl
                for domain in ['account.xiaomi.com', '.mina.mi.com']:
                    pending = _pending_session.cookie_jar.filter_cookies(yarl.URL(f'https://{domain}/'))
                    s.cookie_jar.update_cookies(pending, yarl.URL(f'https://{domain}/'))
            async with s.get(req.cookie_text, timeout=aiohttp.ClientTimeout(total=15)):
                pass
            sts_cookies = {k: v.value for k, v in
                           s.cookie_jar.filter_cookies(yarl.URL('https://api2.mina.mi.com/')).items()}
            service_token = sts_cookies.get('serviceToken', '')
            if not service_token:
                raise Exception("未从 STS 响应中获取到 serviceToken，请重试")
            log.info("已获取 serviceToken: %s...", service_token[:30])
        except Exception as e:
            await s.close()
            return {"ok": False, "msg": f"STS 请求失败: {e}"}

        # 构造 token
        from miservice.miaccount import get_random
        account = MiAccount(s, username, password, str(TOKEN_PATH))
        account.token = {
            'deviceId': pending.get('deviceId', get_random(16).upper()),
            'userId': pending.get('userId', ''),
            'passToken': pending.get('passToken', ''),
            'micoapi': (pending.get('ssecurity', ''), service_token),
        }
        _save_auth_and_token(account)
        # 保存精简 cookie 到配置
        simple_cookie = f"userId={account.token['userId']}; passToken={account.token['passToken']}; serviceToken={service_token}; deviceId={account.token['deviceId']}"
        cfg = load_config()
        cfg.setdefault("xiaomi", {})["cookie_text"] = simple_cookie
        save_config(cfg)

        na = MiNAService(account)
        devices = await na.device_list()
        await s.close()
        _pending_login.clear()
        _pending_session = None
        if devices:
            return {"ok": True, "msg": f"短信验证完成！发现 {len(devices)} 台设备", "devices": devices}
        return {"ok": True, "msg": "短信验证完成，但未发现小爱音箱设备", "devices": []}

    # ── 方式B：浏览器 Cookie ──
    from http.cookies import SimpleCookie
    sc = SimpleCookie()
    sc.load(req.cookie_text)
    cookies = {k: m.value for k, m in sc.items()}

    if not cookies.get('userId'):
        return {"ok": False, "msg": "未识别有效内容。请确认粘贴的是短信验证后的跳转 URL（以 https://api2.mina.mi.com/sts 开头）或完整 Cookie"}
    if not cookies.get('passToken') or not cookies.get('serviceToken'):
        return {"ok": False, "msg": "Cookie 缺少 passToken 或 serviceToken，请复制完整 Cookie"}

    try:
        if _pending_session and not _pending_session.closed:
            await _pending_session.close()

        s = aiohttp.ClientSession()
        import yarl
        sc2 = SimpleCookie()
        for k, v in cookies.items():
            try: sc2[k] = v
            except Exception: pass
        for domain in ['account.xiaomi.com', '.mina.mi.com']:
            s.cookie_jar.update_cookies(sc2, yarl.URL(f'https://{domain}/'))

        from miservice.miaccount import get_random
        account = MiAccount(s, username, password, str(TOKEN_PATH))
        account.token = {
            'deviceId': cookies.get('deviceId', get_random(16).upper()),
            'userId': cookies['userId'],
            'passToken': cookies['passToken'],
            'micoapi': ('', cookies['serviceToken']),
        }
        na = MiNAService(account)
        devices = await na.device_list()
        _save_auth_and_token(account)
        cfg = load_config()
        cfg.setdefault("xiaomi", {})["cookie_text"] = req.cookie_text
        save_config(cfg)
        await s.close()
        _pending_login.clear()
        _pending_session = None
        if devices:
            return {"ok": True, "msg": f"登录成功，发现 {len(devices)} 台设备", "devices": devices}
        return {"ok": True, "msg": "登录成功，但未发现小爱音箱设备", "devices": []}
    except Exception as e:
        log.info(f"verify failed: {e}")
        return {"ok": False, "msg": f"验证失败: {e}"}


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


def _build_ai_prompt(query: str, devices: list[dict], aliases: dict) -> str:
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
        eid = d["entity_id"]
        if eid in aliases:
            entry["alias"] = aliases[eid]
        device_list.append(entry)

    system = (
        "你是 Home Assistant 语音助手。根据用户的自然语言指令，从设备列表中选择最匹配的设备并返回操作指令。\n"
        "规则：\n"
        "1. 优先使用设备的 alias（别名）来匹配用户指令\n"
        "2. 如果用户提到房间名(主卧/客厅/餐厅/次卧/厨房/阳台/书房)，优先匹配名称含该房间的设备\n"
        "3. 如果用户说温度数字，service 用 set_temperature，temperature 设为数字\n"
        "4. 如果用户说百分比，对应 brightness_pct 或 volume_level\n"
        "5. 如果没有匹配的设备或指令不明确，返回 {\"action\":\"none\"}\n"
        "6. 只返回 JSON，不要任何其他文字。\n"
        "7. 如果用户指令包含定时/计划/每天/每周/每小时/几点/半/分等时间相关词汇（如\"每天七点半打开热水器\"），"
        "这是定时指令，返回格式增加 type、schedule_time、schedule_days 字段：\n"
        "   {\"type\":\"schedule\",\"entity_id\":\"...\",\"domain\":\"...\",\"service\":\"...\","
        "\"schedule_time\":\"07:30\",\"schedule_days\":\"daily\",\"reply\":\"回复文字\"}\n"
        "   - schedule_time 格式为 HH:MM（24小时制）\n"
        "   - schedule_days: \"daily\"=每天, \"weekdays\"=工作日, \"weekends\"=周末, 或具体星期如 \"mon,wed,fri\"\n"
        "   - 普通即时指令不需要 type 字段\n"
        "返回格式：{\"entity_id\":\"...\",\"domain\":\"...\",\"service\":\"...\",<额外参数>, \"reply\":\"回复文字\"}"
    )

    user = f"用户指令: {query}\n\n可用设备:\n{_json.dumps(device_list, ensure_ascii=False, indent=2)}"
    return system, user


async def ai_parse_command(query: str) -> dict | None:
    """用 OpenAI 解析用户指令，返回 HA action 或 None"""
    cfg = load_config()
    oai_cfg = cfg.get("openai", {})
    api_key = oai_cfg.get("api_key", "")
    if not api_key:
        return None

    ha_cfg = cfg.get("homeassistant", {})
    if not ha_cfg.get("token"):
        return None

    # 获取设备列表
    ha_headers = {
        "Authorization": f"Bearer {ha_cfg['token']}",
        "Content-Type": "application/json",
    }
    base = ha_cfg["url"].rstrip("/")
    controllable = {"light", "switch", "climate", "scene", "media_player", "script",
                    "automation", "input_boolean", "fan", "cover", "lock", "vacuum", "button"}

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/api/states", headers=ha_headers,
                            timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return None
                all_states = await r.json()
    except Exception:
        return None

    devices = []
    for st in all_states:
        eid = st.get("entity_id", "")
        domain = eid.split(".")[0] if "." in eid else ""
        if domain not in controllable:
            continue
        attrs = st.get("attributes", {})
        devices.append({
            "name": attrs.get("friendly_name", eid),
            "entity_id": eid,
            "domain": domain,
            "state": st.get("state", ""),
        })

    aliases = cfg.get("device_aliases", {})
    system_prompt, user_prompt = _build_ai_prompt(query, devices, aliases)
    model = oai_cfg.get("model", "gpt-4o-mini")

    try:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": 800,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        api_base = oai_cfg.get("api_base", "https://api.openai.com").rstrip("/")
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{api_base}/v1/chat/completions",
                              json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    body = await r.text()
                    log.warning("AI API 返回 %s: %s", r.status, body[:300])
                    return None
                data = await r.json()

        msg = data["choices"][0].get("message", {})
        content = msg.get("content") or msg.get("reasoning_content") or ""
        if not content:
            log.warning("AI 返回空内容, response keys: %s", list(data.keys()))
            if data.get("choices"):
                log.warning("choice keys: %s, msg keys: %s",
                           list(data["choices"][0].keys()),
                           list(data["choices"][0].get("message", {}).keys()))
            return None
        content = content.strip()
        log.info("AI raw content: %s", content[:200])

        # 提取 JSON（可能被 markdown 包裹）
        if "```" in content:
            parts = content.split("```")
            content = parts[1] if len(parts) > 1 else content
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        # 尝试直接解析
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            # 尝试找到第一个 { 到最后一个 }
            start = content.find("{")
            end = content.rfind("}")
            if start >= 0 and end > start:
                content = content[start:end+1]
                result = json.loads(content)
            else:
                raise

        if result.get("action") == "none":
            return None
        return result
    except Exception as e:
        log.warning("AI 解析失败: %s, content was: %s", e, content[:200] if 'content' in dir() else 'N/A')
        return None


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
    ha = HAClient(req.url, req.token)
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
    # 先尝试正则匹配
    parser = IntentParser(req.rules)
    result = parser.parse(req.text)

    # AI 托管：正则未匹配时用 AI 解析
    ai_used = False
    if not result:
        cfg = load_config()
        if cfg.get("ai_mode") and cfg.get("openai", {}).get("api_key"):
            result = await ai_parse_command(req.text)
            ai_used = True
        if not result:
            return {"matched": False, "executed": False, "msg": "未匹配任何规则", "action": None}

    domain = result.pop("domain", "")
    service = result.pop("service", "")
    reply = result.pop("reply", "好的")
    delay = result.pop("delay_minutes", None)
    schedule_type = result.pop("type", None)
    schedule_time = result.pop("schedule_time", None)
    schedule_days = result.pop("schedule_days", None)

    cfg = load_config()
    ha_cfg = cfg.get("homeassistant", {})
    ha_url = ha_cfg.get("url", "")
    ha_token = ha_cfg.get("token", "")

    if not ha_url or not ha_token:
        return {"matched": True, "executed": False, "msg": "HA 未配置", "action": result, "ai_used": ai_used}

    if schedule_type == "schedule" and schedule_time:
        if not ha_token:
            return {"matched": True, "executed": False, "msg": "HA 未配置", "action": result, "ai_used": ai_used}
        ha = HAClient(ha_url, ha_token)
        async with aiohttp.ClientSession() as s:
            automation_id, created = await _create_ha_automation(
                ha, domain, service, result, schedule_time, schedule_days, s,
            )
        return {
            "matched": True,
            "executed": created,
            "msg": f"{'🤖 AI' if ai_used else '📋 规则'} → 创建定时自动化 {schedule_time} {'成功' if created else '失败'}",
            "action": {**result, "domain": domain, "service": service, "reply": reply,
                       "type": "schedule", "schedule_time": schedule_time, "schedule_days": schedule_days},
            "ai_used": ai_used,
        }

    if delay:
        return {"matched": True, "executed": False, "msg": f"定时任务需通过桥接器执行（{delay}分钟延迟）", "action": result, "ai_used": ai_used}

    try:
        async with aiohttp.ClientSession() as s:
            ha_headers = {
                "Authorization": f"Bearer {ha_token}",
                "Content-Type": "application/json",
            }
            url = f"{ha_url.rstrip('/')}/api/services/{domain}/{service}"
            async with s.post(url, json=result, headers=ha_headers,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                ok = r.status in (200, 201)
        return {
            "matched": True,
            "executed": ok,
            "msg": f"{'🤖 AI' if ai_used else '📋 规则'} → HA {'成功' if ok else '失败'}（{r.status}）",
            "action": {**result, "domain": domain, "service": service, "reply": reply},
            "ai_used": ai_used,
        }
    except Exception as e:
        return {
            "matched": True,
            "executed": False,
            "msg": f"HA 调用异常: {e}",
            "action": {**result, "domain": domain, "service": service, "reply": reply},
            "ai_used": ai_used,
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


# ──────────────── 启动 ────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT, log_level="warning")
