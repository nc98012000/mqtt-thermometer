//! MQTT 觀看程式：訂閱模擬器的 topic，在終端機即時顯示裝置狀態、正弦波圖表與訊息紀錄。
//!
//! 兩個頁籤（Tab / F1 / F2 切換）：
//!   監控：1/2/3 切換 溫度/濕度/電壓、+/- 調整時間窗、r 重連、c 清除、q 離開
//!   設定：↑↓ 選欄位、直接打字編輯、Enter 套用並儲存、Esc 還原
//! 設定存在執行檔旁的 viewer-config.json。Ctrl+C 任何頁面都可離開。

use std::collections::{BTreeMap, VecDeque};
use std::fs;
use std::io;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use ratatui::crossterm::event::{self, Event as TermEvent, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::prelude::*;
use ratatui::symbols::Marker;
use ratatui::widgets::{Axis, Block, Chart, Dataset, GraphType, LegendPosition, List, ListItem, Paragraph, Row, Table, Tabs};
use ratatui::DefaultTerminal;
use rumqttc::{Client, Event, MqttOptions, Packet, QoS};
use serde::{Deserialize, Serialize};

const TEMP_ALARM: f64 = 33.0; // 與模擬器一致
const MAX_WINDOW: f64 = 600.0; // 最多保留 10 分鐘歷史
const MAX_LOG: usize = 200;
const COLORS: [Color; 6] = [Color::Cyan, Color::Yellow, Color::Magenta, Color::Green, Color::LightRed, Color::LightBlue];

// ---------------- 設定 ----------------

#[derive(Serialize, Deserialize, Clone, PartialEq)]
#[serde(default)]
struct Settings {
    host: String,
    port: u16,
    topic: String,
    username: String,
    password: String,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            host: "broker.emqx.io".into(),
            port: 1883,
            topic: "pspasia/test1/#".into(),
            username: String::new(),
            password: String::new(),
        }
    }
}

impl Settings {
    /// 設定檔放在執行檔旁邊，不管從哪個目錄啟動都讀同一份
    fn path() -> PathBuf {
        std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|d| d.join("viewer-config.json")))
            .unwrap_or_else(|| PathBuf::from("viewer-config.json"))
    }

    fn load() -> Self {
        fs::read_to_string(Self::path())
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default()
    }

    fn save(&self) -> io::Result<()> {
        fs::write(Self::path(), serde_json::to_string_pretty(self)?)
    }
}

const FIELDS: [&str; 5] = ["Broker 主機", "Port", "Topic", "帳號（選填）", "密碼（選填）"];
const PORT_FIELD: usize = 1;
const PASSWORD_FIELD: usize = 4;

/// 設定頁的編輯中內容，按 Enter 才會變成正式設定
struct Form {
    values: [String; 5],
    selected: usize,
    error: Option<String>,
}

impl Form {
    fn from(s: &Settings) -> Self {
        Self {
            values: [s.host.clone(), s.port.to_string(), s.topic.clone(), s.username.clone(), s.password.clone()],
            selected: 0,
            error: None,
        }
    }

    fn to_settings(&self) -> Result<Settings, String> {
        let host = self.values[0].trim().to_string();
        if host.is_empty() {
            return Err("Broker 主機不可空白".into());
        }
        let port = match self.values[PORT_FIELD].trim().parse::<u16>() {
            Ok(p) if p > 0 => p,
            _ => return Err("Port 必須是 1 ~ 65535".into()),
        };
        let topic = self.values[2].trim().to_string();
        if topic.is_empty() {
            return Err("Topic 不可空白".into());
        }
        Ok(Settings {
            host,
            port,
            topic,
            username: self.values[3].trim().to_string(),
            password: self.values[PASSWORD_FIELD].clone(),
        })
    }
}

// ---------------- MQTT ----------------

enum Msg {
    Connected,
    Error(String),
    Publish { topic: String, payload: Vec<u8>, retain: bool },
}

/// 一次連線。每次套用新設定就關掉舊的、開一個新的；訊息帶 generation，舊連線殘留的訊息會被忽略。
struct Connection {
    client: Client,
    stop: Arc<AtomicBool>,
}

impl Connection {
    fn close(&self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = self.client.try_disconnect();
    }
}

