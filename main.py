#!/usr/bin/env python3
"""
统一内容抓取API服务器
提供贴吧和微信公众号文章抓取服务
完全保留原有代码逻辑,只做路由合并
"""

import time
import json
import os
import re
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import traceback
from urllib.parse import urlparse, parse_qs
import urllib.request
import logging
from logging.handlers import RotatingFileHandler

# 导入原始爬虫类的依赖
from playwright.sync_api import sync_playwright
import requests
from bs4 import BeautifulSoup

# 去水印功能的导入
try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    print("⚠️ OpenCV未安装,将跳过去水印功能。如需去水印请安装: pip install opencv-python")

app = Flask(__name__)
CORS(app)

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not os.path.exists('logs'):
    os.makedirs('logs')
    
file_handler = RotatingFileHandler('logs/unified_api.log', maxBytes=10240000, backupCount=10)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
))
file_handler.setLevel(logging.INFO)
app.logger.addHandler(file_handler)


# ============================================================================
# 贴吧抓取类 - 完全保留原有逻辑
# ============================================================================
class TiebaPostScraperAPI:
    def __init__(self, remove_watermarks=True):
        # API服务器的工作目录
        self.work_dir = os.path.abspath(os.path.dirname(__file__))
        self.static_dir = os.path.join(self.work_dir, 'static', 'tieba')
        self.images_dir = os.path.join(self.static_dir, 'images')
        self.posts_dir = os.path.join(self.static_dir, 'posts')
        
        # 去水印功能开关
        self.remove_watermarks = remove_watermarks and HAS_CV2
        
        self.ensure_directories()
        
        if self.remove_watermarks:
            logger.info("🎨 已启用自动去水印功能")
        elif remove_watermarks and not HAS_CV2:
            logger.warning("⚠️ 去水印功能需要OpenCV,请运行: pip install opencv-python")
        else:
            logger.info("📷 未启用去水印功能")
    
    def ensure_directories(self):
        """确保所需目录存在"""
        for directory in [self.static_dir, self.images_dir, self.posts_dir]:
            if not os.path.exists(directory):
                os.makedirs(directory)
                logger.info(f"✅ 创建目录: {directory}")
    
    def clean_old_files(self, max_age_hours=24):
        """清理超过指定时间的旧文件"""
        try:
            current_time = time.time()
            max_age_seconds = max_age_hours * 3600
            
            for directory in [self.images_dir, self.posts_dir]:
                for root, dirs, files in os.walk(directory):
                    for file in files:
                        file_path = os.path.join(root, file)
                        if current_time - os.path.getctime(file_path) > max_age_seconds:
                            try:
                                os.remove(file_path)
                                logger.info(f"🗑️ 清理旧文件: {file_path}")
                            except Exception as e:
                                logger.error(f"清理文件失败 {file_path}: {e}")
                    
                    # 清理空目录
                    for dir_name in dirs:
                        dir_path = os.path.join(root, dir_name)
                        try:
                            if not os.listdir(dir_path):
                                os.rmdir(dir_path)
                                logger.info(f"🗑️ 清理空目录: {dir_path}")
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"清理旧文件时出错: {e}")
    
    def extract_post_id(self, url):
        """从贴吧链接中提取帖子ID"""
        try:
            patterns = [
                r'/p/(\d+)',  # 标准格式
                r'tid=(\d+)',  # 参数格式
            ]
            
            for pattern in patterns:
                match = re.search(pattern, url)
                if match:
                    return match.group(1)
            
            return None
        except Exception as e:
            logger.error(f"❌ 解析URL失败: {e}")
            return None
    
    def clean_tieba_url(self, url):
        """清理贴吧URL,去除多余参数"""
        try:
            post_id = self.extract_post_id(url)
            if post_id:
                clean_url = f"https://tieba.baidu.com/p/{post_id}"
                return clean_url
            return url
        except:
            return url
    
    def remove_watermark(self, image_path):
        """去除图片右下角水印"""
        if not HAS_CV2:
            logger.warning(f"⚠️ 跳过去水印 (OpenCV未安装): {os.path.basename(image_path)}")
            return False
        
        try:
            image = cv2.imread(image_path)
            if image is None:
                logger.error(f"❌ 无法读取图像: {image_path}")
                return False
            
            height, width = image.shape[:2]
            mask = np.zeros(image.shape[:2], np.uint8)
            
            watermark_width = min(420, int(width * 0.3))
            watermark_height = min(50, int(height * 0.1))
            
            cv2.rectangle(mask, 
                         (width - watermark_width, height - watermark_height), 
                         (width, height), 
                         255, -1)
            
            denoised_image = cv2.inpaint(image, mask, 3, cv2.INPAINT_TELEA)
            success = cv2.imwrite(image_path, denoised_image)
            
            if success:
                logger.info(f"✅ 已去水印: {os.path.basename(image_path)}")
                return True
            else:
                logger.error(f"❌ 保存去水印图片失败: {os.path.basename(image_path)}")
                return False
                
        except Exception as e:
            logger.error(f"❌ 去水印处理失败 {os.path.basename(image_path)}: {e}")
            return False
    
    def download_images(self, images, post_id):
        """下载帖子中的图片到静态目录"""
        if not images:
            return []
        
        # 创建帖子专属的图片目录
        post_images_dir = os.path.join(self.images_dir, post_id)
        if not os.path.exists(post_images_dir):
            os.makedirs(post_images_dir)
        
        downloaded_images = []
        
        for i, img in enumerate(images, 1):
            try:
                img_url = img['src']
                if not img_url:
                    continue
                
                # 确定文件扩展名
                if 'jpeg' in img_url.lower() or 'jpg' in img_url.lower():
                    ext = '.jpg'
                elif 'png' in img_url.lower():
                    ext = '.png'
                elif 'gif' in img_url.lower():
                    ext = '.gif'
                elif 'webp' in img_url.lower():
                    ext = '.webp'
                else:
                    ext = '.jpg'  # 默认
                
                img_filename = f"image_{i:03d}{ext}"
                img_filepath = os.path.join(post_images_dir, img_filename)
                
                # 下载图片
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Referer': 'https://tieba.baidu.com/'
                }
                
                req = urllib.request.Request(img_url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as response:
                    if response.status == 200:
                        with open(img_filepath, 'wb') as f:
                            f.write(response.read())
                        
                        # 去水印处理
                        watermark_removed = False
                        if self.remove_watermarks:
                            watermark_removed = self.remove_watermark(img_filepath)
                        
                        # 生成可访问的URL
                        image_url = f"/tieba/images/{post_id}/{img_filename}"
                        
                        downloaded_images.append({
                            'original_url': img_url,
                            'local_path': img_filepath,
                            'filename': img_filename,
                            'image_url': image_url,
                            'alt': img.get('alt', ''),
                            'title': img.get('title', ''),
                            'watermark_removed': watermark_removed
                        })
                        logger.info(f"📷 已下载图片 {i}: {img_filename}")
                    else:
                        logger.warning(f"⚠️ 图片下载失败 {i}: HTTP {response.status}")
                        
            except Exception as e:
                logger.error(f"❌ 下载图片 {i} 失败: {e}")
                continue
        
        return downloaded_images
    
    def scrape_tieba_post(self, post_url):
        """抓取贴吧帖子内容"""
        logger.info(f"🔍 开始抓取贴吧帖子: {post_url}")
        
        clean_url = self.clean_tieba_url(post_url)
        logger.info(f"🔗 清理后的URL: {clean_url}")
        
        # 直接使用浏览器方式
        logger.info("🌐 使用浏览器方式抓取...")
        return self.scrape_with_browser(clean_url)
    
    def scrape_with_browser(self, post_url):
        """使用浏览器抓取"""
        logger.info("🌐 启动浏览器进行抓取...")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-extensions',
                    '--disable-plugins',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-web-security',
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--single-process',
                ]
            )
            
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
            )
            
            page = context.new_page()
            
            try:
                logger.info("🌐 正在访问帖子页面...")
                page.set_default_timeout(45000)
                
                # 预热访问
                try:
                    page.goto("https://www.baidu.com", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(3)
                except:
                    pass
                
                # 访问目标页面
                response = page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
                logger.info(f"📄 页面响应状态: {response.status if response else '无响应'}")
                
                time.sleep(3)
                
                page_title = page.title()
                page_url = page.url
                logger.info(f"📰 页面标题: {page_title}")
                logger.info(f"🔗 最终URL: {page_url}")
                
                # 滚动页面加载更多内容
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(2)
                except:
                    pass
                
                # 获取页面内容并解析
                page_content = page.content()
                return self.parse_html_content(page_content, post_url)
                
            except Exception as e:
                logger.error(f"❌ 浏览器抓取失败: {e}")
                return {
                    'success': False,
                    'error': str(e),
                    'url': post_url,
                    'method': 'browser_extraction'
                }
            finally:
                browser.close()
    
    def parse_html_content(self, html_content, post_url):
        """解析HTML内容 - 完全保留原有逻辑"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # 提取帖子基本信息
            post_info = self.extract_post_info(soup, post_url)
            
            # 提取主帖内容
            main_post = self.extract_main_post(soup)
            
            # 提取回复内容
            replies = self.extract_replies(soup)
            
            # 收集所有图片
            all_images = []
            if main_post.get('images'):
                all_images.extend(main_post['images'])
            
            for reply in replies:
                if reply.get('images'):
                    all_images.extend(reply['images'])
            
            logger.info(f"✅ HTML解析成功: 主帖+{len(replies)}条回复, {len(all_images)}张图片")
            
            # 生成Markdown
            main_markdown = self.generate_main_markdown(post_info, main_post)
            comments_markdown = self.generate_comments_markdown(post_info, replies)
            
            return {
                'success': True,
                'post_info': post_info,
                'main_post': main_post,
                'replies': replies,
                'total_replies': len(replies),
                'all_images': all_images,
                'total_images': len(all_images),
                'main_markdown': main_markdown,
                'comments_markdown': comments_markdown,
                'char_count': len(main_markdown) + len(comments_markdown),
                'method': 'html_parsing',
                'extraction_time': datetime.now().isoformat(),
                'url': post_url
            }
            
        except Exception as e:
            logger.error(f"❌ HTML解析失败: {e}")
            return {
                'success': False,
                'error': str(e),
                'url': post_url,
                'method': 'html_parsing'
            }
    
    def extract_post_info(self, soup, post_url):
        """提取帖子基本信息"""
        post_info = {
            'title': '未找到标题',
            'author': '未知作者',
            'post_time': '未知时间',
            'forum_name': '未知贴吧',
            'post_id': self.extract_post_id(post_url),
            'url': post_url
        }
        
        # 提取标题
        title_selectors = [
            '.core_title_txt',
            '.p_title',
            'h1.core_title_txt',
            'h3.core_title_txt',
            '[class*="title"]'
        ]
        
        for selector in title_selectors:
            try:
                title_elem = soup.select_one(selector)
                if title_elem:
                    title_text = title_elem.get_text().strip()
                    if title_text and len(title_text) > 3:
                        post_info['title'] = title_text
                        logger.info(f"✅ 找到标题: {title_text}")
                        break
            except:
                continue
        
        # 提取贴吧名称
        forum_selectors = [
            '.card_title',
            '.forum_name',
            'a[href*="/f?kw="]'
        ]
        
        for selector in forum_selectors:
            try:
                forum_elem = soup.select_one(selector)
                if forum_elem:
                    forum_text = forum_elem.get_text().strip()
                    if forum_text and len(forum_text) > 1:
                        post_info['forum_name'] = forum_text.replace('吧', '')
                        logger.info(f"✅ 找到贴吧: {forum_text}")
                        break
            except:
                continue
        
        return post_info
    
    def extract_main_post(self, soup):
        """提取主帖内容"""
        main_post = {
            'author': '未知作者',
            'post_time': '未知时间',
            'content': [],
            'images': []
        }
        
        # 查找主帖容器
        main_post_selectors = [
            '.d_post_content',
            '.p_postlist .l_post:first-child',
            '.core_reply_wrapper .l_post:first-child'
        ]
        
        main_post_elem = None
        for selector in main_post_selectors:
            try:
                main_post_elem = soup.select_one(selector)
                if main_post_elem:
                    logger.info(f"✅ 找到主帖容器: {selector}")
                    break
            except:
                continue
        
        if main_post_elem:
            # 提取作者
            author_selectors = [
                '.p_author_name',
                '.username',
                'a[username]'
            ]
            
            for selector in author_selectors:
                try:
                    author_elem = main_post_elem.select_one(selector)
                    if author_elem:
                        author_text = author_elem.get_text().strip()
                        if author_text:
                            main_post['author'] = author_text
                            logger.info(f"✅ 主帖作者: {author_text}")
                            break
                except:
                    continue
            
            # 提取内容和图片
            main_post['content'], main_post['images'], main_post['content_elements'] = self.extract_post_content(main_post_elem)
        
        return main_post
    
    def extract_replies(self, soup):
        """提取回复内容"""
        replies = []
        
        reply_selectors = [
            '.l_post[data-field*="content"]',
            '.core_reply .l_post'
        ]
        
        reply_elements = []
        for selector in reply_selectors:
            try:
                elements = soup.select(selector)
                if elements and len(elements) > 1:
                    reply_elements = elements[1:]  # 跳过第一个(主帖)
                    logger.info(f"✅ 找到 {len(reply_elements)} 条回复")
                    break
            except:
                continue
        
        for i, reply_elem in enumerate(reply_elements[:10], 1):  # 限制前10条回复
            try:
                reply_data = {
                    'floor': i + 1,
                    'author': '未知用户',
                    'post_time': '未知时间',
                    'content': [],
                    'images': []
                }
                
                # 提取回复作者
                author_selectors = [
                    '.p_author_name',
                    '.username',
                    'a[username]'
                ]
                
                for selector in author_selectors:
                    try:
                        author_elem = reply_elem.select_one(selector)
                        if author_elem:
                            author_text = author_elem.get_text().strip()
                            if author_text:
                                reply_data['author'] = author_text
                                break
                    except:
                        continue
                
                # 提取回复内容和图片
                reply_data['content'], reply_data['images'], reply_data['content_elements'] = self.extract_post_content(reply_elem)
                
                if reply_data['content'] or reply_data['images']:
                    replies.append(reply_data)
                    logger.info(f"📝 回复 {i}: {reply_data['author']} - {len(reply_data['content'])}段落, {len(reply_data['images'])}图片")
                
            except Exception as e:
                logger.error(f"⚠️ 处理回复 {i} 失败: {e}")
                continue
        
        return replies
    
    def extract_post_content(self, post_elem):
        """按原始顺序提取帖子内容(文本和图片混合) - 完全保留原有逻辑"""
        content_elements = []  # 存储混合的内容元素
        
        # 查找内容容器
        content_selectors = [
            '.d_post_content',
            '.p_content',
            '.post_content',
            '.content'
        ]
        
        content_elem = None
        for selector in content_selectors:
            try:
                content_elem = post_elem.select_one(selector)
                if content_elem:
                    break
            except:
                continue
        
        if not content_elem:
            content_elem = post_elem
        
        # 按原始DOM顺序遍历所有子元素
        processed_texts = set()
        
        # 特殊处理贴吧的HTML结构 - 使用更简单直接的方法
        # 直接处理HTML内容,按<br>分隔文本,保持图片位置
        content_html = str(content_elem)
        
        # 先提取所有文本,按<br>分割
        import re
        # 将<br>替换为特殊分隔符
        text_content = re.sub(r'<br[^>]*>', '|||BR|||', content_html)
        # 移除所有HTML标签,保留文本
        text_content = re.sub(r'<[^>]+>', '', text_content)
        # 按分隔符分割
        text_parts = text_content.split('|||BR|||')
        
        # 同时查找所有图片
        img_elements = content_elem.find_all('img')
        
        # 混合处理文本和图片
        img_index = 0
        for part in text_parts:
            part = part.strip()
            if part and self._is_valid_text_content(part, processed_texts):
                clean_text = self._clean_text_content(part)
                if clean_text and clean_text not in processed_texts:
                    text_info = {
                        'type': 'text',
                        'content': clean_text,
                        'tag': 'text'
                    }
                    content_elements.append(text_info)
                    processed_texts.add(clean_text)
                    logger.info(f"📝 发现文本: {clean_text[:50]}...")
            
            # 在每个文本段落后可能有图片
            if img_index < len(img_elements):
                img = img_elements[img_index]
                img_src = img.get('src') or img.get('data-original') or img.get('original')
                if img_src:
                    # 处理相对URL
                    if img_src.startswith('//'):
                        img_src = 'https:' + img_src
                    elif img_src.startswith('/'):
                        img_src = 'https://tieba.baidu.com' + img_src
                    
                    # 检查是否是有效的百度图片URL
                    if ('baidu.com' in img_src and (
                        'imgsrc.baidu.com' in img_src or 
                        'hiphotos.baidu.com' in img_src or 
                        'tiebapic.baidu.com' in img_src)) or 'BDE_Image' in img.get('class', []):
                        
                        # 检查是否已经添加过这个图片
                        img_already_added = any(
                            el['type'] == 'image' and el['src'] == img_src 
                            for el in content_elements
                        )
                        
                        if not img_already_added:
                            img_info = {
                                'type': 'image',
                                'src': img_src,
                                'alt': img.get('alt', ''),
                                'title': img.get('title', '')
                            }
                            content_elements.append(img_info)
                            logger.info(f"📷 发现图片: {img_src}")
                            img_index += 1
        
        # 处理剩余的图片
        while img_index < len(img_elements):
            img = img_elements[img_index]
            img_src = img.get('src') or img.get('data-original') or img.get('original')
            if img_src:
                if img_src.startswith('//'):
                    img_src = 'https:' + img_src
                elif img_src.startswith('/'):
                    img_src = 'https://tieba.baidu.com' + img_src
                
                if ('baidu.com' in img_src and (
                    'imgsrc.baidu.com' in img_src or 
                    'hiphotos.baidu.com' in img_src or 
                    'tiebapic.baidu.com' in img_src)) or 'BDE_Image' in img.get('class', []):
                    
                    img_already_added = any(
                        el['type'] == 'image' and el['src'] == img_src 
                        for el in content_elements
                    )
                    
                    if not img_already_added:
                        img_info = {
                            'type': 'image',
                            'src': img_src,
                            'alt': img.get('alt', ''),
                            'title': img.get('title', '')
                        }
                        content_elements.append(img_info)
                        logger.info(f"📷 发现图片: {img_src}")
            img_index += 1
        
        # 如果上面的方法没有找到内容,使用备用方法
        if not content_elements:
            all_elements = content_elem.find_all(['p', 'div', 'span', 'img', 'br'], recursive=True)
            if not all_elements:
                all_elements = [content_elem]
            
            for elem in all_elements:
                try:
                    # 处理图片元素
                    if elem.name == 'img':
                        img_src = elem.get('src') or elem.get('data-original') or elem.get('original')
                        if img_src:
                            # 处理相对URL
                            if img_src.startswith('//'):
                                img_src = 'https:' + img_src
                            elif img_src.startswith('/'):
                                img_src = 'https://tieba.baidu.com' + img_src
                        
                            # 检查是否是有效的百度图片URL
                            if ('baidu.com' in img_src and (
                                'imgsrc.baidu.com' in img_src or 
                                'hiphotos.baidu.com' in img_src or 
                                'tiebapic.baidu.com' in img_src)) or 'BDE_Image' in elem.get('class', []):
                                
                                # 检查是否已经添加过这个图片
                                img_already_added = any(
                                    el['type'] == 'image' and el['src'] == img_src 
                                    for el in content_elements
                                )
                                
                                if not img_already_added:
                                    img_info = {
                                        'type': 'image',
                                        'src': img_src,
                                        'alt': elem.get('alt', ''),
                                        'title': elem.get('title', '')
                                    }
                                    content_elements.append(img_info)
                                    logger.info(f"📷 发现图片: {img_src}")
                    
                    # 处理文本元素
                    elif elem.name in ['p', 'div', 'span']:
                        # 跳过包含图片的元素,避免重复
                        if elem.find('img'):
                            continue
                        
                        text = elem.get_text(strip=True)
                        
                        # 清理和过滤文本
                        if self._is_valid_text_content(text, processed_texts):
                            # 进一步清理文本
                            clean_text = self._clean_text_content(text)
                            if clean_text and clean_text not in processed_texts:
                                text_info = {
                                    'type': 'text',
                                    'content': clean_text,
                                    'tag': elem.name
                                }
                                content_elements.append(text_info)
                                processed_texts.add(clean_text)
                                logger.info(f"📝 发现文本: {clean_text[:50]}...")
                                
                except Exception as e:
                    logger.warning(f"⚠️ 处理元素失败: {e}")
                    continue
        
        # 如果没有找到内容,尝试备用方法
        if not content_elements:
            content_elements = self._fallback_content_extraction(content_elem, processed_texts)
        
        # 分离为传统格式以保持向后兼容
        content_paragraphs = [elem['content'] for elem in content_elements if elem['type'] == 'text']
        images = [elem for elem in content_elements if elem['type'] == 'image']
        
        return content_paragraphs, images, content_elements
    
    def _fallback_content_extraction(self, content_elem, processed_texts):
        """备用内容提取方法"""
        content_elements = []
        logger.info("使用备用内容提取方法...")
        
        # 方法1:直接获取文本并按标点符号分割
        direct_text = content_elem.get_text(separator=' ', strip=True)
        
        if direct_text and len(direct_text) > 20:
            logger.info(f"备用方法获取到文本长度: {len(direct_text)}")
            # 分割成段落 - 使用多种分隔符
            sentences = []
            for delimiter in ['。', '！', '？', '\n\n', '\r\n']:
                if delimiter in direct_text:
                    sentences = direct_text.split(delimiter)
                    break
            
            if not sentences:
                sentences = [direct_text]  # 如果没有分隔符,使用整段文本
            
            for sentence in sentences:
                sentence = sentence.strip()
                if sentence and len(sentence) > 5:  # 降低长度要求
                    # 添加结束符号(如果需要的话)
                    if not sentence.endswith(('。', '！', '？', '.', '!', '?')):
                        sentence += '。'
                    
                    if self._is_valid_text_content(sentence, processed_texts):
                        clean_text = self._clean_text_content(sentence)
                        if clean_text and clean_text not in processed_texts:
                            text_info = {
                                'type': 'text',
                                'content': clean_text,
                                'tag': 'p'
                            }
                            content_elements.append(text_info)
                            processed_texts.add(clean_text)
                            logger.info(f"📝 备用方法发现文本: {clean_text[:50]}...")
        
        # 方法2:如果方法1没有结果,尝试更宽松的提取
        if not content_elements:
            logger.info("尝试更宽松的文本提取...")
            # 尝试提取所有文本节点
            from bs4 import NavigableString
            
            for element in content_elem.descendants:
                if isinstance(element, NavigableString):
                    text = str(element).strip()
                    if text and len(text) > 3:
                        # 更宽松的过滤条件
                        skip_patterns = ['script', 'style', 'noscript']
                        if not any(pattern in text.lower() for pattern in skip_patterns):
                            clean_text = self._clean_text_content(text)
                            if (clean_text and 
                                clean_text not in processed_texts and
                                len(clean_text) > 3):
                                
                                text_info = {
                                    'type': 'text',
                                    'content': clean_text,
                                    'tag': 'text'
                                }
                                content_elements.append(text_info)
                                processed_texts.add(clean_text)
                                logger.info(f"📝 宽松模式发现文本: {clean_text[:50]}...")
        
        # 方法3:如果还是没有结果,直接输出调试信息
        if not content_elements:
            logger.warning("所有文本提取方法都失败,输出调试信息...")
            logger.info(f"内容元素HTML长度: {len(str(content_elem))}")
            logger.info(f"内容元素文本长度: {len(content_elem.get_text())}")
            # 输出前500个字符用于调试
            sample_text = content_elem.get_text()[:500]
            logger.info(f"内容元素样本文本: {sample_text}")
        
        return content_elements
    
    def _is_valid_text_content(self, text, processed_texts):
        """检查文本内容是否有效"""
        if not text or len(text) < 3:  # 降低最小长度要求
            return False
        
        if text in processed_texts:
            return False
        
        # 过滤无用的界面文字 - 更精确的过滤
        filter_patterns = [
            '点击展开,查看完整图片', '收起回复', '查看全部', '显示全部楼层', 
            '只看楼主', '来自', '使用', '客户端', '更多', 'APP', '手机版',
            '该楼层疑似违规', '隐藏此楼', '查看此楼', '贴吧', '百度', '登录', '注册'
        ]
        
        # 完全匹配或包含这些短语时才过滤
        for pattern in filter_patterns:
            if pattern in text:
                return False
        
        # 过滤纯数字、纯链接、纯符号
        if (text.isdigit() or 
            text.startswith('http') or
            text in ['.', '。', '!', '！', '?', '？', ',', '，', '；', ';']):
            return False
        
        # 过滤过短的无意义文本
        if len(text.strip()) == 1 and text.strip() in ['回复', '举报', '删除']:
            return False
        
        return True
    
    def _clean_text_content(self, text):
        """清理文本内容"""
        # 去除多余的空白字符
        text = ' '.join(text.split())
        
        # 去除HTML实体
        import html
        text = html.unescape(text)
        
        # 去除特殊字符开头的内容
        if text.startswith(('回复', '@', '#')):
            return None
        
        # 去除末尾的无用字符
        text = text.rstrip('_-=+')
        
        return text.strip()
    
    def generate_main_markdown(self, post_info, main_post):
        """生成主帖Markdown内容"""
        markdown = f"# {post_info['title']}\n\n"
        markdown += f"**贴吧**: {post_info['forum_name']}吧\n"
        markdown += f"**作者**: {main_post.get('author', '未知')}\n\n"
        markdown += "---\n\n"
        
        # 主帖内容 - 按原始顺序混合显示
        if main_post.get('content_elements'):
            img_counter = 1
            for element in main_post['content_elements']:
                if element['type'] == 'text':
                    markdown += f"{element['content']}\n\n"
                elif element['type'] == 'image':
                    markdown += f"![图片{img_counter}]({element['src']})\n\n"
                    img_counter += 1
        else:
            # 备用方案
            if main_post.get('content'):
                for paragraph in main_post['content']:
                    markdown += f"{paragraph}\n\n"
            
            if main_post.get('images'):
                for i, img in enumerate(main_post['images'], 1):
                    markdown += f"![图片{i}]({img['src']})\n\n"
        
        return markdown
    
    def generate_comments_markdown(self, post_info, replies):
        """生成评论Markdown内容"""
        if not replies:
            return ""
        
        markdown = f"# {post_info['title']} - 评论区\n\n"
        markdown += f"**原帖链接**: {post_info['url']}\n\n"
        markdown += "---\n\n"
        
        for reply in replies:
            markdown += f"## {reply['floor']}楼 - {reply['author']}\n\n"
            
            if reply.get('content_elements'):
                img_counter = 1
                for element in reply['content_elements']:
                    if element['type'] == 'text':
                        markdown += f"{element['content']}\n\n"
                    elif element['type'] == 'image':
                        markdown += f"![{reply['floor']}楼图片{img_counter}]({element['src']})\n\n"
                        img_counter += 1
            else:
                # 备用方案
                if reply.get('content'):
                    for paragraph in reply['content']:
                        markdown += f"{paragraph}\n\n"
                
                if reply.get('images'):
                    for i, img in enumerate(reply['images'], 1):
                        markdown += f"![{reply['floor']}楼图片{i}]({img['src']})\n\n"
            
            markdown += "---\n\n"
        
        return markdown
    
    def update_markdown_with_local_images(self, markdown_content, downloaded_images, post_id):
        """更新Markdown中的图片链接为本地路径"""
        if not downloaded_images:
            return markdown_content
        
        # 创建URL映射
        url_map = {}
        for img in downloaded_images:
            url_map[img['original_url']] = img['image_url']
        
        # 替换图片链接
        for original_url, local_url in url_map.items():
            markdown_content = markdown_content.replace(f"]({original_url})", f"]({local_url})")
        
        return markdown_content


# ============================================================================
# 微信抓取类 - 完全保留原有逻辑
# ============================================================================
class WeChatArticleScraperAPI:
    def __init__(self, remove_watermarks=True):
        # API服务器的工作目录
        self.work_dir = os.path.abspath(os.path.dirname(__file__))
        self.static_dir = os.path.join(self.work_dir, 'static', 'wechat')
        self.images_dir = os.path.join(self.static_dir, 'images')
        self.articles_dir = os.path.join(self.static_dir, 'articles')
        
        # 去水印功能开关
        self.remove_watermarks = remove_watermarks and HAS_CV2
        
        self.ensure_directories()
        
        if self.remove_watermarks:
            logger.info("🎨 已启用自动去水印功能")
        elif remove_watermarks and not HAS_CV2:
            logger.warning("⚠️ 去水印功能需要OpenCV,请运行: pip install opencv-python")
        else:
            logger.info("📷 未启用去水印功能")
    
    def ensure_directories(self):
        """确保所需目录存在"""
        for directory in [self.static_dir, self.images_dir, self.articles_dir]:
            if not os.path.exists(directory):
                os.makedirs(directory)
                logger.info(f"✅ 创建目录: {directory}")
    
    def clean_old_files(self, max_age_hours=24):
        """清理超过指定时间的旧文件"""
        try:
            current_time = time.time()
            max_age_seconds = max_age_hours * 3600
            
            for directory in [self.images_dir, self.articles_dir]:
                for root, dirs, files in os.walk(directory):
                    for file in files:
                        file_path = os.path.join(root, file)
                        if current_time - os.path.getctime(file_path) > max_age_seconds:
                            try:
                                os.remove(file_path)
                                logger.info(f"🗑️ 清理旧文件: {file_path}")
                            except Exception as e:
                                logger.error(f"清理文件失败 {file_path}: {e}")
                    
                    # 清理空目录
                    for dir_name in dirs:
                        dir_path = os.path.join(root, dir_name)
                        try:
                            if not os.listdir(dir_path):
                                os.rmdir(dir_path)
                                logger.info(f"🗑️ 清理空目录: {dir_path}")
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"清理旧文件时出错: {e}")
    
    def remove_watermark(self, image_path):
        """去除图片右下角水印"""
        if not HAS_CV2:
            logger.warning(f"⚠️ 跳过去水印 (OpenCV未安装): {os.path.basename(image_path)}")
            return False
        
        try:
            # 读取图像
            image = cv2.imread(image_path)
            if image is None:
                logger.error(f"❌ 无法读取图像: {image_path}")
                return False
            
            height, width = image.shape[:2]
            
            # 创建掩码,标记右下角水印区域
            mask = np.zeros(image.shape[:2], np.uint8)
            
            # 根据图片大小动态调整水印区域
            watermark_width = min(420, int(width * 0.3))
            watermark_height = min(50, int(height * 0.1))
            
            # 绘制矩形掩码标记水印区域
            cv2.rectangle(mask, 
                         (width - watermark_width, height - watermark_height), 
                         (width, height), 
                         255, -1)
            
            # 使用inpaint函数修复图像,去除水印
            denoised_image = cv2.inpaint(image, mask, 3, cv2.INPAINT_TELEA)
            
            # 保存处理后的图像,覆盖原文件
            success = cv2.imwrite(image_path, denoised_image)
            
            if success:
                logger.info(f"✅ 已去水印: {os.path.basename(image_path)}")
                return True
            else:
                logger.error(f"❌ 保存去水印图片失败: {os.path.basename(image_path)}")
                return False
                
        except Exception as e:
            logger.error(f"❌ 去水印处理失败 {os.path.basename(image_path)}: {e}")
            return False
    
    def extract_article_id(self, url):
        """从微信链接中提取文章ID"""
        try:
            parsed = urlparse(url)
            if 'mp.weixin.qq.com' in parsed.netloc:
                params = parse_qs(parsed.query)
                return {
                    'url': url,
                    'domain': parsed.netloc,
                    'params': params
                }
            return None
        except Exception as e:
            logger.error(f"❌ 解析URL失败: {e}")
            return None
    
    def download_images(self, images, article_id):
        """下载文章中的图片到静态目录"""
        if not images:
            return []
        
        # 创建文章专属的图片目录
        article_images_dir = os.path.join(self.images_dir, article_id)
        if not os.path.exists(article_images_dir):
            os.makedirs(article_images_dir)
        
        downloaded_images = []
        
        for i, img in enumerate(images, 1):
            try:
                img_url = img['src']
                if not img_url:
                    continue
                
                # 确定文件扩展名
                if 'jpeg' in img_url.lower() or 'jpg' in img_url.lower():
                    ext = '.jpg'
                elif 'png' in img_url.lower():
                    ext = '.png'
                elif 'gif' in img_url.lower():
                    ext = '.gif'
                elif 'webp' in img_url.lower():
                    ext = '.webp'
                else:
                    ext = '.jpg'  # 默认
                
                # 生成文件名
                img_filename = f"image_{i:03d}{ext}"
                img_filepath = os.path.join(article_images_dir, img_filename)
                
                # 下载图片
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Referer': 'https://mp.weixin.qq.com/'
                }
                
                req = urllib.request.Request(img_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status == 200:
                        with open(img_filepath, 'wb') as f:
                            f.write(response.read())
                        
                        # 下载完成后自动去水印(如果启用)
                        watermark_removed = False
                        if self.remove_watermarks:
                            watermark_removed = self.remove_watermark(img_filepath)
                        
                        # 生成可访问的URL
                        image_url = f"/weixin/images/{article_id}/{img_filename}"
                        
                        downloaded_images.append({
                            'original_url': img_url,
                            'local_path': img_filepath,
                            'filename': img_filename,
                            'image_url': image_url,  # 供前端访问的URL
                            'alt': img.get('alt', ''),
                            'title': img.get('title', ''),
                            'watermark_removed': watermark_removed
                        })
                        logger.info(f"📷 已下载图片 {i}: {img_filename}")
                    else:
                        logger.warning(f"⚠️ 图片下载失败 {i}: HTTP {response.status}")
                        
            except Exception as e:
                logger.error(f"❌ 下载图片 {i} 失败: {e}")
                continue
        
        return downloaded_images
    
    def scrape_wechat_article(self, article_url):
        """抓取微信公众号文章内容"""
        logger.info(f"🔍 开始抓取微信文章: {article_url}")
        
        # 首先尝试用requests简单获取
        try:
            logger.info("📄 预检查网页可访问性...")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
            
            response = requests.get(article_url, headers=headers, timeout=10)
            logger.info(f"📊 HTTP状态码: {response.status_code}")
            
            if response.status_code == 200:
                if "环境异常" in response.text or "完成验证" in response.text:
                    logger.warning("⚠️ 检测到需要验证,尝试浏览器方式...")
                elif len(response.text) < 1000:
                    logger.warning("⚠️ 内容过少,可能被拦截,尝试浏览器方式...")
                else:
                    logger.info("✅ 预检查通过,尝试直接解析HTML...")
                    return self.parse_html_content(response.text, article_url)
            else:
                logger.warning(f"⚠️ HTTP状态码异常: {response.status_code},尝试浏览器方式...")
                
        except Exception as e:
            logger.warning(f"⚠️ 预检查失败: {e},尝试浏览器方式...")
        
        # 使用浏览器方式
        return self.scrape_with_browser(article_url)
    
    def parse_html_content(self, html_content, article_url):
        """直接解析HTML内容"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # 提取标题
            title_elem = (soup.find('h1', {'id': 'activity-name'}) or 
                         soup.find('h1', class_='rich_media_title') or
                         soup.find('h1'))
            
            article_title = title_elem.get_text().strip() if title_elem else "未找到标题"
            
            # 提取作者
            author_elem = (soup.find('span', {'id': 'js_name'}) or
                          soup.find('span', class_='profile_nickname'))
            author_name = author_elem.get_text().strip() if author_elem else "未知作者"
            
            # 提取发布时间
            time_elem = soup.find('span', {'id': 'publish_time'})
            publish_time = time_elem.get_text().strip() if time_elem else "未知时间"
            
            # 提取正文内容 - 保持原始排版顺序
            content_elem = (soup.find('div', {'id': 'js_content'}) or
                           soup.find('div', class_='rich_media_content'))
            
            content_elements = []
            images = []
            
            if content_elem:
                processed_texts = set()
                
                for element in content_elem.find_all(recursive=True):
                    # 处理图片
                    if element.name == 'img':
                        img_src = element.get('src') or element.get('data-src')
                        if img_src:
                            # 处理微信图片URL
                            if img_src.startswith('//'):
                                img_src = 'https:' + img_src
                            elif img_src.startswith('/'):
                                img_src = 'https://mp.weixin.qq.com' + img_src
                            
                            img_already_added = any(
                                el['type'] == 'image' and el['src'] == img_src 
                                for el in content_elements
                            )
                            
                            if not img_already_added:
                                img_info = {
                                    'type': 'image',
                                    'src': img_src,
                                    'alt': element.get('alt', ''),
                                    'title': element.get('title', ''),
                                    'order': len(content_elements)
                                }
                                content_elements.append(img_info)
                                images.append(img_info)
                                logger.info(f"📷 发现图片: {img_src}")
                    
                    # 处理文本内容
                    elif element.name in ['p', 'div', 'section', 'span', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                        has_block_children = bool(element.find(['p', 'div', 'section', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']))
                        
                        if has_block_children and element.name in ['div', 'section']:
                            continue
                        
                        if element.find('img'):
                            continue
                            
                        text = element.get_text().strip()
                        if (text and 
                            len(text) > 10 and
                            text not in processed_texts and
                            not text.startswith('http') and
                            '阅读原文' not in text and
                            '点击查看' not in text):
                            
                            is_substring_of_existing = any(
                                text in existing_text or existing_text in text
                                for existing_text in processed_texts
                                if len(existing_text) > len(text)
                            )
                            
                            if not is_substring_of_existing:
                                text_info = {
                                    'type': 'text',
                                    'content': text,
                                    'tag': element.name,
                                    'order': len(content_elements)
                                }
                                content_elements.append(text_info)
                                processed_texts.add(text)
            
            # 按order排序确保顺序正确
            content_elements.sort(key=lambda x: x['order'])
            
            # 统计信息
            text_count = len([elem for elem in content_elements if elem['type'] == 'text'])
            image_count = len([elem for elem in content_elements if elem['type'] == 'image'])
            
            logger.info(f"✅ HTML解析成功: {text_count} 个文本段落, {image_count} 张图片")
            
            return {
                'success': True,
                'title': article_title,
                'author': author_name,
                'publish_time': publish_time,
                'content_elements': content_elements,
                'content': [elem['content'] for elem in content_elements if elem['type'] == 'text'],
                'images': images,
                'paragraph_count': text_count,
                'image_count': image_count,
                'total_elements': len(content_elements),
                'error': None,
                'method': 'html_parsing'
            }
            
        except Exception as e:
            logger.error(f"❌ HTML解析失败: {e}")
            return self.scrape_with_browser(article_url)
    
    def scrape_with_browser(self, article_url):
        """使用浏览器抓取"""
        logger.info("🌐 启动浏览器进行抓取...")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-extensions',
                    '--disable-plugins',
                    '--disable-images',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--single-process',
                ]
            )
            
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/14.0 Mobile/15E148 Safari/604.1"
                ),
                viewport={"width": 375, "height": 667},
            )
            
            page = context.new_page()
            
            try:
                logger.info("🌐 正在访问文章页面...")
                page.set_default_timeout(30000)
                
                response = page.goto(article_url, wait_until="domcontentloaded")
                logger.info(f"📄 页面响应状态: {response.status}")
                
                time.sleep(5)
                
                # 检查验证
                page_content = page.content()
                if "环境异常" in page_content or "完成验证" in page_content:
                    logger.warning("⚠️ 检测到需要验证,等待处理...")
                    time.sleep(10)
                
                # 提取内容
                article_data = self.extract_article_content_from_page(page)
                article_data['url'] = article_url
                article_data['extraction_time'] = datetime.now().isoformat()
                
                return article_data
                
            except Exception as e:
                logger.error(f"❌ 浏览器抓取失败: {e}")
                return {
                    'success': False,
                    'error': str(e),
                    'url': article_url,
                    'extraction_time': datetime.now().isoformat()
                }
            finally:
                browser.close()
    
    def extract_article_content_from_page(self, page):
        """从页面提取文章内容(浏览器版本)"""
        try:
            # 各种选择器
            title_selectors = ['#activity-name', '.rich_media_title', 'h1']
            content_selectors = ['#js_content', '.rich_media_content', 'article']
            date_selectors = ['#publish_time', '.publish_time']
            author_selectors = ['#js_name', '.profile_nickname']
            
            # 提取标题
            article_title = "未找到标题"
            for selector in title_selectors:
                try:
                    title_elem = page.query_selector(selector)
                    if title_elem:
                        title_text = title_elem.inner_text().strip()
                        if title_text and len(title_text) > 3:
                            article_title = title_text
                            break
                except:
                    continue
            
            # 提取作者
            author_name = "未知作者"
            for selector in author_selectors:
                try:
                    author_elem = page.query_selector(selector)
                    if author_elem:
                        author_text = author_elem.inner_text().strip()
                        if author_text and len(author_text) > 1:
                            author_name = author_text
                            break
                except:
                    continue
            
            # 提取发布时间
            publish_time = "未知时间"
            for selector in date_selectors:
                try:
                    date_elem = page.query_selector(selector)
                    if date_elem:
                        date_text = date_elem.inner_text().strip()
                        if date_text and len(date_text) > 3:
                            publish_time = date_text
                            break
                except:
                    continue
            
            # 提取内容
            content_elements = []
            images = []
            content_found = False
            
            for selector in content_selectors:
                try:
                    content_elem = page.query_selector(selector)
                    if content_elem:
                        logger.info(f"✅ 使用选择器找到内容: {selector}")
                        
                        # 简化的内容提取
                        # 提取所有文本段落
                        paragraphs = page.query_selector_all(f"{selector} p, {selector} div")
                        for i, para in enumerate(paragraphs):
                            try:
                                text = para.inner_text().strip()
                                if text and len(text) > 10 and '阅读原文' not in text:
                                    content_elements.append({
                                        'type': 'text',
                                        'content': text,
                                        'tag': 'p',
                                        'order': len(content_elements)
                                    })
                            except:
                                continue
                        
                        # 提取所有图片
                        img_elements = page.query_selector_all(f"{selector} img")
                        for img_elem in img_elements:
                            try:
                                img_src = img_elem.get_attribute('src') or img_elem.get_attribute('data-src')
                                if img_src:
                                    if img_src.startswith('//'):
                                        img_src = 'https:' + img_src
                                    elif img_src.startswith('/'):
                                        img_src = 'https://mp.weixin.qq.com' + img_src
                                    
                                    img_info = {
                                        'type': 'image',
                                        'src': img_src,
                                        'alt': img_elem.get_attribute('alt') or '',
                                        'title': img_elem.get_attribute('title') or '',
                                        'order': len(content_elements)
                                    }
                                    content_elements.append(img_info)
                                    images.append(img_info)
                            except:
                                continue
                        
                        if content_elements:
                            content_found = True
                            break
                            
                except Exception as e:
                    logger.warning(f"⚠️ 选择器 {selector} 失败: {e}")
                    continue
            
            # 排序确保正确顺序
            content_elements.sort(key=lambda x: x['order'])
            
            # 统计信息
            text_count = len([elem for elem in content_elements if elem['type'] == 'text'])
            image_count = len([elem for elem in content_elements if elem['type'] == 'image'])
            
            logger.info(f"📝 提取到 {text_count} 个文本段落, {image_count} 张图片")
            
            return {
                'success': True,
                'title': article_title,
                'author': author_name,
                'publish_time': publish_time,
                'content_elements': content_elements,
                'content': [elem['content'] for elem in content_elements if elem['type'] == 'text'],
                'images': images,
                'paragraph_count': text_count,
                'image_count': image_count,
                'total_elements': len(content_elements),
                'error': None,
                'method': 'browser_extraction'
            }
            
        except Exception as e:
            logger.error(f"❌ 内容提取失败: {e}")
            return {
                'success': False,
                'title': "提取失败",
                'author': "未知",
                'publish_time': "未知",
                'content_elements': [],
                'content': [],
                'images': [],
                'paragraph_count': 0,
                'image_count': 0,
                'total_elements': 0,
                'error': str(e),
                'method': 'browser_extraction'
            }
    
    def generate_markdown(self, article_data, downloaded_images=None):
        """生成Markdown内容,使用本地图片URL"""
        markdown_content = f"# {article_data.get('title', '未知标题')}\n\n"
        
        # 创建图片URL映射
        image_url_map = {}
        if downloaded_images:
            for img in downloaded_images:
                image_url_map[img['original_url']] = img['image_url']
        
        # 按原始顺序混合显示文本和图片
        img_counter = 1
        for element in article_data.get('content_elements', []):
            if element['type'] == 'text':
                # 根据标签类型添加适当的markdown格式
                if element['tag'] in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                    level = int(element['tag'][1])
                    markdown_content += f"{'#' * (level + 1)} {element['content']}\n\n"
                else:
                    markdown_content += f"{element['content']}\n\n"
            elif element['type'] == 'image':
                # 使用本地图片URL或原始URL
                img_url = image_url_map.get(element['src'], element['src'])
                markdown_content += f"![图片{img_counter}]({img_url})\n"
                if element['alt']:
                    markdown_content += f"*{element['alt']}*\n"
                markdown_content += "\n"
                img_counter += 1
        
        return markdown_content


# 创建全局实例
tieba_scraper = TiebaPostScraperAPI()
wechat_scraper = WeChatArticleScraperAPI()


# ============================================================================
# Flask路由
# ============================================================================

@app.route('/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'service': 'unified-content-scraper-api',
        'endpoints': {
            'tieba': '/tieba/scrape',
            'wechat': '/weixin/scrape'
        }
    })


