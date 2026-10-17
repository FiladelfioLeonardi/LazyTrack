/*
  LazyTrack Egg: your Mochi in your pocket.

  An ESP32 + a 240x320 color screen + 3 buttons + a buzzer. It connects to WiFi, logs into your
  LazyTrack account and shows the same Mochi as the app: its look, HP, level, age, how it's
  doing right now (drunk, high, coughing) and what you're up to. It reads your friend card from
  the friends server every minute, so it works anywhere with WiFi (a phone hotspot too).

  Buttons (like a Tamagotchi):  A = next / menu   B = OK / pet Mochi   C = back / screen off
    Home:     A opens the menu, B pets Mochi (hearts!), C turns the screen off (any button wakes it)
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
  String tier = "okay", tierName = "OKAY", name = "Mochi", nowWhat = "", nowLabel = "";
  uint32_t nowSince = 0, built = 0;
  bool offline = false;
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
enum { V_HOME, V_STATS, V_INFO, V_MENU, V_LOG, V_FRIENDS };
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
  for (const char *k : {"hp", "tier", "tierName", "name", "gen", "age", "best", "deaths", "fx", "now", "sprite", "scene", "offline", "built"}) f[k] = true;
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

void loadHabits() {   // which habits you track: LazyTrack puts them in egg_setup
  habitsAt = millis();
  String body;
  if (api("GET", "/rest/v1/egg_setup?select=habits", "", body) != 200) return;
  JsonDocument d;
  if (deserializeJson(d, body)) return;
  int n = 0;
  for (JsonObject h : d[0]["habits"].as<JsonArray>()) {
    if (n >= 16) break;
    habits[n].k = h["k"] | "";
    habits[n].n = h["n"] | "";
    habits[n].bad = h["bad"] | false;
    if (habits[n].k.length()) n++;
  }
  nhabits = n;
  if (n) prefs.putString("habits", body);
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
  if (!screenOn) { screenOn = true; if (PIN_BL >= 0) digitalWrite(PIN_BL, HIGH); gfx->displayOn(); }
  if (kind == "love") { petUntil = millis() + 3000; showMsg(who.substring(0, 14), "sent Mochi love <3", 6000); tuneLove(); }
  else if (kind == "wave") { shakeUntil = millis() + 1500; showMsg(who.substring(0, 14), "waved at you!", 6000); tunePoke(); }
  else { shakeUntil = millis() + 2000; showMsg(who.substring(0, 14), a.size() > 1 ? "poked you " + String(a.size()) + " times!" : "poked you!", 6000); tunePoke(); }
}

// ---------------------------------------------------------------- petting
int loveToday() {
  long day = clockOk() ? (long)(time(nullptr) / 86400) : 0;
  return prefs.getLong("loveDay", -1) == day ? prefs.getInt("love", 0) : 0;
}

void pet() {
  long day = clockOk() ? (long)(time(nullptr) / 86400) : 0;
  int n = loveToday() + 1;
  prefs.putLong("loveDay", day);
  prefs.putInt("love", n);
  petUntil = millis() + 1800;
  tunePet();
  String sub = card.drunk ? "*hic* ...love you too" : card.high ? "duuude... so soft" : card.coughy ? "*cough* thanks <3"
             : card.tier == "sick" ? "that helps a little" : n == 1 ? "first pet today!" : String(n) + " pets today";
  showMsg("<3", sub, 2200);
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

// Mochi standing on the ground, with the face that fits right now (high, drunk, coughing) like in the app.
// dx: drunk wobble / coughing shake, sideways only (it never floats)
void drawStage(int dx, bool blink, bool coughing = false) {
  if (view != 0) return;
  for (int y = 0; y < SH; y++) for (int x = 0; x < SW; x++) stage[y * SW + x] = bgAt(SX + x, SY + y);
  const char *px = DEFAULT_N;
  bool petting = millis() < petUntil;
  if (card.sprite) {
    px = blink || petting ? card.fb : card.fn;   // eyes closed while being petted: happy
    if (card.fxFrames) {
      if (coughing) px = card.fc;
      else if (card.drunk) px = card.fd[constrain(card.drunk, 1, 3) - 1];
      else if (card.high) px = card.fh;
    }
  }
  int ox = PX - SX + dx, oy = PY - SY;
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
    text(20, 110, "C  back", 1, GREY);
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
  text(18, 296, status.startsWith("Poked") || status.startsWith("Easy") || status.startsWith("Couldn") || status.startsWith("Pokes") ? status.substring(0, 33) : "A next   B poke   C back", 1, GREY);
}

void drawFrame() {  // the background and everything on it; Mochi itself comes from drawStage
  if (view == V_MENU) {
    static const String items[] = {"Log", "Friends", "Today", "Info"};
    drawList("MENU", items, nullptr, nullptr, 4, menuSel, "A down   B open   C back");
    return;
  }
  if (view == V_LOG) {
    if (!nhabits) {
      drawBackground();
      panel(8, 8, 221, 150);
      text(20, 20, "LOG", 2, INK);
      text(20, 52, "Nothing to log yet.", 1, INK);
      text(20, 70, "Update LazyTrack on your computer,", 1, GREY);
      text(20, 86, "and run supabase/egg.sql once.", 1, GREY);
      text(20, 140, "C  back", 1, GREY);
      return;
    }
    String names[16]; bool dots[16], bad[16];
    for (int i = 0; i < nhabits; i++) { names[i] = habits[i].n; names[i].setCharAt(0, toupper(names[i][0])); bad[i] = habits[i].bad; dots[i] = true; }
    drawList("LOG", names, dots, bad, nhabits, logSel, "A down   B log it   C back");
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
  } else if (view == V_STATS) {
    panel(8, 8, 221, 304);
    text(20, 20, "TODAY", 2, INK);
    for (int i = 0; i < card.nstats; i++) text(20, 52 + i * 28, card.stats[i], 2, INK);
    if (!card.nstats) text(20, 52, "No stats shared yet", 1, GREY);
    text(20, 280, "Record " + String(max(card.best, card.age)) + "d  Deaths " + String(card.deaths), 1, GREY);
    text(20, 296, "C  back", 1, GREY);
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
    text(20, 160, "HOME:  A menu   B pet   C screen off", 1, GREY);
    text(20, 176, "MENUS: A next   B ok    C back", 1, GREY);
    text(20, 200, "B here  sync now", 1, GREY);
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
  // 27 MHz like TFT_eSPI: fast enough, and steady over jumper wires (faster can give a white screen)
  if (!gfx->begin(27000000)) Serial.println("screen didn't start: check the wiring and the chip setting");
  Serial.printf("LazyTrack Egg: screen %s on CS %d DC %d RST %d SCK %d MOSI %d\n",
#if defined(SCREEN_ST7789)
                "ST7789",
#else
                "ILI9341",
#endif
                PIN_CS, PIN_DC, PIN_RST, PIN_SCK, PIN_MOSI);
  BG = rgb(0xefeaff); CARD = rgb(0xffffff); SOFT = rgb(0xf3f0fb); YELLOW = rgb(0xffe45e); GREEN = rgb(0x3fbf6f); RED = rgb(0xe0303a); GREY = rgb(0x6a6475);
  stage = (uint16_t *)malloc(SW * SH * 2);
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
  if (hs.length()) {
    JsonDocument d;
    if (!deserializeJson(d, hs)) for (JsonObject h : d[0]["habits"].as<JsonArray>()) {
      if (nhabits >= 16) break;
      habits[nhabits].k = h["k"] | ""; habits[nhabits].n = h["n"] | ""; habits[nhabits].bad = h["bad"] | false;
      if (habits[nhabits].k.length()) nhabits++;
    }
  }
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

void onButton(int b) {
  if (!screenOn) { setScreen(true); drawFrame(); return; }   // any button wakes it up
  tuneClick();
  switch (view) {
    case V_HOME:
      if (b == 0) { view = V_MENU; menuSel = 0; drawFrame(); }
      else if (b == 1) pet();
      else if (undoId && millis() < msgUntil) undoLog();
      else setScreen(false);
      break;
    case V_MENU:
      if (b == 0) { menuSel = (menuSel + 1) % 4; drawFrame(); }
      else if (b == 2) { view = V_HOME; drawFrame(); }
      else if (menuSel == 0) {
        if (!nhabits || millis() - habitsAt > 300000) { status = "Loading..."; loadHabits(); }
        view = V_LOG; logSel = 0; drawFrame();
      } else if (menuSel == 1) {
        view = V_FRIENDS; friendSel = 0;
        if (millis() - friendsAt > 60000 || !nfriends) {
          drawBackground(); panel(40, 140, 160, 40); text(62, 154, "Loading...", 2, INK);
          loadFriends();
        }
        status = ""; drawFrame();
      } else { view = menuSel == 2 ? V_STATS : V_INFO; drawFrame(); }
      break;
    case V_LOG:
      if (b == 0 && nhabits) { logSel = (logSel + 1) % nhabits; drawFrame(); }
      else if (b == 1 && nhabits) doLog(logSel);
      else if (b == 2) { view = V_MENU; drawFrame(); }
      break;
    case V_FRIENDS:
      if (b == 0 && nfriends) { friendSel = (friendSel + 1) % nfriends; status = ""; drawFrame(); }
      else if (b == 1 && nfriends) { sendPoke(friendSel); drawFrame(); }
      else if (b == 2) { view = V_MENU; status = ""; drawFrame(); }
      break;
    case V_INFO:
      if (b == 1) { status = "Syncing..."; drawFrame(); sync(); checkPokes(); tuneClick(); if (view == V_INFO) drawFrame(); break; }
      // fall through
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
