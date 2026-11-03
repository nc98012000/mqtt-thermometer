"""MQTT 感測器溫度計 GUI 監控程式 (Thermometer GUI Monitor)

使用 Tkinter Canvas 自行繪製擬真水銀溫度計，
即時訂閱 MQTT 接收 sensor-01、sensor-02、sensor-03 的遙測數據與連線狀態。

執行方式：
  python thermometer_gui.py
"""
import io
import json
import math
import os
import queue
import sys
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk
from datetime import datetime

# Windows 控制台 UTF-8 設定
if sys.platform == 'win32':
    try:
        if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding.lower() != 'utf-8':
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
        if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding.lower() != 'utf-8':
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
    except (AttributeError, ValueError):
        pass

import paho.mqtt.client as mqtt

from config import BROKER_HOST, BROKER_PORT, TOPIC_PREFIX, ALL_DEVICES as DEVICES
from xiaomi_decrypt_receiver import parse_bthome_v2, DEFAULT_MAC

# 溫度計規格與警戒值
SCALE_MIN_C = -10.0
SCALE_MAX_C = 50.0
TEMP_ALARM = 33.0

# 介面色彩配置 (Dark Mode Palette)
COLOR_BG = "#0d1117"
COLOR_CARD_BG = "#161b22"
COLOR_CARD_BORDER = "#30363d"
COLOR_CARD_ALARM = "#f85149"
COLOR_TEXT_MAIN = "#f0f6fc"
COLOR_TEXT_MUTED = "#8b949e"
COLOR_TEXT_DIM = "#484f58"

# 溫度動態顏色
COLOR_COLD = "#58a6ff"       # < 20°C
COLOR_NORMAL = "#3fb950"     # 20°C ~ 28°C
COLOR_WARM = "#d29922"       # 28°C ~ 33°C
COLOR_ALARM = "#f85149"      # >= 33°C


