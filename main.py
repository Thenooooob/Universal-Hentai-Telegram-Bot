import sys
import io
import asyncio
import json
import re
import httpx
import os
import shutil
import jmcomic
import html
from PIL import Image
from curl_cffi.requests import AsyncSession
from telegram import Update, InputMediaPhoto
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# 强制接管 Linux 系统的标准输出编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ====== 加载 .env (与 main.py 同目录, 可选; 已存在的环境变量优先) ======
def _load_dotenv(path):
    if not os.path.isfile(path):
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# ====== 你的专属配置区 (敏感值一律从环境变量 / .env 读取, 见 .env.example) ======
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

EH_COOKIES = dict(
    igneous=os.environ.get("EH_IGNEOUS", ""),
    ipb_member_id=os.environ.get("EH_IPB_MEMBER_ID", ""),
    ipb_pass_hash=os.environ.get("EH_IPB_PASS_HASH", ""),
    sk=os.environ.get("EH_SK", ""),
    nw="1"
)
EH_COOKIES = {k: v for k, v in EH_COOKIES.items() if v}

# 投递模式:
#   "instant_view" -> 整合为 Telegraph 即时浏览页面, 只发一条链接 (推荐, 默认)
#   "media_group"  -> 旧方式, 10 张一组逐批发送图片
#   "both"         -> 即时浏览 + 媒体组都发
DELIVERY_MODE = "instant_view"

# Telegraph 单页最多放多少张图 (超过则自动分页并加“下一页”导航)
TELEGRAPH_PAGE_SIZE = 100
TELEGRAPH_SHORT_NAME = "gallerybot"
TELEGRAPH_AUTHOR = "Gallery Bot"

# 图片托管: Telegraph 自带的 /upload 已被官方废弃, 改用 catbox 系图床。
#   - 填了 CATBOX_USERHASH -> 走 catbox 永久版(文件永不过期, 绑定你的账号, 可在网站管理删除)
#   - 留空 ""             -> 自动回退 litterbox 临时图床(LITTERBOX_TIME 后过期)
# 注意: userhash 是私密凭据, 只写在 .env / 环境变量里, 不要提交。
CATBOX_USERHASH = os.environ.get("CATBOX_USERHASH", "")
CATBOX_API = "https://catbox.moe/user/api.php"

# litterbox 临时图床(仅在未填 userhash 时使用), 取值: "1h" / "12h" / "24h" / "72h"
LITTERBOX_TIME = "72h"
LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ====== Nhentai 配置 ======
# 旧版 /api/gallery/{id} 已被官方废弃并彻底拦在 Cloudflare 后面 (返回 403)。
# 现已迁移到 API v2 (/api/v2/...), 该接口不走 Cloudflare 挑战, curl_cffi 即可直连。
NH_API_BASE = "https://nhentai.net/api/v2"
NH_IMPERSONATE = "chrome131"
# 图片 CDN 服务器, 运行时优先从 /api/v2/config 拉取最新列表, 失败则用此默认值兜底。
NH_DEFAULT_IMAGE_SERVERS = [
    "https://i1.nhentai.net", "https://i2.nhentai.net",
    "https://i3.nhentai.net", "https://i4.nhentai.net",
]
# ========================

jm_download_lock = asyncio.Lock()
_telegraph_token = None
_telegraph_lock = asyncio.Lock()


# 内存中格式转码, 并保证体积在 Telegraph 限制内
def ensure_jpeg(img_bytes, max_bytes=None):
    try:
        img = Image.open(io.BytesIO(img_bytes))
        already_ok = (img.format == 'JPEG' and img.mode == 'RGB')
        if img.mode != 'RGB':
            img = img.convert('RGB')

        if already_ok and (max_bytes is None or len(img_bytes) <= max_bytes):
            return img_bytes

        quality = 90
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=quality)
        data = out.getvalue()

        # 若仍超过上限, 逐步降质 / 缩放
        while max_bytes is not None and len(data) > max_bytes and quality > 35:
            quality -= 15
            out = io.BytesIO()
            img.save(out, format='JPEG', quality=quality)
            data = out.getvalue()

        while max_bytes is not None and len(data) > max_bytes:
            w, h = img.size
            if w < 600 or h < 600:
                break
            img = img.resize((int(w * 0.8), int(h * 0.8)), Image.LANCZOS)
            out = io.BytesIO()
            img.save(out, format='JPEG', quality=quality)
            data = out.getvalue()

        return data
    except Exception:
        return img_bytes


