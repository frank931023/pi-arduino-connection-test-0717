"""
Raspberry Pi 端 - 接收 PC 指令並控制 Arduino

主要作為 Arduino 控制端點：等待來自 PC 控制中心的 API 請求，再把指令轉發給 Arduino。
另外在 / 提供一個本機網頁（templates/index.html），方便單獨檢查或操作這一台 Pi。

Arduino 韌體協定(天車/人工嘴控制韌體):
  - 每個指令的回應是多行純文字,一定以 `DONE` 或 `ERR: <原指令>` 收尾,
    中間可能安靜好幾秒(校正動作是阻塞的),不能用「多久沒回應」判斷指令結束。
  - 韌體會不定期主動插入心跳行 `HB,<millis>,<IDLE|BUSY>`,任何時候都可能出現
    (包含指令回應正中間),必須濾掉,不算進回應內容也不能當結尾訊號。
  - 指令逾時後,遲到的 DONE/ERR 可能還在路上,下一個指令送出前必須先讀到那個
    結尾訊號出現,否則後面每個回應都會錯開一格。

沒接 Arduino 也能啟動：序列埠開不起來時 Flask 照常服務，/api/status 回報
arduino_status=disconnected，接上後下一個請求自動重連（docker-compose 掛的是整個 /dev，
所以插拔、重新編號都不需要重建容器）。

⚠ 狀態都在這個程序的記憶體裡（序列埠握把、指令鎖），只能跑單一程序：
  gunicorn 必須是 1 個 worker（用 --threads 增加並行），見 Dockerfile。
"""
import os
import re
import threading
import time

from flask import Flask, jsonify, render_template, request
import serial

import commands

app = Flask(__name__)

PORT = os.getenv('SERIAL_PORT', '/dev/ttyUSB0')
BAUD_RATE = int(os.getenv('BAUD_RATE', '115200'))
PI_ID = os.getenv('PI_ID', 'pi-001')  # 用於識別不同的 Raspberry Pi
APP_VERSION = os.getenv('APP_VERSION', 'dev')  # image build 時寫入（git commit），PC 端顯示用
# 單一指令最長容許耗時。真機校正是低速模式,走完整個行程單趟可能超過 30 秒,
# 所以預設值要比模擬版(數秒)寬鬆很多,個別請求仍可用 `timeout` 欄位覆蓋。
COMMAND_TIMEOUT = float(os.getenv('COMMAND_TIMEOUT', '130'))
# 請求可指定的 timeout 上限。PC 端從 /api/status 讀這兩個值，不自己寫死。
MAX_COMMAND_TIMEOUT = float(os.getenv('MAX_COMMAND_TIMEOUT', '300'))
# /api/status 與 /api/ping 送 `?` 的等待上限。要短：等待期間序列埠被佔住，指令得排隊。
PROBE_TIMEOUT = 3.0
MAX_COMMAND_LENGTH = 64
# 附在靜態檔網址後面：更新後瀏覽器一定拿新的 JS/CSS，不會用舊快取呼叫改過的 API
ASSET_VERSION = str(int(time.time()))

ser = None
# Flask 是多執行緒的，序列埠一次只能有一個請求在用
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

# 正在執行的指令（不論來自 PC 或本機網頁）。同一時間只允許一個：
#   - 第二個指令立刻回 409，而不是在序列埠鎖上排隊到逾時
#   - /api/status 看到有指令在跑就直接回 busy，不去搶序列埠（否則會等到逾時，被 PC 誤判離線）
_job_lock = threading.Lock()
_job = None  # {"command": str, "started": monotonic}


def _begin_job(cmd):
    global _job
    with _job_lock:
        if _job is not None:
            return False
        _job = {"command": cmd, "started": time.monotonic()}
        return True


def _end_job():
    global _job
    with _job_lock:
        _job = None


def current_job():
    """正在跑的指令 {"command", "seconds"}，沒有則 None。"""
    with _job_lock:
        if _job is None:
            return None
        return {"command": _job["command"], "seconds": round(time.monotonic() - _job["started"], 1)}


def _record_heartbeat(line):
    """解析 HB,<millis>,<IDLE|BUSY>,順便偵測 millis 是否倒退(代表 Arduino 重開過)。"""
    m = _HB_RE.match(line)
    if not m:
        return
    millis = int(m.group(1))
    prev = _last_heartbeat["millis"]
    if prev is not None and millis < prev:
        print(f"[{PI_ID}] 偵測到 Arduino uptime 倒退 ({prev}ms -> {millis}ms)，可能已重開機", flush=True)
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
        print(f"[{PI_ID}] Connecting to Arduino on {PORT} at {BAUD_RATE}...", flush=True)
        ser = serial.Serial(PORT, BAUD_RATE, timeout=1)
        time.sleep(3)  # 開啟序列埠會觸發 DTR 重開機，等開機流程跑完
        ser.reset_input_buffer()  # 這時還沒有指令在跑，可以放心把開機雜訊(如 READY)清掉
        _desynced = False
        print(f"[{PI_ID}] Arduino Connected!", flush=True)
        return True
    except Exception as e:
        print(f"[{PI_ID}] Arduino not available on {PORT}: {e} (will retry on the next request)", flush=True)
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
                # 上一個指令逾時了，它的 DONE/ERR 可能還在路上。
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
    if isinstance(data, dict):
        return data
    if request.form:
        return request.form
    return None