@app.route('/tieba/scrape', methods=['POST'])
def scrape_tieba():
    """抓取贴吧帖子接口"""
    try:
        # 获取请求参数
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({
                'success': False,
                'error': 'Missing required parameter: url'
            }), 400
        
        post_url = data['url']
        download_images = data.get('download_images', True)
        
        logger.info(f"🚀 收到贴吧抓取请求: {post_url}")
        
        # 清理旧文件
        tieba_scraper.clean_old_files()
        
        # 验证URL
        if 'tieba.baidu.com' not in post_url:
            return jsonify({
                'success': False,
                'error': 'Invalid Tieba post URL'
            }), 400
        
        # 抓取帖子
        post_data = tieba_scraper.scrape_tieba_post(post_url)
        
        if not post_data.get('success'):
            return jsonify(post_data), 500
        
        # 生成帖子ID
        post_id = post_data.get('post_info', {}).get('post_id', 'unknown')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_title = re.sub(r'[^\w\s-]', '', post_data.get('post_info', {}).get('title', 'post'))
        safe_title = re.sub(r'[-\s]+', '-', safe_title)[:30]
        unique_id = f"{safe_title}_{post_id}_{timestamp}"
        
        # 下载图片
        downloaded_images = []
        if download_images and post_data.get('all_images'):
            logger.info(f"📷 开始下载 {len(post_data['all_images'])} 张图片...")
            downloaded_images = tieba_scraper.download_images(post_data['all_images'], unique_id)
            logger.info(f"✅ 成功下载 {len(downloaded_images)} 张图片")
        
        # 生成Markdown内容(使用本地图片URL)
        main_markdown = post_data.get('main_markdown', '')
        comments_markdown = post_data.get('comments_markdown', '')
        
        # 更新图片链接为本地URL
        if downloaded_images:
            main_markdown = tieba_scraper.update_markdown_with_local_images(
                main_markdown, downloaded_images, unique_id)
            comments_markdown = tieba_scraper.update_markdown_with_local_images(
                comments_markdown, downloaded_images, unique_id)
        
        # 保存帖子JSON到静态目录
        post_filename = f"{unique_id}.json"
        post_filepath = os.path.join(tieba_scraper.posts_dir, post_filename)
        
        # 构建完整的响应数据
        response_data = {
            'success': True,
            'post_id': unique_id,
            'title': post_data.get('post_info', {}).get('title'),
            'forum_name': post_data.get('post_info', {}).get('forum_name'),
            'author': post_data.get('main_post', {}).get('author'),
            'total_replies': post_data.get('total_replies', 0),
            'image_count': len(downloaded_images),
            'total_images': post_data.get('total_images', 0),
            'main_markdown': main_markdown,  # n8n使用的主帖内容
            'comments_markdown': comments_markdown,  # 评论内容
            'extraction_time': datetime.now().isoformat(),
            'method': post_data.get('method'),
            'source_url': post_url,
            'images': []  # 图片访问信息
        }
        
        # 添加图片访问信息 - 使用动态URL
        base_url = request.host_url.rstrip('/')
        for img in downloaded_images:
            response_data['images'].append({
                'filename': img['filename'],
                'url': f"{base_url}{img['image_url']}",  # 动态拼接完整访问URL
                'alt': img['alt'],
                'title': img['title'],
                'watermark_removed': img['watermark_removed']
            })
        
        # 保存完整数据到JSON文件
        full_data = {
            **response_data,
            'post_info': post_data.get('post_info', {}),
            'main_post': post_data.get('main_post', {}),
            'replies': post_data.get('replies', []),
        }
        
        with open(post_filepath, 'w', encoding='utf-8') as f:
            json.dump(full_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"✅ 抓取完成: {post_data.get('post_info', {}).get('title')}")
        logger.info(f"📁 JSON文件: {post_filepath}")
        logger.info(f"📝 主帖Markdown长度: {len(main_markdown)} 字符")
        logger.info(f"📝 评论Markdown长度: {len(comments_markdown)} 字符")
        
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"❌ 抓取失败: {e}")
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500