# ====== Telegraph 即时浏览引擎 ======
async def _get_telegraph_token(client: httpx.AsyncClient):
    global _telegraph_token
    async with _telegraph_lock:
        if _telegraph_token:
            return _telegraph_token
        resp = await client.get(
            "https://api.telegra.ph/createAccount",
            params=dict(short_name=TELEGRAPH_SHORT_NAME, author_name=TELEGRAPH_AUTHOR),
        )
        data = resp.json()
        if not data.get("ok"):
            raise Exception(f"Telegraph 账号创建失败: {data.get('error')}")
        _telegraph_token = data["result"]["access_token"]
        return _telegraph_token


async def _upload_image(client: httpx.AsyncClient, img_bytes: bytes):
    """上传单张图片, 返回可热链的直链 URL, 失败返回 None。
    填了 CATBOX_USERHASH 走 catbox 永久版; catbox 失败或未填则回退 litterbox 临时图床。"""
    files = {"fileToUpload": ("image.jpg", img_bytes, "image/jpeg")}

    # 1. catbox 永久版 (需 userhash)
    if CATBOX_USERHASH:
        for attempt in range(3):
            try:
                resp = await client.post(
                    CATBOX_API,
                    data={"reqtype": "fileupload", "userhash": CATBOX_USERHASH},
                    files=files,
                    timeout=60.0,
                )
                body = resp.text.strip()
                if resp.status_code == 200 and body.startswith("http"):
                    return body
                await asyncio.sleep(2 + attempt * 2)
            except Exception:
                await asyncio.sleep(1 + attempt)
        # catbox 始终失败 -> 落到 litterbox 兜底

    # 2. litterbox 临时图床 (兜底 / 未填 userhash)
    for attempt in range(3):
        try:
            resp = await client.post(
                LITTERBOX_API,
                data={"reqtype": "fileupload", "time": LITTERBOX_TIME},
                files=files,
                timeout=60.0,
            )
            body = resp.text.strip()
            if resp.status_code == 200 and body.startswith("http"):
                return body
            await asyncio.sleep(2 + attempt * 2)
        except Exception:
            await asyncio.sleep(1 + attempt)
    return None


async def _create_telegraph_page(client: httpx.AsyncClient, token: str, title: str, nodes: list):
    resp = await client.post(
        "https://api.telegra.ph/createPage",
        data=dict(
            access_token=token,
            title=title[:256] if title else "Gallery",
            author_name=TELEGRAPH_AUTHOR,
            content=json.dumps(nodes, ensure_ascii=False),
            return_content="false",
        ),
        timeout=30.0,
    )
    data = resp.json()
    if not data.get("ok"):
        raise Exception(f"Telegraph 页面创建失败: {data.get('error')}")
    return data["result"]["url"]


