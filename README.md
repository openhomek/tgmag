# tgmag

面向自有或已授权 Telegram 账号的多账号管理 Bot，基于 **aiogram、Telethon、PostgreSQL 和 aiohttp**。

> [!IMPORTANT]
> 本项目仅适用于你本人拥有或已获得明确授权的 Telegram 账号。生产环境推荐使用 **Debian 12 + PostgreSQL + systemd** 部署。

完整的首次生产部署（包括创建独立用户、PostgreSQL 数据库、目录权限和 HTTPS）请按 [DEPLOY.md](DEPLOY.md) 操作；下面的快速开始主要用于本地或前台验证。

## 目录

- [运行要求](#运行要求)
- [快速开始](#快速开始)
- [环境变量](#环境变量)
- [生产环境稳定运行](#生产环境稳定运行)
- [VPS 重启后自动启动](#vps-重启后自动启动)
- [更新代码并快速重启](#更新代码并快速重启)
- [常用运维命令](#常用运维命令)
- [常见问题](#常见问题)

## 运行要求

- Debian 12（推荐）
- Python 3.11+
- PostgreSQL 14+
- Telegram Bot Token
- Telegram API ID 与 API Hash
- 公网 HTTPS 域名（仅 Mini App 需要）
- Gmail 应用专用密码和 catch-all 域名（仅登录邮箱保护需要）

## 快速开始

### 1. 克隆仓库并安装依赖

```bash
git clone https://github.com/openhomek/tgmag.git
cd tgmag
./ops/install_debian12.sh
cp .env.example .env
```

安装脚本会安装所需系统依赖、创建 `.venv` 虚拟环境，并安装 Python 依赖。

### 2. 配置环境变量

编辑 `.env`：

```bash
nano .env
```

至少填写以下必需配置：

```env
BOT_TOKEN=你的_Telegram_Bot_Token
TG_API_ID=你的_Telegram_API_ID
TG_API_HASH=你的_Telegram_API_Hash
ADMIN_IDS=你的_Telegram_用户_ID
DATABASE_URL=postgresql+asyncpg://数据库用户:数据库密码@127.0.0.1:5432/数据库名
FERNET_KEY=你的_Fernet_密钥
```

### 3. 生成 Fernet 密钥

```bash
.venv/bin/python -c \
'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

把完整输出写入 `.env` 的 `FERNET_KEY`。

> [!WARNING]
> Bot 已保存数据后不要直接更换 `FERNET_KEY`，否则已有手机号、Session、2FA 和登录邮箱等加密数据可能无法解密。迁移服务器时必须安全迁移原密钥。

### 4. 初始化数据库

```bash
. .venv/bin/activate
alembic upgrade head
```

### 5. 前台测试运行

```bash
python -m app.main
```

确认 Bot 能正常启动并开始 polling 后，按 `Ctrl+C` 停止，再按下文配置 systemd。

## 环境变量

### 必需配置

| 环境变量 | 必需 | 说明 |
|---|:---:|---|
| `BOT_TOKEN` | 是 | 从 `@BotFather` 获取的 Telegram Bot Token。 |
| `ADMIN_IDS` | 是 | Bot 管理员的 Telegram 数值用户 ID；多个 ID 使用英文逗号分隔。 |
| `DATABASE_URL` | 是 | PostgreSQL 的 SQLAlchemy asyncpg 连接串。 |
| `FERNET_KEY` | 是 | 用于加密手机号、Session、2FA 和登录邮箱等敏感数据的主密钥。 |

### 登录邮箱保护

登录邮箱保护默认开启。启用时需要配置以下变量：

| 环境变量 | 必需条件 | 说明 |
|---|:---:|---|
| `LOGIN_EMAIL_PROTECTION_ENABLED` | 否 | 是否启用自动登录邮箱保护，默认 `true`；不使用时设为 `false`。 |
| `LOGIN_EMAIL_ALIAS_DOMAINS` | 启用邮箱保护时 | catch-all 域名列表；多个域名使用英文逗号分隔，第一个为初始默认域名。 |
| `LOGIN_EMAIL_GMAIL_USERNAME` | 启用邮箱保护时 | 接收 catch-all 转发邮件的 Gmail 邮箱地址。 |
| `LOGIN_EMAIL_GMAIL_APP_PASSWORD` | 启用邮箱保护时 | Gmail 应用专用密码，不是 Google 账号的普通登录密码。 |
| `LOGIN_EMAIL_POLL_TIMEOUT_SECONDS` | 否 | 等待 catch-all 转发验证码的时间，默认 `300` 秒（5 分钟）；等待期间不会重复请求验证码。 |

示例：

```env
LOGIN_EMAIL_PROTECTION_ENABLED=true
LOGIN_EMAIL_ALIAS_DOMAINS=mail-a.example.com,mail-b.example.net
LOGIN_EMAIL_GMAIL_USERNAME=your-account@gmail.com
LOGIN_EMAIL_GMAIL_APP_PASSWORD=replace_with_google_app_password
LOGIN_EMAIL_POLL_TIMEOUT_SECONDS=300
```

每个 TG 账号都可以在 Mini App 的“登录邮箱保护”中单独填写等待时长，单位为整数小时，允许 `0–720`。默认值为 `0`，即收到有效的 777000 登录提醒后立即换绑；大于 `0` 时，在固定窗口内只转发并累计提醒，到期换绑一次。修改只影响之后的新窗口，不改变已经开始的窗口。

不使用登录邮箱保护时：

```env
LOGIN_EMAIL_PROTECTION_ENABLED=false
```

### Mini App

| 环境变量 | 必需条件 | 说明 |
|---|:---:|---|
| `MINI_APP_ENABLED` | 否 | 是否启用 Mini App，默认 `false`。 |
| `MINI_APP_PUBLIC_URL` | 启用 Mini App 时 | Telegram 客户端可访问的完整 HTTPS `/mini-app` 地址。 |
| `MINI_APP_HOST` | 否 | Mini App 监听地址，推荐 `127.0.0.1`。 |
| `MINI_APP_PORT` | 否 | Mini App 监听端口，默认 `8080`。 |

> [!CAUTION]
> `.env`、Bot Token、API Hash、数据库密码、Fernet 密钥、Telegram Session 和 Gmail 应用专用密码都属于敏感信息，不要提交到 GitHub。

## 生产环境稳定运行

仓库内已经提供 systemd 服务文件：

```text
ops/systemd/tg-account-bot.service
```

默认配置使用：

| 项目 | 默认值 |
|---|---|
| 部署目录 | `/opt/tg-account-bot` |
| 服务用户 | `tg-account-bot` |
| systemd 服务名 | `tg-account-bot` |
| 环境变量文件 | `/opt/tg-account-bot/.env` |
| 启动命令 | `/opt/tg-account-bot/.venv/bin/python -m app.main` |

> [!IMPORTANT]
> 下方 systemd 命令不是“快速开始”的直接下一步。执行前必须先完成 [DEPLOY.md 第 2～6 节](DEPLOY.md#2-安装系统依赖和代码)：创建 `tg-account-bot` 用户，将代码部署到 `/opt/tg-account-bot`，配置权限和 `.env`，安装依赖并执行数据库迁移。否则 `/opt/tg-account-bot` 或服务用户不存在，命令会失败。

安装 systemd 服务：

```bash
cd /opt/tg-account-bot
sudo install -m 644 ops/systemd/tg-account-bot.service \
  /etc/systemd/system/tg-account-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-account-bot
```

`enable --now` 会同时完成两件事：

1. 立即启动 Bot；
2. 设置 VPS 重启后自动启动 Bot。

服务文件还配置了 `Restart=always`，Bot 进程异常退出后，systemd 会自动尝试重新启动。

## VPS 重启后自动启动

只需执行一次：

```bash
sudo systemctl enable --now tg-account-bot
```

确认开机自启已经启用：

```bash
sudo systemctl is-enabled tg-account-bot
```

正常应输出：`enabled`

确认当前服务正在运行：

```bash
sudo systemctl is-active tg-account-bot
```

正常应输出：`active`

查看完整状态：

```bash
sudo systemctl status tg-account-bot --no-pager
```

建议配置完成后主动重启一次 VPS 进行验证：

```bash
sudo reboot
```

重新连接服务器后检查：

```bash
sudo systemctl status tg-account-bot --no-pager
```

## 更新代码并快速重启

### 推荐：完整安全更新

代码、依赖或数据库迁移可能发生变化时，使用下面这组命令：

```bash
cd /opt/tg-account-bot && \
sudo -u tg-account-bot git pull --ff-only && \
sudo -u tg-account-bot .venv/bin/python -m pip install -r requirements.txt && \
sudo -u tg-account-bot .venv/bin/alembic upgrade head && \
sudo systemctl restart tg-account-bot && \
sudo systemctl status tg-account-bot --no-pager
```

这组命令会依次完成：

1. 拉取 GitHub 最新代码；
2. 同步 Python 依赖；
3. 执行数据库迁移；
4. 重启 Bot；
5. 检查服务状态。

### 快速：仅更新普通代码

确认本次更新没有修改 `requirements.txt`，也没有新增数据库迁移时，可以使用：

```bash
cd /opt/tg-account-bot && \
sudo -u tg-account-bot git pull --ff-only && \
sudo systemctl restart tg-account-bot && \
sudo systemctl status tg-account-bot --no-pager
```

### 只修改了 `.env`

保存 `.env` 后重启服务即可：

```bash
sudo systemctl restart tg-account-bot
sudo systemctl status tg-account-bot --no-pager
```

### 实时查看启动日志

```bash
sudo journalctl -u tg-account-bot -f
```

按 `Ctrl+C` 退出日志查看，不会停止 Bot。

> [!NOTE]
> `git pull --ff-only` 会在服务器代码存在冲突或本地提交时停止，而不会自动覆盖修改，适合生产服务器使用。生产环境不建议直接手工修改仓库内的代码。

## 常用运维命令

| 操作 | 命令 |
|---|---|
| 启动 Bot | `sudo systemctl start tg-account-bot` |
| 停止 Bot | `sudo systemctl stop tg-account-bot` |
| 重启 Bot | `sudo systemctl restart tg-account-bot` |
| 查看状态 | `sudo systemctl status tg-account-bot --no-pager` |
| 查看最近 100 行日志 | `sudo journalctl -u tg-account-bot -n 100 --no-pager` |
| 实时查看日志 | `sudo journalctl -u tg-account-bot -f` |
| 启用开机自启 | `sudo systemctl enable tg-account-bot` |
| 关闭开机自启 | `sudo systemctl disable tg-account-bot` |
| 查看是否开机自启 | `sudo systemctl is-enabled tg-account-bot` |
| 查看当前是否运行 | `sudo systemctl is-active tg-account-bot` |

## 常见问题

### 手机号验证码收不到

`/login` 调用 Telegram MTProto 登录接口，验证码的实际送达方式由 Telegram 决定。Bot 会明确显示 Telegram 返回的方式，例如 App 内验证码、短信、电话或邮箱；“验证码已发送”只表示 Telegram 已接受请求，不表示运营商或目标客户端已经完成送达。

请先按界面显示的“送达方式”检查对应位置，并避免短时间连续请求。Mini App 会在 Telegram 返回的等待期内复用当前请求，不会因为重复点击而再次发码。若目标账号仍有一台已登录设备，可以使用 Mini App“操作 → 二维码登录”，也可以使用 Bot 主菜单的“扫码登录”、手机号输入页的“改用二维码登录”，或 `/qr_login`：

1. 在 Bot 中生成一次性二维码；
2. 在已登录目标账号的另一台 Telegram 客户端中进入“设置 → 设备 → 连接桌面设备”；
3. 扫描二维码并确认；
4. 若账号启用了 2FA，再按 Bot 提示输入 2FA 密码。

二维码是一次性登录凭证，不要转发或截图给他人。Mini App 会在页面保持打开时自动刷新过期二维码；Bot 对话中的二维码过期后可重新点击生成。取消流程或服务重启后都需要重新生成。扫码设备和显示二维码的设备必须是两台不同设备，或至少能让已登录客户端调用摄像头扫描另一块屏幕。

### 服务启动后立即退出

查看详细日志：

```bash
sudo journalctl -u tg-account-bot -n 200 --no-pager
```

常见原因：

- `.env` 缺少必需变量或变量格式错误；
- PostgreSQL 没有启动或 `DATABASE_URL` 不正确；
- 尚未执行数据库迁移；
- 项目目录、`.env` 或 `data/` 权限不正确；
- 同一个 Bot Token 正在被另一个实例使用，产生 polling 冲突。

### 数据库迁移未完成

```bash
cd /opt/tg-account-bot
sudo -u tg-account-bot .venv/bin/alembic upgrade head
sudo systemctl restart tg-account-bot
```

### 拉取代码时提示本地修改冲突

先查看服务器上被修改的文件：

```bash
cd /opt/tg-account-bot
git status
```

不要直接执行 `git reset --hard`，除非你确认服务器上的本地修改全部可以丢弃。

### 修改了 systemd 服务文件但没有生效

重新安装服务文件并加载配置：

```bash
cd /opt/tg-account-bot
sudo install -m 644 ops/systemd/tg-account-bot.service \
  /etc/systemd/system/tg-account-bot.service
sudo systemctl daemon-reload
sudo systemctl restart tg-account-bot
```

## License

[MIT](LICENSE)
