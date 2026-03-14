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

# ====== 你的专属配置区 ======
TELEGRAM_BOT_TOKEN = "8779795912:AAGAb92XBJzNQkYzhpd8vY3jwXbvVqKAnfQ"

EH_COOKIES = dict(
    igneous="td4ntdeh4nceib1px",
    ipb_member_id="9213034",
    ipb_pass_hash="7cfd101b628d0baf1a46e37aa64befde",
    nw="1"
)
# ========================

jm_download_lock = asyncio.Lock()

# 内存中格式转码
def ensure_jpeg(img_bytes):
    try:
        img = Image.open(io.BytesIO(img_bytes))
        if img.format == 'JPEG' and img.mode == 'RGB':
            return img_bytes
        if img.mode!= 'RGB':
            img = img.convert('RGB')
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=90)
        return out.getvalue()
    except Exception as e:
        return img_bytes

# 1. Nhentai 全本提取引擎
async def fetch_nhentai(gallery_id: str, msg: Update.message, context, chat_id):
    await msg.edit_text("🔍 [Nhentai] 正在使用 curl_cffi 穿透 Cloudflare...")
    
    async with AsyncSession(impersonate="chrome124") as session:
        resp = await session.get(f"https://nhentai.net/api/gallery/{gallery_id}")
        if resp.status_code!= 200:
            raise Exception(f"Cloudflare 拦截了请求或画廊不存在 (HTTP {resp.status_code})")
            
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise Exception("Cloudflare 依然返回了验证码。")
            
        title = data.get("title", dict()).get("english", f"nhentai {gallery_id}")
        media_id = data.get("media_id")
        pages_list = data.get("images", dict()).get("pages", list())
        
        image_urls = list()
        for i, p in enumerate(pages_list):
            ext_map = dict(j="jpg", p="png", w="webp")
            ext = ext_map.get(p.get("t"), "jpg")
            image_urls.append(f"https://i.nhentai.net/galleries/{media_id}/{i+1}.{ext}")
            
        if not image_urls:
            raise Exception("画廊中没有图片。")

        await msg.edit_text(f"🔍 [Nhentai] 成功获取 {len(image_urls)} 张图片，开始边下边发...")

        media_group = list()
        for idx, img_url in enumerate(image_urls):
            img_resp = await session.get(img_url)
            if img_resp.status_code == 200:
                img_bytes = ensure_jpeg(img_resp.content)
                caption = title[:1000] if idx == 0 else None
                media_group.append(InputMediaPhoto(media=img_bytes, caption=caption))
                
            if len(media_group) == 10 or idx == len(image_urls) - 1:
                if len(media_group) > 0:
                    try:
                        await context.bot.send_media_group(chat_id=chat_id, media=media_group)
                    except Exception as e:
                        print(f"发送图集出错: {e}")
                    media_group.clear() 
                    await msg.edit_text(f"🔍 [Nhentai] 正在火速传输中... 进度: {idx+1}/{len(image_urls)}")
                    await asyncio.sleep(2.5) 
                    
        return title

# 2. E-Hentai / ExHentai 全本提取引擎 (已退回 httpx 并加入防死链 NL 回退机制)
async def fetch_ehentai(clean_url: str, msg: Update.message, context, chat_id):
    await msg.edit_text("🔞 [EH/EX] 正在注入凭证并扫描全本画廊目录...")
    
    headers = dict()
    headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    
    viewer_links = list()
    page_num = 0
    title = "EH/EX Gallery"
    
    # 恢复使用稳定版 httpx 处理 EHentai
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
            
        await msg.edit_text(f"🔞 [EH/EX] 扫描完成！共 {len(viewer_links)} 张，开始绕过 P2P 节点进行边下边发...")
            
        media_group = list()
        success_count = 0
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
                    except Exception as e:
                        # 【核心修复】：如果节点被墙导致超时/拒绝连接，立即使用 nl 回退到官方中央服务器
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
                                pass # 彻底失败则跳过该图，不崩溃
                                
                    if img_bytes:
                        img_bytes = ensure_jpeg(img_bytes)
                        caption = title[:1000] if success_count == 0 else None
                        media_group.append(InputMediaPhoto(media=img_bytes, caption=caption))
                        success_count += 1
                        
            except Exception as e:
                print(f"获取页面异常跳过: {e}")
                
            if len(media_group) == 10 or idx == len(viewer_links) - 1:
                if len(media_group) > 0:
                    try:
                        await context.bot.send_media_group(chat_id=chat_id, media=media_group)
                    except Exception as e:
                        print(f"发送图集出错: {e}")
                    media_group.clear() 
                    await msg.edit_text(f"🔞 [EH/EX] 正在火速传输中... 进度: {idx+1}/{len(viewer_links)}")
                    await asyncio.sleep(3) 
                    
            await asyncio.sleep(0.8) 
            
        return title

# 3. JMComic (禁漫) 绝对路径控制提取法
async def fetch_jmcomic(jm_type: str, jm_id: str, msg: Update.message, context, chat_id):
    type_name = "单章" if jm_type == "photo" else "整本"
    
    def download_jm_sync():
        safe_dir = os.path.abspath(f"./jm_dl_safe_{jm_id}")
        os.makedirs(safe_dir, exist_ok=True)
        
        original_cwd = os.getcwd()
        os.chdir(safe_dir)
        
        try:
            option = jmcomic.JmOption.default()
            try:
                option.client_dict['impl'] = 'api'
            except:
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
        title, images, safe_dir = await asyncio.to_thread(download_jm_sync)
        
    await msg.edit_text(f"🔞 [JMComic] 像素解密完成！捕获 {len(images)} 张图片，正在洗图发送中...")

    try:
        media_group = list()
        for idx, img_path in enumerate(images):
            with open(img_path, 'rb') as f:
                img_bytes = f.read()
                
            img_bytes = ensure_jpeg(img_bytes)
                
            caption = title[:1000] if idx == 0 else None
            media_group.append(InputMediaPhoto(media=img_bytes, caption=caption))
            
            if len(media_group) == 10 or idx == len(images) - 1:
                if len(media_group) > 0:
                    try:
                        await context.bot.send_media_group(chat_id=chat_id, media=media_group)
                    except Exception as e:
                        print(f"发送图集出错: {e}")
                    media_group.clear()
                    await msg.edit_text(f"🔞 [JMComic] 正在火速传输中... 进度: {idx+1}/{len(images)}")
                    await asyncio.sleep(2.5)
    finally:
        shutil.rmtree(safe_dir, ignore_errors=True)
        
    return title

# 4. Telegram 交互中枢
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if text.startswith("/start"):
        await context.bot.send_message(chat_id=chat_id, text="🚀 终极图集提取机器人已就绪！支持 nhentai / e-hentai / jmcomic。")
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
                title = await fetch_nhentai(gallery_id, msg, context, chat_id)
            elif eh_match:
                clean_url = f"https://{eh_match.group(1)}/g/{eh_match.group(2)}/{eh_match.group(3)}/"
                title = await fetch_ehentai(clean_url, msg, context, chat_id)
            elif jm_id:
                title = await fetch_jmcomic(jm_type, jm_id, msg, context, chat_id)
            
            safe_title = html.escape(title)
            await msg.edit_text(f"🎉 <b>全集传输完毕！</b>\n📖 {safe_title}\n<i>*请在上方聊天记录中翻阅相册*</i>", parse_mode="HTML")
            
        except Exception as e:
            safe_error = html.escape(str(e))
            await msg.edit_text(f"❌ <b>处理失败:</b>\n{safe_error}", parse_mode="HTML")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    app.run_polling()
