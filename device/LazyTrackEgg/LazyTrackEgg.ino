/*
  LazyTrack Egg: your Mochi in your pocket.

  An ESP32 + a 240x320 color screen + 3 buttons + a buzzer. It connects to WiFi, logs into your
  LazyTrack account and shows the same Mochi as the app: its look, HP, level, age, how it's
  doing right now (drunk, high, coughing) and what you're up to. It reads your friend card from
  the friends server every minute, so it works anywhere with WiFi (a phone hotspot too).

  Buttons:  A = next screen (Mochi / stats / info)   B = sync now   C = screen off / on

  Setup: see README.md next to this folder. Short version:
    1. Arduino IDE: board "ESP32 Dev Module", libraries "GFX Library for Arduino" + "ArduinoJson"
    2. copy secrets.example.h to secrets.h and fill in your WiFi + LazyTrack login
    3. pick your screen chip below, check the pins, upload
*/
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <ArduinoJson.h>
#include <Arduino_GFX_Library.h>
#include "secrets.h"

// ---------------------------------------------------------------- your hardware
// The chip on the back of the screen board: ST7789 (most 2.0") or ILI9341 (most 2.4" / 2.8")
// #define SCREEN_ILI9341
#define SCREEN_ST7789
// Colors look like a photo negative? flip this
#define SCREEN_INVERT false

// SPI screen wiring (ESP32 DevKit). These are the same pins as a TFT_eSPI setup (User_Setup.h), so a
// screen that already worked with TFT_eSPI works here without rewiring. Change to match your wires.
#define PIN_SCK 18   // SCK / CLK
#define PIN_MOSI 23  // SDA / MOSI / DIN
#define PIN_CS 15    // CS
#define PIN_DC 2     // DC / RS / A0
#define PIN_RST 4    // RST / RES
#define PIN_BL -1    // BL / LED (backlight) pin, or -1 when it's wired straight to 3.3V (then C only blanks the screen)
#define BTN_A 32     // buttons: one leg to the pin, the other to GND (no resistors needed)
#define BTN_B 33
#define BTN_C 25
#define PIN_BUZZ 26  // passive buzzer: + to this pin, - to GND

// the friends server (same as the app; this key is the public one made to sit in apps)
static const char *SUPA_URL = "https://zmrzumltciteeznzjwaw.supabase.co";
static const char *SUPA_KEY = "sb_publishable_mmX_kRPufTOQk1M4fmjjFg_HenbqicQ";

// ---------------------------------------------------------------- screen
Arduino_DataBus *bus = new Arduino_ESP32SPI(PIN_DC, PIN_CS, PIN_SCK, PIN_MOSI, GFX_NOT_DEFINED);
#if defined(SCREEN_ST7789)
Arduino_GFX *gfx = new Arduino_ST7789(bus, PIN_RST, 0 /* portrait */, SCREEN_INVERT, 240, 320);
#else
Arduino_GFX *gfx = new Arduino_ILI9341(bus, PIN_RST, 0 /* portrait */, SCREEN_INVERT);
#endif

static uint16_t rgb(uint32_t hex) { return ((hex >> 8) & 0xF800) | ((hex >> 5) & 0x07E0) | ((hex >> 3) & 0x001F); }
static const uint16_t INK = 0x18E4;  // #1d1b20, the app's outline color
static uint16_t BG, CARD, SOFT, YELLOW, GREEN, RED, GREY;

// ---------------------------------------------------------------- what we know
struct Card {
  bool ok = false;
  int hp = 100, gen = 1, age = 0, best = 0, deaths = 0;
  String tier = "okay", tierName = "OKAY", name = "Mochi", nowWhat = "";
  uint32_t nowSince = 0;
  int drunk = 0, high = 0, coughy = 0;
  bool sprite = false;
  uint16_t pal[36];
  char fn[257], fb[257];  // normal frame, blinking frame
  String stats[8];
  int nstats = 0;
} card;

Preferences prefs;
String token, refresh, uid;
uint32_t tokenUntil = 0, lastSync = 0, lastTry = 0;
String status = "Starting...";
int view = 0;  // 0 Mochi, 1 stats, 2 info
bool screenOn = true;

