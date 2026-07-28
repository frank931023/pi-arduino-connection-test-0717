"""
Raspberry Pi 端 - 接收 PC 指令並控制 Arduino

此應用只作為 Arduino 控制端點，不提供網頁介面。
等待來自 PC 的 POST API 請求，然後將指令轉發給 Arduino。

Arduino 韌體協定(天車/人工嘴控制韌體):
  - 每個指令的回應是多行純文字,一定以 `DONE` 或 `ERR: <原指令>` 收尾,
    中間可能安靜好幾秒(校正動作是阻塞的),不能用「多久沒回應」判斷指令結束。
  - 韌體會不定期主動插入心跳行 `HB,<millis>,<IDLE|BUSY>`,任何時候都可能出現
    (包含指令回應正中間),必須濾掉,不算進回應內容也不能當結尾訊號。
  - 指令逾時後,遲到的 DONE/ERR 可能還在路上,下一個指令送出前必須先讀到那個
    結尾訊號出現,否則後面每個回應都會錯開一格。
"""
import os
import re
import threading
import time
from flask import Flask, request, jsonify
import serial

app = Flask(__name__)

PORT = os.getenv('SERIAL_PORT', '/dev/ttyUSB0')
BAUD_RATE = int(os.getenv('BAUD_RATE', '115200'))
PI_ID = os.getenv('PI_ID', 'pi-001')  # 用於識別不同的 Raspberry Pi
# 單一指令最長容許耗時。真機校正是低速模式,走完整個行程單趟可能超過 30 秒,
# 所以預設值要比模擬版(數秒)寬鬆很多,個別請求仍可用 `timeout` 欄位覆蓋。
COMMAND_TIMEOUT = float(os.getenv('COMMAND_TIMEOUT', '130'))

ser = None
# Flask 預設 threaded=True，序列埠一次只能有一個請求在用
_serial_lock = threading.Lock()
_last_connect_attempt = 0.0
RECONNECT_INTERVAL = 5.0  # 秒；避免每個請求都重試而拖慢回應

# 上一個指令逾時、沒讀到 DONE/ERR 就是 desync 狀態:那個遲到的結尾訊號
# 可能還在路上,必須在下一個指令送出前先讀掉,否則回應永遠錯位。
_desynced = False

# 從心跳行順便得知的最新狀態,只在指令執行期間剛好收到心跳時才會更新
# (這裡沒有背景執行緒持續讀取,閒置時的心跳不會被消耗到)。
_last_heartbeat = {"millis": None, "arduino_state": None, "received_at": None}

_HB_RE = re.compile(r'^HB,(-?\d+),(IDLE|BUSY)$')


def _record_heartbeat(line):
    """解析 HB,<millis>,<IDLE|BUSY>,順便偵測 millis 是否倒退(代表 Arduino 重開過)。"""
    m = _HB_RE.match(line)
    if not m:
        return
    millis = int(m.group(1))
    prev = _last_heartbeat["millis"]
    if prev is not None and millis < prev:
        print(f"[{PI_ID}] 偵測到 Arduino uptime 倒退 ({prev}ms -> {millis}ms)，可能已重開機")
    _last_heartbeat["millis"] = millis
    _last_heartbeat["arduino_state"] = m.group(2)
    _last_heartbeat["received_at"] = time.monotonic()


def _connect_serial():
    """建立 Serial 連線。呼叫端必須持有 _serial_lock。"""
    global ser, _last_connect_attempt, _desynced

    if ser and ser.is_open:
        return True

    now = time.monotonic()
    if now - _last_connect_attempt < RECONNECT_INTERVAL:
        return False
    _last_connect_attempt = now

    try:
        print(f"[{PI_ID}] Connecting to Arduino on {PORT} at {BAUD_RATE}...")
        ser = serial.Serial(PORT, BAUD_RATE, timeout=1)
        time.sleep(3)  # 開啟序列埠會觸發 DTR 重開機，等開機流程跑完
        ser.reset_input_buffer()  # 這時還沒有指令在跑，可以放心把開機雜訊(如 READY)清掉
        _desynced = False
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


def _read_until_terminal(deadline):
    """
    持續讀行直到出現 `DONE` 或 `ERR:` 開頭的結尾行，或超過 deadline。
    心跳行(HB,...)會被濾掉，不算進回傳的 lines，也不能當結尾訊號。

    呼叫端必須持有 _serial_lock。回傳 (reached_terminal, lines, terminal)。
    """
    lines = []
    while time.monotonic() < deadline:
        raw = ser.readline()  # ser.timeout 很短(1 秒)，讀不到東西就回來讓外層檢查 deadline
        if not raw:
            continue
        line = raw.decode('utf-8', errors='replace').strip()
        if not line:
            continue
        if line.startswith('HB,'):
            _record_heartbeat(line)
            continue
        lines.append(line)
        if line == 'DONE' or line.startswith('ERR:'):
            return True, lines, ('DONE' if line == 'DONE' else 'ERR')
    return False, lines, None


