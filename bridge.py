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
VERSION = "3.2.0"
CONFIG_PATH = Path("config/config.yaml")
LOG_PATH = Path("logs/bridge.log")
TOKEN_PATH = Path("config/.mi.token")
AUTH_PATH = Path("config/auth.json")
WEB_PORT = 47521

LATEST_ASK_API = "https://userprofile.mina.mi.com/device_profile/v2/conversation?source=dialogu&hardware={hardware}&timestamp={timestamp}&limit=2"
COOKIE_TEMPLATE = "deviceId={device_id}; serviceToken={service_token}; userId={user_id}"
GET_ASK_BY_MINA = ["M01"]

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
                                      hardware: str, last_ts: int) -> tuple[list[dict], int]:
    """HTTP API 方式获取最新对话（参考 xiaomusic get_latest_ask_from_xiaoai）"""
    cookies = {"deviceId": device_id}
    for i in range(3):
        try:
            url = LATEST_ASK_API.format(
                hardware=hardware, timestamp=str(int(time.time() * 1000))
            )
            timeout = aiohttp.ClientTimeout(total=15)
            r = await session.get(url, timeout=timeout, cookies=cookies)
            if r.status != 200:
                log.warning("HTTP 轮询返回 %s (第%d次)", r.status, i + 1)
                if i == 2 and r.status == 401:
                    return [], last_ts  # 401 需要重登录，但这里先返回空
                continue
            data = await r.json()
            d = data.get("data")
            if not d:
                return [], last_ts
            records = json.loads(d).get("records", [])
            if records:
                return records, last_ts
        except Exception as e:
            log.warning("HTTP 轮询异常 (第%d次): %s", i + 1, e)
            continue
    return [], last_ts


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

    # 基于时间戳的去重，key=device_id
    last_timestamp: dict[str, int] = {}

    _bridge_session = aiohttp.ClientSession()
    try:
        account = MiAccount(_bridge_session, mi_cfg["username"], mi_cfg["password"],
                           str(TOKEN_PATH) if TOKEN_PATH.parent.exists() else None)
        _set_token(account, cfg)
        log.info("正在登录小米账号...")
        ok = await account.login("micoapi")
        if not ok:
            bridge_state["error"] = "小米账号登录失败，请在页面中先通过 Cookie 验证登录"
            bridge_state["running"] = False
            return

        # 保存 token 到文件
        try:
            TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            if hasattr(account, 'token') and account.token:
                with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                    json.dump(account.token, f)
        except Exception:
            pass

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
            for domain in ['account.xiaomi.com', 'api2.mina.mi.com']:
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
                    records, _ = await _get_latest_ask_from_xiaoai(
                        _bridge_session, device_id, hardware or "L06A", cur_ts
                    )

                for rec in records or []:
                    # 时间戳去重
                    ts = rec.get("time", rec.get("timestamp_ms", 0))
                    if not ts:
                        continue
                    ts = int(ts)
                    if ts <= last_timestamp.get(device_id, 0):
                        continue
                    last_timestamp[device_id] = ts

                    # 提取用户问题
                    query = _extract_query(rec)
                    if not query:
                        continue
                    log.info("🎤 收到: %s", query)
                    action = parser.parse(query)
                    if action:
                        domain = action.pop("domain")
                        service = action.pop("service")
                        reply = action.pop("reply", "好的")
                        delay = action.pop("delay_minutes", None)

                        if delay:
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
    return cfg


class SaveConfigRequest(BaseModel):
    config: dict[str, Any]


@app.post("/api/config")
async def post_config(req: SaveConfigRequest):
    cfg = req.config
    # 如果密码是脱敏值则保留原密码
    existing = load_config()
    if cfg.get("xiaomi", {}).get("password") == "••••••••":
        cfg["xiaomi"]["password"] = existing.get("xiaomi", {}).get("password", "")
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

    s = aiohttp.ClientSession()
    try:
        # 参考 xiaomusic login_miboy：先注入 token，再调 login("micoapi")
        account = MiAccount(s, username, password, str(TOKEN_PATH))
        _set_token(account, cfg)
        ok = await account.login("micoapi")
        if not ok:
            await s.close()
            return {"ok": False, "msg": "登录失败，请检查账号密码或尝试 Cookie 验证登录"}

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
        if not s.closed:
            await s.close()
        return {"ok": False, "msg": f"请求失败: {e}"}


class XiaomiVerifyRequest(BaseModel):
    cookie_text: str = ""


@app.post("/api/test/xiaomi/verify")
async def xiaomi_verify(req: XiaomiVerifyRequest):
    """用户粘贴浏览器 cookie，注入到 session 中完成登录（参考 xiaomusic 方式）"""
    global _pending_session
    try:
        from miservice import MiAccount, MiNAService
    except ImportError:
        return {"ok": False, "msg": "缺少依赖: pip install miservice"}

    if not _pending_login.get('username'):
        return {"ok": False, "msg": "请先点击「测试连接」触发验证流程"}

    username = _pending_login['username']
    password = _pending_login['password']

    if not req.cookie_text:
        return {"ok": False, "msg": "请粘贴浏览器中的 Cookie 内容"}

    # 使用 SimpleCookie 解析（参考 xiaomusic parse_cookie_string_to_dict）
    from http.cookies import SimpleCookie
    sc = SimpleCookie()
    sc.load(req.cookie_text)
    cookies = {k: m.value for k, m in sc.items()}

    user_id = cookies.get('userId', '')
    pass_token = cookies.get('passToken', '')
    service_token = cookies.get('serviceToken', '')

    if not user_id:
        return {"ok": False, "msg": "Cookie 中未找到 userId，请确认已登录小米账号"}
    if not pass_token:
        return {"ok": False, "msg": "Cookie 中未找到 passToken，请确认已登录小米账号"}
    if not service_token:
        return {"ok": False, "msg": "Cookie 中未找到 serviceToken，请确认已登录小米账号并复制完整 Cookie"}

    try:
        if _pending_session and not _pending_session.closed:
            await _pending_session.close()

        s = aiohttp.ClientSession()
        # 注入 cookie 到 session
        import yarl
        sc2 = SimpleCookie()
        for k, v in cookies.items():
            try:
                sc2[k] = v
            except Exception:
                pass
        for domain in ['account.xiaomi.com', 'api2.mina.mi.com']:
            s.cookie_jar.update_cookies(sc2, yarl.URL(f'https://{domain}/'))

        from miservice.miaccount import get_random

        # 参考 xiaomusic 方式：注入 token 后调用 login("micoapi")
        account = MiAccount(s, username, password, str(TOKEN_PATH))
        account.token = {
            'deviceId': cookies.get('deviceId', get_random(16).upper()),
            'userId': user_id,
            'passToken': pass_token,
            'micoapi': ('', service_token),
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
    parser = IntentParser(req.rules)
    result = parser.parse(req.text)
    return {"matched": result is not None, "action": result}


# ──────────────── 启动 ────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT, log_level="warning")
