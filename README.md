# Raspberry Pi Arduino 控制端

Raspberry Pi 側的 Flask 應用：接收來自 PC 控制中心的 HTTP 指令，透過 USB 序列埠轉發給 Arduino 控制 LED。

這個應用**不提供網頁介面**，純粹是 Arduino 的 API 端點。使用者介面在 PC 端（`../pc-led-controller`）。

## 系統架構

```
PC 控制中心 (Flask, port 5001)
        │  HTTP API
        ▼
Raspberry Pi (Flask, port 5000)   ← 你在這裡
        │  Serial over USB (115200)
        ▼
Arduino (analogWrite 控制 LED)
```

---

## 快速開始

### 需要的硬體

- Raspberry Pi（3B+ 或更新）
- Arduino（Nano 或 Uno）
- USB 線連接 Pi 和 Arduino
- Pi 與 PC 在同一個區域網路

### 步驟 1：準備 Pi 環境（每台新的 Pi 第一次做一次）

這一步決定 Docker 容器能不能正常上網。**照順序做完並通過最後的驗證再往下**，可以避免後面 build 時卡在「pip 裝不了套件」的網路坑。

```bash
sudo apt update

# 1a. 安裝 iptables —— Docker 靠它建立容器對外的網路（NAT）規則。
#     Raspberry Pi OS trixie / Debian 13 預設可能只有 nftables、缺 iptables，
#     少了它 Docker 的容器會完全連不出網路。
sudo apt install -y iptables

# 1b. 用官方腳本安裝最新版 Docker。
#     不要用 apt 的 docker.io（trixie 上版本偏舊，nftables 環境容易出容器網路問題），
#     官方腳本裝的是最新穩定版，並附帶正確的 systemd 設定。
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# 1c.（選用）把自己加進 docker 群組，之後不用每個指令都 sudo。登出再登入才生效。
sudo usermod -aG docker $USER

# 1d. 驗證容器網路正常 —— 這步通過再往下。
#     會印出一個 IP（如 151.101.0.223）代表容器能解析並連到外網。
docker run --rm python:3.11-slim python -c "import socket; print(socket.gethostbyname('pypi.org'))"
```

> 若 1d 沒印出 IP、而是 `Temporary failure in name resolution` 或卡住，
> 代表容器網路不通，跳到「故障排除 → 容器連不上網路 / build 裝不了套件」照做，修好再回來。

### 步驟 2：部署應用

```bash
# 2a. 取得程式碼
git clone <這個 repo>
cd pi-arduino-connection-test-0717

# 2b. 建立這台 Pi 的專屬設定
cp .env.example .env
nano .env                 # 至少要改 PI_ID

# 2c. 啟動（第一次會自動 build image）
sudo docker compose up -d

# 2d. 查看日誌（確認 Arduino 有連上）
sudo docker compose logs -f
```

啟動成功的話，日誌會出現：

```
[pi-001] Connecting to Arduino on /dev/ttyUSB0 at 115200...
[pi-001] Arduino Connected!
```

### 替代方案：不用 Docker 直接執行

不想用 Docker 的話，跳過上面步驟 1、2，直接跑：

```bash
pip3 install -r requirements.txt

# 需要序列埠權限，加完要重新登入才生效
sudo usermod -a -G dialout $USER

PI_ID=pi-001 python3 app.py
```

> 直接執行時**不會讀 `.env` 檔**（那是 Docker Compose 的功能）。
> 設定要直接帶在指令前面，或用 `export` 設成環境變數。
> 另外此時連接埠固定是 5000，`HOST_PORT` 只對 Docker 有效。

---

## 設定

**所有設定都集中在 `.env`，不需要改程式碼或 `docker-compose.yml`。**

| 變數 | 說明 | 預設值 |
|---|---|---|
| `PI_ID` | 這台 Pi 的識別碼。多台務必各自不同，且要跟 PC 端新增裝置時填的「裝置 ID」一致 | `pi-001` |
| `SERIAL_PORT` | Arduino 接在哪個序列埠 | `/dev/ttyUSB0` |
| `BAUD_RATE` | 鮑率，必須與 Arduino 韌體 `Serial.begin()` 的數字一致 | `115200` |
| `HOST_PORT` | 對外開放的連接埠，PC 端連這個（**僅 Docker 有效**） | `5000` |