class ThermometerCanvas(tk.Canvas):
    """自訂 Canvas 擬真水銀溫度計元件"""

    def __init__(self, parent, width=110, height=270, **kwargs):
        super().__init__(parent, width=width, height=height, bg=COLOR_CARD_BG,
                         highlightthickness=0, **kwargs)
        self.canvas_width = width
        self.canvas_height = height

        # 玻璃管與儲液球幾何參數
        self.stem_x = 75
        self.stem_top = 22
        self.stem_bottom = 210
        self.stem_width = 16
        self.bulb_center_x = self.stem_x
        self.bulb_center_y = 232
        self.bulb_radius = 20

        self.current_temp = 20.0
        self.is_alarm = False
        self.is_online = True
        self.draw()

    def set_data(self, temp, is_alarm, is_online):
        self.current_temp = temp if temp is not None else 0.0
        self.is_alarm = is_alarm
        self.is_online = is_online
        self.draw()

    def temp_to_y(self, temp):
        """將溫度值對應至 Canvas 的 Y 座標"""
        clamped = max(SCALE_MIN_C, min(SCALE_MAX_C, temp))
        ratio = (clamped - SCALE_MIN_C) / (SCALE_MAX_C - SCALE_MIN_C)
        # 溫度越高，Y 越小（越接近頂端）
        return self.stem_bottom - ratio * (self.stem_bottom - self.stem_top)

    def get_temp_color(self, temp, is_alarm):
        if is_alarm or temp >= TEMP_ALARM:
            return COLOR_ALARM
        elif temp >= 28.0:
            return COLOR_WARM
        elif temp >= 20.0:
            return COLOR_NORMAL
        else:
            return COLOR_COLD

    def draw(self):
        self.delete("all")

        # 1. 繪製刻度線與數值標籤 (左側)
        mark_temps = [50, 40, 30, 20, 10, 0, -10]
        for t in mark_temps:
            y = self.temp_to_y(t)
            # 刻度數字
            self.create_text(
                self.stem_x - 30, y,
                text=f"{t:2d}°",
                font=("JetBrains Mono", 8),
                fill=COLOR_TEXT_MUTED,
                anchor="e"
            )
            # 長刻度線
            self.create_line(
                self.stem_x - 22, y, self.stem_x - 12, y,
                fill="#484f58", width=1.5
            )

        # 繪製 5°C 短刻度
        sub_temps = [45, 35, 25, 15, 5, -5]
        for t in sub_temps:
            y = self.temp_to_y(t)
            self.create_line(
                self.stem_x - 18, y, self.stem_x - 12, y,
                fill="#30363d", width=1
            )

        # 繪製 33°C 告警警戒線 (紅色虛線)
        alarm_y = self.temp_to_y(TEMP_ALARM)
        self.create_line(
            self.stem_x - 26, alarm_y, self.stem_x + 18, alarm_y,
            fill=COLOR_ALARM, width=1.5, dash=(3, 2)
        )
        self.create_text(
            self.stem_x - 30, alarm_y,
            text="33°⚠",
            font=("JetBrains Mono", 8, "bold"),
            fill=COLOR_ALARM,
            anchor="e"
        )

        # 2. 玻璃管身外殼陰影與背景軌道
        stem_half = self.stem_width / 2
        tube_left = self.stem_x - stem_half
        tube_right = self.stem_x + stem_half

        # 玻璃管外框 (圓頂長柱形)
        self.create_oval(
            tube_left, self.stem_top - stem_half,
            tube_right, self.stem_top + stem_half,
            fill="#090d13", outline="#30363d", width=1.5
        )
        self.create_rectangle(
            tube_left, self.stem_top,
            tube_right, self.stem_bottom,
            fill="#090d13", outline="#30363d", width=1.5
        )

        # 3. 水銀儲液球外框 (Bottom Bulb)
        bx = self.bulb_center_x
        by = self.bulb_center_y
        br = self.bulb_radius
        self.create_oval(
            bx - br, by - br, bx + br, by + br,
            fill="#090d13", outline="#30363d", width=1.5
        )

        # 4. 液柱色彩判定
        if not self.is_online:
            fill_color = "#484f58"
        else:
            fill_color = self.get_temp_color(self.current_temp, self.is_alarm)

        # 5. 水銀液柱繪製
        liquid_y = self.temp_to_y(self.current_temp)
        liquid_margin = 2.5
        liq_left = tube_left + liquid_margin
        liq_right = tube_right - liquid_margin

        # 彎月頂 (液體表面圓弧)
        if liquid_y < self.stem_bottom:
            self.create_oval(
                liq_left, liquid_y - (self.stem_width - 2 * liquid_margin) / 2,
                liq_right, liquid_y + (self.stem_width - 2 * liquid_margin) / 2,
                fill=fill_color, outline=""
            )
            self.create_rectangle(
                liq_left, liquid_y,
                liq_right, self.stem_bottom + 5,
                fill=fill_color, outline=""
            )

        # 6. 水銀儲液球內部液體
        inner_br = br - 3
        self.create_oval(
            bx - inner_br, by - inner_br, bx + inner_br, by + inner_br,
            fill=fill_color, outline=""
        )

        # 7. 玻璃反光質感 (立體反射高光)
        # 管身高光線
        self.create_line(
            tube_left + 3, self.stem_top,
            tube_left + 3, self.stem_bottom,
            fill="#ffffff", width=1.2
        )
        # 球部弧形高光
        self.create_arc(
            bx - inner_br + 3, by - inner_br + 3,
            bx + inner_br - 3, by + inner_br - 3,
            start=115, extent=65, style="arc",
            outline="#ffffff", width=2
        )