def parse_timeout(data):
    """回傳 (timeout 或 None, 錯誤訊息)。None = 用預設的 COMMAND_TIMEOUT。"""
    raw = data.get('timeout')
    if raw in (None, ''):
        return None, None
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return None, f"Invalid timeout value: {raw!r}"
    if not 0 < timeout <= MAX_COMMAND_TIMEOUT:
        return None, f"Timeout must be more than 0 and at most {MAX_COMMAND_TIMEOUT:g} seconds"
    return timeout, None


def ok(**data):
    return jsonify({"status": "success", "pi_id": PI_ID, **data})


def fail(message, http_status, **data):
    return jsonify({"status": "error", "pi_id": PI_ID, "error": message, **data}), http_status


def busy_response(job):
    return fail("BUSY", 409, running=job)


# ===== 本機網頁 =====

@app.route('/')
def index():
    """單台 Pi 的本機網頁（英文介面、風格同 PC 控制中心）。日常操作請用 PC 控制中心。"""
    return render_template('index.html', pi_id=PI_ID, serial_port=PORT, baud_rate=BAUD_RATE,
                           command_timeout=COMMAND_TIMEOUT, app_version=APP_VERSION,
                           v=ASSET_VERSION)


# ===== API 端點 =====

@app.route('/api/health', methods=['GET'])
def health():
    """程序存活檢查（Docker healthcheck / Ansible 用）。不碰序列埠，所以永遠很快。"""
    return ok(app_version=APP_VERSION)


@app.route('/api/commands', methods=['GET'])
def command_catalog():
    """韌體指令表，PC 控制中心與本機網頁都用它產生按鈕（見 commands.py）。"""
    return ok(**commands.catalog(COMMAND_TIMEOUT))


@app.route('/api/status', methods=['GET'])
def status():
    """
    回傳此 Pi 的狀態。PC 端靠這個決定要顯示哪種燈號。

    這裡會真的送一次 `?`(查詢天車名稱)，而不是只檢查序列埠開得起來——韌體沒
    燒錄或 BAUD_RATE 設錯時，埠一樣開得成功但 Arduino 永遠不回話。只看 is_open
    的話 PC 會顯示綠燈，燈卻不會亮。

    有指令正在跑時不送 `?`，直接回 arduino_status=busy 和正在跑的指令。

    無論 Arduino 死活都回 200：這是「狀態回報」端點，Flask 活著就算成功，
    Arduino 的狀態放在 arduino_status 欄位。
    """
    body = {
        "app_version": APP_VERSION,
        "command_timeout": COMMAND_TIMEOUT,
        "max_command_timeout": MAX_COMMAND_TIMEOUT,
    }

    job = current_job()
    if job:
        body.update(arduino_status="busy", running=job)
    else:
        connected, result = send_command("?", timeout=PROBE_TIMEOUT)
        if connected:
            body.update(arduino_status="connected",
                        rig_name=result["lines"][0] if result["lines"] else None)
        else:
            # 方便直接看出是沒接、還是沒回話
            body.update(arduino_status="disconnected", arduino_error=result)

    # 心跳只在指令執行期間剛好收到才會更新，所以可能是舊的或根本沒有
    if _last_heartbeat["millis"] is not None:
        body["heartbeat"] = {
            "millis": _last_heartbeat["millis"],
            "arduino_state": _last_heartbeat["arduino_state"],
            "seconds_ago": round(time.monotonic() - _last_heartbeat["received_at"], 1),
        }
    return ok(**body)


@app.route('/api/ping', methods=['GET', 'POST'])
def ping():
    """測試 Arduino 連線，送 `?` 查詢天車名稱作為存活確認"""
    job = current_job()
    if job:
        return busy_response(job)

    connected, result = send_command("?", timeout=PROBE_TIMEOUT)
    if not connected:
        return fail(result, 503)
    return ok(rig_name=result["lines"][0] if result["lines"] else None,
              arduino_response=result["lines"])


@app.route('/api/command', methods=['POST'])
def command():
    """
    接收自訂命令直接發給 Arduino，例如 {"command": "SXM,500", "timeout": 40}。

    韌體的完整協定(多行回應、DONE/ERR 收尾、心跳過濾)見檔案開頭註解。
    `timeout`(秒，選填，上限 MAX_COMMAND_TIMEOUT)覆蓋預設的 COMMAND_TIMEOUT。
    同一時間只跑一個指令，前一個還沒結束時回 409。
    """
    data = request_payload()
    if data is None:
        return fail("Invalid or missing request body", 400)

    cmd = str(data.get('command', '')).strip()
    if not cmd:
        return fail("Missing command", 400)
    if len(cmd) > MAX_COMMAND_LENGTH or any(c in cmd for c in '\r\n'):
        # 指令後面會補 \n 原樣寫進 Serial：夾帶換行等於一次送兩個指令，
        # 這裡卻只等一個 DONE，之後的回應就全部錯位。
        return fail(f"Command must be one line, at most {MAX_COMMAND_LENGTH} characters", 400)

    timeout, error = parse_timeout(data)
    if error:
        return fail(error, 400)

    if not _begin_job(cmd):
        return busy_response(current_job())
    try:
        connected, result = send_command(cmd, timeout=timeout)
    finally:
        _end_job()

    if not connected:
        return fail(result, 503, command=cmd)
    return ok(command=cmd,
              arduino_result=result["terminal"],   # "DONE" 或 "ERR"(韌體看不懂這個指令)
              arduino_response=result["lines"])    # 完整多行回應，逐行陣列


# 啟動時先試連一次，讓 log 立刻顯示 Arduino 狀態（失敗也不擋 Flask 啟動）
open_serial()

if __name__ == '__main__':
    # 開發用；正式環境（Docker）由 gunicorn 啟動，見 Dockerfile
    print(f"[{PI_ID}] Starting Flask server...", flush=True)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