改完重啟即可生效：

```bash
sudo docker compose up -d
```

### 為什麼設定放在 `.env`

`.env` **不進版控**（見 `.gitignore`），所以每台 Pi 有自己的值，而 `docker-compose.yml` 在所有 Pi 上完全相同。這代表：

```bash
git pull && sudo docker compose up -d --build
```

更新程式時**永遠不會跟你這台的設定衝突**。設定若寫死在 compose 檔裡，每台 Pi 都會背著一個不能 commit 的本地修改。

### 找出 Arduino 的序列埠

```bash
ls -l /dev/serial/by-id/
```

- Arduino Nano（CH340 晶片）通常是 `/dev/ttyUSB0`
- Arduino Uno / 原廠 Nano 通常是 `/dev/ttyACM0`

**只需要改 `SERIAL_PORT` 一個地方**——USB 裝置映射（`devices:`）和程式開啟的埠都由它決定，不會有只改到一邊的問題。

---

## Arduino 韌體

Pi 用純文字指令跟 Arduino 溝通，每則指令以 `\n` 結尾：

| 指令 | Arduino 應回傳 |
|---|---|
| `PING` | `OK` |
| `SET:<0-255>` | `SET:<實際設定值>` |

### 範例韌體

```cpp
const int LED_PIN = 9;  // 必須是 PWM 腳位

void setup() {
  Serial.begin(115200);   // 要跟 .env 的 BAUD_RATE 一致
  pinMode(LED_PIN, OUTPUT);
  analogWrite(LED_PIN, 0);
}

void loop() {
  if (Serial.available()) {
    String command = Serial.readStringUntil('\n');
    command.trim();

    if (command == "PING") {
      Serial.println("OK");
    }
    else if (command.startsWith("SET:")) {
      int brightness = constrain(command.substring(4).toInt(), 0, 255);
      analogWrite(LED_PIN, brightness);
      Serial.print("SET:");
      Serial.println(brightness);
    }
    else {
      Serial.println("ERROR: Unknown command");
    }
  }
}
```

> **每則指令都必須回一行**，即使是錯誤也要回。Pi 端會等待回應，
> Arduino 不回話會被當成連線異常（見下方「無回應」）。

---

## API 端點

所有回應都是 JSON，且都含 `pi_id` 欄位。

### `GET /api/status`

回報這台 Pi 與 Arduino 的狀態。PC 端就是靠它來決定顯示哪種燈號。

**這個端點永遠回 200**（只要 Flask 活著），Arduino 的死活看 `arduino_status` 欄位。

```json
{ "status": "success", "pi_id": "pi-001", "arduino_status": "connected" }
```

Arduino 有問題時多帶一個 `arduino_error`，直接告訴你是哪一種：

```json
{
  "status": "success",
  "pi_id": "pi-001",
  "arduino_status": "disconnected",
  "arduino_error": "NO_RESPONSE_FROM_ARDUINO"
}
```

| `arduino_error` | 意義 |
|---|---|
| `SERIAL_DISCONNECTED` | 序列埠開不起來——USB 沒接，或 `SERIAL_PORT` 設錯 |
| `NO_RESPONSE_FROM_ARDUINO` | 埠開得起來但 Arduino 不回話——韌體沒燒錄，或 `BAUD_RATE` 不符 |
| `ERROR: ...` | 讀寫過程出錯，通常是 USB 中途被拔掉 |

> **這個端點會真的送一次 `PING`，而不是只檢查序列埠開得起來。**
> 韌體沒燒錄或 `BAUD_RATE` 設錯時，埠一樣開得成功、Arduino 卻永遠不回話——
> 只檢查 `is_open` 的話 PC 會顯示綠燈，燈卻不會亮。
> 代價是 PC 每次輪詢（預設 5 秒）都會產生一次序列埠往返，對 Arduino 來說微不足道。

### `GET|POST /api/ping`

實際送一則 `PING` 給 Arduino 並等回應。

**成功 200：**
```json
{ "status": "success", "pi_id": "pi-001", "response": "OK" }
```

