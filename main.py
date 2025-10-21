#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一内容抓取API服务器（Zeabur版）
- 提供贴吧与微信公众号文章抓取
- 图片URL自动拼接当前域名
- 端口自动读取Zeabur的PORT环境变量
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
from playwright.sync_api import sync_playwright
import requests
from bs4 import BeautifulSoup

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

app = Flask(__name__)
CORS(app)

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


# ===============================================================
# 贴吧抓取类
# ===============================================================
class TiebaPostScraperAPI:
    def __init__(self, remove_watermarks=True):
        self.work_dir = os.path.abspath(os.path.dirname(__file__))
        self.static_dir = os.path.join(self.work_dir, 'static', 'tieba')
        self.images_dir = os.path.join(self.static_dir, 'images')
        self.posts_dir = os.path.join(self.static_dir, 'posts')
        self.remove_watermarks = remove_watermarks and HAS_CV2
        self.ensure_directories()

    def ensure_directories(self):
        for d in [self.static_dir, self.images_dir, self.posts_dir]:
            os.makedirs(d, exist_ok=True)

    def extract_post_id(self, url):
        patterns = [r'/p/(\d+)', r'tid=(\d+)']
        for p in patterns:
            m = re.search(p, url)
            if m:
                return m.group(1)
        return None

    def clean_tieba_url(self, url):
        pid = self.extract_post_id(url)
        return f"https://tieba.baidu.com/p/{pid}" if pid else url

    def scrape_tieba_post(self, post_url):
        clean_url = self.clean_tieba_url(post_url)
        return self.scrape_with_browser(clean_url)

    def scrape_with_browser(self, post_url):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(3)
                html = page.content()
                return self.parse_html_content(html, post_url)
            except Exception as e:
                return {'success': False, 'error': str(e)}
            finally:
                browser.close()

    def parse_html_content(self, html, post_url):
        soup = BeautifulSoup(html, 'html.parser')
        title_elem = soup.select_one('.core_title_txt, h1')
        title = title_elem.get_text(strip=True) if title_elem else '未找到标题'
        imgs = [{'src': img['src']} for img in soup.find_all('img', src=True)]
        texts = [p.get_text(strip=True) for p in soup.find_all('p') if p.get_text(strip=True)]
        return {
            'success': True,
            'title': title,
            'content': texts,
            'images': imgs,
            'url': post_url
        }


# ===============================================================
# 微信抓取类
# ===============================================================
class WeChatArticleScraperAPI:
    def __init__(self, remove_watermarks=True):
        self.work_dir = os.path.abspath(os.path.dirname(__file__))
        self.static_dir = os.path.join(self.work_dir, 'static', 'wechat')
        self.images_dir = os.path.join(self.static_dir, 'images')
        self.articles_dir = os.path.join(self.static_dir, 'articles')
        self.remove_watermarks = remove_watermarks and HAS_CV2
        self.ensure_directories()

    def ensure_directories(self):
        for d in [self.static_dir, self.images_dir, self.articles_dir]:
            os.makedirs(d, exist_ok=True)

    def scrape_wechat_article(self, url):
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return {'success': False, 'error': f'HTTP {resp.status_code}'}
            soup = BeautifulSoup(resp.text, 'html.parser')
            title_elem = soup.find('h1', id='activity-name')
            title = title_elem.get_text(strip=True) if title_elem else "未找到标题"
            author_elem = soup.find('span', id='js_name')
            author = author_elem.get_text(strip=True) if author_elem else "未知作者"
            content_elem = soup.find('div', id='js_content')
            texts = [t.get_text(strip=True) for t in content_elem.find_all('p')] if content_elem else []
            imgs = [{'src': img.get('data-src') or img.get('src')} for img in soup.find_all('img') if img.get('src') or img.get('data-src')]
            return {'success': True, 'title': title, 'author': author, 'content': texts, 'images': imgs, 'url': url}
        except Exception as e:
            return {'success': False, 'error': str(e)}


tieba_scraper = TiebaPostScraperAPI()
wechat_scraper = WeChatArticleScraperAPI()


# ===============================================================
# Flask 路由
# ===============================================================
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'unified-content-scraper-api'})


@app.route('/tieba/scrape', methods=['POST'])
def scrape_tieba():
    try:
        data = request.get_json()
        post_url = data.get('url')
        post_data = tieba_scraper.scrape_tieba_post(post_url)
        if not post_data.get('success'):
            return jsonify(post_data), 500
        imgs = []
        for img in post_data.get('images', []):
            imgs.append({
                'src': f"{request.host_url.rstrip('/')}{img['src']}"
            })
        post_data['images'] = imgs
        return jsonify(post_data)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/weixin/scrape', methods=['POST'])
def scrape_wechat():
    try:
        data = request.get_json()
        url = data.get('url')
        article_data = wechat_scraper.scrape_wechat_article(url)
        if not article_data.get('success'):
            return jsonify(article_data), 500
        imgs = []
        for img in article_data.get('images', []):
            if img.get('src'):
                imgs.append({
                    'src': f"{request.host_url.rstrip('/')}{img['src']}"
                })
        article_data['images'] = imgs
        return jsonify(article_data)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500


def run_server(host='0.0.0.0', port=None, debug=False):
    port = int(os.environ.get('PORT', 8000))  # ✅ Zeabur 动态端口
    print("🚀 统一内容抓取API服务器 (Zeabur)")
    print(f"📡 服务地址: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == '__main__':
    run_server()