/// 在背景執行緒跑 MQTT，事件透過 channel 丟給 UI。rumqttc 斷線後會在下一次迭代自動重連。
fn spawn_mqtt(s: &Settings, gen: u64, tx: Sender<(u64, Msg)>) -> Connection {
    let mut opts = MqttOptions::new(format!("psp-viewer-{}-{gen}", std::process::id()), s.host.clone(), s.port);
    opts.set_keep_alive(Duration::from_secs(30));
    if !s.username.is_empty() {
        opts.set_credentials(s.username.clone(), s.password.clone());
    }
    let (client, mut connection) = Client::new(opts, 10);
    let stop = Arc::new(AtomicBool::new(false));

    let (sub_client, stopped, topic) = (client.clone(), stop.clone(), s.topic.clone());
    thread::spawn(move || {
        for notification in connection.iter() {
            if stopped.load(Ordering::Relaxed) {
                break;
            }
            let msg = match notification {
                Ok(Event::Incoming(Packet::ConnAck(_))) => {
                    // 每次（重新）連線都要重新訂閱
                    let _ = sub_client.try_subscribe(topic.as_str(), QoS::AtLeastOnce);
                    Msg::Connected
                }
                Ok(Event::Incoming(Packet::Publish(p))) => Msg::Publish {
                    topic: p.topic.clone(),
                    payload: p.payload.to_vec(),
                    retain: p.retain,
                },
                Ok(_) => continue,
                Err(e) => {
                    let _ = tx.send((gen, Msg::Error(e.to_string())));
                    // 等 2 秒再重連，期間若被關閉就直接結束
                    for _ in 0..20 {
                        if stopped.load(Ordering::Relaxed) {
                            return;
                        }
                        thread::sleep(Duration::from_millis(100));
                    }
                    continue;
                }
            };
            if tx.send((gen, msg)).is_err() {
                break; // UI 已關閉
            }
        }
    });

    Connection { client, stop }
}

// ---------------- 資料 ----------------

#[derive(Deserialize, Default, Clone)]
#[serde(default)]
struct Telemetry {
    seq: u64,
    ts: String,
    temperature: f64,
    humidity: f64,
    voltage: f64,
    alarm: bool,
}

struct Sample {
    x: f64, // 觀看程式啟動後經過的秒數
    temp: f64,
    hum: f64,
    volt: f64,
}

#[derive(Default)]
struct Device {
    online: Option<bool>,
    last: Option<Telemetry>,
    history: VecDeque<Sample>,
}

#[derive(Clone, Copy, PartialEq)]
enum Metric {
    Temp,
    Hum,
    Volt,
}

