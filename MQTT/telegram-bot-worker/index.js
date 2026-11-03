/**
 * 小米溫濕度計 3 × TaiwanSensor 閘道器 Telegram 查詢 Bot (Cloudflare Worker)
 * 
 * 運作架構：
 * 1. 溫濕度計定時發送 BLE 廣播 (BTHome V2 格式)
 * 2. TaiwanSensor 閘道器 (ESP32) 收到後透過網路線直發 broker.emqx.io:1883
 * 3. 使用者在 Telegram 傳送 /temp 或點擊【🔄 立即更新】按鈕
 * 4. Telegram Webhook 觸發此 Cloudflare Worker
 * 5. Worker 連線至 EMQX 擷取閘道器最新廣播，解碼 BTHome V2，秒回 Telegram！
 * 
 * 👉 全程走硬體閘道器與雲端，您的個人電腦完全不用開機！
 */

import { connect } from 'cloudflare:sockets';

// 設定參數
const TELEGRAM_TOKEN = "8749430348:AAEyRAfyHMVP1kg3YHW4nfgqPnGk28ybBag";
const MQTT_HOST = "broker.emqx.io";
const MQTT_PORT = 1883;
const MQTT_TOPIC = "ble/esp32-eth-ble-mqtt-x4-2c8628/raw/A4C1384A09DE";

const enc = new TextEncoder();
const dec = new TextDecoder();

function utf8Field(s) {
  const b = enc.encode(s);
  return [b.length >> 8, b.length & 0xff, ...b];
}

function remainingLength(n) {
  const out = [];
  do {
    let d = n % 128;
    n = Math.floor(n / 128);
    if (n > 0) d |= 0x80;
    out.push(d);
  } while (n > 0);
  return out;
}

// 快速 MQTT 接收器：連線並等待閘道器最新廣播封包 (最長等待 6 秒)
async function fetchGatewayPacket(timeoutMs = 6000) {
  const socket = connect({ hostname: MQTT_HOST, port: MQTT_PORT });
  const writer = socket.writable.getWriter();
  const reader = socket.readable.getReader();
  const clientId = 'cf-tg-' + Math.random().toString(16).slice(2, 10);

  // MQTT CONNECT
  const vh = [0, 4, 77, 81, 84, 84, 4, 0x02, 0, 30, ...utf8Field(clientId)];
  await writer.write(new Uint8Array([0x10, ...remainingLength(vh.length), ...vh]));

  // MQTT SUBSCRIBE
  const sub = [0, 1, ...utf8Field(MQTT_TOPIC), 0];
  await writer.write(new Uint8Array([0x82, ...remainingLength(sub.length), ...sub]));

  let buf = new Uint8Array(0);
  const deadline = Date.now() + timeoutMs;
  const packets = [];

  try {
    while (Date.now() < deadline && packets.length < 2) {
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;

      const timer = new Promise((r) => setTimeout(() => r({ timeout: true }), remaining));
      const res = await Promise.race([reader.read(), timer]);
      if (res.timeout || res.done) break;

      const merged = new Uint8Array(buf.length + res.value.length);
      merged.set(buf);
      merged.set(res.value, buf.length);
      buf = merged;

      // 解析 MQTT 封包
      while (buf.length >= 2) {
        let mul = 1, len = 0, i = 1, b;
        do {
          if (i >= buf.length) { b = null; break; }
          b = buf[i++];
          len += (b & 0x7f) * mul;
          mul *= 128;
        } while (b & 0x80);

        if (b === null || buf.length < i + len) break;
        const type = buf[0] >> 4;
        const body = buf.slice(i, i + len);
        buf = buf.slice(i + len);

        if (type === 3) { // PUBLISH
          const tl = (body[0] << 8) | body[1];
          const payload = body.slice(2 + tl);
          try {
            const json = JSON.parse(dec.decode(payload));
            packets.push(json);
          } catch (_) {}
        }
      }
    }
  } finally {
    try { await writer.write(new Uint8Array([0xe0, 0])); } catch (_) {}
    try { await socket.close(); } catch (_) {}
  }

  return packets;
}