async def create_instant_view(title: str, images: list, msg, context, chat_id):
    """把图片字节列表整合成 Telegraph 即时浏览页面, 返回首页 URL。"""
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True,
                                 headers={"User-Agent": BROWSER_UA}) as client:
        token = await _get_telegraph_token(client)

        # 1. 逐张上传到图床, 拿到直链
        srcs = list()
        total = len(images)
        for idx, img_bytes in enumerate(images):
            safe_bytes = ensure_jpeg(img_bytes)
            src = await _upload_image(client, safe_bytes)
            if src:
                srcs.append(src)
            if (idx + 1) % 5 == 0 or idx == total - 1:
                try:
                    await msg.edit_text(f"📰 [即时浏览] 正在上传整合... 进度: {idx + 1}/{total}")
                except Exception:
                    pass
            await asyncio.sleep(0.3)  # 轻微节流, 规避图床限流

        if not srcs:
            raise Exception("所有图片上传图床均失败 (litterbox 不可达或被限流)。")

        # 2. 分页 (图片过多时拆成多页, 逆序创建以便正向串联导航链接)
        chunks = [srcs[i:i + TELEGRAPH_PAGE_SIZE] for i in range(0, len(srcs), TELEGRAPH_PAGE_SIZE)]
        total_pages = len(chunks)
        next_url = None
        first_url = None

        for page_index in range(total_pages - 1, -1, -1):
            chunk = chunks[page_index]
            page_title = title if total_pages == 1 else f"{title} ({page_index + 1}/{total_pages})"

            nodes = list()
            for src in chunk:
                nodes.append({"tag": "figure", "children": [{"tag": "img", "attrs": {"src": src}}]})
            if next_url:
                nodes.append({
                    "tag": "p",
                    "children": [{"tag": "a", "attrs": {"href": next_url}, "children": ["➡️ 下一页"]}],
                })

            page_url = await _create_telegraph_page(client, token, page_title, nodes)
            next_url = page_url
            first_url = page_url

        return first_url


# ====== 统一投递: 即时浏览 / 媒体组 ======
async def _send_media_group(images: list, title: str, msg, context, chat_id, tag="图集"):
    media_group = list()
    for idx, img_bytes in enumerate(images):
        safe_bytes = ensure_jpeg(img_bytes)
        caption = title[:1000] if idx == 0 else None
        media_group.append(InputMediaPhoto(media=safe_bytes, caption=caption))

        if len(media_group) == 10 or idx == len(images) - 1:
            if media_group:
                try:
                    await context.bot.send_media_group(chat_id=chat_id, media=media_group)
                except Exception as e:
                    print(f"发送图集出错: {e}")
                media_group.clear()
                try:
                    await msg.edit_text(f"📦 [{tag}] 正在火速传输中... 进度: {idx + 1}/{len(images)}")
                except Exception:
                    pass
                await asyncio.sleep(2.5)


async def deliver_gallery(title: str, images: list, msg, context, chat_id, tag="图集"):
    """根据 DELIVERY_MODE 投递图片。返回投递摘要文本 (用于最终提示)。"""
    if not images:
        raise Exception("没有可投递的图片。")

    iv_url = None
    if DELIVERY_MODE in ("instant_view", "both"):
        try:
            iv_url = await create_instant_view(title, images, msg, context, chat_id)
        except Exception as e:
            print(f"即时浏览生成失败, 回退媒体组: {e}")
            if DELIVERY_MODE == "instant_view":
                # 纯即时浏览模式下失败 -> 自动回退媒体组, 保证有结果
                try:
                    await msg.edit_text(f"⚠️ 即时浏览生成失败({html.escape(str(e))})，已回退为直接发图...")
                except Exception:
                    pass
                await asyncio.sleep(1)
                await _send_media_group(images, title, msg, context, chat_id, tag)
                return None

    if DELIVERY_MODE in ("media_group", "both"):
        await _send_media_group(images, title, msg, context, chat_id, tag)

    return iv_url


