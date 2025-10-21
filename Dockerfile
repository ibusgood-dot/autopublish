# 使用官方 Playwright Python 基础镜像（包含浏览器 + 依赖）
FROM mcr.microsoft.com/playwright/python:v1.54.0-noble

WORKDIR /app

# 复制并安装项目依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/


# 复制应用代码
COPY main.py .


# 初始化历史文件
RUN echo '{"latest_two_articles": []}' > latest_two_articles.json

# 暴露端口
EXPOSE 8002

# 启动应用
CMD ["python", "main.py"]