**Arduino 沒接／沒回應 503：**
```json
{ "status": "error", "pi_id": "pi-001", "error": "SERIAL_DISCONNECTED" }
```

### `POST /api/set_brightness`

```
Content-Type: application/json
{ "value": 128 }
```

`value` 會被夾在 0–255。也接受 form 格式（`value=128`）。

**成功 200：**
```json
{ "status": "success", "pi_id": "pi-001", "brightness": 128, "arduino_response": "SET:128" }
```

**400**：body 不是合法 JSON／form，或 `value` 不是數字
**503**：Arduino 斷線

### `POST /api/command`

直接把字串丟給 Arduino，用於韌體支援的其他指令。

```
Content-Type: application/json
{ "command": "SET:255" }
```

**成功 200：**
```json
{ "status": "success", "pi_id": "pi-001", "command": "SET:255", "arduino_response": "SET:255" }
```

**400**：body 無效，或 `command` 是空的
**503**：Arduino 斷線

### 狀態碼一覽

| 狀態碼 | 意義 |
|---|---|
| `200` | 指令送達 Arduino 並收到回應 |
| `400` | 請求本身有問題（body 無效、參數缺失或格式錯） |
| `503` | Flask 活著，但 Arduino 連不上或沒回應 |

**`503` 是刻意的設計。** Arduino 被拔掉時如果照樣回 200，PC 端會顯示綠燈「連線正常」，但燈根本不會亮——這種「壞掉卻不出聲」的狀況最難 debug。現在 Arduino 一斷，PC 端立刻顯示黃色的「Arduino 未接」。

---

## 部署到多台 Pi

每台的步驟完全一樣，只有 `.env` 內容不同。**每台新 Pi 都要先做一次「步驟 1：準備 Pi 環境」**（裝 iptables + 官方 Docker），再跑下面的部署：

```bash
git clone <這個 repo> && cd pi-arduino-connection-test-0717
cp .env.example .env
nano .env
sudo docker compose up -d
```

**Pi 1** 的 `.env`：
```ini
PI_ID=pi-001
SERIAL_PORT=/dev/ttyUSB0
```

**Pi 2** 的 `.env`（這台插的是 Arduino Uno）：
```ini
PI_ID=pi-002
SERIAL_PORT=/dev/ttyACM0
```

然後在 PC 控制中心新增裝置，**裝置 ID 要跟該台的 `PI_ID` 一致**：

| 裝置 ID | 位址 |
|---|---|
| `pi-001` | `192.168.1.100:5000` |
| `pi-002` | `192.168.1.101:5000` |

### 用預先建好的 image 部署

不想在每台 Pi 上 build 的話：

```bash
# 在開發機上建好並推上去（Pi 是 ARM，平台要指定）
docker buildx build --platform linux/arm64 -t <registry>/pi-arduino:latest --push .

# 每台 Pi 上
docker run -d --name pi-arduino-controller --restart unless-stopped \
  --env-file .env \
  --device /dev/ttyUSB0:/dev/ttyUSB0 \
  -p 5000:5000 \
  <registry>/pi-arduino:latest
```

> ⚠️ `docker run` 的 `--device` **不會**讀 `.env` 裡的 `SERIAL_PORT`，得自己填成一致的值。
> 填錯會變成「程式開 `ttyACM0`、但映射進去的是 `ttyUSB0`」這種很難查的狀況。
> `docker compose` 會自動展開變數，沒有這個問題——所以還是推薦 compose。

`.env` 已列入 `.dockerignore`，不會被打包進 image，設定與 image 是分離的。

---

## 測試

```bash
# 在 Pi 本機
curl http://localhost:5000/api/status
curl http://localhost:5000/api/ping
curl -X POST http://localhost:5000/api/set_brightness \
  -H "Content-Type: application/json" -d '{"value": 100}'

# 從 PC（假設 Pi 是 192.168.1.100）
curl http://192.168.1.100:5000/api/ping
```

---

## 故障排除

### 容器起不來，說找不到裝置

```
error gathering device information ... /dev/ttyUSB0: no such file or directory
```