# 1. Nhentai 全本提取引擎 (已迁移到官方 API v2)
async def fetch_nhentai(gallery_id: str, msg: Update.message, context, chat_id):
    await msg.edit_text("🔍 [Nhentai] 正在调用 API v2 获取画廊信息...")

    async with AsyncSession(impersonate=NH_IMPERSONATE) as session:
        resp = await session.get(f"{NH_API_BASE}/galleries/{gallery_id}", timeout=30)
        if resp.status_code == 404:
            raise Exception(f"画廊 {gallery_id} 不存在或已被删除。")
        if resp.status_code == 429:
            raise Exception("请求过于频繁, 已被 nhentai 限流, 请稍后再试。")
        if resp.status_code != 200:
            raise Exception(f"nhentai API 返回异常 (HTTP {resp.status_code})。")

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            raise Exception("nhentai API 返回了非 JSON 响应 (可能被 Cloudflare 拦截)。")

        title_obj = data.get("title", dict()) or dict()
        title = (title_obj.get("english") or title_obj.get("pretty")
                 or title_obj.get("japanese") or f"nhentai {gallery_id}")
        media_id = data.get("media_id")
        pages_list = data.get("pages", list()) or list()

        # 拉取最新 CDN 图片服务器列表, 失败则回退默认。
        image_servers = list(NH_DEFAULT_IMAGE_SERVERS)
        try:
            cfg = await session.get(f"{NH_API_BASE}/config", timeout=15)
            if cfg.status_code == 200:
                srv = cfg.json().get("image_servers") or list()
                if srv:
                    image_servers = srv
        except Exception:
            pass

        image_urls = list()
        for i, p in enumerate(pages_list):
            # v2 直接给出 path (例: galleries/4002351/1.webp)
            path = p.get("path")
            if not path:
                # 老结构兜底: 用 media_id + 类型扩展名映射拼路径
                ext = dict(j="jpg", p="png", w="webp").get(p.get("t"), "jpg")
                path = f"galleries/{media_id}/{p.get('number', i + 1)}.{ext}"
            server = image_servers[i % len(image_servers)]
            image_urls.append(f"{server}/{path}")

        if not image_urls:
            raise Exception("画廊中没有图片。")

        await msg.edit_text(f"🔍 [Nhentai] 成功获取 {len(image_urls)} 张图片，开始下载...")

        images = list()
        for idx, img_url in enumerate(image_urls):
            try:
                img_resp = await session.get(img_url, timeout=30)
                if img_resp.status_code == 200:
                    images.append(img_resp.content)
            except Exception as e:
                print(f"图片下载失败跳过 ({img_url}): {e}")
            if (idx + 1) % 10 == 0 or idx == len(image_urls) - 1:
                await msg.edit_text(f"🔍 [Nhentai] 正在下载中... 进度: {idx + 1}/{len(image_urls)}")

        if not images:
            raise Exception("所有图片下载失败 (CDN 不可达或被限流)。")

        iv_url = await deliver_gallery(title, images, msg, context, chat_id, tag="Nhentai")
        return title, iv_url


# 2. E-Hentai / ExHentai 全本提取引擎 (httpx + 防死链 NL 回退机制)
async def fetch_ehentai(clean_url: str, msg: Update.message, context, chat_id):
    await msg.edit_text("🔞 [EH/EX] 正在注入凭证并扫描全本画廊目录...")

    headers = dict()
    headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"

    viewer_links = list()
    page_num = 0
    title = "EH/EX Gallery"

    async with httpx.AsyncClient(timeout=30.0, cookies=EH_COOKIES, headers=headers, follow_redirects=True) as client:
        while True:
            page_url = f"{clean_url}?p={page_num}"
            try:
                resp = await client.get(page_url)
            except Exception as e:
                raise Exception(f"网络连接出错，无法获取目录: {e}")

            if "kokomade.jpg" in resp.text or resp.text.strip() == "":
                page_url = page_url.replace("exhentai.org", "e-hentai.org")
                resp = await client.get(page_url)
                if "kokomade.jpg" in resp.text or resp.text.strip() == "":
                    raise Exception("基础凭据无效，或服务器 IP 遭到彻底封禁。")

            if page_num == 0:
                title_match = re.search(r'<h1 id="gn">(.*?)</h1>', resp.text)
                if title_match:
                    title = title_match.group(1)

            links = re.findall(r'href="(https://e[-x]?hentai\.org/s/[^"]+)"', resp.text)
            links = list(dict.fromkeys(links))
            if not links:
                break

            new_links = [l for l in links if l not in viewer_links]
            if not new_links:
                break

            viewer_links.extend(new_links)
            page_num += 1
            await msg.edit_text(f"🔞 [EH/EX] 正在自动翻页扫描... 已发现 {len(viewer_links)} 张图片")
            await asyncio.sleep(1)

        if not viewer_links:
            raise Exception("无法提取阅读页链接，可能触发了高频访问限制。")

        await msg.edit_text(f"🔞 [EH/EX] 扫描完成！共 {len(viewer_links)} 张，开始绕过 P2P 节点下载...")

        images = list()
        for idx, v_url in enumerate(viewer_links):
            try:
                v_resp = await client.get(v_url)
                img_match = re.search(r'<img id="img" src="(.*?)"', v_resp.text)
                # 寻找官方的更换节点指令（New Link参数）
                nl_match = re.search(r"nl\('([^']+)'\)", v_resp.text)

                if img_match:
                    img_src = img_match.group(1)
                    img_bytes = None
                    try:
                        # 第一次尝试：直接从分配到的 H@H 节点下载
                        img_data_resp = await client.get(img_src, timeout=15.0)
                        if img_data_resp.status_code == 200:
                            img_bytes = img_data_resp.content
                    except Exception:
                        # 【核心修复】：节点被墙导致超时/拒绝连接时, 立即用 nl 回退到官方中央服务器
                        if nl_match:
                            try:
                                nl_url = f"{v_url}?nl={nl_match.group(1)}"
                                v_resp_retry = await client.get(nl_url, timeout=15.0)
                                img_match_retry = re.search(r'<img id="img" src="(.*?)"', v_resp_retry.text)
                                if img_match_retry:
                                    img_data_resp = await client.get(img_match_retry.group(1), timeout=15.0)
                                    if img_data_resp.status_code == 200:
                                        img_bytes = img_data_resp.content
                            except Exception:
                                pass  # 彻底失败则跳过该图，不崩溃

                    if img_bytes:
                        images.append(img_bytes)

            except Exception as e:
                print(f"获取页面异常跳过: {e}")

            if (idx + 1) % 5 == 0 or idx == len(viewer_links) - 1:
                await msg.edit_text(f"🔞 [EH/EX] 正在下载中... 进度: {idx + 1}/{len(viewer_links)}")

            await asyncio.sleep(0.8)

        iv_url = await deliver_gallery(title, images, msg, context, chat_id, tag="EH/EX")
        return title, iv_url


