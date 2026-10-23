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
- **设备类型对齐 HA** — 风扇（风量 / 摇头）、窗帘（开合度）、门锁、扫地机、按键、加湿器、热水器、水阀、选择器、数值等都能在规则里配
- **设备 ID 一键复制 / 选择** — 设备管理点 ID 即复制；意图规则页可直接从 HA 设备列表挑设备，不用手抄
- **失效别名识别** — HA 实体 ID 变了之后，旧别名单独计数并可一键清理，不再出现虚假的「已配别名」数量
- **账号掉线邮件提醒** — 小米登录态失效自动发邮件（SMTP 可配，带冷却）
- **面板访问保护** — 可选 HTTP Basic 登录认证，防止同网络的人打开面板改配置
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

页面上的「实体 ID」不用手抄：点右侧 **选择** 按钮，会弹出 HA 当前设备列表，点一行即填入；
也可以到「设备管理」点设备 ID 直接复制。

### 4. 设备管理（别名 / 复制 ID / 失效清理）

- **别名**：给设备配几个口语别名（多个用「、」分隔保存也可以，例如 `左炉灶、左灶、燃气灶`）。
  开启 AI 托管后，说别名优先匹配；正则规则里也可以不用别名。
- **点击设备 ID 一键复制**，粘到意图规则里即可。
- **HA 实体 ID 变化**：HA 重新配对 / 设备迁移时，实体 ID 可能变成新的（`light.abc` → `light.xyz`），
  旧别名就成了孤儿。设备管理会把它们标成「已失效」并**单独计数**，点「清理」即可删除；
  「已配别名」只统计 HA 里真实存在的设备，不会再把 1 个别名算成 2 个。

### 5. 定时任务

对小爱说「每天七点半打开热水器」这类话就能创建定时，详见下一节。

---

## 定时任务（语音定时）

带时间的指令会在 **HA 里创建一条自动化**（实体名形如 `automation.xiaoai_bridge_1750000000`），
在「定时任务」页可以看到状态、上次触发时间，也可以删除。

| 说法示例 | 行为 |
|---|---|
| `每天七点半打开热水器` | 每天 07:30 打开热水器 |
| `工作日早上八点打开客厅灯` | 周一～周五 08:00 开灯 |
| `周末晚上十点关闭客厅空调` | 周六、周日 22:00 关空调 |
| `每周一三五晚上十点关闭客厅空调` | 周一/三/五 22:00 关空调 |
| `三分钟后关闭餐厅空调` | 桥接器内存倒计时，3 分钟后关（**重启会丢**） |

要点：

- 需要先在「高级设置」开启 **AI 托管** 并填好 API Key —— 时间解析走 AI，正则规则不参与。
- 时间支持口语：`七点半`、`19:00`、`晚上十点`、`下午三点一刻`、`中午十二点`。
- 「每天 / 工作日 / 周末 / 星期几」的定时会落到 HA 自动化里，重启桥接器不会丢，HA 那边也能看到。
- 「N 分钟后 …」是延迟执行，跑在桥接器进程内存里，**重启桥接器就没了**；重要的定时请用「每天几点」。
- 定时任务页只显示桥接器创建的自动化；删除时会从 HA 配置里移除并重载，不影响你手写的自动化。

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

# 风扇摇头
- pattern: "(打开|开)摇头"
  action:
    domain: fan
    service: oscillate
    entity_id: fan.living_room
    oscillating: true
    reply: "好的，风扇开始摇头"

# 风扇风量
- pattern: "风扇风量调到{num}[%％]"
  action:
    domain: fan
    service: set_percentage
    entity_id: fan.living_room
    percentage: "{0}"
    reply: "好的，风量已调到 {0}%"

# 窗帘开合度
- pattern: "把窗帘开到{num}[%％]"
  action:
    domain: cover
    service: set_cover_position
    entity_id: cover.living_room
    position: "{0}"
    reply: "窗帘已开到 {0}%"

# 空调扫风
- pattern: "空调(打开|开)扫风"
  action:
    domain: climate
    service: set_swing_mode
    entity_id: climate.living_room_ac
    swing_mode: "on"
    reply: "好的，空调开始扫风"
```

---

## 邮件通知（小米账号掉线提醒）

小米账号的 passToken 失效后，桥接器就收不到语音记录了（小爱会退回原生回答）。
开启邮件提醒后，桥接器检测到登录态失效会自动发一封信，免得你几天后才发现音箱「不听话了」。

配置路径：**高级设置 → 邮件通知 · 小米账号掉线提醒**

| 字段 | 说明 |
|---|---|
| SMTP 服务器 / 端口 | 如 `smtp.qq.com:465`、`smtp.163.com:465` |
| 加密方式 | SSL（465）/ STARTTLS（587）/ 不加密（25） |
| 账号 / 密码 | 邮箱账号与**授权码**（不是登录密码） |
| 发件人 | 留空时等于账号 |
| 收件人 | 多个用逗号分隔 |
| 告警冷却 | 同一类告警的最短发信间隔，默认 30 分钟 |

填完可以点「发送测试邮件」先验证；配置随「保存」一并生效。
触发时机：启动时没有可用凭据、三种登录方式都失败、运行中 Token 过期且自动重新登录失败。

---

## 面板访问保护

**高级设置 → 面板访问保护** 里可以给配置面板加一层 HTTP Basic 认证，默认关闭。
开启后访问 `http://<服务器IP>:47521` 会弹出浏览器登录框，需要输入设置的用户名 / 密码。

- 必须设置密码才能启用（避免把自己锁在外面）。
- 忘记密码：编辑服务器上的 `config/config.yaml`，把 `web_auth.enabled` 改为 `false` 后重启容器。
- Basic 认证口令是明文传输，公网访问请务必套 HTTPS 或反向代理（Nginx / Caddy）。
- 它只保护面板本身，不影响 HA / 小米的接口调用。

---

## 常见问题

**Q：设备管理里能看到风扇/按键，但意图规则里选不到？**
已修复：规则页的设备类型现在与设备管理使用同一份清单（风扇、按键、窗帘、门锁、扫地机、
加湿器、热水器、水阀、警报器、选择器、数值等），并且补上了风量、摇头、扫风、开合度等参数。

**Q：规则页的测试 / 保存 / 新增 / 导入导出突然都用不了？**
已修复：旧版收集规则时用的是全局 `.rule-item` 选择器，访问过设备管理或定时任务后会把那些卡片
也当成规则来读，直接抛异常。现在只收集规则页自己的卡片。

**Q：设备改了名/实体 ID 变了，别名统计对不上？**
到设备管理页点「清理」删除失效别名；「已配别名」只统计当前 HA 里存在的设备。

**Q：说了定时的话但定时任务页是空的？**
① 确认「高级设置 → AI 托管」已开启并填了 API Key；② 该页只显示 `automation.xiaoai_bridge_*`；
③ 「N 分钟后」属于延迟任务，不落 HA，所以这里看不到。

**Q：面板启用访问保护后打不开了？**
见上一节「面板访问保护」的忘记密码处理。

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