impl Metric {
    fn title(self) -> &'static str {
        match self {
            Metric::Temp => "溫度 °C",
            Metric::Hum => "濕度 %",
            Metric::Volt => "電壓 V",
        }
    }
    fn get(self, s: &Sample) -> f64 {
        match self {
            Metric::Temp => s.temp,
            Metric::Hum => s.hum,
            Metric::Volt => s.volt,
        }
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Page {
    Monitor,
    Settings,
}

struct App {
    settings: Settings,
    form: Form,
    page: Page,
    tx: Sender<(u64, Msg)>,
    gen: u64,
    conn: Connection,
    start: Instant,
    connected: bool,
    status: String,
    total: u64,
    devices: BTreeMap<String, Device>,
    log: VecDeque<Line<'static>>,
    metric: Metric,
    window: f64,
    quit: bool,
}

impl App {
    fn new(settings: Settings, tx: Sender<(u64, Msg)>) -> Self {
        let conn = spawn_mqtt(&settings, 0, tx.clone());
        let mut app = Self {
            form: Form::from(&settings),
            settings,
            page: Page::Monitor,
            tx,
            gen: 0,
            conn,
            start: Instant::now(),
            connected: false,
            status: "連線中...".into(),
            total: 0,
            devices: BTreeMap::new(),
            log: VecDeque::new(),
            metric: Metric::Temp,
            window: 60.0,
            quit: false,
        };
        app.push_log(Line::from(format!("🔌 連線到 {}:{} ...", app.settings.host, app.settings.port)).dark_gray());
        app
    }

    fn now(&self) -> f64 {
        self.start.elapsed().as_secs_f64()
    }

    fn push_log(&mut self, line: Line<'static>) {
        self.log.push_back(line);
        while self.log.len() > MAX_LOG {
            self.log.pop_front();
        }
    }

    /// 關掉舊連線，用目前設定重新連線；換 broker/topic 後舊資料沒有意義，一併清掉
    fn reconnect(&mut self) {
        self.conn.close();
        self.gen += 1;
        self.conn = spawn_mqtt(&self.settings, self.gen, self.tx.clone());
        self.connected = false;
        self.status = "連線中...".into();
        self.total = 0;
        self.devices.clear();
        self.push_log(Line::from(format!("🔌 連線到 {}:{} ...", self.settings.host, self.settings.port)).dark_gray());
    }

    fn apply_form(&mut self) {
        let new = match self.form.to_settings() {
            Ok(s) => s,
            Err(e) => {
                self.form.error = Some(e);
                return;
            }
        };
        self.form = Form { selected: self.form.selected, ..Form::from(&new) };
        let saved = new.save();
        if new != self.settings {
            self.settings = new;
            self.reconnect();
        }
        match saved {
            Ok(()) => self.push_log(Line::from(format!("💾 設定已儲存 {}", Settings::path().display())).dark_gray()),
            Err(e) => self.push_log(Line::from(format!("⚠ 設定儲存失敗：{e}")).red()),
        }
        self.page = Page::Monitor;
    }

    fn form_dirty(&self) -> bool {
        self.form.to_settings().map_or(true, |s| s != self.settings)
    }

    fn handle(&mut self, msg: Msg) {
        match msg {
            Msg::Connected => {
                self.connected = true;
                self.status = "已連線".into();
                let text = format!("✅ 已連線 {}:{}，訂閱 {}", self.settings.host, self.settings.port, self.settings.topic);
                self.push_log(Line::from(text).green());
            }
            Msg::Error(e) => {
                self.connected = false;
                self.status = "重連中".into();
                self.push_log(Line::from(format!("⚠ {e}")).red());
            }
            Msg::Publish { topic, payload, retain } => {
                self.total += 1;
                let text = String::from_utf8_lossy(&payload).into_owned();
                let parts: Vec<&str> = topic.rsplitn(3, '/').collect(); // [kind, device, prefix]
                if parts.len() < 2 {
                    self.push_log(Line::from(format!("📩 {topic}: {text}")));
                    return;
                }
                let (kind, name) = (parts[0], parts[1].to_string());
                match kind {
                    "status" => {
                        let online = text == "online";
                        self.devices.entry(name.clone()).or_default().online = Some(online);
                        let tag = if retain { "（retained）" } else { "" };
                        let line = Line::from(format!("{} {name} = {text}{tag}", if online { "🟢" } else { "🔴" }));
                        self.push_log(if online { line.green() } else { line.dark_gray() });
                    }
                    "telemetry" => match serde_json::from_str::<Telemetry>(&text) {
                        Ok(t) => {
                            let x = self.now();
                            let line = format!(
                                "📊 {name} #{}  T={:.2}  H={:.2}  V={:.3}",
                                t.seq, t.temperature, t.humidity, t.voltage
                            );
                            let alarm = t.alarm || t.temperature > TEMP_ALARM;
                            let dev = self.devices.entry(name).or_default();
                            dev.online.get_or_insert(true);
                            dev.history.push_back(Sample { x, temp: t.temperature, hum: t.humidity, volt: t.voltage });
                            while dev.history.front().is_some_and(|s| s.x < x - MAX_WINDOW) {
                                dev.history.pop_front();
                            }
                            dev.last = Some(t);
                            self.push_log(if alarm { Line::from(line + "  🔥").red() } else { Line::from(line) });
                        }
                        Err(_) => self.push_log(Line::from(format!("📩 {topic}: {text}"))),
                    },
                    _ => self.push_log(Line::from(format!("📩 {topic}: {text}"))),
                }
            }
        }
    }

    fn on_key(&mut self, key: KeyEvent) {
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            self.quit = true;
            return;
        }
        match key.code {
            KeyCode::Tab => {
                self.page = if self.page == Page::Monitor { Page::Settings } else { Page::Monitor };
                return;
            }
            KeyCode::F(1) => return self.page = Page::Monitor,
            KeyCode::F(2) => return self.page = Page::Settings,
            _ => {}
        }
        match self.page {
            Page::Monitor => self.on_monitor_key(key),
            Page::Settings => self.on_settings_key(key),
        }
    }

    fn on_monitor_key(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Char('q') | KeyCode::Esc => self.quit = true,
            KeyCode::Char('1') => self.metric = Metric::Temp,
            KeyCode::Char('2') => self.metric = Metric::Hum,
            KeyCode::Char('3') => self.metric = Metric::Volt,
            KeyCode::Char('+') | KeyCode::Char('=') => self.window = (self.window * 2.0).min(MAX_WINDOW),
            KeyCode::Char('-') => self.window = (self.window / 2.0).max(10.0),
            KeyCode::Char('r') => self.reconnect(),
            KeyCode::Char('c') => {
                self.log.clear();
                self.devices.values_mut().for_each(|d| d.history.clear());
            }
            _ => {}
        }
    }

    fn on_settings_key(&mut self, key: KeyEvent) {
        let form = &mut self.form;
        match key.code {
            KeyCode::Up | KeyCode::BackTab => form.selected = (form.selected + FIELDS.len() - 1) % FIELDS.len(),
            KeyCode::Down => form.selected = (form.selected + 1) % FIELDS.len(),
            KeyCode::Backspace => {
                form.values[form.selected].pop();
            }
            KeyCode::Char(c) => {
                if form.selected != PORT_FIELD || c.is_ascii_digit() {
                    form.values[form.selected].push(c);
                }
            }
            KeyCode::Enter => return self.apply_form(),
            KeyCode::Esc => {
                *form = Form { selected: form.selected, ..Form::from(&self.settings) };
                return;
            }
            _ => return,
        }
        form.error = None;
    }
}

