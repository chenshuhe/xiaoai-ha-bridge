# 小爱同学 → Home Assistant 语音桥接器

让**小米小爱智能音箱**通过语音控制 **Home Assistant** 中的设备。只需说出自然语言指令，桥接器自动将语音匹配为 HA 服务调用。

---

## 功能特性

- **自然语言匹配** — 用正则规则将日常口语映射到 HA 服务，支持数字提取（温度、亮度、分钟）
- **可视化规则编辑器** — Web 面板配置，图标化选择灯光/空调/场景等，无需手写 JSON
- **多房间空调控制** — 打开/关闭/调温/定时关闭，四句口语搞定
- **定时执行** — "客厅空调30分钟后关闭"自动后台倒计时
- **TTS 语音反馈** — 执行后小爱音箱语音播报结果
- **扫码登录 + Token 持久化** — 用米家 App 扫一次码，后续自动续期，无需账号密码
- **规则导入/导出** — 一键备份分享规则配置
- **Docker / systemd 双部署** — 开箱即用

---

## 快速开始

### 前提条件

- 一台**小爱智能音箱**（已绑定小米账号）
- 一个 **Home Assistant** 实例（需获取 Long-Lived Access Token）
- Python 3.11+ 或 Docker

### Docker 部署（推荐）

```bash
git clone https://github.com/chenshuhe/xiaoai-ha-bridge.git
cd xiaoai-ha-bridge
docker compose up -d
```

访问 `http://<服务器IP>:47521` 打开配置面板。

### 裸机部署

```bash
pip install -r requirements.txt
python bridge.py
```

### systemd 服务

```bash
sudo cp xiaoai-ha-bridge@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xiaoai-ha-bridge@$USER
```

---

## 更新

### 网页一键更新（推荐）

配置面板 → **高级设置** → **版本更新** → 点「检查更新」，有新版本会出现「立即更新」按钮。

点击后桥接器会从 GitHub 拉取最新的 `bridge.py` 与 `web/index.html`，**先备份、再校验语法**，
通过后覆盖并自动重启。整个过程约 10 秒，失败会自动回滚。

前提是代码通过 bind mount 挂进了容器（仓库自带的 `docker-compose.yml` 已经是这样）。
**如果服务器访问 GitHub 需要代理**，在同一个卡片里填「GitHub 代理」。

以下情况会自动拒绝并提示手动更新：

- 新版本改动了依赖（`requirements.txt`）—— 自动更新装不了依赖
- 没能确认依赖是否变化（网络不稳）—— 宁可不更新，也不能更新完起不来
- 部署方式没挂载代码文件

### 手动更新

```bash
cd xiaoai-ha-bridge
git pull
docker compose restart
```

因为 `bridge.py` 和 `web/` 是挂载进容器的，`git pull` 后重启即可生效，不必重建镜像。

不过**依赖有变化时必须重建**：

```bash
git pull && docker compose up -d --build
```

> 注意 `docker compose restart` 不会重读 `docker-compose.yml`。
> 如果你改过 compose 文件（比如加了挂载），要用 `docker compose up -d` 才会生效。

---

## 配置指南

打开 `http://<服务器IP>:47521`，按顺序完成三步配置：

### 1. 小米账号 → 扫码登录

点击**生成二维码**，用**米家 App** 扫描。登录成功后自动保存 token 并列出发现的音箱，后续无需重复登录。

不需要填写账号密码；token 失效时重新扫一次即可。

### 2. Home Assistant → 测试连接

填写 HA 地址和 Long-Lived Access Token，点击测试。

### 3. 规则 → 新增规则

| 字段 | 说明 | 示例 |
|---|---|---|
| 你说的话 | 正则表达式，`{num}` 匹配数字 | `(打开\|开)(客厅灯)` |
| 控制什么设备 | 灯光/开关/空调/场景... | 💡 灯光 |
| 操作 | 打开/关闭/设置温度... | 打开 |
| 实体 ID | HA 中的 entity_id | `light.living_room` |
| 回复文字 | 执行后小爱播报的内容 | 客厅灯已打开 |

保存规则后点击**启动桥接器**，即可用小爱语音控制。

---

## 规则示例

```yaml
# 开关灯
- pattern: "(打开|开)(客厅灯)"
  action:
    domain: light
    service: turn_on
    entity_id: light.living_room
    reply: "好的，客厅灯已打开"

# 调亮度
- pattern: "把客厅灯调到{num}[%％]"
  action:
    domain: light
    service: turn_on
    entity_id: light.living_room
    brightness_pct: "{0}"
    reply: "好的，亮度已调整"

# 空调调温
- pattern: "主卧空调设{num}度"
  action:
    domain: climate
    service: set_temperature
    entity_id: climate.master_bedroom_ac
    temperature: "{0}"
    reply: "主卧已设为 {0} 度"

# 定时关闭
- pattern: "客厅空调{num}分钟后关闭"
  action:
    domain: climate
    service: turn_off
    entity_id: climate.living_room_ac
    delay_minutes: "{0}"
    reply: "好的，{0} 分钟后关闭客厅空调"
```

---

## 项目结构

```
├── bridge.py              # 主程序（桥接核心 + Web API）
├── config/
│   └── config.yaml        # 配置文件（登录凭据、HA、规则）
├── web/
│   ├── index.html         # Web 配置面板
│   └── static/            # 静态资源
├── Dockerfile             # Docker 镜像
├── docker-compose.yml     # Docker Compose
├── xiaoai-ha-bridge@.service  # systemd 服务文件
└── requirements.txt       # Python 依赖
```

---

## 技术栈

- **Python** — FastAPI + aiohttp + miservice
- **前端** — 原生 HTML/CSS/JS（零构建，一个文件）
- **认证** — 小米 micoapi 会话 + HA Bearer Token
- **对话获取** — 小米 HTTP API + Mina 服务双通道

---

## 许可证

MIT License

---

## 致谢

- [miservice](https://github.com/yihong0618/miservice) — 小米服务 Python SDK
- [xiaomusic](https://github.com/hanxi/xiaomusic) — 登录与对话轮询方案参考
