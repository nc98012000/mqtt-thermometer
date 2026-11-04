/**
 * MQTT 感測器溫度計儀表板 (Sensor Thermometer Dashboard)
 * 負責連接 EMQX 公開 MQTT WebSocket 伺服器並即時渲染水銀柱溫度計
 */

(function () {
  'use strict';

  // --- 預設與自訂設定參數 ---
  const DEFAULT_CONFIG = {
    brokerHost: 'broker.emqx.io',
    topicPrefix: 'pspasia/test1',
    gatewayTopic: 'ble/+/raw/+',
    xiaomiMac: 'A4:C1:38:4A:09:DE'
  };
  const CONFIG_STORAGE_KEY = 'mqtt_dashboard_custom_config_v1';
  let currentConfig = loadStoredConfig();

  const MQTT_WS_PATH = '/mqtt';
  const DEVICES = ['sensor-01', 'sensor-02', 'sensor-03', 'xiaomi-01'];
  const TEMP_ALARM_THRESHOLD = 33.0; // 攝氏 33 度告警線

  function loadStoredConfig() {
    try {
      const saved = localStorage.getItem(CONFIG_STORAGE_KEY);
      if (saved) {
        return Object.assign({}, DEFAULT_CONFIG, JSON.parse(saved));
      }
    } catch (e) {
      console.warn('載入自訂設定失敗，使用預設值', e);
    }
    return Object.assign({}, DEFAULT_CONFIG);
  }

  function saveStoredConfig(cfg) {
    currentConfig = Object.assign({}, DEFAULT_CONFIG, cfg);
    try {
      localStorage.setItem(CONFIG_STORAGE_KEY, JSON.stringify(currentConfig));
    } catch (e) {
      console.warn('儲存設定到 localStorage 失敗', e);
    }
  }

  // 溫度計刻度範圍 (-10°C ~ 50°C)
  const SCALE_MIN_C = -10.0;
  const SCALE_MAX_C = 50.0;

  // --- 全域狀態 ---
  let isFahrenheit = false;
  let soundEnabled = true;
  let totalPackets = 0;
  let client = null;
  let audioCtx = null;

  // 感測器數據緩存
  const sensorsState = {};
  DEVICES.forEach(devId => {
    sensorsState[devId] = {
      temp: null,
      humidity: null,
      voltage: null,
      alarm: false,
      online: true,
      seq: null,
      ts: null,
      lastReceived: null,
      minTemp: null,
      maxTemp: null,
      history: [] // [{ time, temp, alarm }]
    };
  });

  // --- 音效提示 (Web Audio API) ---
  function playAlarmBeep() {
    if (!soundEnabled) return;
    try {
      if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (audioCtx.state === 'suspended') {
        audioCtx.resume();
      }
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(880, audioCtx.currentTime); // A5
      osc.frequency.exponentialRampToValueAtTime(440, audioCtx.currentTime + 0.25);
      gain.gain.setValueAtTime(0.08, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.25);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start();
      osc.stop(audioCtx.currentTime + 0.26);
    } catch (e) {
      console.warn('Audio play failed:', e);
    }
  }

  // --- 溫標轉換 ---
  function formatTemp(tempC) {
    if (tempC === null || tempC === undefined || isNaN(tempC)) return '--';
    if (isFahrenheit) {
      const f = (tempC * 9 / 5) + 32;
      return f.toFixed(1);
    }
    return tempC.toFixed(1);
  }

  function getUnitStr() {
    return isFahrenheit ? '°F' : '°C';
  }

  // 計算溫度在刻度管中的百分比高度 (-10°C ~ 50°C)
  function calcLiquidHeightPercent(tempC) {
    if (tempC === null || isNaN(tempC)) return 0;
    const clamped = Math.max(SCALE_MIN_C, Math.min(SCALE_MAX_C, tempC));
    const percent = ((clamped - SCALE_MIN_C) / (SCALE_MAX_C - SCALE_MIN_C)) * 100;
    return Math.max(2, Math.min(100, percent));
  }

  // 取得溫度對應的液柱色彩配置
  function getLiquidColorStyle(tempC, isAlarm) {
    if (isAlarm || tempC >= TEMP_ALARM_THRESHOLD) {
      return {
        gradient: 'linear-gradient(180deg, #f43f5e 0%, #e11d48 100%)',
        bulb: 'radial-gradient(circle at 35% 35%, #fda4af 0%, #f43f5e 60%, #be123c 100%)',
        glow: 'rgba(244, 63, 94, 0.65)',
        text: '#f43f5e'
      };
    } else if (tempC >= 28.0) {
      return {
        gradient: 'linear-gradient(180deg, #fbbf24 0%, #d97706 100%)',
        bulb: 'radial-gradient(circle at 35% 35%, #fde68a 0%, #fbbf24 60%, #b45309 100%)',
        glow: 'rgba(251, 191, 36, 0.55)',
        text: '#fbbf24'
      };
    } else if (tempC >= 20.0) {
      return {
        gradient: 'linear-gradient(180deg, #34d399 0%, #059669 100%)',
        bulb: 'radial-gradient(circle at 35% 35%, #a7f3d0 0%, #34d399 60%, #047857 100%)',
        glow: 'rgba(52, 211, 153, 0.55)',
        text: '#34d399'
      };
    } else {
      return {
        gradient: 'linear-gradient(180deg, #38bdf8 0%, #0284c7 100%)',
        bulb: 'radial-gradient(circle at 35% 35%, #bae6fd 0%, #38bdf8 60%, #0369a1 100%)',
        glow: 'rgba(56, 189, 248, 0.55)',
        text: '#38bdf8'
      };
    }
  }

  // --- 更新單一感測器 UI ---
  function updateSensorCard(devId) {
    const s = sensorsState[devId];
    const cardEl = document.getElementById(`card-${devId}`);
    if (!cardEl) return;

    // 1. 溫度柱高度與顏色
    const liquidEl = document.getElementById(`liquid-${devId}`);
    const bulbEl = document.getElementById(`bulb-${devId}`);
    const thermoWrapper = document.getElementById(`thermo-wrapper-${devId}`);
    const tempValEl = document.getElementById(`temp-val-${devId}`);
    const tempUnitEl = document.getElementById(`temp-unit-${devId}`);
    const alarmBannerEl = document.getElementById(`alarm-banner-${devId}`);
    const maxTEl = document.getElementById(`max-t-${devId}`);
    const minTEl = document.getElementById(`min-t-${devId}`);
    const humValEl = document.getElementById(`hum-val-${devId}`);
    const humBarEl = document.getElementById(`hum-bar-${devId}`);
    const voltValEl = document.getElementById(`volt-val-${devId}`);
    const voltBarEl = document.getElementById(`volt-bar-${devId}`);
    const seqEl = document.getElementById(`seq-${devId}`);
    const timeEl = document.getElementById(`time-${devId}`);
    const statusTagEl = document.getElementById(`status-tag-${devId}`);
    const pulseDotEl = document.getElementById(`dot-${devId}`);

    // 連線狀態標籤
    if (s.online) {
      statusTagEl.className = 'status-tag online';
      statusTagEl.textContent = 'ONLINE';
      pulseDotEl.style.backgroundColor = '#10b981';
      pulseDotEl.style.boxShadow = '0 0 10px #10b981';
    } else {
      statusTagEl.className = 'status-tag offline';
      statusTagEl.textContent = 'OFFLINE';
      pulseDotEl.style.backgroundColor = '#64748b';
      pulseDotEl.style.boxShadow = 'none';
    }

    if (s.temp !== null) {
      const heightPercent = calcLiquidHeightPercent(s.temp);
      liquidEl.style.height = `${heightPercent}%`;

      const colors = getLiquidColorStyle(s.temp, s.alarm);
      liquidEl.style.background = colors.gradient;
      liquidEl.style.boxShadow = `0 0 16px ${colors.glow}`;
      bulbEl.style.background = colors.bulb;
      bulbEl.style.boxShadow = `0 0 20px ${colors.glow}`;
      tempValEl.style.color = colors.text;

      // 告警狀態樣式
      if (s.alarm) {
        cardEl.classList.add('in-alarm');
        alarmBannerEl.classList.add('active');
        thermoWrapper.style.boxShadow = `inset 0 2px 6px rgba(0, 0, 0, 0.5), 0 0 24px rgba(244, 63, 94, 0.45)`;
      } else {
        cardEl.classList.remove('in-alarm');
        alarmBannerEl.classList.remove('active');
        thermoWrapper.style.boxShadow = 'inset 0 2px 6px rgba(0, 0, 0, 0.5), 0 8px 24px rgba(0, 0, 0, 0.3)';
      }

      // 數值更新
      tempValEl.textContent = formatTemp(s.temp);
      tempUnitEl.textContent = getUnitStr();

      maxTEl.textContent = `${formatTemp(s.maxTemp)} ${getUnitStr()}`;
      minTEl.textContent = `${formatTemp(s.minTemp)} ${getUnitStr()}`;

      // 濕度
      if (s.humidity !== null) {
        humValEl.textContent = `${s.humidity.toFixed(1)} %`;
        humBarEl.style.width = `${Math.min(100, Math.max(0, s.humidity))}%`;
      }

      // 電壓 (3.0V ~ 3.6V 映射為 0% ~ 100%)
      if (s.voltage !== null) {
        voltValEl.textContent = `${s.voltage.toFixed(3)} V`;
        const vPct = ((s.voltage - 3.0) / 0.6) * 100;
        voltBarEl.style.width = `${Math.min(100, Math.max(0, vPct))}%`;
      }

      if (s.seq !== null) {
        seqEl.textContent = `Seq: #${s.seq}`;
      }

      if (s.ts) {
        timeEl.textContent = `最後更新：${s.ts.split('T')[1]?.substring(0, 8) || s.ts}`;
      }

      // 繪製走勢圖
      drawSparkline(devId);
    }
  }

  // --- 繪製即時走勢圖 (Sparkline) ---
  function drawSparkline(devId) {
    const canvas = document.getElementById(`chart-${devId}`);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const s = sensorsState[devId];
    const data = s.history;

    const width = canvas.width;
    const height = canvas.height;
    ctx.clearRect(0, 0, width, height);

    if (data.length < 2) return;

    // 計算 Y 軸邊界（10°C ~ 40°C，或依歷史最小值/最大值擴充）
    let yMin = 12.0;
    let yMax = 38.0;

    const getX = (idx) => (idx / (data.length - 1)) * (width - 24) + 12;
    const getY = (val) => height - 12 - ((val - yMin) / (yMax - yMin)) * (height - 24);

    // 繪製 33°C 告警警戒虛線
    const alarmY = getY(TEMP_ALARM_THRESHOLD);
    ctx.beginPath();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = 'rgba(244, 63, 94, 0.4)';
    ctx.lineWidth = 1;
    ctx.moveTo(10, alarmY);
    ctx.lineTo(width - 10, alarmY);
    ctx.stroke();
    ctx.setLineDash([]);

    // 警戒線小標記
    ctx.fillStyle = 'rgba(244, 63, 94, 0.7)';
    ctx.font = '9px JetBrains Mono';
    ctx.textAlign = 'right';
    ctx.fillText('33° ⚠', width - 12, alarmY - 3);

    // 繪製溫度曲線漸層區域
    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, s.alarm ? 'rgba(244, 63, 94, 0.35)' : 'rgba(56, 189, 248, 0.3)');
    gradient.addColorStop(1, 'rgba(0, 0, 0, 0)');

    ctx.beginPath();
    ctx.moveTo(getX(0), getY(data[0].temp));
    for (let i = 1; i < data.length; i++) {
      ctx.lineTo(getX(i), getY(data[i].temp));
    }
    ctx.lineTo(getX(data.length - 1), height);
    ctx.lineTo(getX(0), height);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    // 繪製主折線
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(data[0].temp));
    for (let i = 1; i < data.length; i++) {
      ctx.lineTo(getX(i), getY(data[i].temp));
    }
    ctx.strokeStyle = s.alarm ? '#f43f5e' : '#38bdf8';
    ctx.lineWidth = 2.5;
    ctx.stroke();

    // 最新點發光圓點
    const lastX = getX(data.length - 1);
    const lastY = getY(data[data.length - 1].temp);

    ctx.beginPath();
    ctx.arc(lastX, lastY, 4, 0, Math.PI * 2);
    ctx.fillStyle = s.alarm ? '#f43f5e' : '#38bdf8';
    ctx.fill();

    ctx.beginPath();
    ctx.arc(lastX, lastY, 8, 0, Math.PI * 2);
    ctx.strokeStyle = s.alarm ? 'rgba(244, 63, 94, 0.4)' : 'rgba(56, 189, 248, 0.4)';
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // --- 更新頂部統計數據 ---
  function updateSummary() {
    let sum = 0;
    let count = 0;
    let maxTemp = -Infinity;
    let maxDevice = '--';
    let alarmCount = 0;

    DEVICES.forEach(devId => {
      const s = sensorsState[devId];
      if (s.temp !== null) {
        sum += s.temp;
        count++;
        if (s.temp > maxTemp) {
          maxTemp = s.temp;
          maxDevice = devId;
        }
      }
      if (s.alarm) {
        alarmCount++;
      }
    });

    const avgEl = document.getElementById('stat-avg-temp');
    const avgUnitEl = document.getElementById('stat-avg-unit');
    const maxEl = document.getElementById('stat-max-temp');
    const maxUnitEl = document.getElementById('stat-max-unit');
    const maxDevEl = document.getElementById('stat-max-device');
    const alarmCountEl = document.getElementById('stat-alarm-count');
    const alarmDetailEl = document.getElementById('stat-alarm-detail');
    const alarmIconContainer = document.getElementById('alarm-icon-container');
    const totalPacketsEl = document.getElementById('stat-total-packets');

    if (count > 0) {
      const avg = sum / count;
      avgEl.textContent = formatTemp(avg);
      avgUnitEl.textContent = getUnitStr();

      maxEl.textContent = formatTemp(maxTemp);
      maxUnitEl.textContent = getUnitStr();
      maxDevEl.textContent = `來自 ${maxDevice}`;
    }

    alarmCountEl.textContent = alarmCount;
    if (alarmCount > 0) {
      alarmDetailEl.textContent = `${alarmCount} 裝置超溫警戒中！`;
      alarmDetailEl.className = 'stat-badge alarm-triggered';
      alarmIconContainer.classList.add('active');
    } else {
      alarmDetailEl.textContent = '全系統正常';
      alarmDetailEl.className = 'stat-badge safe';
      alarmIconContainer.classList.remove('active');
    }

    totalPacketsEl.textContent = totalPackets;
  }

  // --- 連線狀態管理 ---
  function setConnectionStatus(status, text) {
    const pill = document.getElementById('connection-status-pill');
    const textEl = document.getElementById('connection-status-text');
    pill.className = `status-pill ${status}`;
    textEl.textContent = text;
  }

  // --- BTHome V2 封包解析器 (直接解析閘道器 Raw BLE 廣播，單次發布模式) ---
  function parseBTHomeV2Hex(hexStr) {
    if (!hexStr || hexStr.length < 2) return null;
    const bytes = [];
    for (let i = 0; i < hexStr.length; i += 2) {
      bytes.push(parseInt(hexStr.substr(i, 2), 16));
    }
    const header = bytes[0];
    const isEncrypted = Boolean(header & 0x01);
    if (isEncrypted) return null;

    const res = {};
    let idx = 1;
    const len = bytes.length;
    while (idx < len) {
      const objId = bytes[idx++];
      if (objId === 0x00) { // Packet ID
        if (idx < len) res.packetId = bytes[idx++];
      } else if (objId === 0x01) { // 電池 %
        if (idx < len) res.battery = bytes[idx++];
      } else if (objId === 0x02) { // 溫度 (sint16 * 0.01)
        if (idx + 2 <= len) {
          let raw = bytes[idx] | (bytes[idx + 1] << 8);
          if (raw & 0x8000) raw -= 0x10000;
          res.temperature = Math.round(raw * 0.01 * 100) / 100;
          idx += 2;
        }
      } else if (objId === 0x03) { // 濕度 (uint16 * 0.01)
        if (idx + 2 <= len) {
          const raw = bytes[idx] | (bytes[idx + 1] << 8);
          res.humidity = Math.round(raw * 0.01 * 100) / 100;
          idx += 2;
        }
      } else if (objId === 0x2e) { // 濕度 (uint8)
        if (idx < len) res.humidity = bytes[idx++];
      } else if (objId === 0x0c) { // 電壓 (uint16 * 0.001)
        if (idx + 2 <= len) {
          const raw = bytes[idx] | (bytes[idx + 1] << 8);
          res.voltage = Math.round(raw * 0.001 * 1000) / 1000;
          idx += 2;
        }
      } else {
        break;
      }
    }
    return res;
  }

  // --- 處理閘道器直接傳來的 Raw 廣播（免經 Python 橋接） ---
  function handleGatewayRawPayload(deviceId, payloadStr) {
    try {
      const data = JSON.parse(payloadStr);
      const serviceData = data.service_data || [];
      let updated = false;
      const sensor = sensorsState[deviceId];
      if (!sensor) return;

      for (const s of serviceData) {
        if (!s.uuid || !s.uuid.toLowerCase().includes('fcd2')) continue;
        const parsed = parseBTHomeV2Hex(s.data);
        if (!parsed) continue;

        if (parsed.temperature !== undefined) {
          sensor.temp = parsed.temperature;
          updated = true;
        }
        if (parsed.humidity !== undefined) {
          sensor.humidity = parsed.humidity;
          updated = true;
        }
        if (parsed.voltage !== undefined) {
          sensor.voltage = parsed.voltage;
          updated = true;
        }
        if (parsed.battery !== undefined) {
          sensor.battery = parsed.battery;
        }
        if (parsed.packetId !== undefined) {
          sensor.seq = parsed.packetId;
        }
      }

      if (!updated || sensor.temp === null) return;

      sensor.online = true;
      const alarm = sensor.temp >= TEMP_ALARM_THRESHOLD;
      sensor.alarm = alarm;
      sensor.lastReceived = Date.now();
      const now = new Date();
      sensor.ts = now.toTimeString().split(' ')[0];

      // 統計極值
      if (sensor.minTemp === null || sensor.temp < sensor.minTemp) sensor.minTemp = sensor.temp;
      if (sensor.maxTemp === null || sensor.temp > sensor.maxTemp) sensor.maxTemp = sensor.temp;

      // 歷史走勢
      sensor.history.push({ time: Date.now(), temp: sensor.temp, alarm });
      if (sensor.history.length > 30) sensor.history.shift();

      if (alarm) playAlarmBeep();

      updateSensorCard(deviceId);
      updateSummary();
    } catch (e) {
      console.warn('解析閘道器 Raw 封包失敗:', e);
    }
  }

  // --- 處理接收到的 MQTT 訊息 ---
  function handleMqttMessage(topic, payloadStr) {
    totalPackets++;

    // 1. 攔截閘道器直接發布的 BLE Raw 主題 (單次發布模式)
    const targetMacClean = (currentConfig.xiaomiMac || 'A4C1384A09DE').replace(/[:-]/g, '').toUpperCase();
    if (topic.startsWith('ble/') && topic.includes('/raw/')) {
      const parts = topic.split('/');
      const mac = parts[parts.length - 1].replace(/[:-]/g, '').toUpperCase();
      if (mac === targetMacClean) {
        handleGatewayRawPayload('xiaomi-01', payloadStr);
      }
      return;
    }

    // 2. 原本的 Topic 格式：
    // <TOPIC_PREFIX>/<device>/telemetry
    // <TOPIC_PREFIX>/<device>/status
    const parts = topic.split('/');
    if (parts.length < 3) return;

    const deviceId = parts[parts.length - 2];
    const subTopic = parts[parts.length - 1];

    if (!sensorsState[deviceId]) return;

    const sensor = sensorsState[deviceId];

    if (subTopic === 'status') {
      sensor.online = (payloadStr.trim() === 'online');
      updateSensorCard(deviceId);
      return;
    }

    if (subTopic === 'telemetry') {
      try {
        const data = JSON.parse(payloadStr);
        const temp = parseFloat(data.temperature);
        const hum = parseFloat(data.humidity);
        const volt = parseFloat(data.voltage);
        const alarm = Boolean(data.alarm);

        sensor.temp = temp;
        sensor.humidity = hum;
        sensor.voltage = volt;
        sensor.alarm = alarm;
        sensor.seq = data.seq;
        sensor.ts = data.ts;
        sensor.lastReceived = Date.now();

        // 統計極值
        if (sensor.minTemp === null || temp < sensor.minTemp) sensor.minTemp = temp;
        if (sensor.maxTemp === null || temp > sensor.maxTemp) sensor.maxTemp = temp;

        // 加入歷史點 (保留最近 30 筆)
        sensor.history.push({ time: Date.now(), temp, alarm });
        if (sensor.history.length > 30) {
          sensor.history.shift();
        }

        // 若觸發告警，發出音效提示
        if (alarm) {
          playAlarmBeep();
        }

        updateSensorCard(deviceId);
        updateSummary();
      } catch (err) {
        console.error('Failed to parse telemetry JSON:', err, payloadStr);
      }
    }
  }

  // --- 建立 MQTT 連線 ---
  function connectMQTT() {
    if (client) {
      try { client.end(true); } catch (e) {}
    }

    setConnectionStatus('connecting', '連線中...');

    const brokerHost = currentConfig.brokerHost || 'broker.emqx.io';
    const prefix = (currentConfig.topicPrefix || 'pspasia/test1').replace(/\/+$/, '');
    const gatewayTopic = currentConfig.gatewayTopic || 'ble/+/raw/+';

    // 若在 https 協定下使用 wss://:8084，若在 http / file 協定下使用 ws://:8083 或 wss://:8084
    const isHttps = window.location.protocol === 'https:';
    const wsPort = isHttps ? 8084 : 8083;
    const wsProtocol = isHttps ? 'wss' : 'ws';
    const brokerUrl = `${wsProtocol}://${brokerHost}:${wsPort}${MQTT_WS_PATH}`;

    console.log(`正在連線 MQTT: ${brokerUrl}`);

    const clientId = `web-dash-${Math.random().toString(16).substring(2, 10)}`;

    try {
      client = mqtt.connect(brokerUrl, {
        clientId: clientId,
        clean: true,
        connectTimeout: 8000,
        reconnectPeriod: 3000,
      });

      client.on('connect', function () {
        console.log('✅ MQTT 連線成功！');
        setConnectionStatus('connected', `已連線 (${brokerHost})`);
        const telemetryTopic = `${prefix}/#`;
        const topicsToSub = [telemetryTopic, gatewayTopic];
        client.subscribe(topicsToSub, { qos: 0 }, function (err) {
          if (!err) {
            console.log(`✅ 成功訂閱主題: ${topicsToSub.join(' 與 ')}`);
          } else {
            console.error('❌ 訂閱主題失敗:', err);
          }
        });
      });

      client.on('message', function (topic, message) {
        const payloadStr = message.toString();
        handleMqttMessage(topic, payloadStr);
      });

      client.on('error', function (err) {
        console.error('MQTT 連線錯誤:', err);
        setConnectionStatus('disconnected', '連線失敗');
      });

      client.on('reconnect', function () {
        setConnectionStatus('connecting', '重新連線中...');
      });

      client.on('close', function () {
        setConnectionStatus('disconnected', '已斷線');
      });

    } catch (e) {
      console.error('MQTT 初始化例外:', e);
      setConnectionStatus('disconnected', '無法連線');
    }
  }

  // --- 事件監聽綁定 ---
  function initEventBindings() {
    // 單位切換 (°C / °F)
    const unitToggleBtn = document.getElementById('unit-toggle-btn');
    const unitCLabel = document.getElementById('unit-c-label');
    const unitFLabel = document.getElementById('unit-f-label');

    unitToggleBtn.addEventListener('click', function () {
      isFahrenheit = !isFahrenheit;
      unitCLabel.classList.toggle('active', !isFahrenheit);
      unitFLabel.classList.toggle('active', isFahrenheit);

      // 重新渲染所有卡片
      DEVICES.forEach(devId => updateSensorCard(devId));
      updateSummary();
    });

    // 警報音效開關
    const soundToggleBtn = document.getElementById('sound-toggle-btn');
    const soundIconOn = document.getElementById('sound-icon-on');
    const soundIconOff = document.getElementById('sound-icon-off');
    const soundText = document.getElementById('sound-text');

    soundToggleBtn.addEventListener('click', function () {
      soundEnabled = !soundEnabled;
      soundIconOn.classList.toggle('hidden', !soundEnabled);
      soundIconOff.classList.toggle('hidden', soundEnabled);
      soundText.textContent = soundEnabled ? '警報音效' : '已靜音';
      if (soundEnabled) {
        playAlarmBeep(); // 試聽一聲
      }
    });

    // 手動重新連線按鈕
    const reconnectBtn = document.getElementById('reconnect-btn');
    reconnectBtn.addEventListener('click', function () {
      connectMQTT();
    });

    // --- MQTT 訂閱設定彈窗邏輯 ---
    initSettingsModal();
  }

  function initSettingsModal() {
    const modal = document.getElementById('settings-modal');
    const openBtn = document.getElementById('settings-toggle-btn');
    const closeBtn = document.getElementById('close-settings-btn');
    const cancelBtn = document.getElementById('cancel-settings-btn');
    const saveBtn = document.getElementById('save-settings-btn');
    const resetBtn = document.getElementById('reset-settings-btn');

    const inputHost = document.getElementById('cfg-broker-host');
    const inputPrefix = document.getElementById('cfg-topic-prefix');
    const inputGateway = document.getElementById('cfg-gateway-topic');
    const inputMac = document.getElementById('cfg-xiaomi-mac');
    const previewEl = document.getElementById('active-topics-preview');

    function updatePreview() {
      if (!previewEl) return;
      const pfx = (inputPrefix.value.trim() || DEFAULT_CONFIG.topicPrefix).replace(/\/+$/, '');
      const gw = inputGateway.value.trim() || DEFAULT_CONFIG.gatewayTopic;
      previewEl.textContent = `1. 遙測主題: ${pfx}/# \n2. 閘道主題: ${gw}`;
    }

    function populateInputs() {
      if (inputHost) inputHost.value = currentConfig.brokerHost || DEFAULT_CONFIG.brokerHost;
      if (inputPrefix) inputPrefix.value = currentConfig.topicPrefix || DEFAULT_CONFIG.topicPrefix;
      if (inputGateway) inputGateway.value = currentConfig.gatewayTopic || DEFAULT_CONFIG.gatewayTopic;
      if (inputMac) inputMac.value = currentConfig.xiaomiMac || DEFAULT_CONFIG.xiaomiMac;
      updatePreview();
    }

    if (openBtn && modal) {
      openBtn.addEventListener('click', function () {
        populateInputs();
        modal.classList.remove('hidden');
      });
    }

    function closeModal() {
      if (modal) modal.classList.add('hidden');
    }

    if (closeBtn) closeBtn.addEventListener('click', closeModal);
    if (cancelBtn) cancelBtn.addEventListener('click', closeModal);

    // 點擊背景遮罩關閉
    if (modal) {
      modal.addEventListener('click', function (e) {
        if (e.target === modal) closeModal();
      });
    }

    // 即時預覽輸入
    [inputPrefix, inputGateway].forEach(el => {
      if (el) el.addEventListener('input', updatePreview);
    });

    // 儲存設定並重新連線
    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        const newCfg = {
          brokerHost: inputHost ? inputHost.value.trim() || DEFAULT_CONFIG.brokerHost : DEFAULT_CONFIG.brokerHost,
          topicPrefix: inputPrefix ? inputPrefix.value.trim() || DEFAULT_CONFIG.topicPrefix : DEFAULT_CONFIG.topicPrefix,
          gatewayTopic: inputGateway ? inputGateway.value.trim() || DEFAULT_CONFIG.gatewayTopic : DEFAULT_CONFIG.gatewayTopic,
          xiaomiMac: inputMac ? inputMac.value.trim().toUpperCase() || DEFAULT_CONFIG.xiaomiMac : DEFAULT_CONFIG.xiaomiMac
        };
        saveStoredConfig(newCfg);
        closeModal();
        console.log('✅ 已更新 MQTT 訂閱設定：', newCfg);
        connectMQTT();
      });
    }

    // 恢復預設值
    if (resetBtn) {
      resetBtn.addEventListener('click', function () {
        if (confirm('確定要將 MQTT 訂閱設定恢復為原廠預設值嗎？')) {
          try {
            localStorage.removeItem(CONFIG_STORAGE_KEY);
          } catch (e) {}
          currentConfig = Object.assign({}, DEFAULT_CONFIG);
          populateInputs();
          closeModal();
          console.log('🔄 已重設為預設 MQTT 設定');
          connectMQTT();
        }
      });
    }
  }

  // --- 啟動初始化 ---
  function init() {
    initEventBindings();
    connectMQTT();

    // 初始渲染卡片骨架
    DEVICES.forEach(devId => updateSensorCard(devId));
    updateSummary();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