// ---------------- 畫面 ----------------

fn draw(f: &mut Frame, app: &App) {
    let [header, body, help] =
        Layout::vertical([Constraint::Length(1), Constraint::Min(0), Constraint::Length(1)]).areas(f.area());

    draw_header(f, header, app);
    let help_text = match app.page {
        Page::Monitor => {
            draw_monitor(f, body, app);
            " Tab 切換頁籤   1/2/3 溫度/濕度/電壓   +/- 時間窗   r 重連   c 清除   q 離開"
        }
        Page::Settings => {
            draw_settings(f, body, app);
            " Tab 切換頁籤   ↑↓ 選欄位   打字編輯   Backspace 刪除   Enter 套用並儲存   Esc 還原   Ctrl+C 離開"
        }
    };
    f.render_widget(Paragraph::new(help_text).dark_gray(), help);
}

fn draw_header(f: &mut Frame, area: Rect, app: &App) {
    let [tabs_area, status_area] = Layout::horizontal([Constraint::Length(26), Constraint::Min(0)]).areas(area);

    let selected = if app.page == Page::Monitor { 0 } else { 1 };
    let tabs = Tabs::new([" 監控 F1 ", " 設定 F2 "])
        .select(selected)
        .highlight_style(Style::new().black().on_cyan().bold())
        .divider("│");
    f.render_widget(tabs, tabs_area);

    let conn = Span::from(format!("● {}", app.status)).bold();
    let conn = if app.connected { conn.green() } else { conn.yellow() };
    let line = Line::from(vec![
        Span::from(format!(" {}:{}  ", app.settings.host, app.settings.port)),
        Span::from(app.settings.topic.as_str()).cyan(),
        Span::from("  "),
        conn,
        Span::from(format!("  收到 {} 筆", app.total)).dark_gray(),
    ]);
    f.render_widget(Paragraph::new(line), status_area);
}

fn draw_monitor(f: &mut Frame, area: Rect, app: &App) {
    let table_h = (app.devices.len() as u16 + 3).max(4);
    let [table, chart, log] =
        Layout::vertical([Constraint::Length(table_h), Constraint::Min(10), Constraint::Length(8)]).areas(area);
    draw_table(f, table, app);
    draw_chart(f, chart, app);
    draw_log(f, log, app);
}

fn draw_settings(f: &mut Frame, area: Rect, app: &App) {
    const LABEL_W: usize = 16;
    let form = &app.form;
    let mut lines = vec![Line::default()];
    let mut cursor = None;

    for (i, label) in FIELDS.iter().enumerate() {
        let selected = i == form.selected;
        let shown = if i == PASSWORD_FIELD { "*".repeat(form.values[i].chars().count()) } else { form.values[i].clone() };
        let pad = " ".repeat(LABEL_W.saturating_sub(Span::from(*label).width()));
        let marker = if selected { "▶ " } else { "  " };
        let label = Span::from(format!("{marker}{label}{pad}"));
        let value = Span::from(format!("{shown} "));
        let (label, value) = if selected {
            (label.cyan().bold(), value.underlined())
        } else {
            (label, value.gray())
        };
        if selected {
            let x = 1 + 2 + LABEL_W + Span::from(shown.as_str()).width();
            cursor = Some((x as u16, lines.len() as u16 + 1));
        }
        lines.push(Line::from(vec![Span::from(" "), label, value]));
        lines.push(Line::default());
    }

    if let Some(e) = &form.error {
        lines.push(Line::from(format!("  ✖ {e}")).red().bold());
    } else if app.form_dirty() {
        lines.push(Line::from("  ● 有未套用的變更，按 Enter 套用並重新連線").yellow());
    } else {
        lines.push(Line::from("  ✔ 目前設定已套用").green());
    }
    lines.push(Line::default());
    lines.push(Line::from(format!("  設定檔：{}", Settings::path().display())).dark_gray());
    lines.push(Line::from("  Topic 可用萬用字元：+ 單層、# 多層（例：pspasia/test1/#）").dark_gray());
    lines.push(Line::from("  免費公開 broker：broker.emqx.io、test.mosquitto.org、broker.hivemq.com").dark_gray());

    f.render_widget(Paragraph::new(lines).block(Block::bordered().title(" 連線設定 ")), area);
    if let Some((x, y)) = cursor {
        f.set_cursor_position((area.x + x, area.y + y));
    }
}