Docker 在**啟動前**就要找到 `devices:` 指定的裝置。確認：

```bash
ls -la /dev/tty*          # Arduino 真的在嗎？是 USB0 還是 ACM0？
```

對不上就改 `.env` 的 `SERIAL_PORT`，然後 `sudo docker compose up -d`。

### 日誌顯示 `Error connecting to Serial: Permission denied`

Docker 方式應該不會遇到（容器內是 root）。直接執行時：

```bash
sudo usermod -a -G dialout $USER
# 重新登入才會生效
```

### API 回 503 / PC 端顯示「🟡 Arduino 未接」

代表 Flask 正常但序列埠不通。依序檢查：

1. `lsusb` 和 `ls -la /dev/tty*` — USB 線接好了嗎？
2. `.env` 的 `SERIAL_PORT` 跟實際的裝置對得上嗎？
3. `.env` 的 `BAUD_RATE` 跟韌體的 `Serial.begin()` 一致嗎？
4. 韌體真的有燒錄，而且會回應 `PING` 嗎？

手動測序列埠：

```bash
sudo apt install -y screen
sudo screen /dev/ttyUSB0 115200
# 打 PING 然後按 Enter，應該看到 OK
# 離開：Ctrl+A 然後按 K
```

> USB 中途被拔掉不需要重啟容器——程式會自動嘗試重連（每 5 秒最多一次），插回去就會恢復。

### 回應是 `NO_RESPONSE_FROM_ARDUINO`

序列埠開得起來，但 Arduino 沒回話——**這是「線接好了、但軟體那端不對」的訊號**。通常是：

- 韌體沒燒錄，或韌體沒有處理 `PING`
- `BAUD_RATE` 跟韌體的 `Serial.begin()` 不一致（能開埠，但收到的是亂碼）
- 韌體收到指令卻沒有 `Serial.println()` 回應

對照 `SERIAL_DISCONNECTED`（埠根本開不起來，多半是 USB 沒接或 `SERIAL_PORT` 設錯），
這兩個錯誤指向完全不同的地方，不要搞混。

### PC 連不到這台 Pi

```bash
hostname -I                 # 確認 Pi 的 IP
sudo ufw status             # 防火牆有擋嗎
sudo ufw allow 5000
```

### 容器連不上網路 / build 裝不了套件

**症狀**——`docker compose build` 卡在 `pip install`，retry 五次後失敗：

```
WARNING: Retrying ... [Errno -3] Temporary failure in name resolution
ERROR: No matching distribution found for Flask==2.3.3
```

照「步驟 1」裝好環境的新 Pi 不會遇到。**會遇到的多半是之前用舊方式(`apt install docker.io`)裝過、環境沒收乾淨的機器**。這不是 DNS 設定或程式碼問題，而是**容器連不出網路**——`Temporary failure in name resolution` 只是表象，容器連 DNS 查詢的封包都送不出去。

**先分辨是「容器網路」還是「純 DNS」**（`python:3.11-slim` build 時就會下載，測試很快）：

```bash
# 測 1:容器能不能把域名解析成 IP
docker run --rm python:3.11-slim python -c "import socket; print(socket.gethostbyname('pypi.org'))"

# 測 2:繞過 DNS,直接用 IP 連(分辨是 NAT/路由 還是 DNS)
docker run --rm python:3.11-slim python -c "import socket; socket.create_connection(('151.101.0.223',443),5); print('IP OK')"
```

- 測 2 **timeout** → 容器封包出不去，是 **NAT/路由問題**，用下面 A 修。
- 測 2 印出 `IP OK`、只有測 1 失敗 → 是**純 DNS 問題**，用下面 B 修。

#### A. NAT 壞掉:`iptables` 缺失 → dockerd 帶著 `--iptables=false` 啟動

**因果鏈**（Raspberry Pi OS trixie / Debian 13 實際踩過）：系統只有 `nftables`、缺 `iptables` 套件 → Docker 少了它會啟動失敗，於是有人在 systemd unit 加上 `--iptables=false` 硬讓它能開機 → 但這代表 **Docker 不建立容器的對外 NAT(MASQUERADE)規則** → 容器封包出不去。（`docker pull` 卻正常，因為 pull 是 daemon 走**主機網路**、不經過容器 NAT——這就是「主機能上網、容器不行」的來源。）

