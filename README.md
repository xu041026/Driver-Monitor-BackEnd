# 危险驾驶监测平台

一个独立运行的 Django 5.2 后端和中文管理界面，用于接收、保存与回溯驾驶监测终端上报的告警。包含监测总览、告警筛选与详情、受保护的证据图片、设备登记和接入说明。

**初始数据为空，没有预置告警或设备。** 首次启动后创建管理账户，再登记终端。此目录没有修改原 `Driver-Monitoring-System` 源码，也不会打开摄像头或加载任何视觉模型；原核心程序与本接口的实际对接仍需另行完成。

## 1. 在本机启动

需要 Python 3.10 或更高版本。本目录的依赖环境应与 RKNN、OpenCV、PyTorch 等推理环境分开。

### Windows：双击启动

双击 `start-windows.bat`。脚本会在本目录建立 `.venv`、安装固定版本依赖、初始化数据库并打开：

```text
http://127.0.0.1:8000/
```

首次安装需要能够访问 Python 包源。窗口中出现错误时保留错误内容以便排查；关闭服务可在窗口按 `Ctrl+C`。

### 手动启动

在本目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe start.py
```

Linux 使用：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python start.py --no-browser
```

`start.py` 自动执行数据库迁移；默认只监听本机地址。端口被占用时，可以指定 `--port 8001`。调试模式用于本地开发，不作为对外部署方案。

## 2. 首次设置与设备登记

1. 本地调试模式（`DJANGO_DEBUG=1`）第一次在服务器本机访问网页，会进入 `/setup/`，填写用户名和密码以创建管理账户。创建完成后自动进入工作台。该初始化页面同时要求调试模式和本机访问，已有用户后不会再次创建初始账户；生产模式（`DJANGO_DEBUG=0`）关闭网页初始化，应使用 `python manage.py createsuperuser`。
2. 进入“设备管理 → 接入新设备”，填写设备标识、设备名称和可选车牌。例如设备标识可使用 `lubancat-01`。
3. 保存页面显示的设备密钥。**密钥只显示一次，数据库保存其摘要，无法从列表中找回明文。** 将设备标识和密钥配置到终端程序。
4. 用真实事件或下方示例完成一次上报，再到“告警记录”查看。

管理网页要求启用的管理人员账户；只有系统管理员可以创建或停用设备。设备标识与浏览器登录账号是两套身份。停用设备后，该设备不能继续通过接口上报。列表中“已启用”指接入权限，最近上报时间不等于实时在线状态。

也可使用管理命令创建终端：

```powershell
.\.venv\Scripts\python.exe manage.py create_device lubancat-01 --name "鲁班猫监测终端"
```

该命令也会一次性输出设备密钥。

## 3. 用示例发送一个事件

`examples/send_event.py` 只依赖 Python 标准库，可单独复制到终端；不需要安装 Django、OpenCV 或 RKNN。运行它会向平台**实际新增记录**，并非界面模拟数据。

先将设备密钥写入环境变量。PowerShell 中可使用隐藏输入，避免把密钥字面值写进命令历史：

```powershell
$deviceSecret = Read-Host "设备接入密钥" -AsSecureString
$env:DMS_DEVICE_KEY = [System.Net.NetworkCredential]::new('', $deviceSecret).Password
python examples/send_event.py --base-url http://127.0.0.1:8000 --device-id lubancat-01 --event-type eye_closure --duration-ms 2300 --confidence 0.92
```

Linux / Bash：

```bash
read -rsp '设备接入密钥: ' DMS_DEVICE_KEY
export DMS_DEVICE_KEY
python3 examples/send_event.py --base-url https://your-platform.example --device-id lubancat-01 --event-type eye_closure --duration-ms 2300 --confidence 0.92
unset DMS_DEVICE_KEY
```

默认不发送图片；附图时增加 `--image /path/to/eye.jpg`。还支持 `--severity critical`、`--details-json '{"ear":0.16}'` 和 `--timeout 15`。PowerShell 不同版本对 JSON 引号处理存在差异，可先不传补充信息。

脚本会在发送前打印本次事件 UUID 和发生时间，不打印密钥。连接中断不能证明服务端没有保存：重试时在原命令上同时加上输出的 `--event-id` 与 `--occurred-at`，并保持**全部其他字段及图片字节**不变。例如：

```text
--event-id 14b983d4-0bfa-40ca-94d9-dcfb92cccb9c --occurred-at 2026-09-23T10:30:00+08:00
```

两项均省略会生成一个新事件；只填写其中一项会被示例脚本拒绝。脚本不自动重试，也不自动跟随重定向，以免将设备密钥发往其他地址。

## 4. 接口协议

### 健康检查

```http
GET /api/v1/health/
```

