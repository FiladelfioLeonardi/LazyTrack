/*
  LazyTrack Egg: your Mochi in your pocket.

  An ESP32 + a 240x320 color screen + 3 buttons + a buzzer. It connects to WiFi, logs into your
  LazyTrack account and shows the same Mochi as the app: its look, HP, level, age, how it's
  doing right now (drunk, high, coughing) and what you're up to. It reads your friend card from
  the friends server every minute, so it works anywhere with WiFi (a phone hotspot too).

  Buttons (like a Tamagotchi):  A = pet / back   B = menu / OK   C = next
    Home:     B opens the menu, A pets Mochi (hearts!), C turns the screen off (any button wakes it)
    Menu:     Log something (cigarette, water, gym... whatever you track), Friends (see their Mochi,
              poke them), Today's stats, Info
  Friends poking you make the egg buzz. Logs go to LazyTrack within a minute (needs supabase/egg.sql).

  Setup: see README.md next to this folder. Short version:
    1. Arduino IDE: board "ESP32 Dev Module", libraries "GFX Library for Arduino" + "ArduinoJson"
    2. copy secrets.example.h to secrets.h and fill in your WiFi + LazyTrack login
    3. pick your screen chip below, check the pins, upload
*/
#include <WiFi.h>
#include <WiFiMulti.h>
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
// SPI speed for the screen. 27 MHz is steady over breadboard jumper wires. Once it's soldered with short
// wires (in the case), try 40000000: everything gets ~1.5x faster. White screen = too fast, go back.
#define SPI_HZ 27000000

// (types used in function parameters live up here: Arduino IDE declares all functions near the top)
struct CoolF { bool on; int shades; int glint; bool smirk, nod; };   // one moment of the legendary cool move

// the friends server (same as the app; this key is the public one made to sit in apps)
static const char *SUPA_URL = "https://zmrzumltciteeznzjwaw.supabase.co";
static const char *SUPA_KEY = "sb_publishable_mmX_kRPufTOQk1M4fmjjFg_HenbqicQ";

// ---------------------------------------------------------------- screen
Arduino_DataBus *bus = new Arduino_ESP32SPI(PIN_DC, PIN_CS, PIN_SCK, PIN_MOSI, GFX_NOT_DEFINED);
#if defined(SCREEN_ST7789)
Arduino_GFX *lcd = new Arduino_ST7789(bus, PIN_RST, 0 /* portrait */, SCREEN_INVERT, 240, 320);
#else
Arduino_GFX *lcd = new Arduino_ILI9341(bus, PIN_RST, 0 /* portrait */, SCREEN_INVERT);
#endif

// Smooth screens: a whole screen is drawn in memory first, then sent in one go, so nothing flickers or
// builds up line by line. A full 240x320 picture doesn't fit in the ESP32's memory, so it's done in
// bands of 80 rows: everything is drawn with screen coordinates, and the band
// only keeps the rows it covers.
class Band : public Arduino_GFX {
 public:
  uint16_t *fb = nullptr;
  int16_t oy = 0, bh = 0, ox = 0;   // ox: everything drawn is shifted sideways by this much (for slides)
  Band() : Arduino_GFX(240, 320) {}
  bool begin(int32_t) override {
    for (int h : {80, 40}) { fb = (uint16_t *)malloc(240 * h * 2); if (fb) { bh = h; return true; } }   // 80 rows = 38 KB: plenty left for WiFi + HTTPS
    return false;
  }
  void writePixelPreclipped(int16_t x, int16_t y, uint16_t c) override {
    y -= oy; x += ox;
    if (y >= 0 && y < bh && x >= 0 && x < 240) fb[y * 240 + x] = c;
  }
  void writeFillRectPreclipped(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t c) override {
    x += ox;
    int y0 = max(0, y - oy), y1 = min((int)bh, y - oy + h), x0 = max(0, (int)x), x1 = min(240, x + w);
    for (int yy = y0; yy < y1; yy++) { uint16_t *p = fb + yy * 240 + x0; for (int xx = x0; xx < x1; xx++) *p++ = c; }
  }
  void writeFastHLine(int16_t x, int16_t y, int16_t w, uint16_t c) override { writeFillRectPreclipped(x, y, w, 1, c); }
  void writeFastVLine(int16_t x, int16_t y, int16_t h, uint16_t c) override { writeFillRectPreclipped(x, y, 1, h, c); }
  void draw16bitRGBBitmap(int16_t x, int16_t y, uint16_t *bmp, int16_t w, int16_t h) override {
    for (int r = 0; r < h; r++) {
      int yy = y + r - oy;
      if (yy < 0 || yy >= bh) continue;
      for (int cx = 0; cx < w; cx++) { int xx = x + ox + cx; if (xx >= 0 && xx < 240) fb[yy * 240 + xx] = bmp[r * w + cx]; }
    }
  }
} band;
bool banded = false;
Arduino_GFX *gfx = lcd;   // where drawing goes: the band while a screen is being put together

static uint16_t rgb(uint32_t hex) { return ((hex >> 8) & 0xF800) | ((hex >> 5) & 0x07E0) | ((hex >> 3) & 0x001F); }
static const uint16_t INK = 0x18E4;  // #1d1b20, the app's outline color
static uint16_t BG, CARD, SOFT, YELLOW, GREEN, RED, GREY;

// ---------------------------------------------------------------- what we know
struct Card {
  bool ok = false;
  int hp = 100, gen = 1, age = 0, best = 0, deaths = 0;
  String tier = "okay", tierName = "OKAY", name = "Mochi", nowWhat = "", nowLabel = "";
  uint32_t nowSince = 0, built = 0;
  bool offline = false;
  String mood = "happy";
  int tzo = 60;             // your time zone, minutes from UTC (the app sends it)
  int drunk = 0, high = 0, coughy = 0;
  bool sprite = false;
  uint16_t pal[36];
  char fn[257], fb[257];  // normal frame, blinking frame
  char fh[257], fd[3][257], fc[257];   // high, tipsy/drunk/wasted, coughing (same look, those faces)
  bool fxFrames = false;
  int sceneN = 0;         // Mochi's place, the same picture as in the app (sceneN x sceneN pixels)
  uint16_t scenePal[64];
  char scene[56 * 56 + 1];
  String stats[8];
  int nstats = 0;
} card;

// (Arduino IDE declares every function near the top of the sketch, so no function takes these as a parameter)
struct Habit { String k, n; bool bad = false; };
struct Stat { String s, v; bool bad = false, good = false; long tv = 0, yv = 0; bool secs = false, hasY = false; int8_t sign = 0; };   // a line on the Today screen
// (sign: 1 = more is better, -1 = less is better, 0 = neither)
struct Friend {
  String id, name, user, tier = "okay", what, label;
  int hp = 0, drunk = 0, high = 0, coughy = 0;
  bool online = false, hasCard = false, sprite = false;
  time_t seen = 0;
  uint16_t pal[36];
  char px[257];
};

Preferences prefs;
WiFiMulti wifi;   // tries every network in secrets.h and joins whichever is around
String token, refresh, uid;
uint32_t tokenUntil = 0, lastSync = 0, lastTry = 0;
String status = "Starting...";
enum { V_HOME, V_STATS, V_INFO, V_MENU, V_LOG, V_FRIENDS, V_CMP };
int view = V_HOME, menuSel = 0;
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
void tunePet() { beep(1568, 40); beep(1976, 40); beep(2349, 70); }                         // happy chirp
void tuneGood() { beep(784, 60); beep(1047, 60); beep(1319, 110); }                         // logged something good
void tuneBad() { beep(659, 80); beep(523, 120); }                                          // logged a bad habit
void tunePoke() { for (int i = 0; i < 2; i++) { beep(784, 50); beep(1175, 70); } }          // boop-boop, same as the app
void tuneLove() { beep(1047, 80); beep(1319, 80); beep(1568, 80); beep(2093, 160); }

// ---------------------------------------------------------------- talking to the friends server
WiFiClientSecure tls;