fn draw_table(f: &mut Frame, area: Rect, app: &App) {
    let header = Row::new(["裝置", "狀態", "溫度 °C", "濕度 %", "電壓 V", "seq", "時間"]).bold().underlined();
    let rows = app.devices.iter().enumerate().map(|(i, (name, d))| {
        let status = match d.online {
            Some(true) => Span::from("● online").green(),
            Some(false) => Span::from("○ offline").dark_gray(),
            None => Span::from("?"),
        };
        let name = Span::from(name.clone()).fg(COLORS[i % COLORS.len()]).bold();
        match &d.last {
            Some(t) => {
                let temp = Span::from(format!("{:.2}", t.temperature));
                let temp = if t.alarm || t.temperature > TEMP_ALARM { temp.red().bold() } else { temp };
                Row::new(vec![
                    Line::from(name),
                    Line::from(status),
                    Line::from(temp),
                    Line::from(format!("{:.2}", t.humidity)),
                    Line::from(format!("{:.3}", t.voltage)),
                    Line::from(t.seq.to_string()),
                    Line::from(t.ts.clone()),
                ])
            }
            None => Row::new(vec![Line::from(name), Line::from(status)]),
        }
    });
    let widths = [
        Constraint::Length(12),
        Constraint::Length(10),
        Constraint::Length(9),
        Constraint::Length(9),
        Constraint::Length(8),
        Constraint::Length(7),
        Constraint::Min(23),
    ];
    let table = Table::new(rows, widths).header(header).block(Block::bordered().title(" 裝置 "));
    f.render_widget(table, area);
}

fn draw_chart(f: &mut Frame, area: Rect, app: &App) {
    let now = app.now();
    let x_min = (now - app.window).max(0.0);
    let x_max = x_min + app.window;

    let series: Vec<(String, Color, Vec<(f64, f64)>)> = app
        .devices
        .iter()
        .enumerate()
        .map(|(i, (name, d))| {
            let pts = d
                .history
                .iter()
                .filter(|s| s.x >= x_min)
                .map(|s| (s.x, app.metric.get(s)))
                .collect();
            (name.clone(), COLORS[i % COLORS.len()], pts)
        })
        .collect();

    // Y 軸依可見資料自動縮放，上下留 10% 空間
    let (mut y_min, mut y_max) = series
        .iter()
        .flat_map(|(_, _, p)| p.iter().map(|&(_, y)| y))
        .fold((f64::INFINITY, f64::NEG_INFINITY), |(lo, hi), y| (lo.min(y), hi.max(y)));
    if !y_min.is_finite() {
        (y_min, y_max) = (0.0, 1.0);
    }
    let pad = ((y_max - y_min) * 0.1).max(0.01);
    let (y_min, y_max) = (y_min - pad, y_max + pad);

    let datasets = series
        .iter()
        .map(|(name, color, pts)| {
            Dataset::default()
                .name(name.as_str())
                .marker(Marker::Braille)
                .graph_type(GraphType::Line)
                .style(Style::default().fg(*color))
                .data(pts)
        })
        .collect();

    let digits = if app.metric == Metric::Volt { 3 } else { 1 };
    let x_labels = [
        format!("-{:.0}s", app.window),
        format!("-{:.0}s", app.window / 2.0),
        "now".to_string(),
    ];
    let y_labels = [
        format!("{:.*}", digits, y_min),
        format!("{:.*}", digits, (y_min + y_max) / 2.0),
        format!("{:.*}", digits, y_max),
    ];

    let title = Line::from(vec![
        Span::from(" "),
        Span::from(app.metric.title()).bold(),
        Span::from(format!("  （最近 {:.0} 秒）", app.window)).dark_gray(),
        Span::from(" "),
    ]);
    let chart = Chart::new(datasets)
        .block(Block::bordered().title(title))
        .x_axis(Axis::default().bounds([x_min, x_max]).labels(x_labels).dark_gray())
        .y_axis(Axis::default().bounds([y_min, y_max]).labels(y_labels).dark_gray())
        .legend_position(Some(LegendPosition::TopRight))
        .hidden_legend_constraints((Constraint::Ratio(1, 2), Constraint::Ratio(1, 2)));
    f.render_widget(chart, area);
}