// Mochi before the first sync (the plain bean)
static const char *DEFAULT_N =
  "................"
  "................"
  "................"
  "................"
  "....11111111...."
  "...1222222221..."
  "..122222222221.."
  "..122122221221.."
  "..122122221221.."
  "..133222222331.."
  "..122212212221.."
  "..122221122221.."
  "..142222222241.."
  "...1422222241..."
  "....121..121...."
  "....111..111....";

// ---------------------------------------------------------------- sounds (same tunes as the app)
void beep(int f, int ms) { tone(PIN_BUZZ, f, ms); delay(ms + 10); }
void tuneBoot() { beep(523, 70); beep(659, 70); beep(784, 120); }
void tuneClick() { beep(1319, 25); }
void tuneUp() { beep(523, 90); beep(659, 90); beep(784, 90); beep(1047, 200); }
void tuneDown() { beep(392, 160); beep(330, 260); }

// ---------------------------------------------------------------- talking to the friends server
WiFiClientSecure tls;

// POST/GET with the right headers. Returns the HTTP status (or a negative error), fills body.
int http(const String &method, const String &path, const String &payload, String &body, bool auth) {
  HTTPClient h;
  h.setTimeout(12000);
  if (!h.begin(tls, String(SUPA_URL) + path)) return -1;
  h.addHeader("apikey", SUPA_KEY);
  h.addHeader("Content-Type", "application/json");
  if (auth && token.length()) h.addHeader("Authorization", "Bearer " + token);
  int code = method == "POST" ? h.POST(payload) : h.GET();
  body = code > 0 ? h.getString() : "";
  h.end();
  return code;
}

bool takeSession(const String &body) {
  JsonDocument d;
  if (deserializeJson(d, body)) return false;
  if (!d["access_token"].is<const char *>()) return false;
  token = d["access_token"].as<String>();
  refresh = d["refresh_token"].as<String>();
  uid = d["user"]["id"].as<String>();
  tokenUntil = millis() / 1000 + (d["expires_in"] | 3600) - 120;
  prefs.putString("refresh", refresh);
  prefs.putString("uid", uid);
  return true;
}

bool login() {
  String body;
  if (refresh.length()) {  // stay logged in without sending the password again
    if (http("POST", "/auth/v1/token?grant_type=refresh_token", "{\"refresh_token\":\"" + refresh + "\"}", body, false) == 200 && takeSession(body)) return true;
  }
  JsonDocument q;
  q["email"] = LT_EMAIL;
  q["password"] = LT_PASSWORD;
  String payload;
  serializeJson(q, payload);
  int code = http("POST", "/auth/v1/token?grant_type=password", payload, body, false);
  if (code == 200 && takeSession(body)) return true;
  status = code == 400 ? "Login failed: check secrets.h" : "Can't reach the server (" + String(code) + ")";
  return false;
}

uint16_t hexColor(const char *s) { return (s && s[0] == '#') ? rgb(strtoul(s + 1, nullptr, 16)) : INK; }

