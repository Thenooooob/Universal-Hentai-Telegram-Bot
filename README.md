# 🚀 Universal Hentai Telegram Bot

一个 Telegram 漫画图库抓取机器人。向 Bot 发送 nhentai / E-Hentai / ExHentai / 禁漫(JMComic) 链接或车号，Bot 会抓取全本图片，默认整合为一个 **Telegraph 即时浏览（Instant View）** 页面一键发送；也可改为原生媒体相册逐批发送。

## ✨ 特性

- **nhentai**：使用官方 API v2 + `curl_cffi` 浏览器 TLS 指纹，自动拉取最新 CDN 节点。
- **E-Hentai / ExHentai**：注入个人 Cookie 访问里站，自动翻页；H@H 节点不可达时用 `nl` 参数回退官方服务器。
- **JMComic**：基于 `jmcomic` 库（APP API），识别 photo（单章）/ album（整本）链接与 `JM123456` 车号，自动完成图片切割解密；下载在独立临时目录进行，发送后自动删除。
- **即时浏览**：图片上传 catbox（填 userhash 为永久）或 litterbox（72h 临时）后生成 Telegraph 页面，超过 100 张自动分页；生成失败自动回退为媒体相册。
- **格式转码**：Pillow 在内存中把 WebP 等格式转为 JPEG。

## 🛠️ 安装

需要 Python 3.10+。

```bash
git clone https://github.com/Thenooooob/Universal-Hentai-Telegram-Bot.git
cd Universal-Hentai-Telegram-Bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## ⚙️ 配置

所有敏感信息都从环境变量或同目录下的 `.env` 读取，**不要写进代码，也不要提交 `.env`**。

```bash
cp .env.example .env
nano .env
```

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | 是 | @BotFather 申请的 Token |
| `EH_IPB_MEMBER_ID` / `EH_IPB_PASS_HASH` | 访问里站时 | 浏览器登录 e-hentai 后从 Cookie 中复制 |
| `EH_IGNEOUS` / `EH_SK` | 访问里站时 | 登录 exhentai 后从 Cookie 中复制 |
| `CATBOX_USERHASH` | 否 | catbox.moe 账号 userhash；留空用 litterbox 临时图床 |

`.env` 示例（占位值）：

```dotenv
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
EH_IPB_MEMBER_ID=YOUR_IPB_MEMBER_ID
EH_IPB_PASS_HASH=YOUR_IPB_PASS_HASH
EH_IGNEOUS=YOUR_IGNEOUS
EH_SK=
CATBOX_USERHASH=
```

投递方式等非敏感选项（`DELIVERY_MODE`、`TELEGRAPH_PAGE_SIZE`、`LITTERBOX_TIME`）在 `main.py` 顶部修改。

## ▶️ 运行

```bash
python main.py
```

也可以用 systemd 常驻（`WorkingDirectory` 指向本目录，`ExecStart` 指向 venv 里的 python）。

## 💬 使用

先发 `/start` 确认在线，然后直接发送：

- nhentai：`https://nhentai.net/g/177013/`
- E-Hentai / ExHentai：`https://exhentai.org/g/<gid>/<token>/`
- JMComic：`https://18comic.vip/album/123456`、`https://18comic.vip/photo/123456` 或 `JM123456`

## 📜 免责声明

本项目仅供学习交流。请遵守当地法律法规与各站点条款，合理控制请求频率，勿用于商业用途。
