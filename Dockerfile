FROM python:3.11-slim

WORKDIR /app

# 複製依賴檔
COPY requirements.txt .

# 安裝依賴
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼
COPY . .

EXPOSE 5000

CMD ["python", "app.py"]