# 3. JMComic (禁漫) 绝对路径控制提取法
async def fetch_jmcomic(jm_type: str, jm_id: str, msg: Update.message, context, chat_id):
    type_name = "单章" if jm_type == "photo" else "整本"
    safe_dir = os.path.abspath(f"./jm_dl_safe_{jm_id}")

    def count_downloaded():
        n = 0
        for root, dirs, files in os.walk(safe_dir):
            for f in files:
                if f.lower().endswith(('.jpg', '.png', '.webp', '.jpeg')):
                    n += 1
        return n

    def download_jm_sync():
        os.makedirs(safe_dir, exist_ok=True)

        original_cwd = os.getcwd()
        os.chdir(safe_dir)

        try:
            option = jmcomic.JmOption.default()
            try:
                option.client_dict['impl'] = 'api'
            except Exception:
                pass

            client = option.build_jm_client()

            if jm_type == "photo":
                entity = client.get_photo_detail(jm_id)
                title = getattr(entity, 'title', f"JM{jm_id} 单独章节")
                jmcomic.download_photo(jm_id, option)
            else:
                entity = client.get_album_detail(jm_id)
                title = getattr(entity, 'title', f"JM{jm_id} 完整画廊")
                jmcomic.download_album(jm_id, option)
        except Exception as e:
            raise Exception(f"源站拒绝连接或本子已被下架。底层报错: {str(e)}")
        finally:
            os.chdir(original_cwd)

        images = list()
        for root, dirs, files in os.walk(safe_dir):
            for f in files:
                if f.lower().endswith(('.jpg', '.png', '.webp', '.jpeg')):
                    images.append(os.path.join(root, f))

        if not images:
            raise Exception("下载任务成功，但在隔离区中未找到图片，文件可能已损坏。")

        def sort_key(path):
            nums = re.findall(r'\d+', os.path.basename(path))
            return tuple(int(n) for n in nums) if nums else tuple((0,))

        images.sort(key=sort_key)
        return title, images, safe_dir

    async with jm_download_lock:
        await msg.edit_text(f"🔞 [JMComic] {type_name}请求排队成功！正在拉取原图及执行解密...")

        # 下载放到线程里跑, 主协程每 3 秒扫一次隔离区, 把已落盘的图片数量实时回报到 Telegram
        download_task = asyncio.create_task(asyncio.to_thread(download_jm_sync))
        last_count = -1
        while True:
            done, _ = await asyncio.wait({download_task}, timeout=3)
            if done:
                break
            count = await asyncio.to_thread(count_downloaded)
            if count > 0 and count != last_count:
                last_count = count
                try:
                    await msg.edit_text(f"🔞 [JMComic] {type_name}拉取中... 已下载 {count} 张图片")
                except Exception:
                    pass
        title, image_paths, safe_dir = download_task.result()

    await msg.edit_text(f"🔞 [JMComic] 像素解密完成！捕获 {len(image_paths)} 张图片，正在整合发送...")

    try:
        images = list()
        for img_path in image_paths:
            with open(img_path, 'rb') as f:
                images.append(f.read())

        iv_url = await deliver_gallery(title, images, msg, context, chat_id, tag="JMComic")
    finally:
        shutil.rmtree(safe_dir, ignore_errors=True)

    return title, iv_url