fn draw_log(f: &mut Frame, area: Rect, app: &App) {
    let visible = area.height.saturating_sub(2) as usize;
    let items: Vec<ListItem> = app
        .log
        .iter()
        .skip(app.log.len().saturating_sub(visible))
        .map(|l| ListItem::new(l.clone()))
        .collect();
    f.render_widget(List::new(items).block(Block::bordered().title(" 訊息 ")), area);
}

// ---------------- 主程式 ----------------

fn run(terminal: &mut DefaultTerminal, mut app: App, rx: Receiver<(u64, Msg)>) -> io::Result<()> {
    while !app.quit {
        while let Ok((gen, m)) = rx.try_recv() {
            if gen == app.gen {
                app.handle(m);
            }
        }
        terminal.draw(|f| draw(f, &app))?;

        if event::poll(Duration::from_millis(100))? {
            if let TermEvent::Key(key) = event::read()? {
                // Windows 會同時送 Press 與 Release，只處理 Press
                if key.kind == KeyEventKind::Press {
                    app.on_key(key);
                }
            }
        }
    }
    app.conn.close();
    Ok(())
}

fn main() -> io::Result<()> {
    let (tx, rx) = mpsc::channel();
    let app = App::new(Settings::load(), tx);
    let mut terminal = ratatui::init(); // 也會裝 panic hook，當掉時自動還原終端機
    let result = run(&mut terminal, app, rx);
    ratatui::restore();
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;

    fn dump(app: &App) {
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        terminal.draw(|f| draw(f, app)).unwrap();
        let buf = terminal.backend().buffer();
        for y in 0..buf.area.height {
            let line: String = (0..buf.area.width).map(|x| buf[(x, y)].symbol()).collect();
            println!("{line}");
        }
    }

    fn pump(app: &mut App, rx: &Receiver<(u64, Msg)>, secs: u64) {
        let deadline = Instant::now() + Duration::from_secs(secs);
        while Instant::now() < deadline {
            if let Ok((gen, m)) = rx.recv_timeout(Duration::from_millis(100)) {
                if gen == app.gen {
                    app.handle(m);
                }
            }
        }
    }

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    #[test]
    fn form_validation() {
        let mut form = Form::from(&Settings::default());
        assert!(form.to_settings().is_ok());
        form.values[PORT_FIELD] = "0".into();
        assert!(form.to_settings().is_err());
        form.values[PORT_FIELD] = "1883".into();
        form.values[2] = "  ".into();
        assert!(form.to_settings().is_err());
    }

    /// 需要網路：先在另一個視窗跑模擬器，再執行 `cargo test -- --ignored --nocapture`
    /// 連線後透過設定頁把 topic 改成只看 sensor-02，驗證套用後會重新連線並只收到該裝置。
    #[test]
    #[ignore]
    fn live_settings_switch() {
        let (tx, rx) = mpsc::channel();
        let mut app = App::new(Settings::default(), tx);
        pump(&mut app, &rx, 5);
        assert!(app.connected && !app.devices.is_empty());

        app.on_key(key(KeyCode::Tab));
        app.on_key(key(KeyCode::Down));
        app.on_key(key(KeyCode::Down));
        for _ in 0.."pspasia/test1/#".len() {
            app.on_key(key(KeyCode::Backspace));
        }
        "pspasia/test1/sensor-02/#".chars().for_each(|c| app.on_key(key(KeyCode::Char(c))));
        dump(&app);
        app.on_key(key(KeyCode::Enter));

        pump(&mut app, &rx, 5);
        dump(&app);
        assert!(app.connected);
        assert_eq!(app.devices.keys().collect::<Vec<_>>(), ["sensor-02"]);
    }
}
