FROM python:3.10-slim

WORKDIR /app

# 安裝依賴
RUN pip install --no-cache-dir flask pyserial

# 複製程式碼
COPY . .

EXPOSE 5000

CMD ["python", "app.py"]