def send_command(cmd, timeout=None):
    """
    送出指令給 Arduino，讀到 `DONE` / `ERR:` 收尾才算完成。

    回傳 (ok, result)：
      ok=False -> result 是錯誤原因字串，呼叫端應回 503
                  (PC 端只看 HTTP status 判斷 Pi/Arduino 是否可用)。
      ok=True  -> result 是 {"lines": [...], "terminal": "DONE"|"ERR"}。
                  terminal 是韌體自己回報的結果，不是連線狀態——
                  "ERR" 代表韌體看不懂這個指令，連線本身仍是好的，所以 ok 仍是 True。
    """
    global ser, _desynced

    timeout = COMMAND_TIMEOUT if timeout is None else timeout

    with _serial_lock:
        if not _connect_serial():
            return False, "SERIAL_DISCONNECTED"

        try:
            if _desynced:
                # 上一個指令逾時了，牠的 DONE/ERR 可能還在路上。
                # 送新指令前要先讀到那個遲到的結尾訊號，下面讀到的才會是這次指令的回應。
                resynced, _, _ = _read_until_terminal(time.monotonic() + timeout)
                if not resynced:
                    return False, "DESYNC_RECOVERY_TIMEOUT"
                _desynced = False

            # 依 Arduino 韌體要求，必須以 \n 結尾
            ser.write(f"{cmd}\n".encode('utf-8'))
            reached, lines, terminal = _read_until_terminal(time.monotonic() + timeout)
        except Exception as e:
            # USB 被拔掉之類的情況：關掉握把，下次請求才會重新連線
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            return False, f"ERROR: {e}"

    if not reached:
        _desynced = True
        return False, "TIMEOUT_NO_TERMINAL"

    return True, {"lines": lines, "terminal": terminal}


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

    這裡會真的送一次 `?`(查詢天車名稱)，而不是只檢查序列埠開得起來——韌體沒
    燒錄或 BAUD_RATE 設錯時，埠一樣開得成功但 Arduino 永遠不回話。只看 is_open
    的話 PC 會顯示綠燈，燈卻不會亮。

    無論 Arduino 死活都回 200：這是「狀態回報」端點，Flask 活著就算成功，
    Arduino 的狀態放在 arduino_status 欄位。
    """
    ok, result = send_command("?")
    body = {
        "status": "success",
        "pi_id": PI_ID,
        "arduino_status": "connected" if ok else "disconnected"
    }
    if ok:
        body["rig_name"] = result["lines"][0] if result["lines"] else None
    else:
        body["arduino_error"] = result  # 方便直接看出是沒接、還是沒回話

    # 心跳只在指令執行期間剛好收到才會更新，所以可能是舊的或根本沒有
    if _last_heartbeat["millis"] is not None:
        body["heartbeat"] = {
            "millis": _last_heartbeat["millis"],
            "arduino_state": _last_heartbeat["arduino_state"],
            "seconds_ago": round(time.monotonic() - _last_heartbeat["received_at"], 1),
        }
    return jsonify(body)


@app.route('/api/ping', methods=['GET', 'POST'])
def ping():
    """測試 Arduino 連線，送 `?` 查詢天車名稱作為存活確認"""
    ok, result = send_command("?")
    if not ok:
        return jsonify({
            "status": "error",
            "pi_id": PI_ID,
            "error": result
        }), 503

    return jsonify({
        "status": "success",
        "pi_id": PI_ID,
        "rig_name": result["lines"][0] if result["lines"] else None,
        "arduino_response": result["lines"],
    })


@app.route('/api/command', methods=['POST'])
def command():
    """
    接收自訂命令直接發給 Arduino，例如 {"command": "SXM,500"}。

    韌體的完整協定(多行回應、DONE/ERR 收尾、心跳過濾)見檔案開頭註解。
    可選的 `timeout` 欄位(秒)覆蓋預設的 COMMAND_TIMEOUT，校正類指令在真機上
    可能需要拉長到 30 秒以上。
    """
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

    timeout = None
    if 'timeout' in data and data.get('timeout') not in (None, ''):
        try:
            timeout = float(data.get('timeout'))
        except (TypeError, ValueError):
            return jsonify({
                "status": "error",
                "pi_id": PI_ID,
                "error": f"Invalid timeout value: {data.get('timeout')!r}"
            }), 400

    ok, result = send_command(cmd, timeout=timeout)
    if not ok:
        return jsonify({
            "status": "error",
            "pi_id": PI_ID,
            "command": cmd,
            "error": result
        }), 503

    return jsonify({
        "status": "success",
        "pi_id": PI_ID,
        "command": cmd,
        "arduino_result": result["terminal"],   # "DONE" 或 "ERR"(韌體看不懂這個指令)
        "arduino_response": result["lines"],     # 完整多行回應，逐行陣列
    })


# 啟動時先試連一次，讓 log 立刻顯示 Arduino 狀態（失敗也不擋 Flask 啟動）
open_serial()

if __name__ == '__main__':
    # 綁定 0.0.0.0 讓 PC 可以在網路上連線
    print(f"[{PI_ID}] Starting Flask server...")
    app.run(host='0.0.0.0', port=5000, debug=False)