# 4. Telegram 交互中枢
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text
    chat_id = update.effective_chat.id

    if text.startswith("/start"):
        await context.bot.send_message(
            chat_id=chat_id,
            text="🚀 终极图集提取机器人已就绪！支持 nhentai / e-hentai / jmcomic。\n"
                 "📰 默认整合为「即时浏览」(Telegraph) 一键发送整本。",
        )
        return

    nh_match = re.search(r'nhentai\.net/g/(\d+)', text)
    eh_match = re.search(r'(e[-x]?hentai\.org)/g/(\d+)/([a-zA-Z0-9]+)', text)

    jm_type = None
    jm_id = None
    if "18comic" in text or "jmcomic" in text or "jm" in text.lower():
        if "photo/" in text:
            m = re.search(r'photo/(\d+)', text)
            if m:
                jm_id = m.group(1)
                jm_type = "photo"
        elif "album/" in text:
            m = re.search(r'album/(\d+)', text)
            if m:
                jm_id = m.group(1)
                jm_type = "album"
        else:
            m = re.search(r'(?:jm|jmcomic|18comic)[^\d]*(\d+)', text, re.IGNORECASE)
            if m:
                jm_id = m.group(1)
                jm_type = "album"

    if nh_match or eh_match or jm_id:
        msg = await context.bot.send_message(chat_id=chat_id, text="🕒 初始化智能抓取引擎...")
        try:
            if nh_match:
                gallery_id = nh_match.group(1)
                title, iv_url = await fetch_nhentai(gallery_id, msg, context, chat_id)
            elif eh_match:
                clean_url = f"https://{eh_match.group(1)}/g/{eh_match.group(2)}/{eh_match.group(3)}/"
                title, iv_url = await fetch_ehentai(clean_url, msg, context, chat_id)
            elif jm_id:
                title, iv_url = await fetch_jmcomic(jm_type, jm_id, msg, context, chat_id)

            safe_title = html.escape(title)
            if iv_url:
                safe_iv_url = html.escape(iv_url, quote=True)
                await msg.edit_text(
                    f"🎉 <b>整合完成！点击下方链接即时浏览：</b>\n"
                    f"📖 {safe_title}\n"
                    f'<a href="{safe_iv_url}">📰 打开即时浏览 (Instant View)</a>',
                    parse_mode="HTML",
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f'<a href="{safe_iv_url}">点击查看</a>',
                    parse_mode="HTML",
                )
            else:
                await msg.edit_text(
                    f"🎉 <b>全集传输完毕！</b>\n📖 {safe_title}\n"
                    f"<i>*请在上方聊天记录中翻阅相册*</i>",
                    parse_mode="HTML",
                )

        except Exception as e:
            safe_error = html.escape(str(e))
            await msg.edit_text(f"❌ <b>处理失败:</b>\n{safe_error}", parse_mode="HTML")


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        sys.exit("未配置 TELEGRAM_BOT_TOKEN: 请复制 .env.example 为 .env 并填写, 或设置同名环境变量。")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    app.run_polling()