// POST/GET with the right headers. Returns the HTTP status (or a negative error), fills body.
int http(const String &method, const String &path, const String &payload, String &body, bool auth, const char *prefer = nullptr) {
  HTTPClient h;
  h.setTimeout(12000);
  if (!h.begin(tls, String(SUPA_URL) + path)) return -1;
  h.addHeader("apikey", SUPA_KEY);
  h.addHeader("Content-Type", "application/json");
  if (auth && token.length()) h.addHeader("Authorization", "Bearer " + token);
  if (prefer) h.addHeader("Prefer", prefer);
  int code = method == "POST" ? h.POST(payload) : method == "GET" ? h.GET() : h.sendRequest(method.c_str(), payload);
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
  for (const char *k : {"hp", "tier", "tierName", "name", "gen", "age", "best", "deaths", "fx", "now", "sprite", "scene", "offline", "built", "mood", "tzo"}) f[k] = true;
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
  card.nowLabel = c["now"]["label"] | "";
  card.offline = c["offline"] | false;
  card.built = c["built"] | 0;
  card.mood = c["mood"] | "happy";
  card.tzo = c["tzo"] | 60;
  JsonObject sp = c["sprite"];
  const char *n = sp["n"], *b = sp["b"];
  if (n && strlen(n) == 256) {
    int i = 0;
    for (JsonVariant p : sp["p"].as<JsonArray>()) { if (i < 36) card.pal[i++] = hexColor(p.as<const char *>()); }
    strncpy(card.fn, n, 256); card.fn[256] = 0;
    strncpy(card.fb, (b && strlen(b) == 256) ? b : n, 256); card.fb[256] = 0;
    auto take = [&](const char *key, char *dst) { const char *v = sp[key]; strncpy(dst, (v && strlen(v) == 256) ? v : n, 256); dst[256] = 0; return v != nullptr; };
    card.fxFrames = take("h", card.fh);
    take("d1", card.fd[0]); take("d2", card.fd[1]); take("d3", card.fd[2]); take("c", card.fc);
    card.sprite = true;
  }
  JsonObject sc = c["scene"];
  const char *sd = sc["d"];
  int sn = sc["w"] | 0;
  if (sd && sn > 0 && sn <= 56 && (int)strlen(sd) == sn * sn) {
    int i = 0;
    for (JsonVariant p : sc["p"].as<JsonArray>()) { if (i < 64) card.scenePal[i++] = hexColor(p.as<const char *>()); }
    memcpy(card.scene, sd, sn * sn); card.scene[sn * sn] = 0;
    card.sceneN = sn;
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
  if (WiFi.status() != WL_CONNECTED) { status = card.ok ? "Offline: your Mochi from earlier" : "No WiFi"; return; }
  if (!token.length() || millis() / 1000 > tokenUntil) { status = "Logging in..."; if (!login()) return; }
  String body;
  int code = http("GET", "/rest/v1/cards?user_id=eq." + uid + "&select=card,updated_at", "", body, true);
  if (code == 401) { token = ""; if (login()) code = http("GET", "/rest/v1/cards?user_id=eq." + uid + "&select=card,updated_at", "", body, true); }
  if (code == 200 && parseCard(body)) { lastSync = millis(); status = "Synced"; prefs.putBytes("card", body.c_str(), body.length()); }   // kept for offline
  else if (code != 200) status = "Sync failed (" + String(code) + ")";
}

// a logged-in request: logs in (again) when needed. Returns the HTTP status or a negative error.
int api(const String &method, const String &path, const String &payload, String &body, const char *prefer = nullptr) {
  if (WiFi.status() != WL_CONNECTED) return -2;
  if (!token.length() || millis() / 1000 > tokenUntil) { if (!login()) return -3; }
  int code = http(method, path, payload, body, true, prefer);
  if (code == 401) { token = ""; if (login()) code = http(method, path, payload, body, true, prefer); }
  return code;
}

// "2026-10-17T19:22:11.123+00:00" (the server's UTC time) -> seconds since 1970
time_t parseIso(const char *s) {
  int Y, M, D, h, m, sec;
  if (!s || sscanf(s, "%d-%d-%dT%d:%d:%d", &Y, &M, &D, &h, &m, &sec) != 6) return 0;
  Y -= M <= 2;
  long era = (Y >= 0 ? Y : Y - 399) / 400;
  long yoe = Y - era * 400, doy = (153 * (M + (M > 2 ? -3 : 9)) + 2) / 5 + D - 1;
  long days = era * 146097 + yoe * 365 + yoe / 4 - yoe / 100 + doy - 719468;
  return (time_t)days * 86400 + h * 3600 + m * 60 + sec;
}

String isoAt(time_t t) {
  struct tm g;
  gmtime_r(&t, &g);
  char b[24];
  strftime(b, sizeof b, "%Y-%m-%dT%H:%M:%SZ", &g);
  return b;
}

bool clockOk() { return time(nullptr) > 1700000000; }

String agoText(time_t t) {
  if (!t || !clockOk()) return "a while ago";
  long s = (long)(time(nullptr) - t);
  if (s < 90) return "just now";
  if (s < 3600) return String(s / 60) + " min ago";
  if (s < 86400) return String(s / 3600) + "h ago";
  return String(s / 86400) + "d ago";
}

// ---------------------------------------------------------------- what's on top of Home for a moment
String msgTitle, msgSub;
uint32_t msgUntil = 0, petUntil = 0, shakeUntil = 0;
long undoId = 0;            // the last log, while it can still be taken back (C)
void drawFrame();

void showMsg(const String &title, const String &sub, uint32_t ms) {
  msgTitle = title; msgSub = sub; msgUntil = millis() + ms;
  view = V_HOME;
  drawFrame();
}

// ---------------------------------------------------------------- logging from the egg
Habit habits[16];
int nhabits = 0, logSel = 0;
uint32_t habitsAt = 0;

Stat today[24];
int ntoday = 0, todayPage = 0, todaySel = 0;

String fmtVal(long v, bool secs) {   // 3h20m / 45m for times, plain numbers otherwise
  if (!secs) return String(v);
  return v >= 3600 ? String(v / 3600) + "h" + String((v % 3600) / 60) + "m" : String(v / 60) + "m";
}

// egg_setup holds {"list": [your habits], "today": [today's numbers]} (older LazyTrack: just the list)
void parseSetup(const String &body) {
  JsonDocument d;
  if (deserializeJson(d, body)) return;
  JsonVariant h = d[0]["habits"];
  JsonArray list = h.is<JsonArray>() ? h.as<JsonArray>() : h["list"].as<JsonArray>();
  int n = 0;
  for (JsonObject x : list) {
    if (n >= 16) break;
    habits[n].k = x["k"] | ""; habits[n].n = x["n"] | ""; habits[n].bad = x["bad"] | false;
    if (habits[n].k.length()) n++;
  }
  if (n) nhabits = n;
  int t = 0;
  for (JsonObject x : h["today"].as<JsonArray>()) {
    if (t >= 24) break;
    long v = x["v"] | 0;
    today[t].s = x["s"] | "";
    today[t].secs = String(x["u"] | "") == "s";
    today[t].tv = v;
    today[t].v = fmtVal(v, today[t].secs);
    today[t].hasY = !x["y"].isNull();
    today[t].yv = x["y"] | 0;
    today[t].sign = x["bad"].isNull() ? 0 : (x["bad"].as<bool>() ? -1 : 1);
    today[t].bad = (x["bad"] | false) && v > 0;
    today[t].good = !(x["bad"] | true) && v > 0;
    t++;
  }
  if (t) ntoday = t;
}

void loadHabits() {   // your habits + today's numbers, put in egg_setup by LazyTrack
  habitsAt = millis();
  String body;
  if (api("GET", "/rest/v1/egg_setup?select=habits", "", body) != 200) return;
  parseSetup(body);
  if (nhabits) prefs.putString("habits", body);
}

// logs made without WiFi wait here ("kind,time" lines) and go out on the next sync
void queueLog(const String &k, time_t t) {
  String q = prefs.getString("queue", "");
  if (q.length() < 1500) prefs.putString("queue", q + k + "," + String((long)t) + "\n");
}

bool sendLog(const String &k, time_t t, long *id) {
  JsonDocument q;
  q["kind"] = k;
  if (t > 1700000000) q["ts"] = isoAt(t);
  String pl, body;
  serializeJson(q, pl);
  int code = api("POST", "/rest/v1/egg_inbox", pl, body, "return=representation");
  if (code != 201) return false;
  if (id) { JsonDocument d; if (!deserializeJson(d, body)) *id = d[0]["id"] | 0; }
  return true;
}

void flushQueue() {
  String q = prefs.getString("queue", ""), left;
  if (!q.length()) return;
  int start = 0;
  while (start < (int)q.length()) {
    int nl = q.indexOf('\n', start);
    if (nl < 0) nl = q.length();
    String line = q.substring(start, nl);
    start = nl + 1;
    int comma = line.indexOf(',');
    if (comma <= 0) continue;
    if (!sendLog(line.substring(0, comma), (time_t)line.substring(comma + 1).toInt(), nullptr)) left += line + "\n";
  }
  prefs.putString("queue", left);
}

// Mochi reacts right away; LazyTrack's real numbers follow within a minute
void reactTo(const String &k) {
  if (k == "joint") card.high = min(3, card.high + 1);
  else if (k == "alcohol") card.drunk = max(1, card.drunk);
  else if (k == "cigarette" || k == "vape") card.coughy = max(1, card.coughy);
}

void doLog(int i) {
  const Habit &h = habits[i];
  time_t t = clockOk() ? time(nullptr) : 0;
  long id = 0;
  bool sent = sendLog(h.k, t, &id);
  if (!sent) queueLog(h.k, t);
  reactTo(h.k);
  undoId = sent ? id : 0;
  h.bad ? tuneBad() : tuneGood();
  showMsg(h.bad ? "LOGGED" : "NICE!", "1 " + h.n + (sent ? "   C = undo" : "   (sends once online)"), 5000);
}

void undoLog() {
  String body;
  int code = api("DELETE", "/rest/v1/egg_inbox?id=eq." + String(undoId), "", body, "return=representation");
  undoId = 0;
  tuneClick();
  showMsg("UNDONE", code == 200 && body.length() > 4 ? "Taken back" : "Already saved: undo it in the app", 3000);
  sync();
}

// ---------------------------------------------------------------- friends
Friend friends[12];
int nfriends = 0, friendSel = 0;
uint32_t friendsAt = 0;

void loadFriends() {
  friendsAt = millis();
  String body;
  if (api("GET", "/rest/v1/friendships?status=eq.accepted&select=requester,addressee", "", body) != 200) return;
  JsonDocument d;
  if (deserializeJson(d, body)) return;
  String ids[12];
  int n = 0;
  for (JsonObject r : d.as<JsonArray>()) {
    if (n >= 12) break;
    String a = r["requester"] | "", b = r["addressee"] | "";
    ids[n++] = a == uid ? b : a;
  }
  if (!n) { nfriends = 0; return; }
  String in = "(";
  for (int i = 0; i < n; i++) { if (i) in += ","; in += ids[i]; }
  in += ")";
  String pb, cb;
  JsonDocument pd, cd;
  if (api("GET", "/rest/v1/profiles?id=in." + in + "&select=id,username,display_name", "", pb) != 200 || deserializeJson(pd, pb)) return;
  bool cards = api("GET", "/rest/v1/cards?user_id=in." + in + "&select=user_id,updated_at,hp:card->hp,tier:card->>tier,fx:card->fx,"
                   "now:card->now,offline:card->offline,built:card->built,pal:card->sprite->p,px:card->sprite->n", "", cb) == 200
               && !deserializeJson(cd, cb);
  int k = 0;
  for (JsonObject p : pd.as<JsonArray>()) {
    if (k >= 12) break;
    Friend &f = friends[k];
    f = Friend();
    f.id = p["id"] | "";
    f.user = p["username"] | "";
    f.name = p["display_name"] | "";
    if (!f.name.length()) f.name = f.user;
    if (cards) for (JsonObject c : cd.as<JsonArray>()) {
      if (f.id != (const char *)(c["user_id"] | "")) continue;
      f.hasCard = true;
      f.hp = c["hp"] | 0;
      f.tier = c["tier"] | "okay";
      f.seen = parseIso(c["updated_at"] | "");
      bool off = c["offline"] | false;
      f.online = !off && clockOk() && f.seen && time(nullptr) - f.seen < 300;
      time_t built = c["built"] | 0;
      if (f.online && built && time(nullptr) - built < 1800) {   // older than 30 min: it may have worn off
        f.drunk = c["fx"]["drunk"] | 0; f.high = c["fx"]["high"] | 0; f.coughy = c["fx"]["coughy"] | 0;
      }
      if (f.online) { f.what = c["now"]["what"] | ""; f.label = c["now"]["label"] | ""; }
      const char *px = c["px"];
      if (px && strlen(px) == 256) {
        int i = 0;
        for (JsonVariant v : c["pal"].as<JsonArray>()) { if (i < 36) f.pal[i++] = hexColor(v.as<const char *>()); }
        memcpy(f.px, px, 256); f.px[256] = 0;
        f.sprite = true;
      }
    }
    k++;
  }
  nfriends = k;
  // online first, then by name
  for (int i = 1; i < nfriends; i++) for (int j = i; j > 0 && (friends[j].online > friends[j - 1].online ||
       (friends[j].online == friends[j - 1].online && friends[j].name < friends[j - 1].name)); j--) std::swap(friends[j], friends[j - 1]);
}

String nameOf(const String &id) {
  for (int i = 0; i < nfriends; i++) if (friends[i].id == id) return friends[i].name;
  return "";
}

void sendPoke(int i) {   // (an index, not a Friend&: Arduino declares functions above the struct)
  const Friend &f = friends[i];
  String body;
  int code = api("POST", "/rest/v1/pokes", "{\"receiver\":\"" + f.id + "\",\"kind\":\"poke\"}", body, "return=minimal");
  if (code == 201) { tunePoke(); status = "Poked " + f.name + "!"; }
  else { tuneDown(); status = code == 403 ? "Easy! Try again in a minute" : code == 404 ? "Pokes need supabase/egg.sql" : "Couldn't poke (" + String(code) + ")"; }
}

// a friend poked you (from their egg or their app): buzz, wake up, show who
void checkPokes() {
  String body;
  if (!uid.length() || api("GET", "/rest/v1/pokes?receiver=eq." + uid + "&seen_egg=is.null&order=id.asc&limit=5&select=id,kind,sender", "", body) != 200) return;
  JsonDocument d;
  if (deserializeJson(d, body)) return;
  JsonArray a = d.as<JsonArray>();
  if (!a.size()) return;
  String ids;
  for (JsonObject p : a) { if (ids.length()) ids += ","; ids += String((long)(p["id"] | 0)); }
  String r;
  api("PATCH", "/rest/v1/pokes?id=in.(" + ids + ")", "{\"seen_egg\":\"now\"}", r, "return=minimal");
  JsonObject p = a[a.size() - 1];
  String sender = p["sender"] | "", kind = p["kind"] | "poke";
  String who = nameOf(sender);
  if (!who.length()) { loadFriends(); who = nameOf(sender); }
  if (!who.length()) who = "A friend";
  if (!screenOn) { screenOn = true; if (PIN_BL >= 0) digitalWrite(PIN_BL, HIGH); lcd->displayOn(); }
  if (kind == "love") { petUntil = millis() + 3000; showMsg(who.substring(0, 14), "sent Mochi love <3", 6000); tuneLove(); }
  else if (kind == "wave") { shakeUntil = millis() + 1500; showMsg(who.substring(0, 14), "waved at you!", 6000); tunePoke(); }
  else { shakeUntil = millis() + 2000; showMsg(who.substring(0, 14), a.size() > 1 ? "poked you " + String(a.size()) + " times!" : "poked you!", 6000); tunePoke(); }
}

// ---------------------------------------------------------------- petting
int loveToday() {
  long day = clockOk() ? (long)(time(nullptr) / 86400) : 0;
  return prefs.getLong("loveDay", -1) == day ? prefs.getInt("love", 0) : 0;
}

void pet() {   // hearts, closed happy eyes, a little wiggle and a chirp; no text
  long day = clockOk() ? (long)(time(nullptr) / 86400) : 0;
  int n = loveToday() + 1;
  prefs.putLong("loveDay", day);
  prefs.putInt("love", n);
  petUntil = millis() + 1800;
  tunePet();
}

// ---------------------------------------------------------------- drawing
// The whole screen is Mochi's place (like Home in the app), Mochi stands on the ground in the middle,
// with your name on top and HP at the bottom. Only the part around Mochi is redrawn when it moves.
static const int FEET = 232;                            // where Mochi's feet touch the ground (screen y)
static const int SCALE = 7;                             // 16 px sprite -> 112 px: the same size next to its place as in the app
static const int PX = (240 - 16 * SCALE) / 2, PY = FEET + 2 - 16 * SCALE;   // Mochi's top-left when standing still
static const int SX = PX - 10, SY = PY - 2, SW = 16 * SCALE + 20, SH = 16 * SCALE + 4;   // the part redrawn
static const int SCENE_Y = FEET - 240 * 84 / 100;       // the scene is 240x240 (full width, like the app), ground at FEET
uint16_t *stage;                                        // drawn here, then sent in one go (no flicker)

uint16_t tierBg() {
  if (card.tier == "legend") return rgb(0xffeeb0);
  if (card.tier == "tired") return rgb(0xd9d3e6);
  if (card.tier == "sick") return rgb(0xd3dbc4);
  return rgb(0xc9b8ff);
}

int sceneIndex(char ch) {
  if (ch >= '0' && ch <= '9') return ch - '0';
  if (ch >= 'a' && ch <= 'z') return ch - 'a' + 10;
  if (ch >= 'A' && ch <= 'Z') return ch - 'A' + 36;
  return ch == '-' ? 62 : ch == '_' ? 63 : 0;
}

// the background at a screen pixel: the whole scene across the full width, exactly what the app shows;
// above it the sky goes on, below it the ground
uint16_t bgAt(int x, int y) {
  int n = card.sceneN;
  if (!n) return y > FEET ? rgb(0x7cc97f) : tierBg();
  int sx = x * n / 240, sy = (y - SCENE_Y) * n / 240;
  if (sy < 0) sy = 0; if (sy >= n) sy = n - 1;
  return card.scenePal[sceneIndex(card.scene[sy * n + sx])];
}

void drawBackground() {  // full screen, in strips through the stage buffer
  if (gfx == &band) {     // putting a screen together: just this band's rows
    for (int y = 0; y < band.bh; y++) for (int x = 0; x < 240; x++) band.fb[y * 240 + x] = bgAt(x, band.oy + y);
    return;
  }
  int rows = (SW * SH) / 240;
  for (int y0 = 0; y0 < 320; y0 += rows) {
    int h = min(rows, 320 - y0);
    for (int y = 0; y < h; y++) for (int x = 0; x < 240; x++) stage[y * 240 + x] = bgAt(x, y0 + y);
    gfx->draw16bitRGBBitmap(0, y0, stage, 240, h);
  }
}

uint16_t cellColor(char ch, bool fromCard) {
  if (!fromCard) {  // the default bean
    switch (ch) { case '1': return INK; case '2': return rgb(0xb9a7ff); case '3': return rgb(0xff8fb1); case '4': return rgb(0x9686d6); }
    return 0;
  }
  int i = (ch >= '0' && ch <= '9') ? ch - '0' : (ch >= 'a' && ch <= 'z') ? ch - 'a' + 10 : -1;
  return i >= 0 && i < 36 ? card.pal[i] : 0;
}

// Asleep: late at night where you are (1 to 7), or when the app says Mochi is sleepy
bool asleep() {
  if (card.mood == "sleepy") return true;
  if (!clockOk()) return false;
  long local = (long)(time(nullptr) % 86400) + card.tzo * 60L;
  int h = (int)(((local % 86400) + 86400) % 86400 / 3600);
  return h >= 1 && h < 7;
}

// Z's floating up from Mochi's head while he sleeps: drawn in their own little box above his head
// (sent on its own, so nothing else on the screen moves)
static const int ZX = 150, ZY = 58, ZW = 84, ZH = 64;
void drawZs(uint32_t t) {
  if (gfx != lcd || !banded) return;
  uint16_t *buf = band.fb;   // free between screens: used as scratch
  for (int y = 0; y < ZH; y++) for (int x = 0; x < ZW; x++) buf[y * ZW + x] = bgAt(ZX + x, ZY + y);
  static const char *Z5 = "XXXXX...X...X...X...XXXXX";   // a 5x5 Z
  for (int k = 0; k < 3; k++) {
    uint32_t life = (t + k * 1100) % 3300;                   // each Z: 3.3 s from his head up and away
    int sc = 2 + (int)(life * 3 / 3300);                      // grows as it rises: 2, 3, 4 px per dot
    int zy = ZH - 12 - (int)(life * (ZH - 6) / 3300);
    int zx = 4 + (int)(life * 40 / 3300) + (int)(sin((t + k * 700) / 380.0) * 5);
    uint16_t col = life > 2600 ? rgb(0x8c84a6) : rgb(0x3d3650);   // fades a little at the top
    for (int i = 0; i < 25; i++) if (Z5[i] == 'X')
      for (int y = 0; y < sc; y++) for (int x = 0; x < sc; x++) {
        int yy = zy + (i / 5) * sc + y, xx = zx + (i % 5) * sc + x;
        if (yy >= 0 && yy < ZH && xx >= 0 && xx < ZW) buf[yy * ZW + xx] = col;
      }
  }
  lcd->draw16bitRGBBitmap(ZX, ZY, buf, ZW, ZH);
}

// Mochi standing on the ground, with the face that fits right now (high, drunk, coughing) like in the app.
// dx: drunk wobble / coughing shake, sideways only (it never floats)
// ---------------------------------------------------------------- "deal with it": the legendary cool move
// Out of nowhere: shades drop onto Mochi's face, a glint runs across them while fire and explosions go off
// behind him, he smirks (toothpick), nods twice, and the shades fly off. Same timeline as the app.
uint32_t coolStart = 0, nextCool = 0;
CoolF coolFrame(uint32_t t) {
  static const int G[6] = {3, 4, 5, 10, 11, 12};
  if (t < 350) return {true, -3 + (int)(t / 70) * 2, -1, false, false};
  if (t < 450) return {true, 4, -1, false, false};
  if (t < 650) return {true, 5, -1, false, false};
  if (t < 1010) return {true, 5, G[min(5, (int)((t - 650) / 60))], false, false};
  if (t < 1460) return {true, 5, -1, true, false};
  if (t < 1760) return {true, 5, -1, true, true};
  if (t < 2020) return {true, 5, -1, true, false};
  if (t < 2320) return {true, 5, -1, true, true};
  if (t < 4520) return {true, 5, -1, true, false};
  if (t < 4730) { static const int Y[3] = {4, 1, -2}; return {true, Y[min(2, (int)((t - 4520) / 70))], -1, true, false}; }
  return {false, 0, -1, false, false};
}

// Mochi's 16x16 colours right now (0 = see-through), with the cool move drawn on top when it's on
void mochiNow(uint16_t cell[16][16], const char *px, const CoolF &cf) {
  for (int cy = 0; cy < 16; cy++) for (int cx = 0; cx < 16; cx++) {
    char ch = px[cy * 16 + cx];
    cell[cy][cx] = ch == '.' ? 0 : cellColor(ch, card.sprite);
  }
  if (!cf.on) return;
  static const char *SH[3] = {"..kkkkkkkkkkkk..", "...kggk..kggk...", "....kk....kk...."};
  const uint16_t K = rgb(0x1d1b20), LENS = rgb(0x3a3648), PICK = rgb(0xe3b980);
  if (cf.smirk) {
    uint16_t skin = cell[11][8] ? cell[11][8] : rgb(0xb9a7ff);
    for (int y = 9; y <= 10; y++) for (int x = 4; x < 12; x++)
      if (cell[y][x] == K || cell[y][x] == INK || cell[y][x] == 0xFFFF || cell[y][x] == rgb(0xe0303a)) cell[y][x] = skin;
    for (int x = 6; x <= 9; x++) cell[9][x] = K;          // flat mouth...
    cell[8][10] = K;                                       // ...one corner up
    cell[10][10] = cell[10][11] = cell[11][12] = PICK;     // toothpick
  }
  for (int i = 0; i < 3; i++) {
    int y = cf.shades + i;
    if (y < 0 || y > 15) continue;
    for (int x = 0; x < 16; x++) if (SH[i][x] != '.') cell[y][x] = SH[i][x] == 'k' ? K : LENS;
  }
  if (cf.glint >= 0 && cf.shades + 1 >= 0 && cf.shades + 1 < 16) cell[cf.shades + 1][cf.glint] = 0xFFFF;
  if (cf.nod) { for (int y = 13; y > 0; y--) memcpy(cell[y], cell[y - 1], sizeof cell[y]); memset(cell[0], 0, sizeof cell[0]); }
}

const char *framePx(bool blink, bool coughing) {
  if (!card.sprite) return DEFAULT_N;
  const char *px = blink || millis() < petUntil || asleep() ? card.fb : card.fn;   // eyes closed: petted (happy) or asleep
  if (card.fxFrames) {
    if (coughing) px = card.fc;
    else if (card.drunk) px = card.fd[constrain(card.drunk, 1, 3) - 1];
    else if (card.high) px = card.fh;
  }
  return px;
}

void drawStage(int dx, bool blink, bool coughing = false, bool anyView = false) {
  if (view != V_HOME && !anyView) return;
  for (int y = 0; y < SH; y++) for (int x = 0; x < SW; x++) stage[y * SW + x] = bgAt(SX + x, SY + y);
  bool petting = millis() < petUntil;
  static uint16_t cell[16][16];
  mochiNow(cell, framePx(blink, coughing), CoolF{false, 0, -1, false, false});
  int ox = PX - SX + dx, oy = PY - SY;
  for (int cy = 0; cy < 16; cy++) for (int cx = 0; cx < 16; cx++) {
    uint16_t col = cell[cy][cx];
    if (!col) continue;
    for (int y = 0; y < SCALE; y++) {
      int yy = oy + cy * SCALE + y;
      if (yy < 0 || yy >= SH) continue;
      for (int x = 0; x < SCALE; x++) {
        int xx = ox + cx * SCALE + x;
        if (xx >= 0 && xx < SW) stage[yy * SW + xx] = col;
      }
    }
  }
  if (petting) {   // little hearts floating up on both sides
    static const char *H = ".X.X.XXXXXXXXXX.XXX...X..";   // 5x5 heart
    uint32_t left = petUntil - millis();
    for (int k = 0; k < 3; k++) {
      int life = (int)((1800 - left + k * 600) % 1800);
      int hy = 70 - life * 70 / 1800, hx = (k % 2 ? SW - 26 : 8) + (k == 2 ? 6 : 0);
      for (int i = 0; i < 25; i++) if (H[i] == 'X')
        for (int y = 0; y < 3; y++) for (int x = 0; x < 3; x++) {
          int yy = hy + (i / 5) * 3 + y, xx = hx + (i % 5) * 3 + x;
          if (yy >= 0 && yy < SH && xx >= 0 && xx < SW) stage[yy * SW + xx] = RED;
        }
    }
  }
  gfx->draw16bitRGBBitmap(SX, SY, stage, SW, SH);
}

// The epic part: pixel fire (the classic "Doom fire": heat rises cell by cell, cooling and drifting at random)
// and explosions, across the whole width behind Mochi, with a dark red wash and a screen shake.
static const int FC = 8, FW = 240 / FC, FH = (SH + FC - 1) / FC;   // chunky fire cells of 8x8 px over the strip Mochi stands in
uint8_t fireHeat[FW * FH];
static const uint32_t FIRE_PAL[36] = {0x28060a, 0x38080a, 0x480a0a, 0x580c0a, 0x680e0a, 0x78110b, 0x88150c, 0x98180d, 0xa81b0e, 0xb7200f, 0xc12910, 0xcb3312, 0xd53c13, 0xe04514, 0xe55016, 0xea5b18, 0xee671b, 0xf3721d, 0xf67e1f, 0xf88921, 0xfb9524, 0xfda026, 0xffab29, 0xffb632, 0xffc03b, 0xffca45, 0xffd44e, 0xffdc5f, 0xffe374, 0xffea89, 0xfff19d, 0xfff6b1, 0xfff8c5, 0xfffad8, 0xfffdec, 0xffffff};

uint16_t blend(uint16_t c, uint32_t hex, int a) {   // a: 0..256 of hex over c
  int r = (c >> 11) << 3, g = ((c >> 5) & 63) << 2, b = (c & 31) << 3;
  r += (((int)(hex >> 16) - r) * a) >> 8; g += (((int)((hex >> 8) & 255) - g) * a) >> 8; b += (((int)(hex & 255) - b) * a) >> 8;
  return rgb(((uint32_t)r << 16) | (g << 8) | b);
}

void coolScene(uint32_t t) {
  CoolF cf = coolFrame(t);
  static uint16_t cell[16][16];
  mochiNow(cell, framePx(false, false), cf);
  long ft = (long)t - 650;                       // fire time: starts with the glint
  bool fire = ft >= 0 && ft < 3550;
  static uint32_t lastFire = 0;
  bool step = millis() - lastFire >= 100;
  if (step) lastFire = millis();
  if (fire && step) {
    int src = ft < 2050 ? 11 : max(0, 11 - (int)(ft - 2050) / 90);
    for (int x = 0; x < FW; x++) fireHeat[(FH - 1) * FW + x] = src ? max(0, src - (int)random(3)) : 0;
    for (int y = 1; y < FH; y++) for (int x = 0; x < FW; x++) {
      int from = y * FW + x, r = random(3), to = from - FW - r + 1;
      if (to >= 0) fireHeat[to] = max(0, fireHeat[from] - (int)random(3));
    }
  }
  int wash = !fire ? 0 : ft < 250 ? ft * 140 / 250 : ft > 2750 ? max(0L, 140 - (ft - 2750) * 140 / 800) : 140;
  static int shake = 0;
  if (step) shake = fire && ft < 450 ? ((int)random(3) - 1) * 4 : 0;
  static const int BOOM[5][3] = {{0, 7, 4}, {200, 22, 3}, {420, 15, 1}, {700, 4, 9}, {950, 26, 8}};
  for (int y0 = 0; y0 < SH; y0 += band.bh) {
    int h = min((int)band.bh, SH - y0);
    for (int y = 0; y < h; y++) {
      int sy = SY + y0 + y, fy = (y0 + y) / FC;
      int rowWash = wash * (y0 + y) / SH;          // the wash fades out toward the top, so there's no hard edge
      for (int x = 0; x < 240; x++) {
        uint16_t c = bgAt(x, sy);
        if (fire) {
          int v = fireHeat[fy * FW + x / FC];
          if (v > 1) c = rgb(FIRE_PAL[min(35, v * 35 / 11)]);
          else if (rowWash) c = blend(c, 0x28060a, rowWash);
          for (auto &b : BOOM) {                   // explosions: flash, fireball, smoke ring
            long bt = ft - b[0];
            if (bt < 0 || bt > 650) continue;
            int R = 1 + (int)(bt * 4 / 650), fx = x / FC, d2 = (fx - b[1]) * (fx - b[1]) + (fy - b[2]) * (fy - b[2]);
            if (d2 > R * R) continue;
            int edge = (int)(sqrtf(d2) * 100 / R);
            c = bt < 80 ? 0xFFFF : edge > 82 ? (bt > 400 ? rgb(0x463e48) : rgb(0xbe2814)) : edge > 55 ? rgb(0xf07814) : bt > 450 ? rgb(0x786458) : rgb(0xffdc5a);
          }
        }
        int cx = (x - PX - shake), cy = sy - PY;
        if (cx >= 0 && cy >= 0 && cx < 16 * SCALE && cy < 16 * SCALE) { uint16_t m = cell[cy / SCALE][cx / SCALE]; if (m) c = m; }
        band.fb[y * 240 + x] = c;
      }
    }
    lcd->draw16bitRGBBitmap(0, SY + y0, band.fb, 240, h);
  }
  static bool boomSound = false, ting = false;
  if (t < 600) boomSound = ting = false;
  if (fire && !boomSound) { tone(PIN_BUZZ, 147, 320); boomSound = true; }   // BWAAM
  if (t > 1000 && !ting) { tone(PIN_BUZZ, 1760, 70); ting = true; }          // ting
}

void playCool() {
  if (coolStart || !banded) return;
  memset(fireHeat, 0, sizeof fireHeat);
  coolStart = millis();
}

bool stale() {   // LazyTrack closed or the computer asleep: what it last sent isn't true anymore
  time_t t = time(nullptr);
  return card.offline || (t > 1700000000 && card.built && t - (time_t)card.built > 600);
}

String doing() {   // bottom line: what you're doing right now, like "Playing LoL - 25m"
  const String &w = card.nowWhat;
  if (stale() || !w.length()) return "";
  if (w == "away") return "Away from the computer";
  String s = card.nowLabel;
  if (!s.length())
    s = w == "coding" ? "Coding" : w == "youtube" ? "Watching YouTube" : w == "social" ? "On socials" : w == "streaming" ? "Watching something"
      : w == "gaming" ? "Gaming" : w == "browsing" ? "Browsing" : "On the computer";
  s.setCharAt(0, toupper(s[0]));
  if (card.nowSince) {
    long mins = (long)(time(nullptr) - (time_t)card.nowSince) / 60;
    if (mins > 0 && mins < 1440) s += mins >= 60 ? " - " + String(mins / 60) + "h" + String(mins % 60) + "m" : " - " + String(mins) + "m";
  }
  return s;
}

void text(int x, int y, const String &s, uint8_t size, uint16_t col) {
  gfx->setTextSize(size);
  gfx->setTextColor(col);
  gfx->setCursor(x, y);
  gfx->print(s);
}

void panel(int x, int y, int w, int h) {  // the app's card look: white, thick dark outline, a hard shadow
  gfx->fillRoundRect(x + 3, y + 3, w, h, 10, INK);
  gfx->fillRoundRect(x, y, w, h, 10, CARD);
  gfx->drawRoundRect(x, y, w, h, 10, INK);
  gfx->drawRoundRect(x + 1, y + 1, w - 2, h - 2, 9, INK);
}

// a list in the app's card style, the picked row in yellow
void drawList(const char *title, const String *items, const bool *dots, const bool *bad, int n, int sel, const char *hint) {
  drawBackground();
  panel(8, 8, 221, 304);
  text(20, 20, title, 2, INK);
  int rows = 9, first = sel >= rows ? sel - rows + 1 : 0;
  for (int i = first; i < n && i < first + rows; i++) {
    int y = 48 + (i - first) * 27;
    if (i == sel) { gfx->fillRoundRect(16, y - 4, 205, 24, 6, YELLOW); gfx->drawRoundRect(16, y - 4, 205, 24, 6, INK); }
    int x = 24;
    if (dots) { gfx->fillCircle(30, y + 7, 4, bad && bad[i] ? RED : GREEN); x = 42; }
    text(x, y, items[i].substring(0, dots ? 14 : 15), 2, INK);
  }
  if (n > first + rows) text(206, 292, "v", 1, GREY);
  text(20, 296, hint, 1, GREY);
}

// ---------------------------------------------------------------- the menu: a drawer over your (dimmed) Mochi
void menuIcon(int i, int x, int y, bool on) {
  uint16_t fill = on ? CARD : SOFT;
  if (i == 0) {          // Log: a little notepad with a plus
    gfx->fillRoundRect(x, y, 28, 28, 6, rgb(0xff8fb1)); gfx->drawRoundRect(x, y, 28, 28, 6, INK);
    gfx->fillRect(x + 12, y + 6, 4, 16, INK); gfx->fillRect(x + 6, y + 12, 16, 4, INK);
  } else if (i == 1) {   // Friends: two little heads
    gfx->fillCircle(x + 9, y + 10, 6, rgb(0x9be8a9)); gfx->drawCircle(x + 9, y + 10, 6, INK);
    gfx->fillCircle(x + 20, y + 12, 6, rgb(0xbfe9ff)); gfx->drawCircle(x + 20, y + 12, 6, INK);
    gfx->fillRoundRect(x + 1, y + 18, 16, 10, 5, rgb(0x9be8a9)); gfx->drawRoundRect(x + 1, y + 18, 16, 10, 5, INK);
    gfx->fillRoundRect(x + 12, y + 20, 16, 8, 4, rgb(0xbfe9ff)); gfx->drawRoundRect(x + 12, y + 20, 16, 8, 4, INK);
  } else if (i == 2) {   // Today: bars
    gfx->fillRoundRect(x, y, 28, 28, 6, fill); gfx->drawRoundRect(x, y, 28, 28, 6, INK);
    gfx->fillRect(x + 5, y + 15, 5, 8, rgb(0xffe45e)); gfx->drawRect(x + 5, y + 15, 5, 8, INK);
    gfx->fillRect(x + 12, y + 9, 5, 14, rgb(0x9be8a9)); gfx->drawRect(x + 12, y + 9, 5, 14, INK);
    gfx->fillRect(x + 19, y + 5, 5, 18, rgb(0xc9b8ff)); gfx->drawRect(x + 19, y + 5, 5, 18, INK);
  } else {               // Info: an "i"
    gfx->fillCircle(x + 14, y + 14, 13, rgb(0xbfe9ff)); gfx->drawCircle(x + 14, y + 14, 13, INK);
    gfx->fillRect(x + 12, y + 6, 4, 4, INK); gfx->fillRect(x + 12, y + 12, 4, 11, INK);
  }
}

void dimBand(uint8_t level) {   // darkens what's already in the band: 1 = a bit, 2 = half
  if (gfx != &band) return;
  for (int i = 0; i < 240 * band.bh; i++) {
    uint16_t p = band.fb[i];
    band.fb[i] = level >= 2 ? (p >> 1) & 0x7BEF : p - ((p >> 2) & 0x39E7);
  }
}

uint8_t menuDim = 2;
void drawMenu() {
  int16_t slide = band.ox;
  band.ox = 0;
  drawBackground();
  drawStage(0, false, false, true);    // your Mochi stays there, dimmed, behind the drawer
  dimBand(menuDim);
  band.ox = slide;
  const int W = 196;
  gfx->fillRoundRect(-12 + 4, 4 + 4, W + 12, 312, 14, INK);   // hard shadow, like the app's cards
  gfx->fillRoundRect(-12, 4, W + 12, 312, 14, CARD);
  gfx->drawRoundRect(-12, 4, W + 12, 312, 14, INK);
  gfx->drawRoundRect(-11, 5, W + 10, 310, 13, INK);
  text(16, 18, "MENU", 3, INK);
  text(16, 46, card.name.substring(0, 24), 1, GREY);
  int online = 0; for (int i = 0; i < nfriends; i++) online += friends[i].online;
  static const char *names[] = {"Log", "Friends", "Today", "Info"};
  String subs[4] = {nhabits ? String(nhabits) + " habits" : String("set up in the app"),
                    nfriends ? String(online) + " online" : String("add some in the app"),
                    "HP " + String(card.hp) + " - " + card.tierName,
                    WiFi.status() == WL_CONNECTED ? WiFi.SSID().substring(0, 18) : String("no WiFi")};
  for (int i = 0; i < 4; i++) {
    int y = 66 + i * 58;
    bool on = i == menuSel;
    if (on) {
      gfx->fillRoundRect(10 + 3, y + 3, W - 20, 50, 10, INK);
      gfx->fillRoundRect(10, y, W - 20, 50, 10, YELLOW);
      gfx->drawRoundRect(10, y, W - 20, 50, 10, INK);
    }
    menuIcon(i, 20, y + 11, on);
    text(58, y + 10, names[i], 2, INK);
    text(58, y + 32, subs[i], 1, on ? INK : GREY);
    if (on) gfx->fillTriangle(W - 22, y + 18, W - 22, y + 32, W - 14, y + 25, INK);
  }
  text(16, 300, "C next  B open  A back", 1, GREY);
}

// ---------------------------------------------------------------- today: everything you track, in pages
void drawToday() {
  drawBackground();
  panel(8, 8, 221, 304);
  text(20, 20, "TODAY", 2, INK);
  if (!ntoday) {   // older LazyTrack: only what's on your friend card
    for (int i = 0; i < card.nstats; i++) text(20, 52 + i * 28, card.stats[i], 2, INK);
    text(20, 256, "Only what friends see. For all of", 1, GREY);
    text(20, 270, "it: update LazyTrack + run egg.sql", 1, GREY);
    text(20, 296, "A  back", 1, GREY);
    return;
  }
  const int per = 10;
  int pages = (ntoday + per - 1) / per;
  if (todaySel >= ntoday) todaySel = 0;
  todayPage = todaySel / per;
  if (pages > 1) text(180, 24, String(todayPage + 1) + "/" + String(pages), 1, GREY);
  for (int i = todayPage * per, r = 0; i < ntoday && r < per; i++, r++) {
    int y = 50 + r * 24;
    if (i == todaySel) { gfx->fillRoundRect(14, y - 4, 209, 22, 5, YELLOW); gfx->drawRoundRect(14, y - 4, 209, 22, 5, INK); }
    else if (r % 2 == 0) gfx->fillRoundRect(14, y - 4, 209, 22, 5, SOFT);
    if (today[i].bad || today[i].good) gfx->fillCircle(24, y + 7, 4, today[i].bad ? RED : GREEN);
    text(34, y, today[i].s.substring(0, 11), 2, INK);
    text(215 - today[i].v.length() * 12, y, today[i].v, 2, INK);
  }
  text(20, 296, "C next  B vs yesterday  A back", 1, GREY);
}

// ---------------------------------------------------------------- yesterday vs today, for one stat
void cmpBar(int y, const String &label, const String &val, long v, long top, uint16_t col) {
  text(20, y, label, 1, GREY);
  text(215 - val.length() * 18, y + 12, val, 3, INK);
  int w = top > 0 ? (int)(186L * v / top) : 0;
  gfx->fillRoundRect(20, y + 44, 196, 18, 5, SOFT); gfx->drawRoundRect(20, y + 44, 196, 18, 5, INK);
  if (w > 0) { gfx->fillRoundRect(25, y + 48, max(w, 6), 10, 3, col); }
}

void drawCompare() {
  drawBackground();
  panel(8, 8, 221, 304);
  if (!ntoday) { text(20, 20, "NOTHING YET", 2, INK); text(20, 296, "A  back", 1, GREY); return; }
  Stat &st = today[todaySel % ntoday];
  text(20, 18, "VS YESTERDAY", 1, GREY);
  String name = st.s; if (name.length()) name.setCharAt(0, toupper(name[0]));
  text(20, 32, name.substring(0, 12), 3, INK);
  if (!st.hasY) {
    text(20, 90, "Update LazyTrack on", 2, INK);
    text(20, 112, "your computer to see", 2, INK);
    text(20, 134, "yesterday here.", 2, INK);
    text(20, 296, "C next  A back", 1, GREY);
    return;
  }
  long top = max(st.tv, st.yv);
  cmpBar(70, "YESTERDAY", fmtVal(st.yv, st.secs), st.yv, top, rgb(0xc9b8ff));
  cmpBar(148, "TODAY SO FAR", fmtVal(st.tv, st.secs), st.tv, top, YELLOW);
  long d = st.tv - st.yv;
  String verdict; uint16_t col = INK;
  if (d == 0) verdict = "Same as yesterday";
  else {
    verdict = fmtVal(d > 0 ? d : -d, st.secs) + (d > 0 ? " more" : " less");
    if (st.sign) {
      bool better = (d > 0) == (st.sign > 0);
      col = better ? GREEN : RED;
      verdict += better ? " nice!" : "";
    }
  }
  gfx->fillRoundRect(14, 232, 209, 34, 8, col == INK ? SOFT : col); gfx->drawRoundRect(14, 232, 209, 34, 8, INK);
  text(24, 242, verdict.substring(0, 16), 2, col == INK ? INK : CARD);
  text(20, 296, "C next stat  A back", 1, GREY);
}

uint16_t tierColor(const String &t) {
  return t == "legend" ? rgb(0xffeeb0) : t == "tired" ? rgb(0xd9d3e6) : t == "sick" ? rgb(0xd3dbc4) : t == "shiny" ? rgb(0xbfe9ff) : rgb(0xc9b8ff);
}

void drawFriend() {
  if (!nfriends) {
    drawBackground();
    panel(8, 8, 221, 120);
    text(20, 20, "FRIENDS", 2, INK);
    text(20, 52, WiFi.status() == WL_CONNECTED ? "No friends yet." : "No WiFi right now.", 1, INK);
    text(20, 70, "Add some in the LazyTrack app", 1, GREY);
    text(20, 86, "(Friends tab).", 1, GREY);
    text(20, 110, "A  back", 1, GREY);
    return;
  }
  Friend &f = friends[friendSel];
  gfx->fillRect(0, 0, 240, 232, tierColor(f.tier));
  gfx->fillRect(0, 232, 240, 88, rgb(0x7cc97f));
  panel(8, 8, 221, 50);
  text(18, 16, f.name.substring(0, 15), 2, INK);
  text(18, 38, "@" + f.user.substring(0, 20), 1, GREY);
  text(196, 38, String(friendSel + 1) + "/" + String(nfriends), 1, GREY);
  // their Mochi, standing on the ground (same size as yours)
  const char *px = f.sprite ? f.px : DEFAULT_N;
  for (int cy = 0; cy < 16; cy++) for (int cx = 0; cx < 16; cx++) {
    char ch = px[cy * 16 + cx];
    if (ch == '.') continue;
    uint16_t col;
    if (f.sprite) { int i = (ch >= '0' && ch <= '9') ? ch - '0' : (ch >= 'a' && ch <= 'z') ? ch - 'a' + 10 : -1; if (i < 0 || i >= 36) continue; col = f.pal[i]; }
    else { col = cellColor(ch, false); if (!col) continue; }
    gfx->fillRect(PX + cx * SCALE, PY + cy * SCALE, SCALE, SCALE, col);
  }
  panel(8, 246, 221, 66);
  if (f.hasCard) {
    text(18, 256, "HP " + String(f.hp), 2, INK);
    String fx = f.drunk ? (f.drunk >= 3 ? "WASTED" : f.drunk == 2 ? "DRUNK" : "TIPSY") : f.high ? (f.high >= 3 ? "SO HIGH" : f.high == 2 ? "HIGH" : "BUZZED") : f.coughy ? "COUGHING" : "";
    if (fx.length()) {
      int fw = 10 + fx.length() * 6;
      gfx->fillRoundRect(110, 257, fw, 14, 4, f.drunk ? rgb(0xffd28a) : f.high ? rgb(0x9be8a9) : rgb(0xd9d2e9));
      gfx->drawRoundRect(110, 257, fw, 14, 4, INK);
      text(115, 261, fx, 1, INK);
    }
  } else text(18, 256, "No card yet", 2, INK);
  String line;
  if (f.online) {
    String w = f.label.length() ? f.label : f.what == "coding" ? "coding" : f.what == "gaming" ? "gaming" : f.what == "youtube" ? "watching YouTube"
             : f.what == "away" ? "away from the computer" : f.what.length() ? "on the computer" : "";
    if (w.length()) w.setCharAt(0, toupper(w[0]));
    line = "Online" + (w.length() ? " - " + w : String(""));
    gfx->fillCircle(22, 281, 4, GREEN);
  } else {
    line = f.seen ? "Last seen " + agoText(f.seen) : "Not seen yet";
    gfx->fillCircle(22, 281, 4, GREY);
  }
  text(32, 278, line.substring(0, 31), 1, INK);
  text(18, 296, status.startsWith("Poked") || status.startsWith("Easy") || status.startsWith("Couldn") || status.startsWith("Pokes") ? status.substring(0, 33) : "C next   B poke   A back", 1, GREY);
}

void drawScreen();
void scrollSetup();

void drawFrame() {   // a whole screen, without flicker: built band by band in memory, each sent in one go
  if (!banded) { drawScreen(); return; }
  gfx = &band;
  for (band.oy = 0; band.oy < 320; band.oy += band.bh) {
    if (band.ox) { int16_t s = band.ox; band.ox = 0; drawBackground(); band.ox = s; }   // behind a sliding panel
    drawScreen();
    lcd->draw16bitRGBBitmap(0, band.oy, band.fb, 240, min((int)band.bh, 320 - band.oy));
  }
  gfx = lcd;
}

void drawScreen() {  // the background and everything on it; Mochi itself comes from drawStage
  if (view == V_MENU) { drawMenu(); return; }
  if (view == V_STATS) { drawToday(); return; }
  if (view == V_CMP) { drawCompare(); return; }
  if (view == V_LOG) {
    if (!nhabits) {
      drawBackground();
      panel(8, 8, 221, 150);
      text(20, 20, "LOG", 2, INK);
      text(20, 52, "Nothing to log yet.", 1, INK);
      text(20, 70, "Update LazyTrack on your computer,", 1, GREY);
      text(20, 86, "and run supabase/egg.sql once.", 1, GREY);
      text(20, 140, "A  back", 1, GREY);
      return;
    }
    String names[16]; bool dots[16], bad[16];
    for (int i = 0; i < nhabits; i++) { names[i] = habits[i].n; names[i].setCharAt(0, toupper(names[i][0])); bad[i] = habits[i].bad; dots[i] = true; }
    drawList("LOG", names, dots, bad, nhabits, logSel, "C next   B log it   A back");
    return;
  }
  if (view == V_FRIENDS) { drawFriend(); return; }
  drawBackground();
  if (view == V_HOME) {
    String name = card.name.substring(0, 15);
    panel(8, 8, max(150, (int)name.length() * 12 + 22), 46);
    text(18, 16, name, 2, INK);
    text(18, 38, "MOCHI #" + String(card.gen) + " - " + (card.age ? String(card.age) + "D OLD" : String("BORN TODAY")), 1, GREY);
    if (millis() < msgUntil) {   // a moment's message: logged, poked, petted
      int tsz = msgTitle.length() <= 10 ? 3 : 2, tw = msgTitle.length() * 6 * tsz;
      int w = constrain(max(tw, (int)min((int)msgSub.length(), 36) * 6) + 28, 120, 224);
      panel((240 - w) / 2, 64, w, 50);
      text((240 - tw) / 2, tsz == 3 ? 72 : 76, msgTitle, tsz, INK);
      text((240 - (int)min((int)msgSub.length(), 36) * 6) / 2, 100, msgSub.substring(0, 36), 1, GREY);
    }
    panel(8, 246, 221, 66);
    text(18, 256, "HP", 2, INK);
    int on = (card.hp + 5) / 10;
    for (int i = 0; i < 10; i++) {
      int x = 46 + i * 13;
      gfx->drawRect(x, 256, 11, 14, INK);
      if (i < on) gfx->fillRect(x + 2, 258, 7, 10, card.tier == "sick" ? RED : card.tier == "legend" ? YELLOW : GREEN);
    }
    text(182, 256, String(card.hp), 2, INK);
    int bw = 10 + card.tierName.length() * 6;
    gfx->fillRoundRect(18, 276, bw, 14, 4, YELLOW);
    gfx->drawRoundRect(18, 276, bw, 14, 4, INK);
    text(23, 280, card.tierName, 1, INK);
    // how Mochi is right now, always shown: sober, tipsy/drunk/wasted, buzzed/high, coughing
    static const char *DR[] = {"TIPSY", "DRUNK", "WASTED"}, *HI[] = {"BUZZED", "HIGH", "SO HIGH"};
    String fx = card.drunk ? DR[constrain(card.drunk, 1, 3) - 1] : card.high ? HI[constrain(card.high, 1, 3) - 1] : card.coughy ? "COUGHING" : "SOBER";
    if (card.drunk && card.high) fx += String(" + ") + HI[constrain(card.high, 1, 3) - 1];
    int fw = 10 + fx.length() * 6;
    uint16_t fcol = fx == "SOBER" ? rgb(0xc9f2d4) : card.drunk ? rgb(0xffd28a) : card.high ? rgb(0x9be8a9) : rgb(0xd9d2e9);
    gfx->fillRoundRect(26 + bw, 276, fw, 14, 4, fcol);
    gfx->drawRoundRect(26 + bw, 276, fw, 14, 4, INK);
    text(31 + bw, 280, fx, 1, INK);
    text(18, 297, (doing().length() ? doing() : status).substring(0, 33), 1, GREY);
    drawStage(0, false);
  } else {
    panel(8, 8, 221, 304);
    text(20, 20, "INFO", 2, INK);
    text(20, 52, "WiFi: " + (WiFi.status() == WL_CONNECTED ? WiFi.SSID() : String("not connected")), 1, INK);
    text(20, 70, "IP: " + WiFi.localIP().toString(), 1, INK);
    text(20, 88, "Status: " + status.substring(0, 26), 1, INK);
    text(20, 106, "Last sync: " + (lastSync ? String((millis() - lastSync) / 1000) + "s ago" : String("never")), 1, INK);
    String q = prefs.getString("queue", "");
    int waiting = 0; for (char ch : q) if (ch == '\n') waiting++;
    text(20, 124, "Pets today: " + String(loveToday()) + (waiting ? "   Logs waiting: " + String(waiting) : ""), 1, INK);
    text(20, 160, "HOME:  B menu   A pet   C screen off", 1, GREY);
    text(20, 176, "MENUS: C next   B ok    A back", 1, GREY);
    text(20, 200, "B here  sync now", 1, GREY);
    text(20, 216, "C here  legendary cool move (test)", 1, GREY);
  }
}

// ---------------------------------------------------------------- buttons
// Buttons are caught by interrupts, so a press is never missed, even while the egg is busy talking to the
// server or drawing a slide: it's remembered and handled right after. Debounced in the interrupt: a press
// counts when the button goes down at least 200 ms after the last press and 40 ms after it last came up
// (so the little flickers of a switch opening or closing don't count as extra presses).
struct Btn { int pin; uint32_t accepted, rose; } btns[3] = {{BTN_A, 0, 0}, {BTN_B, 0, 0}, {BTN_C, 0, 0}};
volatile uint8_t btnQueue = 0;   // one bit per button waiting to be handled

void IRAM_ATTR btnEdge(void *arg) {
  Btn *b = (Btn *)arg;
  uint32_t t = millis();
  if (digitalRead(b->pin) == HIGH) { b->rose = t; return; }   // let go
  if (t - b->accepted > 200 && t - b->rose > 40) { b->accepted = t; btnQueue |= 1 << (b - btns); }
}

int pressed() {  // the next press waiting (0-2), or -1
  noInterrupts();
  uint8_t q = btnQueue;
  int i = q & 1 ? 0 : q & 2 ? 1 : q & 4 ? 2 : -1;
  if (i >= 0) btnQueue &= ~(1 << i);
  interrupts();
  return i;
}

void setScreen(bool on) {
  screenOn = on;
  if (PIN_BL >= 0) {   // backlight on a pin: really off
    digitalWrite(PIN_BL, on ? HIGH : LOW);
    on ? lcd->displayOn() : lcd->displayOff();
  } else if (!on) {
    // backlight wired to 3.3V: the light can't go off, and "display off" shows white on these screens,
    // so it goes black instead (move the screen's LED wire to a GPIO and set PIN_BL for a real off)
    lcd->fillScreen(0x0000);
  }
}

// ---------------------------------------------------------------- main
void setup() {
  Serial.begin(115200);
  for (auto &b : btns) { pinMode(b.pin, INPUT_PULLUP); attachInterruptArg(digitalPinToInterrupt(b.pin), btnEdge, &b, CHANGE); }
  if (PIN_BL >= 0) { pinMode(PIN_BL, OUTPUT); digitalWrite(PIN_BL, HIGH); }
  // 27 MHz like TFT_eSPI: fast enough, and steady over jumper wires (faster can give a white screen)
  if (!lcd->begin(SPI_HZ)) Serial.println("screen didn't start: check the wiring and the chip setting");
  Serial.printf("LazyTrack Egg: screen %s on CS %d DC %d RST %d SCK %d MOSI %d\n",
#if defined(SCREEN_ST7789)
                "ST7789",
#else
                "ILI9341",
#endif
                PIN_CS, PIN_DC, PIN_RST, PIN_SCK, PIN_MOSI);
  BG = rgb(0xefeaff); CARD = rgb(0xffffff); SOFT = rgb(0xf3f0fb); YELLOW = rgb(0xffe45e); GREEN = rgb(0x3fbf6f); RED = rgb(0xe0303a); GREY = rgb(0x6a6475);
  stage = (uint16_t *)malloc(SW * SH * 2);
  banded = band.begin(0);   // before WiFi takes its share of memory
  scrollSetup();
  Serial.printf("screen buffer: %d rows per band\n", banded ? band.bh : 0);
  prefs.begin("lazytrack", false);
  refresh = prefs.getString("refresh", "");
  uid = prefs.getString("uid", "");
  tls.setInsecure();  // a pocket toy: skip certificate checks (the server still needs your login)
  Serial.println("connecting to WiFi...");
  status = "Connecting to WiFi...";
  drawFrame();
  drawStage(0, false);
  tuneBoot();
  // the last Mochi it saw, so it shows up even without WiFi (outside, no hotspot)
  String saved;
  size_t n = prefs.getBytesLength("card");
  if (n > 0 && n < 16384) { char *buf = (char *)malloc(n + 1); prefs.getBytes("card", buf, n); buf[n] = 0; saved = buf; free(buf); }
  if (saved.length()) { parseCard(saved); status = "Offline: last seen Mochi"; drawFrame(); drawStage(0, false); }
  String hs = prefs.getString("habits", "");   // the Log menu works before the first sync too
  if (hs.length()) parseSetup(hs);
  WiFi.mode(WIFI_STA);
  wifi.addAP(WIFI_SSID, WIFI_PASS);
#ifdef WIFI2_SSID
  wifi.addAP(WIFI2_SSID, WIFI2_PASS);
#endif
#ifdef WIFI3_SSID
  wifi.addAP(WIFI3_SSID, WIFI3_PASS);
#endif
  for (int i = 0; i < 20 && wifi.run(1000) != WL_CONNECTED; i++) {}
  configTime(0, 0, "pool.ntp.org");  // the clock, for "coding for 25 min"
  sync();
  if (WiFi.status() == WL_CONNECTED) { flushQueue(); loadHabits(); loadFriends(); checkPokes(); }
  drawFrame();
}

// Screen changes slide vertically using the screen's own hardware scrolling: the screen shifts its
// picture by itself, and only the new strip coming in has to be sent. That's what makes it smooth on an
// SPI screen (redrawing the whole 240x320 picture for every frame of a slide is too slow: you'd see it
// being drawn top to bottom). The chip can only scroll along its long side, so slides go up and down.
void hwScroll(int s) {   // display line 0 shows memory line s
  bus->beginWrite();
  bus->writeCommand(0x37);   // VSCRSADD (same on ST7789 and ILI9341)
  bus->write16((uint16_t)(s % 320));
  bus->endWrite();
}

void scrollSetup() {     // the whole 320 lines scroll, no fixed areas
  bus->beginWrite();
  bus->writeCommand(0x33);   // VSCRDEF
  bus->write16(0); bus->write16(320); bus->write16(0);
  bus->endWrite();
  hwScroll(0);
}

void pushRows(int y0, int y1) {   // draws rows y0..y1 of the current screen and sends just those
  gfx = &band;
  band.ox = 0;
  for (int y = y0; y < y1; ) {
    band.oy = y;
    int h = min((int)band.bh, y1 - y);
    drawScreen();
    lcd->draw16bitRGBBitmap(0, y, band.fb, 240, h);
    y += h;
  }
  gfx = lcd;
}

// up = the new screen comes up from the bottom and pushes the old one out the top (going into something);
// down = it comes down from the top (going back)
void transition(int to, bool up) {
  view = to;
  if (!banded) { drawFrame(); return; }
  const int N = 14;
  int prev = 0;
  for (int i = 1; i <= N; i++) {
    float t = i / (float)N, e = 1 - (1 - t) * (1 - t) * (1 - t);   // ease out: quick start, soft landing
    int d = i == N ? 320 : (int)(320 * e);
    if (d <= prev) continue;
    if (up) { pushRows(prev, d); hwScroll(d); }
    else { pushRows(320 - d, 320 - prev); hwScroll(320 - d); }
    prev = d;
  }
  hwScroll(0);
}

// Physical buttons: A = pet / back, B = menu / OK, C = next (and on Home: undo a log, or screen off).
// Below, the code works with roles: 0 = next / menu, 1 = OK / pet, 2 = back / screen off.
int role(int phys) {
  if (view == V_HOME) return phys == 1 ? 0 : phys == 0 ? 1 : 2;   // B menu, A pet, C off/undo
  return phys == 2 ? 0 : phys == 1 ? 1 : 2;                         // C next, B ok, A back
}

void onButton(int phys) {
  if (!screenOn) { setScreen(true); drawFrame(); return; }   // any button wakes it up
  int b = role(phys);
  if (!(view == V_HOME && b == 1)) tuneClick();               // petting has its own chirp
  switch (view) {
    case V_HOME:
      if (b == 0) { menuSel = 0; transition(V_MENU, true); }
      else if (b == 1) pet();
      else if (undoId && millis() < msgUntil) undoLog();
      else setScreen(false);
      break;
    case V_MENU:
      if (b == 0) { menuSel = (menuSel + 1) % 4; drawFrame(); }
      else if (b == 2) transition(V_HOME, false);
      else if (menuSel == 0) {
        if (!nhabits || millis() - habitsAt > 300000) loadHabits();
        logSel = 0; transition(V_LOG, true);
      } else if (menuSel == 1) {
        friendSel = 0;
        if (millis() - friendsAt > 60000 || !nfriends) { panel(40, 140, 160, 40); text(62, 154, "Loading...", 2, INK); loadFriends(); }
        status = ""; transition(V_FRIENDS, true);
      } else if (menuSel == 2) {
        if (millis() - habitsAt > 30000) loadHabits();   // fresh numbers
        todaySel = 0; transition(V_STATS, true);
      } else transition(V_INFO, true);
      break;
    case V_LOG:
      if (b == 0 && nhabits) { logSel = (logSel + 1) % nhabits; drawFrame(); }
      else if (b == 1 && nhabits) doLog(logSel);
      else if (b == 2) transition(V_MENU, false);
      break;
    case V_FRIENDS:
      if (b == 0 && nfriends) { friendSel = (friendSel + 1) % nfriends; status = ""; drawFrame(); }
      else if (b == 1 && nfriends) { sendPoke(friendSel); drawFrame(); }
      else if (b == 2) { status = ""; transition(V_MENU, false); }
      break;
    case V_INFO:
      if (b == 1) { status = "Syncing..."; drawFrame(); sync(); checkPokes(); tuneClick(); if (view == V_INFO) drawFrame(); break; }
      if (phys == 2) { transition(V_HOME, false); playCool(); break; }   // C: try the legendary cool move
      if (b == 2 || b == 0) transition(V_MENU, false);
      break;
    case V_STATS:
      if (b == 0 && ntoday) { todaySel = (todaySel + 1) % ntoday; drawFrame(); }
      else if (b == 1 && ntoday) transition(V_CMP, true);
      else if (b == 2 || b == 0) transition(V_MENU, false);
      break;
    case V_CMP:
      if (b == 0 && ntoday) { todaySel = (todaySel + 1) % ntoday; drawFrame(); }
      else if (b == 2 || b == 1) transition(V_STATS, false);
      break;
    default:
      if (b == 2 || b == 0) { view = V_MENU; drawFrame(); }
  }
}

void loop() {
  static uint32_t tick = 0, nextBlink = 4000, blinkEnd = 0;
  int b = pressed();
  if (b >= 0) onButton(b);

  if (millis() - lastTry > 30000) {   // every 30 s: LazyTrack sends changes (drunk, high, sober...) within a minute
    int before = card.hp, fx = card.drunk * 100 + card.high * 10 + card.coughy; String tb = card.tierName, what = card.nowWhat + card.nowLabel;
    sync();
    if (WiFi.status() == WL_CONNECTED) {
      flushQueue();
      checkPokes();
      if (!nhabits || millis() - habitsAt > 600000) loadHabits();
      if (view == V_FRIENDS && millis() - friendsAt > 60000) { loadFriends(); if (friendSel >= nfriends) friendSel = 0; drawFrame(); }
    }
    if (view == V_HOME && (card.hp != before || card.tierName != tb || card.drunk * 100 + card.high * 10 + card.coughy != fx || card.nowWhat + card.nowLabel != what)) drawFrame();
    if (view == V_INFO) drawFrame();
  }
  if (WiFi.status() != WL_CONNECTED && millis() - lastTry > 15000) { lastTry = millis(); if (wifi.run(3000) == WL_CONNECTED) sync(); }
  if (msgUntil && millis() > msgUntil) { msgUntil = 0; undoId = 0; if (view == V_HOME) drawFrame(); }   // the message is done

  // ~8 fps, standing on the ground: blinks, wobbles when drunk, sways when high, shakes when coughing.
  // Only redrawn when something changes, so the screen stays calm.
  // the legendary cool move: out of nowhere, every 15-30 minutes when Mochi is Legendary
  if (card.tier == "legend" && !coolStart && screenOn && view == V_HOME && !asleep()) {
    if (!nextCool) nextCool = millis() + (15 + random(16)) * 60000UL;
    else if (millis() > nextCool) { nextCool = 0; playCool(); }
  }
  if (coolStart) {
    static uint32_t coolTick = 0;
    if (view != V_HOME || !screenOn) { coolStart = 0; }
    else if (millis() - coolTick > 60) {
      coolTick = millis();
      uint32_t t = millis() - coolStart;
      if (coolFrame(t).on || t < 4200) coolScene(t);
      else { coolStart = 0; drawFrame(); }
    }
    return;   // nothing else draws while it plays
  }
  static uint32_t zTick = 0;
  if (screenOn && view == V_HOME && millis() > msgUntil && asleep() && millis() - zTick > 90) { zTick = millis(); drawZs(zTick); }
  static int lastDx = 99; static bool lastBlink = false;
  if (screenOn && view == 0 && millis() - tick > 120) {
    tick = millis();
    uint32_t t = millis();
    if (t > nextBlink) { blinkEnd = t + 160; nextBlink = t + 3500 + random(2500); }
    int dx = 0;
    if (card.drunk) dx = (int)(sin(t / (500.0 - card.drunk * 100)) * (3 + card.drunk * 2));
    else if (card.high) dx = (int)(sin(t / 1100.0) * 3);
    if (card.coughy && (t / 300) % (14 - card.coughy * 3) == 0) dx = ((t / 60) % 2) ? 3 : -3;
    if (t < shakeUntil) dx = ((t / 70) % 2) ? 5 : -5;                       // poked: a little jolt
    else if (t < petUntil) dx = ((t / 200) % 2) ? 2 : -2;                   // petted: happy wiggle
    bool blink = t < blinkEnd;
    bool cough = card.coughy && (t / 300) % (14 - card.coughy * 3) == 0;
    static bool lastCough = false;
    bool hearts = t < petUntil + 150;
    if (dx != lastDx || blink != lastBlink || cough != lastCough || hearts) { drawStage(dx, blink, cough); lastDx = dx; lastBlink = blink; lastCough = cough; }
  }
}
