<div style="display: flex; justify-content: center;">
  <img src="images/logo.png" alt="ns-signer logo">
</div>

<div style="display: flex; justify-content: center;">
  <a href="https://github.com/DotRacel/ns-signer/actions/workflows/docker-image.yml">
    <img src="https://github.com/DotRacel/ns-signer/actions/workflows/docker-image.yml/badge.svg" alt="Docker Image">
  </a>
</div>

<h1 align="center">Nodeseek 每日自动签到工具</h1>

## 功能

- 支持定时每日签到。
- 支持固定 5 鸡腿或随机鸡腿模式。
- 支持签到前随机延迟。
- 自动处理 Cloudflare Turnstile 验证。
- 登录态提前续期：登录放在白天的空闲时段，而不是挤在午夜签到高峰。
- 签到失败会在当天持续重试，而不是直接放弃这一天。

由于官方 TG Bot 会在签到成功后自动提醒，所以暂时没打算做提醒模块。

## 部署方式

目前我们支持了 Docker Compose 部署.

### Docker Compose

创建 `compose.yaml`：

```yaml
services:
  ns-signer:
    image: ghcr.io/dotracel/ns-signer:latest
    container_name: ns-signer
    restart: unless-stopped
    environment:
      NODESEEK_USERNAME: your_username
      NODESEEK_PASSWORD: your_password
      ATTENDANCE_TIME: "09:00"
      RANDOM_DELAY_MAX: "0"
      ATTENDANCE_MODE: fixed
      SESSION_CHECK_TIME: "13:20"
      TZ: Asia/Shanghai
    volumes:
      - ./data:/data
```

需要修改的配置：

- `NODESEEK_USERNAME`：Nodeseek 用户名或邮箱。
- `NODESEEK_PASSWORD`：Nodeseek 密码。
- `ATTENDANCE_TIME`：每日签到时间，24 小时制 `HH:MM`。
- `RANDOM_DELAY_MAX`：签到前最大随机延迟秒数，`0` 表示禁用。
- `ATTENDANCE_MODE`：`fixed` 表示固定 5 鸡腿，`random` 表示随机鸡腿。
- `SESSION_CHECK_TIME`：每日检查登录态的时间，快到期时会提前重新登录。
- `TZ`：容器时区，建议设置为 `Asia/Shanghai`。

可选配置：

- `ATTENDANCE_RETRY_MINUTES` / `ATTENDANCE_RETRY_HOURS`：签到失败后的重试间隔与
  重试窗口，默认每 20 分钟一次、持续 20 小时。
- `SESSION_RENEW_DAYS`：登录态剩余天数低于该值时提前续期，默认 `7`。
- `LOGIN_RETRIES`：单次登录的浏览器重试次数，默认 `15`。Cloudflare Turnstile 大约
  只有一成的页面会真的吐出 token，只有换一个全新浏览器才能重试，所以要留足次数。
- `BROWSER_PROFILE`：签到时是否保留浏览器 profile，默认 `true`。登录不会复用
  profile——登录靠的是不断换新浏览器去碰 Turnstile，复用反而会让每次重试都以同样的
  方式失败。
- 邮箱验证码相关：`EMAIL_ADDRESS`、`IMAP_HOST`、`IMAP_PORT`、`IMAP_USERNAME`、
  `IMAP_PASSWORD`、`IMAP_MAILBOX`、`IMAP_SSL`。见下方「登录是怎么做的」。

然后在该目录下输入 `docker compose up -d`

## 常用操作

手动执行一次签到：

```bash
docker compose run --rm ns-signer python -m ns_signer.main --once
```

强制重新登录并刷新登录态：

```bash
docker compose run --rm ns-signer python -m ns_signer.main --login
```

查看当前保存的登录态还能用多久：

```bash
docker compose run --rm ns-signer python -m ns_signer.main --status
```

注意：`--login` 是强制刷新登录态，不会复用已有会话；`--renew` 则只在快到期时才
重新登录。Docker Compose 部署时登录态保存在宿主机的 `./data/session.json`；本地
直接运行 Python 时，默认保存在项目根目录的 `session.json`。

从浏览器导入一份现成的登录态：

```bash
docker compose run --rm -T ns-signer python -m ns_signer.main --import-session - < cookies.json
```

`cookies.json` 可以直接用 Cookie-Editor 之类的扩展在 nodeseek.com 上导出，导入后会
立刻联网验证一次是否真的处于登录状态。

检查现有登录态是否仍然有效：

```bash
docker compose run --rm ns-signer python -m ns_signer.main --check
```

## 登录是怎么做的

Nodeseek 的登录并不是「POST 用户名密码，拿到 cookie」这么简单，它的前端会：

1. 等 Cloudflare Turnstile 产出 token；
2. 等站点反爬脚本发放 `fog` cookie；
3. 连同凭据一起发送设备指纹 `x-integrity-token`（以及已有的 `x-security-token`）；
4. 从响应头里取回刷新后的 `x-security-token` / `x-csrf-token` 并保存。

漏掉第 3 步不会报错——接口照样返回 `200 {"success": true, "redirect":
"/emailSignIn.html"}`，意思是「这台设备不认识，请改用邮箱验证码」，而不是「登录
成功」。所以本工具不会只看 `success`，而是在拿到会话后再访问一个需要登录的接口
做验证，验证通过才会保存。

对于 NodeSeek 从没见过的设备，光有密码是进不去的——它只会发邮箱验证码。因此还有
两条路可以完成首次登录：

1. **配置邮箱**（上面的 `IMAP_*`）。工具会自己请求验证码、从邮箱里读出来、完成
   登录。之后拿到的 `security_token` 会被保存下来，这台设备就被认识了，后续可以
   直接走密码登录。
2. **导入一份浏览器登录态**（`--import-session`）。在自己的浏览器里正常登录一次，
   把 cookie 导出来交给工具即可，不需要提供邮箱密码。

登录态本身有效期约 30 天，而且每次签到后都会把刷新过的 cookie 存回去，所以正常
情况下并不需要频繁登录。