**確認：**

```bash
apt-cache policy iptables                  # 壞掉時是 Installed/Candidate: (none)
ps aux | grep dockerd | grep -v grep       # 看 dockerd 是否帶著 --iptables=false
sudo iptables -t nat -L POSTROUTING -n     # 壞掉時 POSTROUTING 是空的,沒有 MASQUERADE
cat /proc/sys/net/ipv4/ip_forward          # 要是 1;若為 0:sudo sysctl -w net.ipv4.ip_forward=1
```

**修復（依序執行）：**

```bash
# 1. 修 apt 索引(iptables 顯示 no candidate 時多半是索引損壞)並裝回 iptables
sudo rm -rf /var/lib/apt/lists/*
sudo apt-get update
sudo apt-get install -y iptables
apt-cache policy iptables                  # 確認 Candidate 有版本了(如 1.8.11-2)

# 2. 升級 Docker 到官方最新版(舊版 27.x 在 trixie/nftables 上問題較多)
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# 3. 移除人為加的 --iptables=false,讓官方預設 unit 接手
sudo mv /etc/systemd/system/docker.service /etc/systemd/system/docker.service.disabled

# 4. 重載 + 重啟 daemon(同時換上新引擎、重讀 iptables、重建 NAT 規則)
sudo systemctl daemon-reload
sudo systemctl restart docker
```

> 若第 3 步 `/etc/systemd/system/docker.service` 是你要保留的自訂設定（不是只為了那個 flag），
> 就別整份移走，改用手術式移除參數：
> ```bash
> sudo sed -i 's/ --iptables=false//g' /etc/systemd/system/docker.service
> sudo systemctl daemon-reload && sudo systemctl restart docker
> ```

**驗證：**

```bash
docker version                                       # Server > Engine > Version 應為 29.x
ps aux | grep dockerd | grep -v grep                 # dockerd 後面不再有 --iptables=false
sudo iptables -t nat -L POSTROUTING -n               # 這次會出現 MASQUERADE 那行
docker run --rm python:3.11-slim python -c "import socket; print(socket.gethostbyname('pypi.org'))"
```

最後印出 IP 就代表通了，重新 `docker compose build --no-cache && docker compose up -d`。

#### B. 純 DNS 問題(公司/內網環境)

容器的上游 DNS 是從主機 `/etc/resolv.conf` 抄的。若測 2 是 `IP OK`、只有解析不到，在 `/etc/docker/daemon.json` 指定內網 DNS：

```bash
cat /etc/resolv.conf                 # 先看主機用的內網 DNS,例:172.17.112.6 / 172.19.31.1
sudo nano /etc/docker/daemon.json
```

```json
{
  "dns": ["172.17.112.6", "172.19.31.1"]
}
```

```bash
sudo systemctl restart docker
```

> ⚠️ 內網環境**不要**填 `8.8.8.8`(Google DNS),公司防火牆常會擋外部 DNS,反而更慘。填主機 `/etc/resolv.conf` 裡實際在用的那組。

---

## 設計備註

幾個不明顯但重要的實作決定：

- **序列埠有加鎖**：Flask 預設多執行緒，同時進來的請求會搶同一個序列埠。沒有鎖的話「清緩衝 → 寫入 → 讀回應」三步會交錯，A 請求讀到 B 的回應。實測 20 個並發請求在無鎖時**全部**錯亂。
- **斷線回 503 而非 200**：理由見上方「狀態碼一覽」。
- **序列埠會自動重連**：連線失敗不會讓 Flask 掛掉，之後每 5 秒最多重試一次（避免每個請求都卡在重連上）。
- **`request.get_json(silent=True)` 而非 `request.json`**：Flask 2.3 的 `request.json` 在 Content-Type 不是 JSON 時會直接拋 415，`request.json or request.form` 這種寫法的 form 分支永遠輪不到執行。

---

## 相關文件

- **Raspberry Pi 端（你在這裡）**：`./app.py`
- **PC 控制中心**：`../pc-led-controller/`

## 許可證

MIT