// BTHome V2 廣播數據解碼
function parseBTHome(hex) {
  if (!hex) return {};
  const d = new Uint8Array(hex.match(/../g).map((h) => parseInt(h, 16)));
  const out = {};
  if (d.length < 1 || (d[0] & 0x01)) return out; // 加密封包跳過

  let i = 1;
  const u16 = (p) => d[p] | (d[p + 1] << 8);
  while (i < d.length) {
    const id = d[i++];
    if (id === 0x00 || id === 0x10 || id === 0x11) { i += 1; }
    else if (id === 0x01) { out.battery = d[i]; i += 1; }
    else if (id === 0x02) { out.temperature = ((u16(i) << 16) >> 16) * 0.01; i += 2; }
    else if (id === 0x03) { out.humidity = u16(i) * 0.01; i += 2; }
    else if (id === 0x2e) { out.humidity = d[i]; i += 1; }
    else if (id === 0x0c) { out.voltage = u16(i) * 0.001; i += 2; }
    else break;
  }
  return out;
}

// 回覆 Telegram 訊息
async function replyTelegram(chatId, text, messageId = null) {
  const keyboard = {
    inline_keyboard: [
      [{ text: "🔄 重新整理讀取最新溫濕度", callback_data: "refresh" }]
    ]
  };

  const body = {
    chat_id: chatId,
    text: text,
    parse_mode: "HTML",
    reply_markup: keyboard
  };

  await fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
}

// 格式化回覆內容
function renderMessage(data) {
  const time = new Date().toLocaleTimeString("zh-TW", { timeZone: "Asia/Taipei", hour12: false });
  const t = data.temperature != null ? `<b>${data.temperature.toFixed(2)} °C</b>` : "--";
  const h = data.humidity != null ? `<b>${data.humidity.toFixed(1)} %</b>` : "--";
  const v = data.voltage != null ? `${data.voltage.toFixed(3)} V` : "--";
  const b = data.battery != null ? `${data.battery} %` : "--";
  const rssi = data.rssi != null ? `${data.rssi} dBm` : "--";

  let statusIcon = "🟢 正常";
  if (data.temperature != null && data.temperature >= 33) {
    statusIcon = "🔥 高溫警戒";
  }

  return `🌡️ <b>小米溫濕度計即時回報</b>
━━━━━━━━━━━━━━━━━━
• 當前溫度：${t}
• 當前濕度：${h}
• 電池電量：${b} (${v})
• 藍牙訊號：${rssi}
• 系統狀態：${statusIcon}
━━━━━━━━━━━━━━━━━━
🕒 讀取時間：${time}
📡 資料來源：TaiwanSensor 硬體閘道器 (免開電腦)`;
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // 1. 提供設定 Webhook 的快捷網址：訪問 /set-webhook 即自動完成 Telegram 綁定
    if (url.pathname === "/set-webhook") {
      const workerUrl = `${url.origin}/webhook`;
      const tgRes = await fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook?url=${encodeURIComponent(workerUrl)}`);
      const resJson = await tgRes.json();
      return new Response(JSON.stringify(resJson, null, 2), {
        headers: { "Content-Type": "application/json" }
      });
    }

    // 2. 處理 Telegram Webhook 推播
    if (url.pathname === "/webhook" && request.method === "POST") {
      try {
        const update = await request.json();
        let chatId = null;

        if (update.message) {
          chatId = update.message.chat.id;
        } else if (update.callback_query) {
          chatId = update.callback_query.message.chat.id;
          // 回應 callback 避免按鈕轉圈
          fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/answerCallbackQuery`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ callback_query_id: update.callback_query.id, text: "正在向硬體閘道器獲取即時數據..." })
          }).catch(() => {});
        }

        if (chatId) {
          // 向閘道器 MQTT 抓取封包
          const packets = await fetchGatewayPacket(6000);
          const state = {};

          for (const p of packets) {
            if (p.rssi != null) state.rssi = p.rssi;
            for (const s of p.service_data || []) {
              if (/fcd2/i.test(s.uuid || "")) {
                Object.assign(state, parseBTHome(s.data || ""));
              }
            }
          }

          if (state.temperature != null || state.humidity != null) {
            await replyTelegram(chatId, renderMessage(state));
          } else {
            await replyTelegram(chatId, "⚠️ 暫時未能從閘道器獲取封包，請確認溫濕度計在閘道器收訊範圍內，稍候點擊下方按鈕重試。");
          }
        }
      } catch (err) {
        console.error("Webhook processing error:", err);
      }

      return new Response("OK");
    }

    // 首頁說明
    return new Response(`Xiaomi Telegram Bot Cloudflare Worker is running!\n\nSet Webhook: ${url.origin}/set-webhook\n`, {
      headers: { "Content-Type": "text/plain; charset=utf-8" }
    });
  }
};