返回 `200 {"status":"ok"}`。这是应用存活检查，不是数据库、设备在线或模型运行状态的全面检查。

### 事件上报

```http
POST /api/v1/events/
Content-Type: application/json
X-Device-ID: lubancat-01
Authorization: Bearer <设备密钥>
```

请求 JSON：

```json
{
  "event_id": "14b983d4-0bfa-40ca-94d9-dcfb92cccb9c",
  "event_type": "eye_closure",
  "occurred_at": "2026-09-23T10:30:00+08:00",
  "severity": "warning",
  "duration_ms": 2300,
  "confidence": 0.92,
  "details": {"ear": 0.16},
  "evidence": null
}
```

| 字段 | 约束 |
| --- | --- |
| `event_id` | 必填，UUID 字符串，每次新事件唯一 |
| `event_type` | 必填，见下方八种行为 |
| `occurred_at` | 必填，带时区的 ISO 8601 / RFC 3339 时间；范围为 `2000-01-01T00:00:00Z` 至服务器当前时间后 24 小时（含边界）；不接受无时区时间 |
| `severity` | 可选，`warning` 或 `critical`；默认 `warning` |
| `duration_ms` | 可选，`null` 或 0–2147483647 的整数，单位毫秒 |
| `confidence` | 可选，`null` 或 0–1 的有限数值 |
| `details` | 可选，JSON 对象，默认 `{}`；UTF-8 编码后最多 16 KiB，嵌套不超过 8 层 |
| `evidence` | 可选，`null` 或仅包含 `content_type`、`data_base64` 的对象 |

支持的行为：

| 标识 | 含义 |
| --- | --- |
| `eye_closure` | 持续闭眼 |
| `yawn` | 打哈欠 |
| `head_down` | 低头 |
| `look_away` | 视线偏离 |
| `phone_use` | 使用手机 |
| `smoking` | 吸烟 |
| `drinking` | 喝水 |
| `hand_anomaly` | 手部异常 |

`hand_anomaly` 不等于已经确认双手离开方向盘；行为名称需与终端实际算法能力一致。

图片对象格式：

```json
{
  "content_type": "image/jpeg",
  "data_base64": "<JPEG或PNG文件内容的Base64编码>"
}
```

支持 `image/jpeg`、`image/png`。不带 `data:image/...;base64,` 前缀。图片文件最多 4 MiB、宽高各不超过 4096、总像素不超过 1600 万；完整 JSON 请求最多 6 MiB。PNG 需要为非交错格式。未知字段、重复 JSON 键、NaN/Infinity 都会被拒绝。设备身份仅来自请求头，不接受请求体中的设备标识。

PNG 校验包含结构、CRC、解压长度与扫描行标记；JPEG 校验包含标记结构和尺寸，**不进行完整像素解码**。如果将来接收不可信公众图片，应增加成熟解码器的解码、重编码及相应资源限制。

### 返回结果与幂等重试

| HTTP 状态 | 说明 |
| --- | --- |
| `201` | 保存成功，`{"status":"created","event_id":"..."}` |
| `200` | 同一设备、相同事件编号和相同规范化内容的重复请求，`{"status":"duplicate","event_id":"..."}`；不新增记录 |
| `400` | JSON 或字段校验失败，查看 `error.fields`（若提供） |
| `401` | 设备不存在、被停用或密钥不正确 |
| `405` | 请求方法错误 |
| `409` | 事件编号已用于不同设备或不同内容；不能用相同编号修改旧事件 |
| `413` | 请求体超过大小上限 |
| `415` | 未使用 `application/json` |
| `503` | 数据库或存储暂不可用；返回 `Retry-After: 5`，稍后以原编号和完整原内容重试 |

错误响应例如：

```json
{
  "error": {
    "code": "validation_error",
    "message": "请求参数不正确。",
    "fields": {"occurred_at": "必须提供带时区的 ISO 8601 时间。"}
  }
}
```

设备接口使用独立密钥，不依赖浏览器会话或 CSRF。网页创建账户、管理设备和退出登录仍使用 Django 的 CSRF 保护。

## 5. 数据与目录

```text
driver-monitor-backend/
├── config/                    # Django 配置与路由
├── events/                    # 设备、告警、接口、校验和测试
├── templates/                 # 本地管理页面
├── static/monitor/            # 本地样式与原生 JavaScript，无外网 CDN
├── examples/send_event.py     # 终端接入示例，仅标准库
├── deploy/nginx.example.conf  # 待替换域名和证书的 HTTPS 代理示例
├── requirements.txt           # 独立后端依赖
├── start.py                   # 本机启动器
├── start-windows.bat          # Windows 启动入口
├── .env.example               # 可选配置示例
└── var/                       # 启动后生成的运行数据，不随源码预置
    ├── db.sqlite3             # 用户、设备和告警记录
    ├── .django-secret         # 本地调试用自动生成的密钥
    ├── evidence/              # 上传图片（包含事件独立子目录）
    └── static/                # 执行 collectstatic 后的静态文件
```