bool parseCard(const String &body) {
  JsonDocument filter;
  JsonObject f = filter[0]["card"].to<JsonObject>();
  for (const char *k : {"hp", "tier", "tierName", "name", "gen", "age", "best", "deaths", "fx", "now", "sprite", "offline"}) f[k] = true;
  f["stats"][0]["s"] = true;
  f["stats"][0]["v"] = true;
  f["stats"][0]["unit"] = true;
  JsonDocument d;
  if (deserializeJson(d, body, DeserializationOption::Filter(filter))) return false;
  JsonObject c = d[0]["card"];
  if (c.isNull()) { status = "No card yet: open LazyTrack once"; return false; }
  String oldTier = card.tier;
  bool first = !card.ok;
  card.hp = c["hp"] | 100;
  card.tier = c["tier"] | "okay";
  card.tierName = c["tierName"] | "OKAY";
  card.name = c["name"] | "Mochi";
  card.gen = c["gen"] | 1;
  card.age = c["age"] | 0;
  card.best = c["best"] | 0;
  card.deaths = c["deaths"] | 0;
  card.drunk = c["fx"]["drunk"] | 0;
  card.high = c["fx"]["high"] | 0;
  card.coughy = c["fx"]["coughy"] | 0;
  card.nowWhat = c["now"]["what"] | "";
  card.nowSince = c["now"]["since"] | 0;
  JsonObject sp = c["sprite"];
  const char *n = sp["n"], *b = sp["b"];
  if (n && strlen(n) == 256) {
    int i = 0;
    for (JsonVariant p : sp["p"].as<JsonArray>()) { if (i < 36) card.pal[i++] = hexColor(p.as<const char *>()); }
    strncpy(card.fn, n, 256); card.fn[256] = 0;
    strncpy(card.fb, (b && strlen(b) == 256) ? b : n, 256); card.fb[256] = 0;
    card.sprite = true;
  }
  card.nstats = 0;
  for (JsonObject s : c["stats"].as<JsonArray>()) {
    if (card.nstats >= 8) break;
    long v = s["v"] | 0;
    String val = String(s["unit"] | "") == "seconds" ? String(v / 3600) + "h " + String((v % 3600) / 60) + "m" : String(v);
    card.stats[card.nstats++] = String(s["s"] | "?") + "  " + val;
  }
  card.ok = true;
  if (!first && oldTier != card.tier) {  // Mochi got better or worse: let you know
    static const char *order[] = {"sick", "tired", "okay", "shiny", "legend"};
    int a = 0, z = 0;
    for (int i = 0; i < 5; i++) { if (oldTier == order[i]) a = i; if (card.tier == order[i]) z = i; }
    z > a ? tuneUp() : tuneDown();
  }
  return true;
}

void sync() {
  lastTry = millis();
  if (WiFi.status() != WL_CONNECTED) { status = "No WiFi"; return; }
  if (!token.length() || millis() / 1000 > tokenUntil) { status = "Logging in..."; if (!login()) return; }
  String body;
  int code = http("GET", "/rest/v1/cards?user_id=eq." + uid + "&select=card,updated_at", "", body, true);
  if (code == 401) { token = ""; if (login()) code = http("GET", "/rest/v1/cards?user_id=eq." + uid + "&select=card,updated_at", "", body, true); }
  if (code == 200 && parseCard(body)) { lastSync = millis(); status = "Synced"; }
  else if (code != 200) status = "Sync failed (" + String(code) + ")";
}

// ---------------------------------------------------------------- drawing
static const int SX = 32, SY = 52, SW = 176, SH = 176;  // the stage, where Mochi lives
static const int SCALE = 10;                            // 16 px sprite -> 160 px
uint16_t *stage;                                        // drawn here, then sent in one go (no flicker)

uint16_t tierBg() {
  if (card.tier == "legend") return rgb(0xffeeb0);
  if (card.tier == "tired") return rgb(0xd9d3e6);
  if (card.tier == "sick") return rgb(0xd3dbc4);
  return rgb(0xc9b8ff);
}

uint16_t cellColor(char ch, bool fromCard) {
  if (!fromCard) {  // the default bean
    switch (ch) { case '1': return INK; case '2': return rgb(0xb9a7ff); case '3': return rgb(0xff8fb1); case '4': return rgb(0x9686d6); }
    return 0;
  }
  int i = (ch >= '0' && ch <= '9') ? ch - '0' : (ch >= 'a' && ch <= 'z') ? ch - 'a' + 10 : -1;
  return i >= 0 && i < 36 ? card.pal[i] : 0;
}