class SensorCard(tk.Frame):
    """單一感測器卡片元件"""

    def __init__(self, parent, device_id, **kwargs):
        super().__init__(parent, bg=COLOR_CARD_BG, bd=1, relief="solid",
                         highlightbackground=COLOR_CARD_BORDER, highlightthickness=1, **kwargs)
        self.device_id = device_id
        self.last_temp = None
        self.max_temp = -999.0
        self.min_temp = 999.0

        self.setup_ui()

    def setup_ui(self):
        self.grid_columnconfigure(1, weight=1)

        # 頂部狀態標題列
        header_frame = tk.Frame(self, bg=COLOR_CARD_BG)
        header_frame.pack(fill="x", padx=16, pady=(14, 8))

        self.lbl_dot = tk.Label(header_frame, text="●", font=("Arial", 12),
                                fg="#3fb950", bg=COLOR_CARD_BG)
        self.lbl_dot.pack(side="left", padx=(0, 6))

        self.lbl_name = tk.Label(header_frame, text=self.device_id,
                                 font=("JetBrains Mono", 12, "bold"),
                                 fg=COLOR_TEXT_MAIN, bg=COLOR_CARD_BG)
        self.lbl_name.pack(side="left")

        self.lbl_status = tk.Label(header_frame, text="ONLINE",
                                   font=("JetBrains Mono", 8, "bold"),
                                   fg="#3fb950", bg="#1c2d20",
                                   padx=6, pady=2)
        self.lbl_status.pack(side="right")

        # 告警提示條 (預設隱藏)
        self.lbl_alarm = tk.Label(self, text="🔥 高溫告警 (> 33°C)",
                                  font=("Microsoft JhengHei", 9, "bold"),
                                  fg="#f85149", bg="#3d1418",
                                  padx=8, pady=3)
        # 先不 pack，有 alarm 時才 pack

        # 主體內容區 (左邊溫度計，右邊遙測數據)
        body_frame = tk.Frame(self, bg=COLOR_CARD_BG)
        body_frame.pack(fill="both", expand=True, padx=14, pady=10)

        # 左邊溫度計
        self.thermo = ThermometerCanvas(body_frame)
        self.thermo.pack(side="left", padx=(0, 10))

        # 右邊數據資訊欄
        info_frame = tk.Frame(body_frame, bg=COLOR_CARD_BG)
        info_frame.pack(side="left", fill="both", expand=True, pady=6)

        # 數位溫度顯示
        lbl_temp_title = tk.Label(info_frame, text="實時溫度",
                                  font=("Microsoft JhengHei", 9),
                                  fg=COLOR_TEXT_MUTED, bg=COLOR_CARD_BG)
        lbl_temp_title.pack(anchor="w")

        self.lbl_temp_val = tk.Label(info_frame, text="-- °C",
                                     font=("JetBrains Mono", 24, "bold"),
                                     fg=COLOR_COLD, bg=COLOR_CARD_BG)
        self.lbl_temp_val.pack(anchor="w", pady=(0, 4))

        # 極值統計
        self.lbl_extremes = tk.Label(info_frame, text="最高: --  最低: --",
                                     font=("JetBrains Mono", 8),
                                     fg=COLOR_TEXT_MUTED, bg=COLOR_CARD_BG)
        self.lbl_extremes.pack(anchor="w", pady=(0, 12))

        # 濕度卡
        hum_box = tk.Frame(info_frame, bg="#0d1117", bd=1, relief="flat", padx=8, pady=6)
        hum_box.pack(fill="x", pady=4)
        tk.Label(hum_box, text="💧 相對濕度", font=("Microsoft JhengHei", 8),
                 fg=COLOR_TEXT_MUTED, bg="#0d1117").pack(anchor="w")
        self.lbl_hum = tk.Label(hum_box, text="-- %",
                                font=("JetBrains Mono", 12, "bold"),
                                fg="#58a6ff", bg="#0d1117")
        self.lbl_hum.pack(anchor="w")

        # 電壓卡
        volt_box = tk.Frame(info_frame, bg="#0d1117", bd=1, relief="flat", padx=8, pady=6)
        volt_box.pack(fill="x", pady=4)
        tk.Label(volt_box, text="⚡ 工作電壓", font=("Microsoft JhengHei", 8),
                 fg=COLOR_TEXT_MUTED, bg="#0d1117").pack(anchor="w")
        self.lbl_volt = tk.Label(volt_box, text="-- V",
                                 font=("JetBrains Mono", 12, "bold"),
                                 fg="#d2a8ff", bg="#0d1117")
        self.lbl_volt.pack(anchor="w")

        # 底部狀態資訊 (序號與時間)
        footer_frame = tk.Frame(self, bg=COLOR_CARD_BG)
        footer_frame.pack(fill="x", padx=16, pady=(6, 12))

        self.lbl_seq = tk.Label(footer_frame, text="Seq: #--",
                                font=("JetBrains Mono", 8),
                                fg=COLOR_TEXT_DIM, bg=COLOR_CARD_BG)
        self.lbl_seq.pack(side="left")

        self.lbl_time = tk.Label(footer_frame, text="等待封包...",
                                 font=("JetBrains Mono", 8),
                                 fg=COLOR_TEXT_DIM, bg=COLOR_CARD_BG)
        self.lbl_time.pack(side="right")

    def update_telemetry(self, data):
        temp = data.get("temperature")
        hum = data.get("humidity")
        volt = data.get("voltage")
        alarm = bool(data.get("alarm"))
        seq = data.get("seq")
        ts = data.get("ts", "")

        if temp is not None:
            self.last_temp = temp
            if temp > self.max_temp: self.max_temp = temp
            if temp < self.min_temp: self.min_temp = temp

            # 依溫度設定色彩
            if alarm or temp >= TEMP_ALARM:
                color = COLOR_ALARM
            elif temp >= 28.0:
                color = COLOR_WARM
            elif temp >= 20.0:
                color = COLOR_NORMAL
            else:
                color = COLOR_COLD

            self.lbl_temp_val.config(text=f"{temp:.1f} °C", fg=color)
            self.lbl_extremes.config(text=f"最高:{self.max_temp:.1f}°C  最低:{self.min_temp:.1f}°C")

            # 更新水銀溫度計
            self.thermo.set_data(temp, alarm, is_online=True)

        if hum is not None:
            self.lbl_hum.config(text=f"{hum:.1f} %")

        if volt is not None:
            self.lbl_volt.config(text=f"{volt:.3f} V")

        if seq is not None:
            self.lbl_seq.config(text=f"Seq: #{seq}")

        if ts:
            time_part = ts.split("T")[1][:8] if "T" in ts else ts
            self.lbl_time.config(text=f"更新: {time_part}")

        # 告警提示條切換與邊框效果
        if alarm:
            if not self.lbl_alarm.winfo_ismapped():
                self.lbl_alarm.pack(fill="x", padx=16, pady=(0, 6), before=self.lbl_dot.master)
            self.config(highlightbackground=COLOR_CARD_ALARM, highlightthickness=2)
        else:
            if self.lbl_alarm.winfo_ismapped():
                self.lbl_alarm.pack_forget()
            self.config(highlightbackground=COLOR_CARD_BORDER, highlightthickness=1)

    def set_online_status(self, is_online):
        if is_online:
            self.lbl_dot.config(fg="#3fb950")
            self.lbl_status.config(text="ONLINE", fg="#3fb950", bg="#1c2d20")
        else:
            self.lbl_dot.config(fg="#8b949e")
            self.lbl_status.config(text="OFFLINE", fg="#8b949e", bg="#21262d")
        if self.last_temp is not None:
            self.thermo.set_data(self.last_temp, False, is_online=is_online)