默认使用 SQLite，适用于本地验证和低并发的小规模单实例部署。证据图片当前使用**本地磁盘存储**，不直接存入 SQLite；数据库事务与磁盘写入不是同一个原子操作。程序会尝试清理常规保存失败后的图片，但断电或进程被强制终止仍可能留下没有对应事件的孤立文件，当前未提供自动核对清理任务。备份时同时保存数据库、证据图片及配置；需要一致性备份时暂停写入或使用 SQLite 备份机制。数据目录可通过 `DMS_DATA_DIR` 修改。

页面按 `Asia/Shanghai` 时区显示。今日告警与趋势按**事件发生时间**统计；设备“最近上报”按服务端接收时间更新。原图通过登录鉴权的视图读取，不提供公开媒体目录。

## 6. 与原核心源码对接

本后端还未接入原 `main.py`、`send_image` 或树莓派/RK3568 的检测循环。后续应新增独立的终端上报适配器，把已有行为结果映射到上述八类事件，且不将逐帧结果全部当作新事件：

1. 在行为开始/确认或结束时确定事件边界，生成并持久化 UUID、真实发生时间及可选时长。
2. 将待发送事件和对应图片放入终端持久队列，让推理循环只负责投递任务。
3. 独立上传任务按接口发送；网络失败时保留完整内容与 UUID 重试，收到 `200/201` 后再确认成功。
4. `400/401/409` 应查明原因，不无限重试；图片与补充字段不能在重试时悄悄变化。

端到端的实际帧率、识别准确率、板端发送队列和网络中断恢复，都不由本后端单独保证。

## 7. 对外部署

本地启动器只监听 `127.0.0.1`。终端在另一台机器时，应通过受控的 HTTPS 反向代理连接后端，不能直接使用终端自己的 `127.0.0.1` 地址。

复制 `.env.example` 为 `.env`，至少配置：

```dotenv
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=<独立生成并妥善保存的随机密钥>
DJANGO_ALLOWED_HOSTS=your-platform.example
DJANGO_CSRF_TRUSTED_ORIGINS=https://your-platform.example
DJANGO_HTTPS=1
DMS_DATA_DIR=/var/lib/driver-monitor-backend
```

不要将实际密钥提交到源码仓库。`DJANGO_SECRET_KEY` 和设备接入密钥分别管理。环境变量优先于 `.env`。

在服务器虚拟环境中初始化：

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py collectstatic --noinput
python manage.py check --deploy
```

然后使用 Waitress 等 WSGI 服务运行 `config.wsgi:application`。例如在可信反向代理后监听本机：

```bash
waitress-serve --listen=127.0.0.1:8000 --trusted-proxy=127.0.0.1 --trusted-proxy-headers=x-forwarded-proto config.wsgi:application
```

`deploy/nginx.example.conf` 提供本机 Nginx → Waitress 的 HTTPS 配置草案。把其中的示例域名、证书路径和静态目录替换为实际值；仓库没有附带证书，示例未经目标服务器部署验证。Nginx 将应用请求转发到 `127.0.0.1:8000`，覆盖 `Host` 与 `X-Forwarded-Proto`，并从 `/var/lib/driver-monitor-backend/static/` 提供 `/static/`。如果采用其他数据目录，需同步修改静态目录。

**不要把 evidence 目录映射为公开静态目录。** 示例配置不提供证据目录，只允许应用经过身份校验后读取图片。Waitress 仅信任来自 `127.0.0.1` 代理的 `X-Forwarded-Proto`，再将其转换为应用感知的请求协议，避免 HTTPS 重定向循环；该端口应仅供本机可信代理访问。不要将可信代理配置改成通配符。若代理在其他主机或容器中，需要重新配置网络和精确的可信代理地址。

生产模式下 `start.py` 改用 Waitress，并使用与上方命令相同的本机代理信任设置，但不自动配置 TLS、反向代理或静态文件服务。部署前还需设置正确的文件权限、进程管理及日志，并在目标服务器执行 `nginx -t` 和实际 HTTPS 登录/上传验证。`check --deploy` 的结果应结合实际代理配置处理。高并发、多实例或长期运营时，建议迁移 PostgreSQL、集中图片存储和日志，并配置备份与监控；当前目录不代表已经完成生产上线。

## 8. 检查与测试

在本目录的虚拟环境中运行：

```bash
python manage.py check
python manage.py test
```

测试使用独立测试数据库；不要通过测试命令向实际监测终端发送事件。接口图片校验不会加载推理模型。