void drawStage(int dx, int dy, bool blink) {
  uint16_t bg = tierBg();
  for (int i = 0; i < SW * SH; i++) stage[i] = bg;
  // grass strip, like the app's park
  for (int y = SH - 18; y < SH; y++) for (int x = 0; x < SW; x++) stage[y * SW + x] = rgb((y - (SH - 18)) < 4 ? 0x9be8a9 : 0x7cc97f);
  const char *px = card.sprite ? (blink ? card.fb : card.fn) : DEFAULT_N;
  int ox = (SW - 16 * SCALE) / 2 + dx, oy = SH - 18 - 16 * SCALE + 6 + dy;   // standing on the grass
  for (int cy = 0; cy < 16; cy++) for (int cx = 0; cx < 16; cx++) {
    char ch = px[cy * 16 + cx];
    if (ch == '.') continue;
    uint16_t col = cellColor(ch, card.sprite);
    if (!col && !card.sprite) continue;
    for (int y = 0; y < SCALE; y++) {
      int yy = oy + cy * SCALE + y;
      if (yy < 0 || yy >= SH) continue;
      for (int x = 0; x < SCALE; x++) {
        int xx = ox + cx * SCALE + x;
        if (xx >= 0 && xx < SW) stage[yy * SW + xx] = col;
      }
    }
  }
  gfx->draw16bitRGBBitmap(SX, SY, stage, SW, SH);
  gfx->drawRect(SX - 3, SY - 3, SW + 6, SH + 6, INK);
  gfx->drawRect(SX - 2, SY - 2, SW + 4, SH + 4, INK);
}

String doing() {
  const String &w = card.nowWhat;
  String s = w == "coding" ? "Coding" : w == "youtube" ? "On YouTube" : w == "social" ? "On socials" : w == "streaming" ? "Watching"
           : w == "gaming" ? "Gaming" : w == "browsing" ? "Browsing" : w == "away" ? "Away" : w.length() ? "On the computer" : "";
  if (s.length() && w != "away" && card.nowSince) {
    long mins = (long)(time(nullptr) - (time_t)card.nowSince) / 60;
    if (mins > 0 && mins < 1440) s += mins >= 60 ? " " + String(mins / 60) + "h" + String(mins % 60) + "m" : " " + String(mins) + "m";
  }
  return s;
}

void text(int x, int y, const String &s, uint8_t size, uint16_t col) {
  gfx->setTextSize(size);
  gfx->setTextColor(col);
  gfx->setCursor(x, y);
  gfx->print(s);
}

void drawFrame() {  // everything that isn't the stage
  gfx->fillScreen(BG);
  if (view == 0) {
    text(14, 14, card.name.substring(0, 16), 2, INK);
    text(14, 34, "MOCHI #" + String(card.gen) + "  " + String(card.age) + "D OLD", 1, GREY);
    // HP: 10 boxes, like the app
    text(14, 248, "HP", 2, INK);
    int on = (card.hp + 5) / 10;
    for (int i = 0; i < 10; i++) {
      int x = 46 + i * 15;
      gfx->drawRect(x, 248, 12, 14, INK);
      if (i < on) gfx->fillRect(x + 2, 250, 8, 10, card.tier == "sick" ? RED : card.tier == "legend" ? YELLOW : GREEN);
    }
    text(200, 248, String(card.hp), 2, INK);
    gfx->fillRoundRect(14, 272, 12 + card.tierName.length() * 12, 22, 5, YELLOW);
    gfx->drawRoundRect(14, 272, 12 + card.tierName.length() * 12, 22, 5, INK);
    text(20, 276, card.tierName, 2, INK);
    String fx = card.drunk ? "DRUNK" : card.high ? "HIGH" : card.coughy ? "COUGHING" : "";
    if (fx.length()) text(26 + card.tierName.length() * 12, 280, fx, 1, RED);
    text(14, 304, doing().length() ? doing() : status, 1, GREY);
  } else if (view == 1) {
    text(14, 14, "TODAY", 2, INK);
    for (int i = 0; i < card.nstats; i++) text(14, 48 + i * 30, card.stats[i], 2, INK);
    if (!card.nstats) text(14, 48, "No stats shared yet", 1, GREY);
    text(14, 304, "Record " + String(max(card.best, card.age)) + "d  Deaths " + String(card.deaths), 1, GREY);
  } else {
    text(14, 14, "INFO", 2, INK);
    text(14, 48, "WiFi: " + (WiFi.status() == WL_CONNECTED ? WiFi.SSID() : String("not connected")), 1, INK);
    text(14, 66, "IP: " + WiFi.localIP().toString(), 1, INK);
    text(14, 84, "Status: " + status, 1, INK);
    text(14, 102, "Last sync: " + (lastSync ? String((millis() - lastSync) / 1000) + "s ago" : String("never")), 1, INK);
    text(14, 140, "A  next screen", 1, GREY);
    text(14, 156, "B  sync now", 1, GREY);
    text(14, 172, "C  screen off / on", 1, GREY);
  }
}

