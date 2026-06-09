# ShellCrash 飞书桥接机器人（绿联 NAS Docker 版）

作用：在飞书里发送 `/status`、`/restart` 等命令，通过绿联 NAS SSH 到小米路由器 `192.168.31.1` 控制 ShellCrash。

## 一、飞书后台需要完成的配置

你已经基本完成：

1. 应用能力：启用「机器人」
2. 权限管理：开通
   - `im:message.group_at_msg:readonly` 获取群组中用户 @ 机器人消息
   - `im:message.p2p_msg:readonly` 读取用户发给机器人的单聊消息
   - `im:message:send_as_bot` 以应用身份发消息
3. 事件与回调：
   - 订阅方式：使用「长连接」接收事件
   - 已添加事件：`im.message.receive_v1` 接收消息
4. 版本管理与发布：创建版本并发布

> 注意：页面提示“应用发布后，当前配置方可生效”，所以需要发布一次。

## 二、准备绿联 NAS 目录

在绿联 NAS 文件管理器里创建目录，例如：

```text
/docker/shellcrash-feishu-bot
```

把本项目所有文件放进去：

```text
bot.py
Dockerfile
docker-compose.yml
requirements.txt
.env.example
README.md
```

然后复制 `.env.example` 为 `.env`。

## 三、填写 `.env`

进入飞书开放平台：

```text
凭证与基础信息
```

复制：

```text
App ID
App Secret
```

填入 `.env`：

```env
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

ROUTER_HOST=192.168.31.1
ROUTER_PORT=22
ROUTER_USER=root
ROUTER_PASSWORD=你的路由器密码

ALLOW_ALL_USERS=true
ALLOWED_OPEN_IDS=
ALLOWED_CHAT_IDS=
```

首次测试可以先用 `ALLOW_ALL_USERS=true`。测试成功后建议按「安全加固」改成白名单。

## 四、在绿联 NAS 上启动

### 方式 A：绿联 Docker / Compose 图形界面

1. 打开绿联 NAS 管理后台
2. 打开 Docker / 容器 / Compose 项目
3. 新建项目
4. 选择本目录里的 `docker-compose.yml`
5. 启动项目
6. 查看日志，看到类似下面内容表示启动成功：

```text
启动飞书长连接客户端... app_id=cli_xxx router=192.168.31.1
```

### 方式 B：SSH 到 NAS 后启动

如果你会 SSH 到 NAS：

```sh
cd /docker/shellcrash-feishu-bot
docker compose up -d --build
docker compose logs -f
```

## 五、回飞书后台验证

容器启动后，回到飞书开放平台：

```text
事件与回调 → 重新验证
```

如果容器已经连上飞书，验证会通过。

## 六、使用方式

把机器人拉进一个只有你自己的群，或者直接私聊机器人。

群里使用时建议 @ 机器人：

```text
@ShellClash桥接飞书 /help
@ShellClash桥接飞书 /status
@ShellClash桥接飞书 /restart
@ShellClash桥接飞书 /log
```

私聊机器人可以直接发：

```text
/help
/status
/restart
/log
```

支持命令：

```text
/help    查看帮助
/whoami  查看你的 sender_id 和 chat_id，用于配置白名单
/status  查看 ShellCrash 状态
/ports   查看代理端口
/log     查看最近日志
/start   启动 ShellCrash
/stop    停止 ShellCrash
/restart 重启 ShellCrash
/update  暂未启用，仅提示
```

## 七、安全加固：强烈建议做

### 1. 设置用户白名单

首次运行后，在飞书给机器人发：

```text
/whoami
```

它会回复：

```text
sender_id(open_id)：ou_xxx
chat_id：oc_xxx
```

然后修改 `.env`：

```env
ALLOW_ALL_USERS=false
ALLOWED_OPEN_IDS=ou_xxx
```

如果只想允许某个群使用，也可以设置：

```env
ALLOWED_CHAT_IDS=oc_xxx
```

改完后重启容器：

```sh
docker compose restart
```

### 2. 改用 SSH 密钥，避免保存路由器密码

在你的电脑或 NAS 上生成密钥：

```sh
ssh-keygen -t ed25519 -f shellcrash_bot_key -N ""
```

把公钥内容追加到路由器：

```sh
cat shellcrash_bot_key.pub
```

复制输出内容，登录路由器后写入：

```sh
mkdir -p /etc/dropbear
vi /etc/dropbear/authorized_keys
```

把私钥 `shellcrash_bot_key` 放到项目目录：

```text
keys/shellcrash_bot_key
```

修改 `.env`：

```env
# ROUTER_PASSWORD=可以删除或留空
SSH_KEY_PATH=/app/keys/shellcrash_bot_key
```

重启容器。

### 3. 关闭 Telnet

你的路由器之前检测到 `23/telnet` 开着。建议后续关闭 Telnet，并改强密码。

## 八、常见问题

### 1. 飞书后台“重新验证”失败

说明 NAS 上的容器没有成功连上飞书。检查：

```sh
docker compose logs -f
```

重点看：

- `FEISHU_APP_ID` 是否正确
- `FEISHU_APP_SECRET` 是否正确
- 飞书应用是否已发布
- NAS 是否能访问外网

### 2. 机器人不回复

检查：

- 是否把机器人拉进群
- 群里是否 @ 机器人
- 是否已开通 `im:message:send_as_bot`
- 是否发布了应用版本
- 容器日志有没有报错

### 3. 提示 SSH 失败

检查：

- NAS 能否访问 `192.168.31.1`
- 路由器 22 端口是否开启
- `.env` 里的路由器密码是否正确
- 如果用密钥，`SSH_KEY_PATH` 是否正确

## 九、设计原则

为了安全，机器人不会执行你在飞书里输入的任意 shell 命令，只支持 `bot.py` 里写死的白名单命令。

## 十、按钮控制面板

新版支持发送：

```text
/menu
```

机器人会回复一张「ShellCrash 控制面板」卡片，包含：

- 查看状态
- 查看端口
- 查看日志
- 重启服务

按钮功能需要在飞书后台额外配置：

```text
事件与回调 → 回调配置 → 使用长连接接收回调 → 添加回调 → 卡片回传交互 card.action.trigger
```

配置后重新部署容器即可。

## 十、按钮控制面板

新版支持发送：

```text
/menu
```

机器人会回复一张「ShellCrash 控制面板」卡片，包含：查看状态、查看端口、查看日志、重启服务。

按钮功能需要在飞书后台额外配置：

```text
事件与回调 → 回调配置 → 使用长连接接收回调 → 添加回调 → 卡片回传交互 card.action.trigger
```

配置后重新部署容器即可。
