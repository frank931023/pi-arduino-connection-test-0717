FROM python:3.11-slim

WORKDIR /app

# print() 立刻出現在 docker compose logs，不被緩衝
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 版本號（CI 帶入 git commit），透過 /api/status 回報給 PC 控制中心
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

EXPOSE 5000

# /api/health 不碰序列埠：沒接 Arduino 時容器仍是 healthy（Flask 本身沒問題）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=4)"

# 必須是 1 個 worker：序列埠握把與「同時只跑一個指令」的鎖都在程序記憶體裡，
# 多個 worker 會各自開序列埠、各自上鎖。並行靠 threads（gthread 的長請求不會被 --timeout 砍掉）。
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:5000", "app:app"]
