"""
Raspberry Pi 端 - 接收 PC 指令並控制 Arduino

此應用只作為 Arduino 控制端點，不提供網頁介面。
等待來自 PC 的 POST API 請求，然後將指令轉發給 Arduino。
"""
import os
import threading
import time
from flask import Flask, request, jsonify
import serial

app = Flask(__name__)

PORT = os.getenv('SERIAL_PORT', '/dev/ttyUSB0')
BAUD_RATE = int(os.getenv('BAUD_RATE', '115200'))
PI_ID = os.getenv('PI_ID', 'pi-001')  # 用於識別不同的 Raspberry Pi

ser = None
# Flask 預設 threaded=True，序列埠一次只能有一個請求在用
_serial_lock = threading.Lock()
_last_connect_attempt = 0.0
RECONNECT_INTERVAL = 5.0  # 秒；避免每個請求都重試而拖慢回應


def _connect_serial():
    """建立 Serial 連線。呼叫端必須持有 _serial_lock。"""
    global ser, _last_connect_attempt

    if ser and ser.is_open:
        return True

    now = time.monotonic()
    if now - _last_connect_attempt < RECONNECT_INTERVAL:
        return False
    _last_connect_attempt = now

    try:
        print(f"[{PI_ID}] Connecting to Arduino on {PORT} at {BAUD_RATE}...")
        ser = serial.Serial(PORT, BAUD_RATE, timeout=1)
        time.sleep(2)  # 等待 Arduino 重啟初始化
        print(f"[{PI_ID}] Arduino Connected!")
        return True
    except Exception as e:
        print(f"[{PI_ID}] Error connecting to Serial: {e}")
        ser = None
        return False


def open_serial():
    """只確認序列埠開得起來，不代表 Arduino 會回話"""
    with _serial_lock:
        return _connect_serial()


def send_command(cmd):
    """
    送出指令給 Arduino 並讀取回傳值。

    回傳 (ok, response)。ok 為 False 時 response 是錯誤原因，
    呼叫端應回 503 而非 200 —— PC 端只看 HTTP status 來判斷 Pi 是否可用。
    """
    global ser

    with _serial_lock:
        if not _connect_serial():
            return False, "SERIAL_DISCONNECTED"

        try:
            # 清空舊緩衝，避免殘留
            ser.reset_input_buffer()
            # 依 Arduino 韌體要求，必須以 \n 結尾
            ser.write(f"{cmd}\n".encode('utf-8'))
            # 讀取一行回傳
            response = ser.readline().decode('utf-8', errors='replace').strip()
        except Exception as e:
            # USB 被拔掉之類的情況：關掉握把，下次請求才會重新連線
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            return False, f"ERROR: {e}"

    if not response:
        return False, "NO_RESPONSE_FROM_ARDUINO"
    return True, response


def request_payload():
    """
    取得請求內容，支援 JSON 與 form。

    Flask 2.3 的 request.json 在 Content-Type 不是 application/json 時會拋 415，
    所以不能寫成 `request.json or request.form`。

    回傳 None 代表 body 缺少或不是合法 JSON——要跟「格式對但欄位沒填」分開回報，
    否則編碼壞掉的請求會得到 "Missing command" 這種找錯方向的訊息。
    """
    data = request.get_json(silent=True)
    if data is not None:
        return data
    if request.form:
        return request.form
    return None


# ===== API 端點 =====

@app.route('/api/status', methods=['GET'])
def status():
    """
    回傳此 Pi 的狀態。PC 端靠這個決定要顯示哪種燈號。

    這裡會真的送一次 PING，而不是只檢查序列埠開得起來——韌體沒燒錄或
    BAUD_RATE 設錯時，埠一樣開得成功但 Arduino 永遠不回話。只看 is_open
    的話 PC 會顯示綠燈，燈卻不會亮。

    無論 Arduino 死活都回 200：這是「狀態回報」端點，Flask 活著就算成功，
    Arduino 的狀態放在 arduino_status 欄位。
    """
    ok, response = send_command("PING")
    body = {
        "status": "success",
        "pi_id": PI_ID,
        "arduino_status": "connected" if ok else "disconnected"
    }
    if not ok:
        body["arduino_error"] = response  # 方便直接看出是沒接、還是沒回話
    return jsonify(body)


@app.route('/api/ping', methods=['GET', 'POST'])
def ping():
    """測試 Arduino 連線"""
    ok, response = send_command("PING")
    if not ok:
        return jsonify({
            "status": "error",
            "pi_id": PI_ID,
            "error": response
        }), 503

    return jsonify({
        "status": "success",
        "pi_id": PI_ID,
        "response": response
    })


@app.route('/api/set_brightness', methods=['POST'])
def set_brightness():
    """接收亮度指令 (0-255)"""
    data = request_payload()
    if data is None:
        return jsonify({
            "status": "error",
            "pi_id": PI_ID,
            "error": "Invalid or missing request body"
        }), 400

    try:
        # 限制範圍在 0 ~ 255
        v = max(0, min(255, int(data.get('value', 0))))
    except (TypeError, ValueError):
        return jsonify({
            "status": "error",
            "pi_id": PI_ID,
            "error": f"Invalid brightness value: {data.get('value')!r}"
        }), 400

    ok, response = send_command(f"SET:{v}")
    if not ok:
        return jsonify({
            "status": "error",
            "pi_id": PI_ID,
            "error": response
        }), 503

    return jsonify({
        "status": "success",
        "pi_id": PI_ID,
        "brightness": v,
        "arduino_response": response
    })


@app.route('/api/command', methods=['POST'])
def command():
    """接收自訂命令直接發給 Arduino"""
    data = request_payload()
    if data is None:
        return jsonify({
            "status": "error",
            "pi_id": PI_ID,
            "error": "Invalid or missing request body"
        }), 400

    cmd = str(data.get('command', '')).strip()

    if not cmd:
        return jsonify({
            "status": "error",
            "pi_id": PI_ID,
            "error": "Missing command"
        }), 400

    ok, response = send_command(cmd)
    if not ok:
        return jsonify({
            "status": "error",
            "pi_id": PI_ID,
            "command": cmd,
            "error": response
        }), 503

    return jsonify({
        "status": "success",
        "pi_id": PI_ID,
        "command": cmd,
        "arduino_response": response
    })


# 啟動時先試連一次，讓 log 立刻顯示 Arduino 狀態（失敗也不擋 Flask 啟動）
open_serial()

if __name__ == '__main__':
    # 綁定 0.0.0.0 讓 PC 可以在網路上連線
    print(f"[{PI_ID}] Starting Flask server...")
    app.run(host='0.0.0.0', port=5000, debug=False)