@app.route('/weixin/scrape', methods=['POST'])
def scrape_wechat():
    """抓取微信文章接口"""
    try:
        # 获取请求参数
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({
                'success': False,
                'error': 'Missing required parameter: url'
            }), 400
        
        article_url = data['url']
        download_images = data.get('download_images', True)
        
        logger.info(f"🚀 收到微信抓取请求: {article_url}")
        
        # 清理旧文件
        wechat_scraper.clean_old_files()
        
        # 验证URL
        if 'mp.weixin.qq.com' not in article_url:
            return jsonify({
                'success': False,
                'error': 'Invalid WeChat article URL'
            }), 400
        
        # 抓取文章
        article_data = wechat_scraper.scrape_wechat_article(article_url)
        
        if not article_data.get('success'):
            return jsonify(article_data), 500
        
        # 生成文章ID(用于文件组织)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_title = re.sub(r'[^\w\s-]', '', article_data.get('title', 'article'))
        safe_title = re.sub(r'[-\s]+', '-', safe_title)[:30]
        article_id = f"{safe_title}_{timestamp}"
        
        # 下载图片
        downloaded_images = []
        if download_images and article_data.get('images'):
            logger.info(f"📷 开始下载 {len(article_data['images'])} 张图片...")
            downloaded_images = wechat_scraper.download_images(article_data['images'], article_id)
            logger.info(f"✅ 成功下载 {len(downloaded_images)} 张图片")
        
        # 生成Markdown内容(使用本地图片URL)
        markdown_content = wechat_scraper.generate_markdown(article_data, downloaded_images)
        
        # 保存文章JSON到静态目录
        article_filename = f"{article_id}.json"
        article_filepath = os.path.join(wechat_scraper.articles_dir, article_filename)
        
        # 构建完整的响应数据
        response_data = {
            'success': True,
            'article_id': article_id,
            'title': article_data.get('title'),
            'author': article_data.get('author'),
            'publish_time': article_data.get('publish_time'),
            'paragraph_count': article_data.get('paragraph_count', 0),
            'image_count': len(downloaded_images),
            'total_elements': article_data.get('total_elements', 0),
            'markdown': markdown_content,  # 这是n8n需要的关键字段
            'extraction_time': datetime.now().isoformat(),
            'method': article_data.get('method'),
            'source_url': article_url,
            'images': []  # 图片访问信息
        }
        
        # 添加图片访问信息 - 使用动态URL
        base_url = request.host_url.rstrip('/')
        for img in downloaded_images:
            response_data['images'].append({
                'filename': img['filename'],
                'url': f"{base_url}{img['image_url']}",  # 动态拼接完整访问URL
                'alt': img['alt'],
                'title': img['title'],
                'watermark_removed': img['watermark_removed']
            })
        
        # 保存完整数据到JSON文件
        full_data = {
            **response_data,
            'content_elements': article_data.get('content_elements', []),
            'raw_content': article_data.get('content', []),
        }
        
        with open(article_filepath, 'w', encoding='utf-8') as f:
            json.dump(full_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"✅ 抓取完成: {article_data.get('title')}")
        logger.info(f"📁 JSON文件: {article_filepath}")
        logger.info(f"📝 Markdown长度: {len(markdown_content)} 字符")
        
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"❌ 抓取失败: {e}")
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500


