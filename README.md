🚀 Universal Hentai Telegram Bot
一个全功能、高性能、抗封锁的 Telegram 漫画图库抓取机器人。支持向 Bot 发送漫画链接或车号，Bot 会自动绕过网络防护、抓取全本高清图片、完成解密，并以**原生 Telegram 媒体相册（Media Group）**的形式分批发送给你，支持边下边发与阅后即焚。

✨ 核心特性
Nhentai：通过集成 curl_cffi 模拟真实浏览器底层 TLS 指纹，完美绕过 Cloudflare 5s 盾与人机验证验证码。

E-Hentai / ExHentai：自动注入个人 Cookie 突破里站封锁，支持多页面的自动翻页与源站防盗链图像抓取。

JMComic (禁漫天堂)：

深度集成 jmcomic 库，智能识别 photo（单章）与 album（全本）链接。

自动调用 APP 端隐藏 API 彻底无视 Cloudflare 网页端拦截。

自动执行复杂的**图片像素切割重组解密（Image Scramble）**算法。

通过 CWD 劫持技术将所有解密文件死死限制在隔离区，防止服务器环境污染。

内存级格式洗白：内置 Pillow 转码引擎，在发送前毫秒级将 Telegram 不支持的 WebP 动图/静态图洗白为标准 JPEG，彻底解决"文件不支持"或"图标破碎"问题。

内存友好与防封控机制：

边下边发：每凑齐 10 张图立即推送至 Telegram 客户端并释放内存，几百页的本子也能顺滑浏览，永不 OOM。

阅后即焚：发送完成后自动销毁本地隔离文件夹，绝不占用服务器硬盘空间。

HTML 级防错：消息反馈面板使用 HTML 转义技术，免疫各类奇怪标题引发的 Telegram Markdown 解析崩溃。

🛠️ 安装与部署
1. 环境要求
推荐使用海外 VPS（如 Ubuntu 22.04 / Debian 11 等）

Python 3.9 或更高版本

2. 克隆与安装依赖bash
git clone https://github.com/你的用户名/Universal-Hentai-Bot.git
cd Universal-Hentai-Bot

建议使用虚拟环境
python3 -m venv venv
source venv/bin/activate

安装所需的核心依赖
pip install python-telegram-bot curl_cffi httpx beautifulsoup4 jmcomic Pillow


### 3. 配置密钥参数
使用文本编辑器（如 `nano main.py` 或 VSCode）打开 `main.py` 文件，找到顶部的配置区域，修改为你自己的参数：

```python
# 替换为在 @BotFather 处申请的 Bot Token
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"

# 替换为从浏览器中提取的 ExHentai 登录 Cookie
EH_COOKIES = dict(
    igneous="YOUR_IGNEOUS_COOKIE",
    ipb_member_id="YOUR_IPB_MEMBER_ID",
    ipb_pass_hash="YOUR_IPB_PASS_HASH",
    nw="1" # 强制跳过敏感警告弹窗
)
4. 运行机器人
Bash
# 以后台常驻模式运行
PYTHONIOENCODING=utf-8 nohup python main.py > bot.log 2>&1 &

# 查看运行日志
tail -f bot.log
💬 使用方法
向你的 Telegram 机器人发送 /start 确保其在线。随后只需直接发送链接或代码：

Nhentai:

发送链接：https://nhentai.net/g/177013/

ExHentai / E-Hentai:

发送链接：https://exhentai.org/g/xxxxxx/xxxxxxx/

JMComic:

发送链接：https://18comic.vip/photo/123456

直接发车号：JM123456

机器人将实时回复当前进度（如排队、脱壳、下载进度、转码），并以 10 张为一组不断发送超高清图像集。

📜 免责声明
本程序仅供 Python 爬虫技术、加密解密算法及逆向工程学习交流使用。请合理控制请求频率，严禁用于任何商业用途或进行大批量恶意攻击。