class ThermometerDashboardApp(tk.Tk):
    """主應用程式視窗"""

    def __init__(self):
        super().__init__()
        self.title("MQTT 感測器溫度計即時監控 (Sensor Dashboard)")
        self.geometry("1100x560")
        self.minsize(980, 520)
        self.configure(bg=COLOR_BG)

        # 佇列用於從 MQTT 線程傳遞訊息至 Tkinter 主線程
        self.msg_queue = queue.Queue()

        self.cards = {}
        self.setup_ui()
        self.start_mqtt_client()
        self.process_queue()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def setup_ui(self):
        # 頂部導航列
        nav_frame = tk.Frame(self, bg="#161b22", height=60)
        nav_frame.pack(fill="x", side="top")

        title_box = tk.Frame(nav_frame, bg="#161b22")
        title_box.pack(side="left", padx=20, pady=12)

        tk.Label(title_box, text="🌡️ MQTT 感測器溫度監控中心",
                 font=("Microsoft JhengHei", 14, "bold"),
                 fg=COLOR_TEXT_MAIN, bg="#161b22").pack(anchor="w")

        tk.Label(title_box, text="3 裝置水銀柱溫度計與實時狀態遙測",
                 font=("Microsoft JhengHei", 9),
                 fg=COLOR_TEXT_MUTED, bg="#161b22").pack(anchor="w")

        # 頂部連線狀態徽章
        status_box = tk.Frame(nav_frame, bg="#161b22")
        status_box.pack(side="right", padx=20)

        self.lbl_mqtt_status = tk.Label(status_box, text="● 連線中...",
                                        font=("JetBrains Mono", 9, "bold"),
                                        fg="#d29922", bg="#2a2010",
                                        padx=10, pady=4)
        self.lbl_mqtt_status.pack(side="right", padx=8)

        broker_info = f"Broker: {BROKER_HOST} | Topic: {TOPIC_PREFIX}/#"
        tk.Label(status_box, text=broker_info,
                 font=("JetBrains Mono", 8),
                 fg=COLOR_TEXT_MUTED, bg="#161b22").pack(side="right", padx=8)

        # 感測器卡片容器 (水平並列所有裝置卡片)
        cards_container = tk.Frame(self, bg=COLOR_BG)
        cards_container.pack(fill="both", expand=True, padx=20, pady=20)

        for i, dev in enumerate(DEVICES):
            card = SensorCard(cards_container, device_id=dev)
            card.pack(side="left", fill="both", expand=True, padx=10)
            self.cards[dev] = card

        # 底部狀態列
        bottom_frame = tk.Frame(self, bg=COLOR_BG)
        bottom_frame.pack(fill="x", side="bottom", padx=20, pady=(0, 10))

        self.lbl_footer = tk.Label(bottom_frame,
                                   text="提示：溫度超過 33.0°C 自動觸發警戒標記 | 支援正弦波平滑動畫",
                                   font=("Microsoft JhengHei", 8),
                                   fg=COLOR_TEXT_DIM, bg=COLOR_BG)
        self.lbl_footer.pack(side="left")

        self.lbl_packet_counter = tk.Label(bottom_frame, text="封包數: 0",
                                           font=("JetBrains Mono", 8),
                                           fg=COLOR_TEXT_DIM, bg=COLOR_BG)
        self.lbl_packet_counter.pack(side="right")
        self.total_packets = 0
        self.xiaomi_state = {"temperature": None, "humidity": None, "voltage": None, "battery": None}

    def start_mqtt_client(self):
        """在背景線程啟動 MQTT 訂閱客戶端"""
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"tk-dash-{uuid.uuid4().hex[:8]}"
        )
        self.client.on_connect = self.on_mqtt_connect
        self.client.on_disconnect = self.on_mqtt_disconnect
        self.client.on_message = self.on_mqtt_message

        def run_loop():
            try:
                self.client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
                self.client.loop_forever()
            except Exception as e:
                print(f"MQTT 連線失敗: {e}")

        self.mqtt_thread = threading.Thread(target=run_loop, daemon=True)
        self.mqtt_thread.start()

    def on_mqtt_connect(self, client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            client.subscribe([(f"{TOPIC_PREFIX}/#", 1), ("ble/+/raw/+", 0)])
            self.msg_queue.put(("system", "connected"))
        else:
            self.msg_queue.put(("system", f"failed: {reason_code}"))

    def on_mqtt_disconnect(self, client, userdata, flags, reason_code, properties):
        self.msg_queue.put(("system", "disconnected"))

    def on_mqtt_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace")
        self.msg_queue.put(("message", topic, payload))

    def process_queue(self):
        """定期檢查佇列，更新 UI (Thread-safe)"""
        try:
            while not self.msg_queue.empty():
                item = self.msg_queue.get_nowait()
                mtype = item[0]

                if mtype == "system":
                    status = item[1]
                    if status == "connected":
                        self.lbl_mqtt_status.config(text="● 已連線", fg="#3fb950", bg="#1c2d20")
                    else:
                        self.lbl_mqtt_status.config(text="● 已斷線", fg="#f85149", bg="#3d1418")

                elif mtype == "message":
                    topic, payload = item[1], item[2]
                    self.total_packets += 1
                    self.lbl_packet_counter.config(text=f"封包數: {self.total_packets}")

                    # 1. 攔截閘道器直接廣播 (單次發布模式，本機直接解析 BTHome V2)
                    if topic.startswith("ble/") and "/raw/" in topic:
                        parts = topic.split("/")
                        mac_clean = parts[-1].replace(":", "").upper()
                        target_mac = DEFAULT_MAC.replace(":", "").upper()
                        if mac_clean == target_mac and "xiaomi-01" in self.cards:
                            try:
                                raw_json = json.loads(payload)
                                updated = False
                                for s in raw_json.get("service_data", []):
                                    if "fcd2" not in s.get("uuid", "").lower():
                                        continue
                                    m = parse_bthome_v2(bytes.fromhex(s.get("data", "")))
                                    if m.get("encrypted"):
                                        continue
                                    for k in self.xiaomi_state:
                                        if k in m:
                                            self.xiaomi_state[k] = m[k]
                                            updated = True
                                if updated and self.xiaomi_state["temperature"] is not None:
                                    now_str = datetime.now().isoformat(timespec="milliseconds")
                                    telem_data = {
                                        "device": "xiaomi-01",
                                        "seq": raw_json.get("rssi"),
                                        "ts": now_str,
                                        "temperature": self.xiaomi_state["temperature"],
                                        "humidity": self.xiaomi_state["humidity"],
                                        "voltage": self.xiaomi_state["voltage"],
                                        "battery": self.xiaomi_state["battery"],
                                        "alarm": self.xiaomi_state["temperature"] > TEMP_ALARM
                                    }
                                    self.cards["xiaomi-01"].set_online_status(True)
                                    self.cards["xiaomi-01"].update_telemetry(telem_data)
                            except Exception:
                                pass
                        continue

                    # 2. 原本的 Topic 解析模式
                    parts = topic.split("/")
                    if len(parts) >= 3:
                        dev = parts[-2]
                        sub = parts[-1]
                        if dev in self.cards:
                            if sub == "status":
                                self.cards[dev].set_online_status(payload.strip() == "online")
                            elif sub == "telemetry":
                                try:
                                    data = json.loads(payload)
                                    self.cards[dev].update_telemetry(data)
                                except Exception:
                                    pass
        finally:
            self.after(50, self.process_queue)

    def on_close(self):
        try:
            self.client.disconnect()
        except Exception:
            pass
        self.destroy()


def main():
    app = ThermometerDashboardApp()
    app.mainloop()


if __name__ == "__main__":
    main()