@app.route('/tieba/images/<path:filename>')
def serve_tieba_image(filename):
    """提供贴吧图片静态文件服务"""
    try:
        return send_from_directory(tieba_scraper.images_dir, filename)
    except Exception as e:
        logger.error(f"❌ 图片服务失败: {e}")
        return jsonify({'error': 'Image not found'}), 404


@app.route('/weixin/images/<path:filename>')
def serve_wechat_image(filename):
    """提供微信图片静态文件服务"""
    try:
        return send_from_directory(wechat_scraper.images_dir, filename)
    except Exception as e:
        logger.error(f"❌ 图片服务失败: {e}")
        return jsonify({'error': 'Image not found'}), 404


@app.route('/tieba/posts/<filename>')
def serve_tieba_post(filename):
    """提供贴吧帖子JSON文件服务"""
    try:
        return send_from_directory(tieba_scraper.posts_dir, filename)
    except Exception as e:
        logger.error(f"❌ 帖子文件服务失败: {e}")
        return jsonify({'error': 'Post not found'}), 404


@app.route('/weixin/articles/<filename>')
def serve_wechat_article(filename):
    """提供微信文章JSON文件服务"""
    try:
        return send_from_directory(wechat_scraper.articles_dir, filename)
    except Exception as e:
        logger.error(f"❌ 文章文件服务失败: {e}")
        return jsonify({'error': 'Article not found'}), 404