// ---------------------------------------------------------------- buttons
struct Btn { int pin; bool down = false; uint32_t at = 0; } btns[3] = {{BTN_A}, {BTN_B}, {BTN_C}};

int pressed() {  // which button was just pressed (0-2), or -1
  for (int i = 0; i < 3; i++) {
    bool d = digitalRead(btns[i].pin) == LOW;
    if (d && !btns[i].down && millis() - btns[i].at > 180) { btns[i].down = true; btns[i].at = millis(); return i; }
    if (!d) btns[i].down = false;
  }
  return -1;
}

void setScreen(bool on) {
  screenOn = on;
  if (PIN_BL >= 0) digitalWrite(PIN_BL, on ? HIGH : LOW);
  on ? gfx->displayOn() : gfx->displayOff();
}

// ---------------------------------------------------------------- main
void setup() {
  Serial.begin(115200);
  for (auto &b : btns) pinMode(b.pin, INPUT_PULLUP);
  if (PIN_BL >= 0) { pinMode(PIN_BL, OUTPUT); digitalWrite(PIN_BL, HIGH); }
  if (!gfx->begin()) Serial.println("screen didn't start: check the wiring and the chip setting");
  BG = rgb(0xefeaff); CARD = rgb(0xffffff); SOFT = rgb(0xf3f0fb); YELLOW = rgb(0xffe45e); GREEN = rgb(0x3fbf6f); RED = rgb(0xe0303a); GREY = rgb(0x6a6475);
  stage = (uint16_t *)malloc(SW * SH * 2);
  prefs.begin("lazytrack", false);
  refresh = prefs.getString("refresh", "");
  uid = prefs.getString("uid", "");
  tls.setInsecure();  // a pocket toy: skip certificate checks (the server still needs your login)
  status = "Connecting to WiFi...";
  drawFrame();
  drawStage(0, 0, false);
  tuneBoot();
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) delay(250);
  configTime(0, 0, "pool.ntp.org");  // the clock, for "coding for 25 min"
  sync();
  drawFrame();
}

void loop() {
  static uint32_t tick = 0, nextBlink = 4000, blinkEnd = 0;
  int b = pressed();
  if (b == 2) { tuneClick(); setScreen(!screenOn); if (screenOn) drawFrame(); }
  else if (b >= 0 && !screenOn) { setScreen(true); drawFrame(); }
  else if (b == 0) { tuneClick(); view = (view + 1) % 3; drawFrame(); }
  else if (b == 1) { tuneClick(); status = "Syncing..."; drawFrame(); sync(); tuneClick(); drawFrame(); }

  if (millis() - lastTry > 60000) { int before = card.hp; String tb = card.tierName; sync(); if (card.hp != before || card.tierName != tb || view == 2) drawFrame(); }
  if (WiFi.status() != WL_CONNECTED && millis() - lastTry > 15000) { WiFi.reconnect(); lastTry = millis(); }

  if (screenOn && view == 0 && millis() - tick > 120) {  // ~8 fps: bob, blink, wobble when drunk, shake when coughing
    tick = millis();
    uint32_t t = millis();
    if (t > nextBlink) { blinkEnd = t + 160; nextBlink = t + 3500 + random(2500); }
    int dy = (t / 700) % 2 ? -4 : 0;
    int dx = 0;
    if (card.drunk) dx = (int)(sin(t / (500.0 - card.drunk * 100)) * (3 + card.drunk * 2));
    else if (card.high) dy += (int)(sin(t / 900.0) * 3);
    if (card.coughy && (t / 300) % (14 - card.coughy * 3) == 0) dx = ((t / 60) % 2) ? 3 : -3;
    drawStage(dx, dy, t < blinkEnd);
  }
}
