import os
import time
from flask import Flask, render_template, request, jsonify
import serial
import serial.tools.list_ports

app = Flask(__name__)

# 直接鎖定你確定的 ttyUSB0
PORT = '/dev/ttyUSB0'
BAUD_RATE = 115200
ser = None

# 初始化 Serial 連線
try:
    print(f"Connecting to Arduino on {PORT} at {BAUD_RATE}...")
    ser = serial.Serial(PORT, BAUD_RATE, timeout=1)
    time.sleep(2)  # 等待 Arduino 重啟初始化
    print("Arduino Connected!")
except Exception as e:
    print(f"Error connecting to Serial: {e}")


def send_command(cmd):
    """送出指令給 Arduino 並讀取回傳值"""
    global ser
    if ser and ser.is_open:
        try:
            # 清空舊緩衝，避免殘留
            ser.reset_input_buffer()
            # 依 Arduino 韌體要求，必須以 \n 結尾
            ser.write(f"{cmd}\n".encode('utf-8'))
            # 讀取一行回傳
            response = ser.readline().decode('utf-8').strip()
            return response
        except Exception as e:
            return f"ERROR: {e}"
    return "SERIAL_DISCONNECTED"

@app.route('/')
def index():
    # 渲染首頁 HTML
    return render_template('index.html')

@app.route('/api/ping', methods=['GET'])
def ping():
    response = send_command("PING")
    return jsonify({"status": "success", "response": response})

@app.route('/api/set_brightness', methods=['POST'])
def set_brightness():
    data = request.json
    value = data.get('value', 0)
    
    # 限制範圍在 0 ~ 255
    v = max(0, min(255, int(value)))
    response = send_command(f"SET:{v}")
    
    return jsonify({
        "status": "success",
        "brightness": v,
        "arduino_response": response
    })

if __name__ == '__main__':
    # 綁定 0.0.0.0 讓同區域網路內的所有電腦都能連線
    app.run(host='0.0.0.0', port=5000)