@app.route('/tieba/list', methods=['GET'])
def list_tieba_posts():
    """列出所有已抓取的贴吧帖子"""
    try:
        posts = []
        for filename in os.listdir(tieba_scraper.posts_dir):
            if filename.endswith('.json'):
                filepath = os.path.join(tieba_scraper.posts_dir, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        post_data = json.load(f)
                        posts.append({
                            'filename': filename,
                            'post_id': post_data.get('post_id'),
                            'title': post_data.get('title'),
                            'extraction_time': post_data.get('extraction_time'),
                            'image_count': post_data.get('image_count', 0),
                            'total_replies': post_data.get('total_replies', 0)
                        })
                except Exception as e:
                    logger.error(f"读取帖子文件失败 {filename}: {e}")
                    continue
        
        # 按时间倒序排序
        posts.sort(key=lambda x: x.get('extraction_time', ''), reverse=True)
        
        return jsonify({
            'success': True,
            'count': len(posts),
            'posts': posts
        })
        
    except Exception as e:
        logger.error(f"❌ 获取帖子列表失败: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/weixin/list', methods=['GET'])
def list_wechat_articles():
    """列出所有已抓取的微信文章"""
    try:
        articles = []
        for filename in os.listdir(wechat_scraper.articles_dir):
            if filename.endswith('.json'):
                filepath = os.path.join(wechat_scraper.articles_dir, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        article_data = json.load(f)
                        articles.append({
                            'filename': filename,
                            'article_id': article_data.get('article_id'),
                            'title': article_data.get('title'),
                            'author': article_data.get('author'),
                            'extraction_time': article_data.get('extraction_time'),
                            'image_count': article_data.get('image_count', 0),
                            'paragraph_count': article_data.get('paragraph_count', 0)
                        })
                except Exception as e:
                    logger.error(f"读取文章文件失败 {filename}: {e}")
                    continue
        
        # 按时间倒序排序
        articles.sort(key=lambda x: x.get('extraction_time', ''), reverse=True)
        
        return jsonify({
            'success': True,
            'count': len(articles),
            'articles': articles
        })
        
    except Exception as e:
        logger.error(f"❌ 获取文章列表失败: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/clean', methods=['POST'])
def clean_old_files():
    """手动清理旧文件接口"""
    try:
        data = request.get_json() or {}
        max_age_hours = data.get('max_age_hours', 24)
        
        tieba_scraper.clean_old_files(max_age_hours)
        wechat_scraper.clean_old_files(max_age_hours)
        
        return jsonify({
            'success': True,
            'message': f'已清理超过 {max_age_hours} 小时的旧文件',
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"❌ 清理文件失败: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500


def run_server(host='0.0.0.0', port=7000, debug=False):
    """启动Flask服务器"""
    print("🚀 统一内容抓取API服务器")
    print("=" * 60)
    print(f"📡 服务地址: http://{host}:{port}")
    print(f"📁 贴吧静态目录: {tieba_scraper.static_dir}")
    print(f"📁 微信静态目录: {wechat_scraper.static_dir}")
    print("=" * 60)
    print("API接口:")
    print(f"  POST /tieba/scrape         - 抓取贴吧帖子")
    print(f"  POST /weixin/scrape        - 抓取微信文章")
    print(f"  GET  /health               - 健康检查")
    print(f"  GET  /tieba/list           - 贴吧帖子列表")
    print(f"  GET  /weixin/list          - 微信文章列表")
    print(f"  POST /clean                - 清理旧文件")
    print("=" * 60)
    print("n8n调用示例:")
    print(f"  # 贴吧")
    print(f"  POST http://localhost:{port}/tieba/scrape")
    print(f'  Body: {{"url": "https://tieba.baidu.com/p/123456"}}')
    print(f"  # 微信")
    print(f"  POST http://localhost:{port}/weixin/scrape")
    print(f'  Body: {{"url": "https://mp.weixin.qq.com/s/xxx"}}')
    print("=" * 60)
    
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == '__main__':
    import sys
    
    # 解析命令行参数
    host = '0.0.0.0'
    port = int(os.environ.get('PORT', 7000))  # 🔧 修改1: 从环境变量读取端口
    debug = False
    
    for arg in sys.argv[1:]:
        if arg.startswith('--host='):
            host = arg.split('=', 1)[1]
        elif arg.startswith('--port='):
            port = int(arg.split('=', 1)[1])
        elif arg == '--debug':
            debug = True
        elif arg == '--help':
            print("使用方法:")
            print("  python unified_scraper_api.py [选项]")
            print("选项:")
            print("  --host=HOST     服务器地址 (默认: 0.0.0.0)")
            print("  --port=PORT     端口号 (默认: 从环境变量PORT读取,否则7000)")
            print("  --debug         启用调试模式")
            print("  --help          显示帮助信息")
            sys.exit(0)
    
    # 启动服务器
    run_server(host=host, port=port, debug=debug)
