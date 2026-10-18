/*
  LazyTrack Egg: your Mochi in your pocket.

  An ESP32 + a 240x320 color screen + 3 buttons + a buzzer. It connects to WiFi, logs into your
  LazyTrack account and shows the same Mochi as the app: its look, HP, level, age, how it's
  doing right now (drunk, high, coughing) and what you're up to. It reads your friend card from
  the friends server every minute, so it works anywhere with WiFi (a phone hotspot too).

  Buttons (like a Tamagotchi):  A = pet / back   B = menu / OK   C = next
    Home:     B opens the menu, A pets Mochi (hearts!) or feeds him, C turns the screen off (any button wakes it)
    Menu:     a ring around Mochi (C turns it, B opens): Log something (cigarette, water, gym... whatever you
              track), Feed, Friends (see their Mochi, poke them), Today's stats, Info
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
#include <nvs.h>
#include <esp_partition.h>   // big things (the offline Mochi, your habits) live in the spare flash area, not the small settings storage
#include <esp_task_wdt.h>
#include <ArduinoJson.h>
#include <Arduino_GFX_Library.h>
#include <NimBLEDevice.h>   // Library Manager: "NimBLE-Arduino" (Bluetooth: the egg goes online through your phone)
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
// Touch (the screen's T_ pins, an XPT2046 chip). Its own pins, so it never fights the screen for the wires.
// Set T_IRQ to -1 if the touch pins aren't connected.
#define T_CLK 14
#define T_CS 21
#define T_DIN 13
#define T_DO 35      // input-only pin: fine, it only listens
#define T_IRQ 27     // goes LOW while a finger is on the screen
int tpXfer(uint8_t cmd);
bool tcalOk = false, touchOn = false;   // touchOn: the touch chip answered at start (no T_ wires = buttons only)
int tcSwap = 0, tcX0 = 0, tcX1 = 1, tcY0 = 0, tcY1 = 1;   // raw readings at screen x = 20 / 220 and y = 20 / 300
// SPI speed for the screen. 27 MHz is steady over breadboard jumper wires. Once it's soldered with short
// wires (in the case), try 40000000: everything gets ~1.5x faster. White screen = too fast, go back.
#define SPI_HZ 27000000

// (types used in function parameters live up here: Arduino IDE declares all functions near the top)
struct Note { uint16_t f, ms; };   // a song note; f = 0: a short rest
struct CoolF { bool on; int shades; int glint; bool smirk, nod; };   // one moment of the legendary cool move
// The network runs in its own task (on the other core), so the screen and buttons never wait for WiFi or the
// server. One lock keeps them from touching the same things at once; while a request waits for the network,
// the lock is let go (see http()), so the screen keeps going.
SemaphoreHandle_t dataLock;
volatile TaskHandle_t lockOwner = nullptr;
inline void LOCK() { xSemaphoreTake(dataLock, portMAX_DELAY); lockOwner = xTaskGetCurrentTaskHandle(); }
inline void UNLOCK() { lockOwner = nullptr; xSemaphoreGive(dataLock); }
SemaphoreHandle_t httpLock;            // one request at a time (they share the secure connection / Bluetooth link)
volatile bool needRedraw = false;      // the network task got news: the screen redraws it
volatile bool netKick = false;         // do a sync right away (after a log, Info B, the phone connecting...)
volatile bool habitsKick = false, friendsKick = false;
volatile int pokeReq = -1;             // the screen asks the network task to poke friend #n
volatile bool undoReq = false;         // ...or to take back the last log
volatile bool friendsDone = false;     // the friend list was loaded once (the Friends screen says "loading" until then)
volatile uint8_t eggCmd = 0;           // from LazyTrack on your computer / phone: 1 = sing, 2 = find my egg
#define FW_VERSION "2026.10.18"
String resetWhy = "";                  // why the egg (re)started: shown on Info, so a crash or power dip is easy to spot
// What the egg was doing, kept through a restart (this memory survives a reset, not a power cut): if it ever
// freezes or crashes, the next start says what it was busy with.
RTC_NOINIT_ATTR char crumb[48];
RTC_NOINIT_ATTR char frozeIn[48];
RTC_NOINIT_ATTR uint32_t crumbMagic;
void setCrumb(const char *what) { strncpy(crumb, what, sizeof crumb - 1); crumb[sizeof crumb - 1] = 0; crumbMagic = 0xC0FFEE42; }
// the watchdog fired: note which tasks were running on each core (the restart follows right after)
extern "C" void esp_task_wdt_isr_user_handler(void) {
  const char *a = pcTaskGetName(xTaskGetCurrentTaskHandleForCore(0)), *b = pcTaskGetName(xTaskGetCurrentTaskHandleForCore(1));
  int k = 0;
  for (const char *p = "core0 "; *p && k < 47; ) frozeIn[k++] = *p++;
  for (const char *p = a ? a : "?"; *p && k < 47; ) frozeIn[k++] = *p++;
  for (const char *p = ", core1 "; *p && k < 47; ) frozeIn[k++] = *p++;
  for (const char *p = b ? b : "?"; *p && k < 47; ) frozeIn[k++] = *p++;
  frozeIn[k] = 0;
}

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
    // 24 rows = 11 KB. (It was 80, then 40, but WiFi + Bluetooth + the network task + a secure connection
    // all need memory at once: the secure connection alone wants ~45 KB.)
    for (int h : {24, 20}) { fb = (uint16_t *)malloc(240 * h * 2); if (fb) { bh = h; return true; } }
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
  bool hasAsleep = false, asleepNow = false;   // LazyTrack says when Mochi sleeps (your learned bedtime, never while you're up)
  String hungry = "";                          // "breakfast" / "lunch" / "dinner" while Mochi is hungry, else ""
  // offline mode: the day plan LazyTrack sent (meals today + tomorrow, your sleep window), run on the egg's own clock
  bool hasPlan = false;
  long planDay = 0;                            // days since 1970 (your date)
  int planBed = 30, planWake = 510;            // minutes after midnight
  struct Meal { char n; int s, e; bool fed; } meals[2][3];
  int nmeals[2] = {0, 0};
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
enum { V_HOME, V_STATS, V_INFO, V_MENU, V_LOG, V_FRIENDS, V_CMP, V_PHONE };
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
void tuneHungry() { beep(330, 90); beep(294, 90); beep(330, 90); beep(392, 160); }       // tummy rumble
void tuneYum() { beep(1047, 60); beep(1319, 60); beep(1047, 60); beep(1568, 140); }        // fed
// ---- little songs: an old handheld-game sound on the buzzer (square wave, one note at a time).
// Played in the background note by note (song tick in loop), so the screen keeps moving. All original tunes.
#define N_C5 523
#define N_D5 587
#define N_E5 659
#define N_F5 698
#define N_G5 784
#define N_A5 880
#define N_B5 988
#define N_C6 1047
#define N_D6 1175
#define N_E6 1319
#define N_G4 392
#define N_A4 440
static const Note SONG_MOCHI[] = {   // Mochi's song: bouncy, a bit proud of himself
  {N_G5, 130}, {N_E5, 130}, {N_G5, 130}, {N_C6, 260}, {N_B5, 130}, {N_A5, 130}, {N_G5, 260}, {0, 130},
  {N_E5, 130}, {N_F5, 130}, {N_G5, 130}, {N_A5, 130}, {N_G5, 130}, {N_E5, 130}, {N_D5, 260}, {0, 130},
  {N_C5, 130}, {N_E5, 130}, {N_G5, 130}, {N_C6, 130}, {N_E6, 130}, {N_D6, 130}, {N_C6, 390}};
static const Note SONG_LULLABY[] = {   // falling asleep: slow and soft
  {N_E5, 380}, {N_G5, 380}, {N_E5, 380}, {N_D5, 760}, {0, 120}, {N_C5, 380}, {N_D5, 380}, {N_E5, 380}, {N_G4, 760}, {0, 120},
  {N_A4, 380}, {N_C5, 380}, {N_E5, 380}, {N_D5, 570}, {N_C5, 900}};
static const Note SONG_FIND[] = {   // "find my egg": loud chirps for ~8 s (any button stops it)
  {N_E6, 110}, {0, 60}, {N_E6, 110}, {0, 420}, {N_E6, 110}, {0, 60}, {N_E6, 110}, {0, 420}, {N_E6, 110}, {0, 60}, {N_E6, 110}, {0, 420},
  {N_E6, 110}, {0, 60}, {N_E6, 110}, {0, 420}, {N_E6, 110}, {0, 60}, {N_E6, 110}, {0, 420}, {N_E6, 110}, {0, 60}, {N_E6, 110}, {0, 420},
  {N_E6, 110}, {0, 60}, {N_E6, 110}, {0, 420}, {N_E6, 110}, {0, 60}, {N_E6, 110}, {0, 420}, {N_E6, 110}, {0, 60}, {N_E6, 110}, {0, 420},
  {N_E6, 110}, {0, 60}, {N_E6, 110}, {0, 420}, {N_E6, 110}, {0, 60}, {N_E6, 110}, {0, 420}, {N_C6, 90}, {N_E6, 90}, {N_G5, 90}, {N_C6, 300}};
static const Note SONG_WAKE[] = {{N_C5, 90}, {N_G5, 90}, {N_C6, 90}, {0, 60}, {N_E6, 220}};   // good morning!
const Note *songNotes = nullptr;
int songLen = 0, songAt = 0;
uint32_t songNext = 0, songStart = 0;

void sing(const Note *notes, int n) { songNotes = notes; songLen = n; songAt = 0; songNext = millis(); songStart = millis(); }
bool singing() { return songNotes != nullptr; }
void songTick() {
  if (!songNotes || millis() < songNext) return;
  if (songAt >= songLen) { songNotes = nullptr; noTone(PIN_BUZZ); return; }
  const Note &x = songNotes[songAt++];
  if (x.f) tone(PIN_BUZZ, x.f, x.ms * 9 / 10); else noTone(PIN_BUZZ);   // a tiny gap between notes, like the old games
  songNext = millis() + x.ms;
}
#define SING(s) sing(s, sizeof(s) / sizeof(s[0]))

void tuneLove() { beep(1047, 80); beep(1319, 80); beep(1568, 80); beep(2093, 160); }

// ---------------------------------------------------------------- Bluetooth: online through your phone
// No WiFi? The LazyTrack iPhone app connects over Bluetooth (in the background, like a watch) and passes the
// egg's requests to the server and the answers back. The link is encrypted (paired once, iOS asks), and the
// phone only ever forwards to the LazyTrack server. The phone also tells the egg the time.
// Messages are split into Bluetooth-sized pieces: 1st byte 'C' = more follows, 'E' = the last piece.
//   egg -> phone (notify):  {"m": "GET", "u": "https://...", "h": {headers}, "b": "body"}
//   phone -> egg (write):   "R<status>\n<body>" (an answer) or "T<unix time>" (the clock)
#define EGG_SVC "6c740001-8f3b-4a2e-9d1c-3b5a7e0f4c21"
#define EGG_TX "6c740002-8f3b-4a2e-9d1c-3b5a7e0f4c21"
#define EGG_RX "6c740003-8f3b-4a2e-9d1c-3b5a7e0f4c21"
NimBLECharacteristic *bleTx = nullptr;
// Only YOUR phone: a new phone can pair only while you put the egg in pairing mode (Info → C → B), and both
// screens show the same 6-digit code that you confirm on the egg (B) and on the phone (Pair). The rest of the
// time the egg only talks to phones it already knows; anything else is turned away.
volatile bool bleLinked = false;      // a known phone is connected, the link encrypted + confirmed with the code
volatile bool bleConnected = false;   // a phone is connected (maybe not paired yet)
volatile uint32_t bleConnAt = 0;      // when it connected (no secure link 30 s later: drop it)
volatile bool bleDrop = false;        // turn the connected phone away (unknown, pairing refused or failed)
volatile bool bleOldBond = false;     // a phone from the old no-code pairing: forget it, it has to pair with the code
NimBLEAddress bleOldAddr;
volatile uint32_t bleNewPairing = 0;  // a phone just paired with the code (the screen says so)
volatile uint32_t blePairUntil = 0;   // pairing mode: new phones accepted until then
volatile uint32_t blePin = 0, blePinAt = 0;   // the code waiting for your OK on the egg
volatile uint16_t blePinConn = 0xFFFF;
NimBLEServer *bleSrv = nullptr;
volatile uint16_t bleMtu = 23;
String bleBuf, bleResp;
volatile bool bleRespReady = false;
uint32_t bleSeen = 0;                 // last time the phone relayed something (for Info)

class EggServerCb : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *s, NimBLEConnInfo &c) override {
    bleConnected = true;
    bleConnAt = millis();
    Serial.printf("Bluetooth: a phone connected (free memory %u)\n", (unsigned)ESP.getFreeHeap());
    NimBLEDevice::startSecurity(c.getConnHandle());
  }
  void onDisconnect(NimBLEServer *s, NimBLEConnInfo &c, int reason) override {
    bleLinked = bleConnected = false;
    Serial.printf("Bluetooth: phone gone (reason %d), findable again\n", reason);
  }
  void onMTUChange(uint16_t mtu, NimBLEConnInfo &c) override { bleMtu = mtu; }
  // a new phone wants to pair: the code shows on the egg, but only in pairing mode
  void onConfirmPassKey(NimBLEConnInfo &c, uint32_t pin) override {
    if (millis() < blePairUntil) { blePinConn = c.getConnHandle(); blePinAt = millis(); blePin = pin + 1000000; Serial.println("Bluetooth: pairing code shown"); }   // +1e6: "000123" stays 6 digits
    else { NimBLEDevice::injectConfirmPasskey(c, false); Serial.println("Bluetooth: a phone tried to pair (not in pairing mode): refused"); }
  }
  void onAuthenticationComplete(NimBLEConnInfo &c) override {
    bleMtu = max((uint16_t)23, c.getMTU());
    bool wasPin = blePin != 0;
    blePin = 0;
    if (c.isEncrypted() && c.isAuthenticated()) {   // known phone, or just paired with the code
      bleLinked = true;
      if (wasPin || millis() < blePairUntil) { blePairUntil = 0; bleNewPairing = millis(); }
      Serial.println("Bluetooth: your phone, online through it");
    } else if (c.isEncrypted()) {                   // paired the old way (no code): not good enough anymore
      bleOldAddr = c.getIdAddress(); bleOldBond = true; bleDrop = true;
      Serial.println("Bluetooth: phone paired without a code: forgetting it, pair again (Info > C > B)");
    } else {
      bleDrop = true;
      Serial.println("Bluetooth: pairing refused or failed");
    }
  }
};

class EggRxCb : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *ch, NimBLEConnInfo &c) override {
    std::string v = ch->getValue();
    if (v.empty() || !c.isEncrypted() || !c.isAuthenticated()) return;   // only your paired phone
    bleBuf += String(v.c_str() + 1).substring(0, v.size() - 1);
    if (v[0] != 'E') return;
    if (bleBuf.startsWith("T")) {               // the phone's clock: set ours if it's missing or off
      time_t t = (time_t)atoll(bleBuf.c_str() + 1);
      if (t > 1700000000 && (time(nullptr) < 1700000000 || abs((long)(time(nullptr) - t)) > 5)) {
        struct timeval tv = {t, 0};
        settimeofday(&tv, nullptr);
      }
    } else if (bleBuf.startsWith("R")) {
      bleResp = bleBuf.substring(1);
      bleRespReady = true;
    }
    bleBuf = "";
  }
};

void bleSetup() {
  NimBLEDevice::init("LazyTrack Egg");
  NimBLEDevice::setMTU(247);
  NimBLEDevice::setSecurityAuth(true, true, true);    // bonded, protected by the 6-digit code, LE Secure Connections
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_DISPLAY_YESNO);   // the egg shows the code, B = yes
  NimBLEServer *srv = bleSrv = NimBLEDevice::createServer();
  srv->advertiseOnDisconnect(true);   // after the phone goes away, be findable again (off by default!)
  srv->setCallbacks(new EggServerCb());
  NimBLEService *svc = srv->createService(EGG_SVC);
  bleTx = svc->createCharacteristic(EGG_TX, NIMBLE_PROPERTY::NOTIFY | NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::READ_ENC);
  NimBLECharacteristic *rx = svc->createCharacteristic(EGG_RX, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR | NIMBLE_PROPERTY::WRITE_ENC);
  rx->setCallbacks(new EggRxCb());
  svc->start();
  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(EGG_SVC);    // so the phone can find it even in the background
  adv->setName("LazyTrack Egg");
  // WiFi and Bluetooth share one radio: advertising many times a second left WiFi waiting (requests stalled 12 s).
  // Every ~0.5-1 s is plenty for your phone to find the egg, even in the background.
  adv->setMinInterval(800);    // x 0.625 ms = 500 ms
  adv->setMaxInterval(1600);   // 1 s
  adv->start();
  Serial.printf("Bluetooth: advertising as LazyTrack Egg (free memory %u)\n", (unsigned)ESP.getFreeHeap());
}

// One request through the phone. Returns the HTTP status (negative = no phone / no answer), fills body.
int bleHttp(const String &method, const String &url, const String &payload, String &body, bool auth, const char *prefer) {
  if (!bleLinked || !bleTx) return -4;
  JsonDocument q;
  q["m"] = method;
  q["u"] = url;
  q["h"]["apikey"] = SUPA_KEY;
  q["h"]["Content-Type"] = "application/json";
  if (auth && token.length()) q["h"]["Authorization"] = "Bearer " + token;
  if (prefer) q["h"]["Prefer"] = prefer;
  if (payload.length()) q["b"] = payload;
  String msg;
  serializeJson(q, msg);
  bleRespReady = false;
  bleResp = "";
  int chunk = max(19, (int)bleMtu - 4);   // a piece + its 1-byte header fits one Bluetooth packet
  uint8_t buf[260];
  for (int i = 0; i < (int)msg.length(); i += chunk) {
    int n = min(chunk, (int)msg.length() - i);
    buf[0] = i + n >= (int)msg.length() ? 'E' : 'C';
    memcpy(buf + 1, msg.c_str() + i, n);
    bool ok = false;
    for (int tries = 0; tries < 40 && !ok && bleLinked; tries++) {   // the radio's queue can be full for a moment
      ok = bleTx->notify(buf, n + 1);
      if (!ok) delay(10);
    }
    if (!ok) return -5;
    delay(4);
  }
  uint32_t t0 = millis();
  while (!bleRespReady && bleLinked && millis() - t0 < 20000) delay(10);
  if (!bleRespReady) return -6;
  bleSeen = millis();
  int nl = bleResp.indexOf('\n');
  int code = bleResp.substring(0, nl < 0 ? bleResp.length() : nl).toInt();
  body = nl < 0 ? "" : bleResp.substring(nl + 1);
  bleResp = "";
  return code;
}

// pairing went wrong (often: the phone "forgot" the egg but the egg still had the old pairing):
// forget all pairings and drop the phone, so its next try pairs fresh. Called from loop().
void bleCheck() {
  if (!bleSrv) return;
  static uint32_t advCheck = 0;   // nobody connected and not findable (shouldn't happen, but never get stuck): advertise again
  if (millis() - advCheck > 5000) {
    advCheck = millis();
    if (bleSrv->getConnectedCount() == 0 && !NimBLEDevice::getAdvertising()->isAdvertising()) {
      bleConnected = bleLinked = false;
      NimBLEDevice::startAdvertising();
      Serial.println("Bluetooth: advertising again");
    }
  }
  if (blePin && millis() - blePinAt > 30000) blePin = 0;          // the code wasn't confirmed in time
  if (bleConnected && !bleLinked) setCrumb(blePin ? "Bluetooth: waiting for the code" : "Bluetooth: phone connecting");
  if (blePairUntil && millis() > blePairUntil) blePairUntil = 0;  // pairing mode over
  bool stuck = bleConnected && !bleLinked && !blePin && millis() - bleConnAt > 30000;
  if (bleOldBond) { bleOldBond = false; NimBLEDevice::deleteBond(bleOldAddr); }
  if (!bleDrop && !stuck) return;
  bleDrop = false;
  for (uint16_t h : bleSrv->getPeerDevices()) bleSrv->disconnect(h);   // turned away (your pairings stay)
  bleConnected = bleLinked = false;
}

void blePairMode() {   // B on the Phone screen: new phones can pair for 2 minutes
  blePairUntil = millis() + 120000;
  if (bleSrv && bleSrv->getConnectedCount() == 0) NimBLEDevice::startAdvertising();
  Serial.printf("Bluetooth: pairing mode on for 2 min (findable: %s, phones connected: %d, free memory %u)\n",
                NimBLEDevice::getAdvertising()->isAdvertising() ? "yes" : "NO", bleSrv ? bleSrv->getConnectedCount() : -1, (unsigned)ESP.getFreeHeap());
}

void bleAnswer(bool yes) {   // the code on the screen: same as on the phone?
  if (!blePin || !bleSrv) return;
  Serial.printf("Bluetooth: code %s on the egg\n", yes ? "confirmed" : "refused");
  setCrumb("Bluetooth: confirming the code");
  NimBLEDevice::injectConfirmPasskey(bleSrv->getPeerInfoByHandle(blePinConn), yes);
  blePin = 0;
}

void bleForgetAll() {
  NimBLEDevice::deleteAllBonds();
  if (bleSrv) for (uint16_t h : bleSrv->getPeerDevices()) bleSrv->disconnect(h);
  bleConnected = bleLinked = false;
}

bool online() { return WiFi.status() == WL_CONNECTED || bleLinked; }
String netName() {
  return WiFi.status() == WL_CONNECTED ? WiFi.SSID() : bleLinked ? String("phone (Bluetooth)")
       : bleConnected ? String("phone: pairing...") : String("offline");
}

// ---------------------------------------------------------------- files: the offline Mochi and your habit list
// The settings storage is tiny (20 KB) and also holds WiFi's calibration and the paired phones: when it filled up,
// pairing a phone froze the egg. So the big things go in the spare flash area (190 KB, "Minimal SPIFFS" scheme).
// Written straight into the "spiffs" flash area (no file system: that didn't fit in the chip's fast memory).
// Slots: card at 0 (up to 60 KB), habits at 64 KB (up to 12 KB). Each: 4-byte length, then the text.
const esp_partition_t *store = nullptr;
bool fsOk = false;
uint32_t slotOf(const char *path) { return path[1] == 'c' ? 0 : 65536; }
uint32_t slotMax(const char *path) { return path[1] == 'c' ? 61440 : 12288; }
String readFile(const char *path) {
  if (!fsOk) return "";
  uint32_t off = slotOf(path), n = 0;
  if (esp_partition_read(store, off, &n, 4) != ESP_OK || n == 0 || n > slotMax(path)) return "";   // empty flash reads 0xFFFFFFFF
  char *buf = (char *)malloc(n + 1);
  if (!buf) return "";
  String out;
  if (esp_partition_read(store, off + 4, buf, n) == ESP_OK) { buf[n] = 0; out = buf; }
  free(buf);
  return out;
}
void writeFile(const char *path, const String &s) {
  uint32_t off = slotOf(path), n = s.length();
  if (!fsOk || n > slotMax(path)) return;
  uint32_t span = ((n + 4 + 4095) / 4096) * 4096;
  if (esp_partition_erase_range(store, off, span) != ESP_OK) return;
  esp_partition_write(store, off, &n, 4);
  esp_partition_write(store, off + 4, s.c_str(), n);
}

// ---------------------------------------------------------------- talking to the friends server
WiFiClientSecure tls;

// POST/GET with the right headers. Returns the HTTP status (or a negative error), fills body.
int httpRaw(const String &method, const String &path, const String &payload, String &body, bool auth, const char *prefer);

int http(const String &method, const String &path, const String &payload, String &body, bool auth, const char *prefer = nullptr) {
  bool held = lockOwner == xTaskGetCurrentTaskHandle();
  if (held) UNLOCK();                                   // let the screen keep going while we wait for the network
  xSemaphoreTakeRecursive(httpLock, portMAX_DELAY);
  String got;
  uint32_t t0 = millis();
  {
    char c[48]; int q = path.indexOf('?');
    snprintf(c, sizeof c, "%s %s", method.c_str(), path.substring(0, q < 0 ? path.length() : q).c_str());
    setCrumb(c);
  }
  int code = httpRaw(method, path, payload, got, auth, prefer);
  // every request in the Serial Monitor: what, the answer, how long (the token never shows)
  int q = path.indexOf('?');
  Serial.printf("  %s %s -> %d (%lu ms)%s%s\n", method.c_str(), path.substring(0, q < 0 ? path.length() : q).c_str(), code, (unsigned long)(millis() - t0),
                code >= 400 ? ": " : "", code >= 400 ? got.substring(0, 120).c_str() : "");
  setCrumb("net: between requests");
  xSemaphoreGiveRecursive(httpLock);
  if (held) LOCK();
  body = got;
  return code;
}

int httpOnce(const String &method, const String &path, const String &payload, String &body, bool auth, const char *prefer);
int httpRaw(const String &method, const String &path, const String &payload, String &body, bool auth, const char *prefer) {
  int code = httpOnce(method, path, payload, body, auth, prefer);
  // a stuck or dropped request: try once more on a fresh connection. A log (POST) is only sent again when it surely
  // didn't reach the server (couldn't connect / send), so nothing gets logged twice.
  bool unsent = code == -1 || code == -2 || code == -4;
  if (code < 0 && code > -100 && WiFi.status() == WL_CONNECTED && (method == "GET" || unsent)) {
    delay(300);
    code = httpOnce(method, path, payload, body, auth, prefer);
  }
  return code;
}

int httpOnce(const String &method, const String &path, const String &payload, String &body, bool auth, const char *prefer) {
  if (WiFi.status() != WL_CONNECTED) return bleHttp(method, String(SUPA_URL) + path, payload, body, auth, prefer);   // through the phone
  // (HTTPClient's defaults, as when it worked. Closing the connection after every request made the library stop
  // reading when the server hung up: 200 with an empty answer.)
  HTTPClient h;
  h.setTimeout(12000);
  if (!h.begin(tls, String(SUPA_URL) + path)) { tls.stop(); return -1; }
  h.addHeader("apikey", SUPA_KEY);
  h.addHeader("Content-Type", "application/json");
  if (auth && token.length()) h.addHeader("Authorization", "Bearer " + token);
  if (prefer) h.addHeader("Prefer", prefer);
  int code = method == "POST" ? h.POST(payload) : method == "GET" ? h.GET() : h.sendRequest(method.c_str(), payload);
  body = code > 0 ? h.getString() : "";
  h.end();
  if (code < 0) tls.stop();   // a broken connection: start fresh next time
  if (code < 0 && bleLinked) return bleHttp(method, String(SUPA_URL) + path, payload, body, auth, prefer);   // WiFi without internet: try the phone
  return code;
}

bool takeSession(const String &body) {
  JsonDocument d;
  DeserializationError e = deserializeJson(d, body);
  if (e || !d["access_token"].is<const char *>()) {
    Serial.printf("login: the server's answer didn't have a session (%s, %u bytes)\n", e.c_str(), (unsigned)body.length());
    return false;
  }
  token = d["access_token"].as<String>();
  refresh = d["refresh_token"].as<String>();
  uid = d["user"]["id"].as<String>();
  tokenUntil = millis() / 1000 + (d["expires_in"] | 3600) - 120;
  prefs.putString("refresh", refresh);
  prefs.putString("uid", uid);
  return true;
}

bool loginNow();
bool login() {   // the session (token) only changes while holding the request lock, so requests never see half of it
  xSemaphoreTakeRecursive(httpLock, portMAX_DELAY);
  bool ok = loginNow();
  xSemaphoreGiveRecursive(httpLock);
  return ok;
}

bool loginNow() {
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
  status = code == 400 ? "Login failed: check secrets.h"
         : code == -4 ? (bleConnected ? "Phone connected, not paired yet" : "No WiFi or phone")
         : code <= -5 ? "Phone didn't answer (" + String(code) + ")"
         : "Can't reach the server (" + String(code) + ")";
  Serial.printf("login: %d (%s), free memory %u\n", code, WiFi.status() == WL_CONNECTED ? "WiFi" : "Bluetooth", (unsigned)ESP.getFreeHeap());
  return false;
}

uint16_t hexColor(const char *s) { return (s && s[0] == '#') ? rgb(strtoul(s + 1, nullptr, 16)) : INK; }

// (used before they're written below)
time_t parseIso(const char *s);
String hungryNow();
long dayNow();
int minuteNow();
int mealBit(const String &name);

bool parseCard(const String &body) {
  JsonDocument filter;
  JsonObject f = filter[0]["card"].to<JsonObject>();
  for (const char *k : {"hp", "tier", "tierName", "name", "gen", "age", "best", "deaths", "fx", "now", "sprite", "scene", "offline", "built", "mood", "tzo", "asleep", "hungry", "plan"}) f[k] = true;
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
  card.hasAsleep = !c["asleep"].isNull();
  card.asleepNow = c["asleep"] | false;
  card.hungry = c["hungry"] | "";
  JsonObject pl = c["plan"];
  if (!pl.isNull()) {
    const char *pd = pl["day"] | "";
    char iso[32]; snprintf(iso, sizeof iso, "%sT00:00:00", pd);
    card.planDay = parseIso(iso) / 86400;
    card.planBed = pl["bed"] | 30; card.planWake = pl["wake"] | 510;
    for (int d = 0; d < 2; d++) {
      int k = 0;
      for (JsonArray m : pl[d ? "m2" : "m"].as<JsonArray>()) {
        if (k >= 3) break;
        const char *n = m[0] | "?";
        card.meals[d][k++] = {n[0], m[1] | 0, m[2] | 0, (m[3] | 0) != 0};
      }
      card.nmeals[d] = k;
    }
    card.hasPlan = card.planDay > 0;
  }
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
  if (!online()) { status = card.ok ? "Offline: Mochi runs on his own" : "No WiFi or phone"; return; }
  if (!token.length() || millis() / 1000 > tokenUntil) { status = "Logging in..."; if (!login()) return; }
  String body;
  int code = http("GET", "/rest/v1/cards?user_id=eq." + uid + "&select=card,updated_at", "", body, true);
  if (code == 401) { token = ""; if (login()) code = http("GET", "/rest/v1/cards?user_id=eq." + uid + "&select=card,updated_at", "", body, true); }
  if (code == 200 && parseCard(body)) {
    lastSync = millis(); status = "Synced";
    // kept for offline (in a file), at most every 10 minutes
    static uint32_t keptAt = 0;
    if (!keptAt || millis() - keptAt > 600000) {
      keptAt = millis();
      writeFile("/card.json", body);
    }
  }
  else if (code != 200) status = "Sync failed (" + String(code) + ")";
}

// a logged-in request: logs in (again) when needed. Returns the HTTP status or a negative error.
int api(const String &method, const String &path, const String &payload, String &body, const char *prefer = nullptr) {
  if (!online()) return -2;
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
  if (n || !list.isNull()) nhabits = n;   // exactly what LazyTrack sent: removed habits disappear (even the last one)
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
  if (t || h["today"].is<JsonArray>()) ntoday = t;
}

void loadHabits() {   // your habits + today's numbers, put in egg_setup by LazyTrack
  habitsAt = millis();
  String body;
  if (api("GET", "/rest/v1/egg_setup?select=habits", "", body) != 200) return;
  parseSetup(body);
  if (nhabits) writeFile("/habits.json", body);
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

String lastLog = "";   // the last log made on the egg ("kind,time"), while C can still take it back
long undoId = 0;       // ...and its id once it reached the server

int queueAt(const String &q, const String &line) {   // where this whole line is in the queue, or -1
  if (q.startsWith(line + "\n")) return 0;
  int at = q.indexOf("\n" + line + "\n");
  return at < 0 ? -1 : at + 1;
}

void dropFromQueue(const String &line) {   // removes one line (re-read first: the screen may have added logs meanwhile)
  String q = prefs.getString("queue", "");
  int at = queueAt(q, line);
  if (at >= 0) prefs.putString("queue", q.substring(0, at) + q.substring(at + line.length() + 1));
}

void flushQueue() {   // network task only
  String q = prefs.getString("queue", "");
  if (!q.length()) return;
  int start = 0;
  while (start < (int)q.length()) {
    int nl = q.indexOf('\n', start);
    if (nl < 0) nl = q.length();
    String line = q.substring(start, nl);
    start = nl + 1;
    int comma = line.indexOf(',');
    if (comma <= 0) { dropFromQueue(line); continue; }
    if (queueAt(prefs.getString("queue", ""), line) < 0) continue;   // taken back meanwhile
    long id = 0;
    if (sendLog(line.substring(0, comma), (time_t)line.substring(comma + 1).toInt(), &id)) {
      dropFromQueue(line);
      if (line == lastLog) undoId = id;
    }
  }
}

// Mochi reacts right away; LazyTrack's real numbers follow within a minute
void reactTo(const String &k) {
  if (k == "joint") card.high = min(3, card.high + 1);
  else if (k == "alcohol") card.drunk = max(1, card.drunk);
  else if (k == "cigarette" || k == "vape") card.coughy = max(1, card.coughy);
}

void doLog(int i) {   // from the screen: queued, the network task sends it right away (the screen never waits for the server)
  const Habit &h = habits[i];
  time_t t = clockOk() ? time(nullptr) : 0;
  queueLog(h.k, t);
  lastLog = h.k + "," + String((long)t);
  undoId = 0;
  netKick = true;
  reactTo(h.k);
  h.bad ? tuneBad() : tuneGood();
  showMsg(h.bad ? "LOGGED" : "NICE!", "1 " + h.n + "   C = undo", 5000);
}

// Mochi is hungry: A feeds him (it goes to LazyTrack like a log, waiting if there's no WiFi)
void feed() {
  time_t t = clockOk() ? time(nullptr) : 0;
  queueLog("feed", t);   // goes out from the network task in a moment
  netKick = true;
  String meal = hungryNow();
  if (clockOk()) {   // remember it here too, so Mochi isn't hungry again before LazyTrack hears about it
    if (prefs.getLong("fedDay", -1) != dayNow()) { prefs.putLong("fedDay", dayNow()); prefs.putInt("fedMask", 0); }
    prefs.putInt("fedMask", prefs.getInt("fedMask", 0) | mealBit(meal));
  }
  tuneYum();
  petUntil = millis() + 1500;   // happy closed eyes for a moment
  showMsg("YUM!", meal.length() ? "Thanks for the " + meal + "!" : "Thanks!", 3000);
}

bool canUndo() { return lastLog.length() && millis() < msgUntil; }

void undoLog() {   // from the screen
  tuneClick();
  String q = prefs.getString("queue", "");
  if (queueAt(q, lastLog) >= 0) {   // not sent yet: just drop it
    dropFromQueue(lastLog);
    lastLog = ""; undoId = 0;
    showMsg("UNDONE", "Taken back", 3000);
  } else if (undoId) { undoReq = true; lastLog = ""; showMsg("UNDOING...", "", 3000); }
  else { lastLog = ""; showMsg("TOO LATE", "Undo it in the app", 3000); }
}

void undoNow() {   // network task: the log already reached the server
  String body;
  int code = api("DELETE", "/rest/v1/egg_inbox?id=eq." + String(undoId), "", body, "return=representation");
  undoId = 0;
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
  static uint32_t pets[3] = {0, 0, 0};   // petted 3 times in 20 seconds: he sings for you
  pets[0] = pets[1]; pets[1] = pets[2]; pets[2] = millis();
  if (pets[0] && pets[2] - pets[0] < 20000 && !singing()) { pets[0] = pets[1] = pets[2] = 0; SING(SONG_MOCHI); return; }
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
uint16_t *stage;                                        // drawn here strip by strip, each sent in one go (no flicker)
static const int STAGE_ROWS = 24;                       // the stage buffer: SW x 24 px (6 KB), Mochi's box is done in strips

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
  int rows = (SW * STAGE_ROWS) / 240;
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

bool stale();

// Asleep: what LazyTrack says (around your usual bedtime, never while you're on the computer);
// with an older LazyTrack: late at night where you are (1 to 7), or when the app says Mochi is sleepy
bool asleep() {
  if (card.hasAsleep && !stale()) return card.asleepNow;
  if (card.hasPlan && clockOk()) {   // offline: your usual sleep window (from LazyTrack's plan)
    int m = minuteNow(), b = card.planBed, w = card.planWake;
    return b < w ? (m >= b && m < w) : (m >= b || m < w);
  }
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
  if (gfx != lcd || !banded || 240 * band.bh < ZW * ZH) return;
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

// while he sings: little music notes float up from his head (same box as the Z's)
void drawNotes(uint32_t t) {
  if (gfx != lcd || !banded || 240 * band.bh < ZW * ZH) return;
  uint16_t *buf = band.fb;
  for (int y = 0; y < ZH; y++) for (int x = 0; x < ZW; x++) buf[y * ZW + x] = bgAt(ZX + x, ZY + y);
  static const char *N7 = "...XX...XXX..X.X..X....X...XXX..XXX.....";   // a 5x8 eighth note
  for (int k = 0; k < 3; k++) {
    uint32_t life = (t + k * 900) % 2700;
    int zy = ZH - 14 - (int)(life * (ZH - 10) / 2700);
    int zx = 6 + k * 22 + (int)(sin((t + k * 500) / 300.0) * 6);
    uint16_t col = k == 1 ? rgb(0xe0303a) : rgb(0x3d3650);
    for (int i = 0; i < 40; i++) if (N7[i] == 'X')
      for (int y = 0; y < 2; y++) for (int x = 0; x < 2; x++) {
        int yy = zy + (i / 5) * 2 + y, xx = zx + (i % 5) * 2 + x;
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
  bool petting = millis() < petUntil;
  static uint16_t cell[16][16];
  mochiNow(cell, framePx(blink, coughing), CoolF{false, 0, -1, false, false});
  int ox = PX - SX + dx, oy = PY - SY;
  static const char *H = ".X.X.XXXXXXXXXX.XXX...X..";   // 5x5 heart (petting: little hearts floating up on both sides)
  uint32_t left = petting ? petUntil - millis() : 0;
  for (int y0 = 0; y0 < SH; y0 += STAGE_ROWS) {   // strip by strip: a small buffer, still no flicker
    int h = min(STAGE_ROWS, SH - y0);
    for (int y = 0; y < h; y++) {
      int yy = y0 + y;
      uint16_t *row = stage + y * SW;
      for (int x = 0; x < SW; x++) row[x] = bgAt(SX + x, SY + yy);
      int cy = (yy - oy) / SCALE;
      if (yy >= oy && cy < 16)
        for (int cx = 0; cx < 16; cx++) {
          uint16_t col = cell[cy][cx];
          if (!col) continue;
          for (int x = 0; x < SCALE; x++) { int xx = ox + cx * SCALE + x; if (xx >= 0 && xx < SW) row[xx] = col; }
        }
      if (petting)
        for (int k = 0; k < 3; k++) {
          int life = (int)((1800 - left + k * 600) % 1800);
          int hy = 70 - life * 70 / 1800, hx = (k % 2 ? SW - 26 : 8) + (k == 2 ? 6 : 0);
          int r = (yy - hy) / 3;
          if (yy < hy || r >= 5) continue;
          for (int c = 0; c < 5; c++) if (H[r * 5 + c] == 'X')
            for (int x = 0; x < 3; x++) { int xx = hx + c * 3 + x; if (xx >= 0 && xx < SW) row[xx] = RED; }
        }
    }
    gfx->draw16bitRGBBitmap(SX, SY + y0, stage, SW, h);
  }
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

// ---------------------------------------------------------------- offline mode: Mochi's day on the egg's own clock
long localNow() { return (long)time(nullptr) + card.tzo * 60L; }   // seconds, your time
long dayNow() { return localNow() / 86400; }
int minuteNow() { return (int)((localNow() % 86400) / 60); }

const char *mealName(char n) { return n == 'b' ? "breakfast" : n == 'l' ? "lunch" : n == 'd' ? "dinner" : "meal"; }
int mealBit(const String &name) { return name == "breakfast" ? 1 : name == "lunch" ? 2 : name == "dinner" ? 4 : 0; }

bool fedHere(const String &name) {   // fed on this egg today (LazyTrack may not know yet)
  return prefs.getLong("fedDay", -1) == dayNow() && (prefs.getInt("fedMask", 0) & mealBit(name));
}

// Hungry right now? LazyTrack's word while it's fresh; otherwise worked out from the plan it sent.
String hungryNow() {
  if (!stale()) return card.hungry.length() && !fedHere(card.hungry) ? card.hungry : "";
  if (!card.hasPlan || !clockOk()) return "";
  long d = dayNow() - card.planDay;
  if (d < 0 || d > 1) return "";                   // the plan is too old: no meals rather than wrong ones
  int m = minuteNow();
  for (int i = 0; i < card.nmeals[d]; i++) {
    const Card::Meal &x = card.meals[d][i];
    String n = mealName(x.n);
    if (m >= x.s && m < x.e && !x.fed && !fedHere(n)) return n;
  }
  return "";
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

// Home's HUD pieces
void darkBox(int x, int y, int w, int h) {   // see-through dark box (78% of the outline color over the scene)
  if (gfx == &band) {
    uint16_t base = rgb(0x161418);
    for (int yy = max(y, (int)band.oy); yy < min(y + h, band.oy + band.bh); yy++)
      for (int xx = x; xx < x + w; xx++) { uint16_t &p = band.fb[(yy - band.oy) * 240 + xx]; p = ((p >> 2) & 0x39E7) + base; }
  } else gfx->fillRect(x, y, w, h, INK);
  gfx->drawRoundRect(x, y, w, h, 4, INK); gfx->drawRoundRect(x + 1, y + 1, w - 2, h - 2, 3, INK);
}

void heart(int x, int y, int fill) {   // a 7x6 pixel heart at 2x: 0 empty, 1 half, 2 full
  static const uint8_t H[6] = {0x6C, 0xFE, 0xFE, 0x7C, 0x38, 0x10};
  uint16_t off = rgb(0x5a5266);
  for (int r = 0; r < 6; r++) for (int c = 0; c < 7; c++)
    if (H[r] & (0x80 >> c)) gfx->fillRect(x + c * 2, y + r * 2, 2, 2, fill == 2 || (fill == 1 && c < 4) ? RED : off);
}

int chip(int x, int y, const String &s, uint16_t fill) {   // a little label; returns its width
  int w = 10 + s.length() * 6;
  gfx->fillRoundRect(x, y, w, 15, 3, fill);
  gfx->drawRoundRect(x, y, w, 15, 3, INK); gfx->drawRoundRect(x + 1, y + 1, w - 2, 13, 2, INK);
  text(x + 5, y + 4, s, 1, INK);
  return w;
}

void hint(int cx, const String &s) {   // the tiny button hints along the bottom (light, with a dark edge)
  int x = cx - s.length() * 3;
  text(x + 1, 307, s, 1, INK);
  text(x, 306, s, 1, CARD);
}

// ---------------------------------------------------------------- the themed screens (menu, lists, info...)
// All screens share the menu's look: Mochi's scene dimmed under deep purple, a title with its icon at the top,
// dark rounded rows (the picked one yellow) and small button hints at the bottom.
static uint16_t TDARK, TLINE, TSUB;   // row color, deep purple, lavender text (set in setup)
static const uint8_t PHONE_ICON[8] = {0x3C, 0x24, 0x24, 0x24, 0x24, 0x3C, 0x2C, 0x3C};
void pixIcon(const uint8_t *rows, int cx, int cy, int sc, uint16_t col);

void themeBg() {
  int16_t slide = band.ox;
  band.ox = 0;
  drawBackground();
  if (gfx == &band) {   // the scene at 25% over a deep purple (#2b2246)
    uint16_t base = rgb(0x201a34);
    for (int k = 0; k < 240 * band.bh; k++) band.fb[k] = ((band.fb[k] >> 2) & 0x39E7) + base;
  } else gfx->fillScreen(TLINE);
  band.ox = slide;
}

void themeHead(const uint8_t *icon, const String &title, const String &sub) {
  int sz = title.length() <= 8 ? 3 : 2, tw = title.length() * 6 * sz, x = (240 - tw) / 2 + (icon ? 14 : 0);
  if (icon) {
    int cx = x - 18, cy = sz == 3 ? 18 : 15;
    gfx->fillCircle(cx + 1, cy + 1, 12, INK); gfx->fillCircle(cx, cy, 12, INK); gfx->fillCircle(cx, cy, 10, YELLOW);
    pixIcon(icon, cx, cy, 2, INK);
  }
  text(x, sz == 3 ? 8 : 10, title, sz, CARD);
  if (sub.length()) { String t = sub.substring(0, 38); t.toUpperCase(); text((240 - t.length() * 6) / 2, 36, t, 1, TSUB); }
}

void themeHints(const String &h) { text((240 - h.length() * 6) / 2, 306, h, 1, rgb(0xbdb6cc)); }

void pill(int x, int y, int w, int h, bool sel) {   // a row: dark, or yellow with a hard shadow when picked
  if (sel) gfx->fillRoundRect(x + 2, y + 2, w, h, 7, INK);
  gfx->fillRoundRect(x, y, w, h, 7, sel ? YELLOW : TDARK);
  gfx->drawRoundRect(x, y, w, h, 7, INK); gfx->drawRoundRect(x + 1, y + 1, w - 2, h - 2, 6, INK);
}

void dcard(int x, int y, int w, int h) { pill(x, y, w, h, false); }   // a dark card for text

void dotAt(int x, int y, uint16_t col) { gfx->fillCircle(x, y, 5, INK); gfx->fillCircle(x, y, 3, col); }

void kv(int y, const String &k, const String &v) {   // a label left (lavender), its value right (white)
  text(22, y, k, 1, TSUB);
  String t = v.substring(0, 24);
  text(218 - t.length() * 6, y, t, 1, CARD);
}

// ---------------------------------------------------------------- the menu: a ring of round buttons around Mochi
// Five round buttons circle a smaller Mochi over the dimmed scene. C turns the ring (the picked one rolls to the top,
// big and yellow), B opens it, A goes back. Its name and a live line about it sit at the top.
static const int MENU_N = 5;
enum { M_LOG, M_FEED, M_FRIENDS, M_TODAY, M_INFO };
static const char *MENU_NAMES[MENU_N] = {"LOG", "FEED", "FRIENDS", "TODAY", "INFO"};
static const uint8_t MENU_ICONS[MENU_N][8] = {   // 8x8 pixel icons
  {0x7C, 0x46, 0x52, 0x7A, 0x52, 0x42, 0x7E, 0x00},   // a note with a plus
  {0x00, 0x18, 0x24, 0xFF, 0x81, 0x81, 0x42, 0x3C},   // a bowl
  {0x00, 0x66, 0x99, 0x66, 0x00, 0xFF, 0x99, 0xFF},   // two friends
  {0x00, 0x02, 0x02, 0x12, 0x12, 0x92, 0x92, 0xFF},   // bars
  {0x18, 0x00, 0x38, 0x18, 0x18, 0x18, 0x3C, 0x00}};  // an "i"
static const int RING_X = 120, RING_Y = 160, RING_R = 88;
float ringRot = 0;   // degrees, while the ring turns (0 = at rest)

void pixIcon(const uint8_t *rows, int cx, int cy, int sc, uint16_t col) {
  for (int y = 0; y < 8; y++) for (int x = 0; x < 8; x++)
    if (rows[y] & (0x80 >> x)) gfx->fillRect(cx - 4 * sc + x * sc, cy - 4 * sc + y * sc, sc, sc, col);
}

String nextMeal() {   // "NEXT: LUNCH 13:05" from LazyTrack's plan, or ""
  if (!card.hasPlan || !clockOk()) return "";
  long d = dayNow() - card.planDay;
  if (d < 0 || d > 1) return "";
  int m = minuteNow();
  for (int i = 0; i < card.nmeals[d]; i++) {
    const Card::Meal &x = card.meals[d][i];
    if (x.s > m && !x.fed) {
      char t[8]; snprintf(t, sizeof t, "%02d:%02d", x.s / 60, x.s % 60);
      String n = mealName(x.n); n.toUpperCase();
      return "NEXT: " + n + " " + t;
    }
  }
  return "";
}

String ringSub(int i) {   // the live line under the name
  int onl = 0; for (int k = 0; k < nfriends; k++) onl += friends[k].online;
  String h = hungryNow(); h.toUpperCase();
  switch (i) {
    case M_LOG: return nhabits ? String(nhabits) + (nhabits == 1 ? " HABIT TO LOG" : " HABITS TO LOG") : String("SET UP IN THE APP");
    case M_FEED: return asleep() ? String("SHH, HE'S SLEEPING") : h.length() ? "HUNGRY FOR " + h : nextMeal().length() ? nextMeal() : String("NOT HUNGRY NOW");
    case M_FRIENDS: return nfriends ? String(onl) + " ONLINE" : String("ADD SOME IN THE APP");
    case M_TODAY: { String t = card.tierName; t.toUpperCase(); return "HP " + String(card.hp) + " - " + t; }
    default: { String n = online() ? netName() : String("offline"); n.toUpperCase(); return n.substring(0, 30); }
  }
}

void ringPos(int i, int &x, int &y, float &top) {   // where button i sits right now; top: 1 at the top, 0 a step away or more
  float a = -90 + ((i - menuSel + MENU_N) % MENU_N) * (360.0f / MENU_N) + ringRot;
  if (a > 180) a -= 360;
  x = RING_X + (int)lroundf(cosf(a * PI / 180) * RING_R);
  y = RING_Y + (int)lroundf(sinf(a * PI / 180) * RING_R);
  top = max(0.0f, 1 - fabsf(a + 90) / (360.0f / MENU_N));
}

void drawMenu() {
  themeBg();
  // Mochi, smaller, in the middle
  static uint16_t cell[16][16];
  mochiNow(cell, framePx(false, false), CoolF{false, 0, -1, false, false});
  for (int cy = 0; cy < 16; cy++) for (int cx = 0; cx < 16; cx++) if (cell[cy][cx]) gfx->fillRect(88 + cx * 4, 128 + cy * 4, 4, 4, cell[cy][cx]);
  // the buttons: the ones further from the top first, so the big one is drawn over them
  int order[MENU_N]; float tops[MENU_N];
  for (int i = 0; i < MENU_N; i++) { int x, y; ringPos(i, x, y, tops[i]); order[i] = i; }
  for (int i = 1; i < MENU_N; i++) for (int j = i; j > 0 && tops[order[j]] < tops[order[j - 1]]; j--) std::swap(order[j], order[j - 1]);
  for (int k = 0; k < MENU_N; k++) {
    int i = order[k], x, y; float top;
    ringPos(i, x, y, top);
    int r = 18 + (int)(5 * top);
    bool big = top > 0.5f;
    if (big) gfx->fillCircle(x + 2, y + 2, r, INK);   // hard shadow
    gfx->fillCircle(x, y, r, INK);
    gfx->fillCircle(x, y, r - 2, big ? YELLOW : CARD);
    pixIcon(MENU_ICONS[i], x, y, big ? 3 : 2, INK);
  }
  // name + live line at the top, hints at the bottom
  String t = MENU_NAMES[menuSel];
  text((240 - t.length() * 18) / 2, 8, t, 3, CARD);
  String sub = ringSub(menuSel).substring(0, 38);
  text((240 - sub.length() * 6) / 2, 34, sub, 1, rgb(0xc9b8ff));
  String hint = "C TURN  B OPEN  A BACK";
  text((240 - hint.length() * 6) / 2, 306, hint, 1, rgb(0xbdb6cc));
}

void turnRing() {   // C: the next button rolls up to the top
  menuSel = (menuSel + 1) % MENU_N;
  if (!banded) { drawFrame(); return; }
  static const float steps[] = {0.62f, 0.32f, 0.12f, 0.0f};   // eased: quick, then settles
  for (float f : steps) { ringRot = (360.0f / MENU_N) * f; drawFrame(); }
  ringRot = 0;
}

// ---------------------------------------------------------------- today: everything you track, in pages
void drawToday() {
  themeBg();
  const int per = 10;
  int pages = max(1, (ntoday + per - 1) / per);
  if (todaySel >= ntoday) todaySel = 0;
  todayPage = ntoday ? todaySel / per : 0;
  themeHead(MENU_ICONS[M_TODAY], "TODAY", pages > 1 ? "PAGE " + String(todayPage + 1) + " OF " + String(pages) : String("SO FAR TODAY"));
  if (!ntoday) {   // older LazyTrack: only what's on your friend card
    dcard(12, 54, 216, 40 + card.nstats * 22);
    for (int i = 0; i < card.nstats; i++) text(24, 66 + i * 22, card.stats[i], 2, CARD);
    text(24, 74 + card.nstats * 22, "Update LazyTrack for all of it", 1, TSUB);
    themeHints("A BACK");
    return;
  }
  for (int i = todayPage * per, r = 0; i < ntoday && r < per; i++, r++) {
    int y = 54 + r * 24;
    bool sel = i == todaySel;
    pill(12, y, 216, 21, sel);
    if (today[i].bad || today[i].good) dotAt(25, y + 10, today[i].bad ? RED : GREEN);
    text(36, y + 3, today[i].s.substring(0, 11), 2, sel ? INK : CARD);
    text(220 - today[i].v.length() * 12, y + 3, today[i].v, 2, sel ? INK : CARD);
  }
  themeHints("C NEXT  B VS YESTERDAY  A BACK");
}

// ---------------------------------------------------------------- yesterday vs today, for one stat
void cmpBar(int y, const String &label, const String &val, long v, long top, uint16_t col) {
  dcard(12, y, 216, 74);
  text(24, y + 10, label, 1, TSUB);
  text(218 - val.length() * 18, y + 8, val, 3, CARD);
  int w = top > 0 ? (int)(188L * v / top) : 0;
  gfx->fillRoundRect(22, y + 44, 196, 18, 5, TLINE); gfx->drawRoundRect(22, y + 44, 196, 18, 5, INK);
  if (w > 0) { gfx->fillRoundRect(26, y + 48, max(w, 6), 10, 3, col); }
}

void drawCompare() {
  themeBg();
  if (!ntoday) { themeHead(MENU_ICONS[M_TODAY], "NOTHING YET", ""); themeHints("A BACK"); return; }
  Stat &st = today[todaySel % ntoday];
  String name = st.s; name.toUpperCase();
  themeHead(MENU_ICONS[M_TODAY], name.substring(0, 16), "VS YESTERDAY");
  if (!st.hasY) {
    dcard(12, 60, 216, 70);
    text(24, 74, "Update LazyTrack on your", 1, CARD);
    text(24, 90, "computer to see yesterday", 1, CARD);
    text(24, 106, "here.", 1, CARD);
    themeHints("C NEXT  A BACK");
    return;
  }
  long top = max(st.tv, st.yv);
  cmpBar(56, "YESTERDAY", fmtVal(st.yv, st.secs), st.yv, top, rgb(0xc9b8ff));
  cmpBar(140, "TODAY SO FAR", fmtVal(st.tv, st.secs), st.tv, top, YELLOW);
  long d = st.tv - st.yv;
  String verdict; uint16_t col = TDARK;
  if (d == 0) verdict = "SAME AS YESTERDAY";
  else {
    verdict = fmtVal(d > 0 ? d : -d, st.secs) + (d > 0 ? " MORE" : " LESS");
    if (st.sign) {
      bool better = (d > 0) == (st.sign > 0);
      col = better ? GREEN : RED;
      verdict += better ? " NICE!" : "";
    }
  }
  verdict.toUpperCase();
  gfx->fillRoundRect(14, 230, 212, 34, 8, INK);
  gfx->fillRoundRect(12, 228, 212, 34, 8, col); gfx->drawRoundRect(12, 228, 212, 34, 8, INK); gfx->drawRoundRect(13, 229, 210, 32, 7, INK);
  String v = verdict.substring(0, 17);
  text((240 - v.length() * 12) / 2, 238, v, 2, CARD);
  themeHints("C NEXT STAT  A BACK");
}

uint16_t tierColor(const String &t) {
  return t == "legend" ? rgb(0xffeeb0) : t == "tired" ? rgb(0xd9d3e6) : t == "sick" ? rgb(0xd3dbc4) : t == "shiny" ? rgb(0xbfe9ff) : rgb(0xc9b8ff);
}

void drawFriend() {
  if (!nfriends) {
    themeBg();
    themeHead(MENU_ICONS[M_FRIENDS], "FRIENDS", !online() ? "OFFLINE RIGHT NOW" : !friendsDone ? "LOADING..." : "NO FRIENDS YET");
    if (!friendsDone && online()) { themeHints("A BACK"); return; }
    dcard(12, 60, 216, 64);
    text(24, 74, "Add some in the LazyTrack", 1, CARD);
    text(24, 90, "app (Friends tab).", 1, CARD);
    themeHints("A BACK");
    return;
  }
  Friend &f = friends[friendSel];
  gfx->fillRect(0, 0, 240, 232, tierColor(f.tier));
  gfx->fillRect(0, 232, 240, 88, rgb(0x7cc97f));
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
  // the same HUD as your Home: their name + hearts top left, which friend top right, what they're up to at the bottom
  String nm = f.name.substring(0, 14);
  int nw = max(110, (int)nm.length() * 12 + 14);
  darkBox(8, 8, nw, 44);
  text(15, 14, nm, 2, CARD);
  text(15, 36, "@" + f.user.substring(0, 18), 1, TSUB);
  String nth = String(friendSel + 1) + "/" + String(nfriends);
  darkBox(232 - (int)nth.length() * 12 - 14, 8, nth.length() * 12 + 14, 24);
  text(232 - nth.length() * 12 - 7, 13, nth, 2, CARD);
  if (f.hasCard) {
    darkBox(8, 56, 94, 24);
    int halves = (f.hp + 5) / 10;
    for (int i = 0; i < 5; i++) heart(14 + i * 17, 62, constrain(halves - i * 2, 0, 2));
    String fx = f.drunk ? (f.drunk >= 3 ? "WASTED" : f.drunk == 2 ? "DRUNK" : "TIPSY") : f.high ? (f.high >= 3 ? "SO HIGH" : f.high == 2 ? "HIGH" : "BUZZED") : f.coughy ? "COUGHING" : "";
    if (fx.length()) chip(106, 60, fx, f.drunk ? rgb(0xffd28a) : f.high ? rgb(0x9be8a9) : rgb(0xd9d2e9));
  } else chip(8, 56, "NO CARD YET", rgb(0xd9d2e9));
  String line; uint16_t dot;
  bool said = status.startsWith("Poked") || status.startsWith("Poking") || status.startsWith("Easy") || status.startsWith("Couldn") || status.startsWith("Pokes");
  if (said) { line = status; dot = status.startsWith("Poked") ? YELLOW : RED; }
  else if (f.online) {
    String w = f.label.length() ? f.label : f.what == "coding" ? "coding" : f.what == "gaming" ? "gaming" : f.what == "youtube" ? "watching YouTube"
             : f.what == "away" ? "away" : "";
    line = w.length() ? w : String("online"); dot = GREEN;
  } else { line = f.seen ? "seen " + agoText(f.seen) : String("not seen yet"); dot = GREY; }
  line.toUpperCase();
  gfx->fillRoundRect(8 + 3, 270 + 3, 224, 26, 7, INK);
  gfx->fillRoundRect(8, 270, 224, 26, 7, CARD);
  gfx->drawRoundRect(8, 270, 224, 26, 7, INK); gfx->drawRoundRect(9, 271, 222, 24, 6, INK);
  dotAt(22, 283, dot);
  if (line.length() <= 15) text(34, 276, line, 2, INK); else text(34, 280, line.substring(0, 32), 1, INK);
  hint(40, "A BACK"); hint(120, "B POKE"); hint(200, "C NEXT");
}

void drawScreen();
void drawInfo();
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

// ---------------------------------------------------------------- your phone: pairing (Info → C)
uint32_t forgetAsk = 0;    // C pressed once: press again within 3 s to forget all phones
int phoneBack = V_INFO;   // where A goes back to from the Phone screen (the menu or Info)
void drawPhone() {
  themeBg();
  if (blePin) {   // a phone wants to pair: the same code is on its screen
    char code[8]; snprintf(code, sizeof code, "%06lu", (unsigned long)(blePin - 1000000));
    themeHead(PHONE_ICON, "PAIR", "SAME NUMBER ON YOUR PHONE?");
    gfx->fillRoundRect(23, 83, 196, 70, 12, INK);
    gfx->fillRoundRect(20, 80, 196, 70, 12, YELLOW); gfx->drawRoundRect(20, 80, 196, 70, 12, INK); gfx->drawRoundRect(21, 81, 194, 68, 11, INK);
    text(34, 99, String(code).substring(0, 3) + " " + String(code).substring(3), 4, INK);
    dcard(12, 172, 216, 56);
    text(24, 186, "Same? Press B here, then tap", 1, CARD);
    text(24, 202, "Pair on the phone too.", 1, CARD);
    themeHints("B YES, PAIR   A NO");
    return;
  }
  int n = NimBLEDevice::getNumBonds();
  themeHead(PHONE_ICON, "PHONE", bleLinked ? "CONNECTED, BACKS UP WIFI" : n ? "PAIRED, NOT NEAR" : "NOT PAIRED YET");
  dcard(12, 54, 216, 52);
  kv(66, "PAIRED PHONES", String(n));
  kv(84, "RIGHT NOW", bleLinked ? "connected" : bleConnected ? "connecting..." : "no phone");
  if (blePairUntil) {
    int left = (int)((blePairUntil - millis()) / 1000);
    gfx->fillRoundRect(14, 120, 214, 96, 10, INK);
    gfx->fillRoundRect(12, 118, 214, 96, 10, YELLOW); gfx->drawRoundRect(12, 118, 214, 96, 10, INK); gfx->drawRoundRect(13, 119, 212, 94, 9, INK);
    text(24, 128, "PAIRING " + String(left / 60) + ":" + (left % 60 < 10 ? "0" : "") + String(left % 60), 2, INK);
    text(24, 156, "On the phone: LazyTrack app >", 1, INK);
    text(24, 172, "Egg > Keep my egg online on.", 1, INK);
    text(24, 188, "A number shows on both.", 1, INK);
  } else {
    dcard(12, 118, 216, 80);
    text(24, 130, "Only phones paired here can", 1, CARD);
    text(24, 146, "use the egg. To add one, press", 1, CARD);
    text(24, 162, "B and confirm the 6-digit code.", 1, CARD);
    text(24, 180, "It keeps the egg online", 1, TSUB);
  }
  bool ask = forgetAsk && millis() - forgetAsk < 3000;
  if (ask) { String w = "C AGAIN = FORGET ALL PHONES"; text((240 - w.length() * 6) / 2, 290, w, 1, rgb(0xff9b9b)); }
  themeHints("B PAIR  C FORGET ALL  A BACK");
}

void drawScreen() {  // the background and everything on it; Mochi itself comes from drawStage
  if (view == V_MENU) { drawMenu(); return; }
  if (view == V_STATS) { drawToday(); return; }
  if (view == V_CMP) { drawCompare(); return; }
  if (view == V_PHONE) { drawPhone(); return; }
  if (view == V_LOG) {
    themeBg();
    themeHead(MENU_ICONS[M_LOG], "LOG", nhabits ? String("WHAT DID YOU JUST DO?") : String("NOTHING TO LOG YET"));
    if (!nhabits) {
      dcard(12, 60, 216, 64);
      text(24, 74, "Update LazyTrack on your", 1, CARD);
      text(24, 90, "computer, run supabase/egg.sql.", 1, CARD);
      themeHints("A BACK");
      return;
    }
    int rows = 9, first = logSel >= rows ? logSel - rows + 1 : 0;
    for (int i = first; i < nhabits && i < first + rows; i++) {
      int y = 54 + (i - first) * 26;
      bool sel = i == logSel;
      pill(12, y, 216, 22, sel);
      dotAt(25, y + 11, habits[i].bad ? RED : GREEN);
      String n = habits[i].n; n.setCharAt(0, toupper(n[0]));
      text(37, y + 4, n.substring(0, 15), 2, sel ? INK : CARD);
    }
    if (nhabits > first + rows) text(214, 292, "v", 1, TSUB);
    themeHints("C NEXT  B LOG IT  A BACK");
    return;
  }
  if (view == V_FRIENDS) { drawFriend(); return; }
  if (view == V_INFO) { drawInfo(); return; }
  drawBackground();
  if (view == V_HOME) {   // a game HUD over the scene: hearts + level top left, the clock top right, what's up at the bottom
    darkBox(8, 8, 94, 24);
    int halves = (card.hp + 5) / 10;   // 5 hearts, 20 HP each, halves too
    for (int i = 0; i < 5; i++) heart(14 + i * 17, 14, constrain(halves - i * 2, 0, 2));
    String tn = card.tierName; tn.toUpperCase();
    int bw = chip(8, 36, tn, tierColor(card.tier));
    static const char *DR[] = {"TIPSY", "DRUNK", "WASTED"}, *HI[] = {"BUZZED", "HIGH", "SO HIGH"};
    String fx = card.drunk ? DR[constrain(card.drunk, 1, 3) - 1] : card.high ? HI[constrain(card.high, 1, 3) - 1] : card.coughy ? "COUGHING" : "";
    if (fx.length()) chip(12 + bw, 36, fx, card.drunk ? rgb(0xffd28a) : card.high ? rgb(0x9be8a9) : rgb(0xd9d2e9));
    darkBox(162, 8, 70, 24);
    char clk[8] = "--:--";
    if (clockOk()) snprintf(clk, sizeof clk, "%02d:%02d", minuteNow() / 60, minuteNow() % 60);
    text(168, 13, clk, 2, CARD);
    if (millis() < msgUntil) {   // a moment's message: logged, poked, fed
      int tsz = msgTitle.length() <= 10 ? 3 : 2, tw = msgTitle.length() * 6 * tsz;
      int w = constrain(max(tw, (int)min((int)msgSub.length(), 36) * 6) + 28, 120, 224);
      int mx = (240 - w) / 2;
      gfx->fillRoundRect(mx + 3, 63, w, 50, 10, INK);
      gfx->fillRoundRect(mx, 60, w, 50, 10, TLINE); gfx->drawRoundRect(mx, 60, w, 50, 10, INK); gfx->drawRoundRect(mx + 1, 61, w - 2, 48, 9, INK);
      text((240 - tw) / 2, tsz == 3 ? 68 : 72, msgTitle, tsz, YELLOW);
      text((240 - (int)min((int)msgSub.length(), 36) * 6) / 2, 96, msgSub.substring(0, 36), 1, CARD);
    }
    // the bar: hungry, asleep, what you're doing, or how the egg is doing
    String hn = hungryNow();
    bool hungry = hn.length() && !asleep();
    String line; uint16_t dot;
    if (hungry) { line = "HUNGRY! A = FEED"; dot = YELLOW; }
    else if (asleep()) { line = "SLEEPING... ZZZ"; dot = rgb(0xc9b8ff); }
    else if (doing().length()) { line = doing(); dot = GREEN; }
    else { line = status == "Synced" || !status.length() ? String("Hanging out") : status; dot = online() ? GREEN : GREY; }
    line.toUpperCase();
    gfx->fillRoundRect(8 + 3, 270 + 3, 224, 26, 7, INK);
    gfx->fillRoundRect(8, 270, 224, 26, 7, CARD);
    gfx->drawRoundRect(8, 270, 224, 26, 7, INK); gfx->drawRoundRect(9, 271, 222, 24, 6, INK);
    gfx->fillCircle(22, 283, 5, INK); gfx->fillCircle(22, 283, 3, dot);
    if (line.length() <= 15) text(34, 276, line, 2, INK); else text(34, 280, line.substring(0, 32), 1, INK);
    String ha = hungry ? "A FEED" : "A PET", hc = canUndo() ? "C UNDO" : "C OFF";
    hint(40, ha); hint(120, "B MENU"); hint(200, hc);
    drawStage(0, false);
  }
}

void drawInfo() {
  themeBg();
  String nn = online() ? netName() : String("offline");
  themeHead(MENU_ICONS[M_INFO], "INFO", nn);
  String q = prefs.getString("queue", "");
  int waiting = 0; for (char ch : q) if (ch == '\n') waiting++;
  dcard(12, 52, 216, 160);
  kv(62, "WIFI", WiFi.status() == WL_CONNECTED ? WiFi.SSID().substring(0, 20) : String("not connected"));
  kv(81, "PHONE", String(bleLinked ? "connected" : bleConnected ? "pairing..." : NimBLEDevice::getNumBonds() ? "paired, not near" : "not paired")
                  + (bleSeen ? " " + String((millis() - bleSeen) / 1000) + "s" : ""));
  kv(100, "STATUS", status.substring(0, 22));
  kv(119, "LAST SYNC", lastSync ? String((millis() - lastSync) / 1000) + "s ago" : String("never"));
  kv(138, "PETS TODAY", String(loveToday()));
  kv(157, "LOGS WAITING", waiting ? String(waiting) : String("none"));
  kv(176, "MEMORY", String(ESP.getFreeHeap() / 1024) + " KB free");
  kv(195, "STARTED", resetWhy + " " + String(millis() / 60000) + "m ago");
  if (!touchOn) text(22, 226, "Touch: not connected (buttons only)", 1, TSUB);
  else {
    text(22, 226, "Touch: tap = pick, again = open,", 1, TSUB);
    text(22, 240, "top = back. Hold here 3 s to set", 1, TSUB);
    text(22, 254, "up touch again.", 1, TSUB);
  }
  themeHints("B SYNC  C PAIR PHONE  A BACK");
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
void netTask(void *);

void setup() {
  Serial.begin(115200);
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: resetWhy = "power on"; break;
    case ESP_RST_SW: resetWhy = "restarted"; break;
    case ESP_RST_PANIC: resetWhy = "CRASH"; break;
    case ESP_RST_INT_WDT: resetWhy = "FROZE (int)"; break;
    case ESP_RST_TASK_WDT: resetWhy = "FROZE (task)"; break;
    case ESP_RST_WDT: resetWhy = "FROZE (other)"; break;
    case ESP_RST_BROWNOUT: resetWhy = "POWER DIP"; break;
    case ESP_RST_EXT: resetWhy = "reset button"; break;
    default: resetWhy = "other " + String((int)esp_reset_reason());
  }
  Serial.printf("\n=== LazyTrack Egg %s: started because: %s ===\n", FW_VERSION, resetWhy.c_str());
  if (crumbMagic == 0xC0FFEE42 && (resetWhy.startsWith("CRASH") || resetWhy.startsWith("FROZE"))) {
    crumb[sizeof crumb - 1] = 0; frozeIn[sizeof frozeIn - 1] = 0;
    Serial.printf("=== it was busy with: %s ===\n", crumb);
    if (resetWhy == "FROZE (task)") Serial.printf("=== running then: %s ===\n", frozeIn);
    resetWhy += String(" in ") + crumb;
  }
  frozeIn[0] = 0;
  setCrumb("starting");
  dataLock = xSemaphoreCreateMutex();
  httpLock = xSemaphoreCreateRecursiveMutex();
  LOCK();   // setup draws; loop() takes it from here
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
  TDARK = rgb(0x3d3456); TLINE = rgb(0x2b2246); TSUB = rgb(0xc9b8ff);
  stage = (uint16_t *)malloc(SW * STAGE_ROWS * 2);
  banded = band.begin(0);   // before WiFi takes its share of memory
  scrollSetup();
  Serial.printf("screen buffer: %d rows per band\n", banded ? band.bh : 0);
  prefs.begin("lazytrack", false);
  if (T_IRQ >= 0) {
    pinMode(T_CLK, OUTPUT); pinMode(T_CS, OUTPUT); pinMode(T_DIN, OUTPUT); pinMode(T_DO, INPUT); pinMode(T_IRQ, INPUT_PULLUP);
    digitalWrite(T_CS, HIGH); digitalWrite(T_CLK, LOW);
    tcalOk = prefs.getBool("tok", false);
    tcSwap = prefs.getInt("tcs", 0); tcX0 = prefs.getInt("tx0", 0); tcX1 = prefs.getInt("tx1", 1);
    tcY0 = prefs.getInt("ty0", 0); tcY1 = prefs.getInt("ty1", 1);
    if (tcX1 == tcX0 || tcY1 == tcY0) tcalOk = false;
    // Is the touch chip really there? Untouched, it reports ~0 pressure and its IRQ line is high.
    // Without the T_ wires the readings are all 0s or all 1s (huge "pressure"), so touch stays off
    // and the egg works with the buttons alone. (Don't touch the screen while it starts.)
    long z = 0; int high = 0;
    for (int i = 0; i < 8; i++) { z += tpXfer(0xB0) + 4095 - tpXfer(0xC0); high += digitalRead(T_IRQ) == HIGH; delay(2); }
    touchOn = high == 8 && z / 8 < 150;
    Serial.printf("touch: %s (pressure %ld)\n", touchOn ? "found" : "not connected, buttons only", z / 8);
  }
  refresh = prefs.getString("refresh", "");
  uid = prefs.getString("uid", "");
  tls.setInsecure();  // a pocket toy: skip certificate checks (the server still needs your login)
  Serial.println("connecting to WiFi...");
  status = "Connecting to WiFi...";
  drawFrame();
  drawStage(0, false);
  tuneBoot();
  // the last Mochi it saw, so it shows up even without WiFi (outside, no hotspot)
  store = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, nullptr);
  fsOk = store && store->size >= 81920;
  String saved = readFile("/card.json");
  if (!saved.length() && prefs.isKey("card")) {   // older egg code kept it in the small settings storage: move it out
    size_t n = prefs.getBytesLength("card");
    if (n > 0 && n < 16384) { char *buf = (char *)malloc(n + 1); if (buf) { prefs.getBytes("card", buf, n); buf[n] = 0; saved = buf; free(buf); } }
    writeFile("/card.json", saved);
  }
  String hs = readFile("/habits.json");
  if (!hs.length() && prefs.isKey("habits")) { hs = prefs.getString("habits", ""); writeFile("/habits.json", hs); }
  prefs.remove("card"); prefs.remove("habits");   // frees the settings storage (WiFi calibration, paired phones need it)
  if (saved.length()) { parseCard(saved); status = "Offline: last seen Mochi"; drawFrame(); drawStage(0, false); }
  {
    nvs_stats_t ns;
    if (nvs_get_stats(nullptr, &ns) == ESP_OK)
      Serial.printf("storage: settings %u of %u entries free, flash area %s\n", (unsigned)ns.free_entries, (unsigned)ns.total_entries, fsOk ? "ok" : "MISSING (Tools > Partition Scheme > Minimal SPIFFS)");
  }
  if (hs.length()) parseSetup(hs);
  WiFi.mode(WIFI_STA);
  WiFi.onEvent([](WiFiEvent_t e, WiFiEventInfo_t info) {   // why WiFi drops, in the Serial Monitor
    if (e == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) Serial.printf("WiFi: disconnected (reason %d)\n", info.wifi_sta_disconnected.reason);
    else if (e == ARDUINO_EVENT_WIFI_STA_GOT_IP) Serial.printf("WiFi: connected, free memory %u\n", (unsigned)ESP.getFreeHeap());
  });
  wifi.addAP(WIFI_SSID, WIFI_PASS);
#ifdef WIFI2_SSID
  wifi.addAP(WIFI2_SSID, WIFI2_PASS);
#endif
#ifdef WIFI3_SSID
  wifi.addAP(WIFI3_SSID, WIFI3_PASS);
#endif
  bleSetup();   // the phone can take over when there's no WiFi
  drawFrame();
  // WiFi, logging in and syncing happen in the background from here: the buttons work right away
  UNLOCK();
  // The watchdog restarts the egg when core 0 gets no idle time for a while. Pairing does heavy math (the code)
  // on core 0 while a secure connection may be starting there too, which took longer than the default 5 s and
  // restarted the egg. So: 20 s, and the network task runs at the lowest priority (it shares, never hogs).
#if ESP_IDF_VERSION_MAJOR >= 5
  esp_task_wdt_config_t wdt = {.timeout_ms = 20000, .idle_core_mask = 1 << 0, .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt);
#endif
  // Core 1, next to the screen: the watchdog only watches core 0, where a slow connection restarted the egg.
  xTaskCreatePinnedToCore(netTask, "net", 12288, nullptr, 1, nullptr, 1);   // 12 KB: with 16 KB the secure connection didn't fit
}

// ---------------------------------------------------------------- the network task (core 0)
void netSync(bool all) {   // with the lock held: card, queued logs, pokes (+ habits / friends when due)
  Serial.printf("net: sync (%s, free memory %u)\n", WiFi.status() == WL_CONNECTED ? "WiFi" : bleLinked ? "phone" : "offline", (unsigned)ESP.getFreeHeap());
  int before = card.hp, fx = card.drunk * 100 + card.high * 10 + card.coughy;
  String tb = card.tierName, what = card.nowWhat + card.nowLabel, rh = hungryNow() + String(asleep());
  String wasHungry = hungryNow();
  sync();
  if (wasHungry.length() && !hungryNow().length() && !card.hungry.length() && !fedHere(wasHungry) && view == V_HOME && screenOn && !stale()) {
    petUntil = millis() + 1500; tuneYum(); showMsg("YUM!", "fed from LazyTrack", 3000);   // fed on the computer or phone
  }
  if (online()) {
    flushQueue();
    checkPokes();
    if (all || habitsKick || !nhabits || millis() - habitsAt > 120000) { habitsKick = false; loadHabits(); }
    if (all || friendsKick || (view == V_FRIENDS && millis() - friendsAt > 60000)) {
      friendsKick = false; loadFriends(); friendsDone = true; if (friendSel >= nfriends) friendSel = 0;
      if (view == V_FRIENDS) needRedraw = true;
    }
  }
  if (view == V_HOME && (card.hp != before || card.tierName != tb || card.drunk * 100 + card.high * 10 + card.coughy != fx
                         || card.nowWhat + card.nowLabel != what || hungryNow() + String(asleep()) != rh)) needRedraw = true;
  if (view == V_INFO || view == V_LOG || view == V_STATS) needRedraw = true;
}

// ---- your egg on the computer / phone (supabase/egg_status.sql): how it's doing, and commands for it
uint32_t eggCmdOff = 0;   // the tables aren't there yet: try again later
const char *viewName() {
  if (!screenOn) return "off";
  static const char *N[] = {"home", "today", "info", "menu", "log", "friends", "today", "phone"};
  return view >= 0 && view < 8 ? N[view] : "home";
}

void eggReport() {   // about once a minute: what LazyTrack shows under "Your egg"
  if (!uid.length() || millis() < eggCmdOff) return;
  String q = prefs.getString("queue", "");
  int waiting = 0; for (char ch : q) if (ch == '\n') waiting++;
  JsonDocument d;
  d["user_id"] = uid;
  d["seen_at"] = "now";
  d["via"] = WiFi.status() == WL_CONNECTED ? "wifi" : "phone";
  d["wifi"] = WiFi.status() == WL_CONNECTED ? WiFi.SSID().substring(0, 40) : String("");
  d["phones"] = min(20, NimBLEDevice::getNumBonds());
  d["phone_linked"] = (bool)bleLinked;
  d["screen"] = viewName();
  d["asleep"] = asleep();
  d["waiting"] = min(1000, waiting);
  d["free_kb"] = (int)(ESP.getFreeHeap() / 1024);
  d["fw"] = FW_VERSION;
  String pl, body;
  serializeJson(d, pl);
  int code = api("POST", "/rest/v1/egg_status", pl, body, "resolution=merge-duplicates,return=minimal");
  Serial.printf("net: checked in with LazyTrack (%d)%s\n", code, code == 404 ? ": run supabase/egg_status.sql" : "");
  if (code == 404) eggCmdOff = millis() + 600000;   // egg_status.sql not run yet
}

void eggCommands() {   // "make him sing", "find my egg", "sync now" from LazyTrack
  if (!uid.length() || millis() < eggCmdOff) return;
  String body;
  int code = api("GET", "/rest/v1/egg_commands?select=id,cmd,created_at&order=id.asc&limit=5", "", body);
  if (code == 404) { eggCmdOff = millis() + 600000; return; }
  if (code != 200) return;
  JsonDocument d;
  if (deserializeJson(d, body) || !d.as<JsonArray>().size()) return;
  String ids;
  for (JsonObject c : d.as<JsonArray>()) {
    if (ids.length()) ids += ",";
    ids += String((long)(c["id"] | 0));
    time_t at = parseIso(c["created_at"] | "");
    if (clockOk() && at && time(nullptr) - at > 600) continue;   // older than 10 min: too late to sing
    String cmd = c["cmd"] | "";
    if (cmd == "sing") eggCmd |= 1;
    else if (cmd == "find") eggCmd |= 2;
    else if (cmd == "sync") netKick = true;
  }
  String r;
  api("DELETE", "/rest/v1/egg_commands?id=in.(" + ids + ")", "", r, "return=minimal");
}

void netTask(void *) {
  LOCK(); status = "Connecting to WiFi..."; UNLOCK();
  for (int i = 0; i < 20 && wifi.run(1000) != WL_CONNECTED && !bleLinked; i++) {}
  configTime(0, 0, "pool.ntp.org");  // the clock, for "coding for 25 min"
  LOCK(); netSync(true); needRedraw = true; UNLOCK();
  uint32_t last = millis(), wifiTry = millis(), reportAt = 0, cmdAt = 0;
  bool wasLinked = bleLinked;
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(200));
    // no WiFi: look for it every 15 s (every 2 min while the phone covers for it)
    if (WiFi.status() != WL_CONNECTED && millis() - wifiTry > (bleLinked ? 120000UL : 15000UL)) {
      wifiTry = millis();
      if (wifi.run(3000) == WL_CONNECTED) { configTime(0, 0, "pool.ntp.org"); netKick = true; }
    }
    static uint32_t linkedAt = 0;   // the phone just connected: catch up in a moment (once pairing is done)
    if (bleLinked && !wasLinked) linkedAt = millis();
    if (linkedAt && millis() - linkedAt > 4000) { linkedAt = 0; netKick = true; }
    wasLinked = bleLinked;
    // a phone is pairing: no server traffic until it's done (pairing and a secure connection at once was too much)
    if (blePairUntil || blePin || (bleConnected && !bleLinked)) continue;
    if (pokeReq >= 0 || undoReq) {   // asked by the screen
      LOCK();
      if (pokeReq >= 0 && pokeReq < nfriends) { sendPoke(pokeReq); if (view == V_FRIENDS) needRedraw = true; }
      pokeReq = -1;
      if (undoReq) { undoReq = false; undoNow(); }
      UNLOCK();
    }
    bool due = millis() - last > 30000;              // every 30 s: LazyTrack's changes arrive within a minute
    if (due || netKick || habitsKick || friendsKick) {
      bool full = due || netKick;
      netKick = false;
      last = millis();
      LOCK();
      if (full) netSync(false);
      else if (online()) {
        if (habitsKick) { habitsKick = false; loadHabits(); needRedraw = true; }
        if (friendsKick) { friendsKick = false; loadFriends(); friendsDone = true; if (friendSel >= nfriends) friendSel = 0; needRedraw = true; }
      }
      UNLOCK();
    }
    if (online() && (!reportAt || millis() - reportAt > 60000)) { reportAt = millis(); LOCK(); eggReport(); UNLOCK(); }
    if (online() && millis() - cmdAt > 12000) { cmdAt = millis(); LOCK(); eggCommands(); UNLOCK(); }
  }
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

void act(int b);

void onButton(int phys) {
  if (songNotes == SONG_FIND) { songNotes = nullptr; noTone(PIN_BUZZ); msgUntil = millis(); tuneYum(); return; }   // found it
  if (!screenOn) { setScreen(true); drawFrame(); return; }   // any button wakes it up
  act(role(phys));
}

void act(int b) {   // b: 0 = next / menu, 1 = OK / pet, 2 = back / screen off (buttons and taps both end up here)
  if (!(view == V_HOME && b == 1)) tuneClick();               // petting has its own chirp
  switch (view) {
    case V_HOME:
      if (b == 0) { menuSel = 0; transition(V_MENU, true); }
      else if (b == 1) { if (hungryNow().length() && !asleep()) feed(); else pet(); }
      else if (canUndo()) undoLog();
      else setScreen(false);
      break;
    case V_MENU:
      if (b == 0) turnRing();
      else if (b == 2) transition(V_HOME, false);
      else if (menuSel == M_FEED) {   // straight back to Mochi: fed, or a word about when he eats
        view = V_HOME;
        if (hungryNow().length() && !asleep()) feed();
        else { String n = nextMeal(); showMsg(asleep() ? "SHH..." : "NOT HUNGRY", asleep() ? "he's sleeping" : n.length() ? n : "he'll tell you when", 3000); }
      } else if (menuSel == M_LOG) {
        if (!nhabits || millis() - habitsAt > 20000) habitsKick = true;   // fresh list in the background
        logSel = 0; transition(V_LOG, true);
      } else if (menuSel == M_FRIENDS) {
        friendSel = 0;
        if (!friendsDone || millis() - friendsAt > 60000) friendsKick = true;   // loads in the background
        status = ""; transition(V_FRIENDS, true);
      } else if (menuSel == M_TODAY) {
        if (!ntoday || millis() - habitsAt > 20000) habitsKick = true;   // fresh numbers in the background
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
      else if (b == 1 && nfriends) { pokeReq = friendSel; status = "Poking " + friends[friendSel].name + "..."; netKick = true; drawFrame(); }
      else if (b == 2) { status = ""; transition(V_MENU, false); }
      break;
    case V_INFO:
      if (b == 1) { status = "Syncing..."; drawFrame(); netKick = true; break; }   // the network task does it (Info updates when done)
      if (b == 0) { forgetAsk = 0; phoneBack = V_INFO; transition(V_PHONE, true); break; }   // C: your phone (pairing)
      if (b == 2 || b == 0) transition(V_MENU, false);
      break;
    case V_STATS:
      if (b == 0 && ntoday) { todaySel = (todaySel + 1) % ntoday; drawFrame(); }
      else if (b == 1 && ntoday) transition(V_CMP, true);
      else if (b == 2 || b == 0) transition(V_MENU, false);
      break;
    case V_PHONE:
      if (blePin) { bleAnswer(b == 1); drawFrame(); break; }                       // the code: B yes, A no
      if (b == 1) { blePairMode(); drawFrame(); }
      else if (b == 0) {
        if (forgetAsk && millis() - forgetAsk < 3000) { forgetAsk = 0; bleForgetAll(); showMsg("FORGOTTEN", "no phones paired now", 3000); }
        else { forgetAsk = millis(); drawFrame(); }
      } else transition(phoneBack, false);
      break;
    case V_CMP:
      if (b == 0 && ntoday) { todaySel = (todaySel + 1) % ntoday; drawFrame(); }
      else if (b == 2 || b == 1) transition(V_STATS, false);
      break;
    default:
      if (b == 2 || b == 0) { view = V_MENU; drawFrame(); }
  }
}

// ---------------------------------------------------------------- touch
// Tap to pick, tap the picked one again to open it, tap the top bar to go back. On Home: tap Mochi to pet,
// anywhere else for the menu. The first touch ever asks you to tap 3 targets (calibration, saved);
// hold a finger on the Info screen for 3 seconds to do it again.

int tpXfer(uint8_t cmd) {   // one 12-bit reading from the touch chip, bit by bit
  digitalWrite(T_CS, LOW);
  for (int i = 7; i >= 0; i--) {
    digitalWrite(T_DIN, (cmd >> i) & 1); delayMicroseconds(1);
    digitalWrite(T_CLK, HIGH); delayMicroseconds(1); digitalWrite(T_CLK, LOW);
  }
  delayMicroseconds(2);
  int v = 0;
  for (int i = 0; i < 16; i++) {
    digitalWrite(T_CLK, HIGH); delayMicroseconds(1);
    v = (v << 1) | digitalRead(T_DO);
    digitalWrite(T_CLK, LOW); delayMicroseconds(1);
  }
  digitalWrite(T_CS, HIGH);
  return (v >> 3) & 0xFFF;
}

bool touchRaw(int &a, int &b) {   // a finger on the screen? raw channel readings, averaged
  if (T_IRQ < 0 || digitalRead(T_IRQ) == HIGH) return false;
  int z = tpXfer(0xB0) + 4095 - tpXfer(0xC0);   // pressure
  if (z < 250) return false;
  long sa = 0, sb = 0;
  for (int i = 0; i < 4; i++) { sa += tpXfer(0xD0); sb += tpXfer(0x90); }
  if (digitalRead(T_IRQ) == HIGH) return false;  // lifted while reading: don't trust it
  a = sa / 4; b = sb / 4;
  return true;
}

bool touchAt(int &x, int &y) {   // a finger on the screen, in screen pixels
  int a, b;
  if (!touchRaw(a, b)) return false;
  int rx = tcSwap ? b : a, ry = tcSwap ? a : b;
  x = constrain(20 + (long)(rx - tcX0) * 200 / (tcX1 - tcX0), 0, 239);
  y = constrain(20 + (long)(ry - tcY0) * 280 / (tcY1 - tcY0), 0, 319);
  return true;
}

bool calTarget(int x, int y, int &a, int &b) {   // false = a button was pressed (cancel)
  gfx = lcd;
  lcd->fillScreen(BG);
  panel(14, 120, 211, 74);
  text(28, 132, "TAP THE +", 2, INK);
  text(28, 156, "Tap the middle of the cross,", 1, GREY);
  text(28, 170, "a fingernail works best.", 1, GREY);
  lcd->drawFastHLine(x - 12, y, 25, RED); lcd->drawFastVLine(x, y - 12, 25, RED); lcd->drawCircle(x, y, 7, INK);
  int ra, rb;
  uint32_t t = millis();
  while (touchRaw(ra, rb) && millis() - t < 5000) delay(10);   // let go of the last tap first
  delay(150);
  long sa = 0, sb = 0; int n = 0;
  uint32_t start = millis();
  while (n < 12) {
    if (btnQueue || millis() - start > 20000) return false;   // a button, or nobody tapping: cancel
    if (touchRaw(ra, rb)) { sa += ra; sb += rb; n++; }
    else if (n) { sa = sb = 0; n = 0; }   // only a steady press counts
    delay(15);
  }
  a = sa / n; b = sb / n;
  tuneClick();
  return true;
}

void calibrate() {
  if (!screenOn) setScreen(true);
  coolStart = 0;
  int a1, b1, a2, b2, a3, b3;
  bool ok = calTarget(20, 20, a1, b1) && calTarget(220, 20, a2, b2) && calTarget(20, 300, a3, b3);
  if (ok) {
    int swap = abs(b2 - b1) > abs(a2 - a1);   // moving right changed the "y" channel: the chip is turned
    int x0 = swap ? b1 : a1, x1 = swap ? b2 : a2, y0 = swap ? a1 : b1, y1 = swap ? a3 : b3;
    if (abs(x1 - x0) > 400 && abs(y1 - y0) > 400) {
      tcSwap = swap; tcX0 = x0; tcX1 = x1; tcY0 = y0; tcY1 = y1; tcalOk = true;
      prefs.putInt("tcs", tcSwap); prefs.putInt("tx0", tcX0); prefs.putInt("tx1", tcX1);
      prefs.putInt("ty0", tcY0); prefs.putInt("ty1", tcY1); prefs.putBool("tok", true);
      tuneGood();
      showMsg("TOUCH ON", "tap Mochi to pet, anywhere for the menu", 4000);
    } else showMsg("TRY AGAIN", "touch didn't read right, tap again", 4000);
  }
  noInterrupts(); btnQueue = 0; interrupts();
  view = V_HOME; drawFrame();
}

void onTap(int x, int y) {
  if (songNotes == SONG_FIND) { songNotes = nullptr; noTone(PIN_BUZZ); msgUntil = millis(); tuneYum(); return; }   // found it
  if (!screenOn) { setScreen(true); drawFrame(); return; }
  if (coolStart) return;
  bool top = y < 44;   // the title bar: back
  switch (view) {
    case V_HOME:
      if (canUndo() && y >= 56 && y < 116) { undoLog(); return; }
      if (x >= PX && x < PX + 16 * SCALE && y >= PY && y < PY + 16 * SCALE + 10) { act(1); return; }   // pet
      act(0); return;                                                                                   // menu
    case V_MENU: {   // tap a button: the top one opens, another one rolls up; Mochi or the title: back
      if (top || (x >= 88 && x < 152 && y >= 128 && y < 192)) { act(2); return; }
      for (int i = 0; i < MENU_N; i++) {
        int bx, by; float t; ringPos(i, bx, by, t);
        if ((x - bx) * (x - bx) + (y - by) * (y - by) > 26 * 26) continue;
        if (i == menuSel) act(1);
        else { tuneClick(); while (menuSel != i) turnRing(); }
        return;
      }
      return;
    }
    case V_LOG: {
      if (top || !nhabits) { act(2); return; }
      int first = logSel >= 9 ? logSel - 8 : 0, i = first + (y - 52) / 26;
      if (y > 290 || i >= nhabits) return;
      if (i == logSel) act(1); else { logSel = i; tuneClick(); drawFrame(); }
      return;
    }
    case V_FRIENDS:
      if (top || !nfriends) { act(2); return; }
      if (x >= PX && x < PX + 16 * SCALE && y >= PY && y < PY + 16 * SCALE) { act(1); return; }   // poke
      act(0); return;                                                                             // next friend
    case V_STATS: {
      if (top || !ntoday) { act(2); return; }
      int r = (y - 52) / 24, i = (todaySel / 10) * 10 + r;
      if (y < 52 || r >= 10 || i >= ntoday) return;
      if (i == todaySel) act(1); else { todaySel = i; tuneClick(); drawFrame(); }
      return;
    }
    case V_CMP: act(top ? 2 : 0); return;   // tap: next stat
    case V_INFO: act(top ? 2 : 1); return;  // tap: sync now
    case V_PHONE: act(top ? 2 : 1); return; // tap: pairing mode / yes
  }
}

void pollTouch() {
  if (T_IRQ < 0 || !touchOn) return;
  static uint32_t last = 0, t0 = 0;
  static bool down = false, held = false;
  static int sx = 0, sy = 0, ups = 0;
  if (millis() - last < 15) return;
  last = millis();
  int x, y;
  static int lows = 0;
  bool now = tcalOk ? touchAt(x, y) : touchRaw(x, y);
  if (!tcalOk) {   // the very first touch: set it up (only after a steady press, never from a flicker)
    lows = now ? lows + 1 : 0;
    if (lows >= 6) { lows = 0; calibrate(); }
    return;
  }
  if (now) {
    ups = 0;
    if (!down) { down = true; held = false; t0 = millis(); sx = x; sy = y; }
    else if (!held && view == V_INFO && millis() - t0 > 3000) { held = true; down = false; calibrate(); }
  } else if (down && ++ups >= 2) {   // lifted (2 readings in a row, so a flicker isn't a new tap)
    down = false;
    if (!held && millis() - t0 < 1500) onTap(sx, sy);
  }
}

void uiLoop();
void loop() {   // the screen and buttons; the network has its own task
  LOCK();
  uiLoop();
  UNLOCK();
  vTaskDelay(1);   // a moment for the network task to get its turn
}

void uiLoop() {
  static uint32_t tick = 0, nextBlink = 4000, blinkEnd = 0;
  if (needRedraw && !coolStart && screenOn) { needRedraw = false; drawFrame(); }
  static uint32_t pinShown = 0;   // a phone sent its code: show it (whatever screen you're on)
  if (blePin && blePin != pinShown) {
    pinShown = blePin;
    if (!screenOn) setScreen(true);
    coolStart = 0; view = V_PHONE; tuneClick(); drawFrame();
  }
  if (!blePin && pinShown) { pinShown = 0; if (view == V_PHONE) drawFrame(); }
  static uint32_t pairShown = 0;
  if (bleNewPairing && bleNewPairing != pairShown) { pairShown = bleNewPairing; tuneUp(); showMsg("PAIRED!", "your phone keeps the egg online", 4000); }
  static uint32_t phoneTick = 0;  // the pairing countdown / connection state on the Phone screen
  if (view == V_PHONE && screenOn && millis() - phoneTick > 1000) { phoneTick = millis(); drawFrame(); }
  if (eggCmd) {   // from LazyTrack: sing / find my egg
    uint8_t c = eggCmd; eggCmd = 0;
    if (!screenOn) setScreen(true);
    coolStart = 0;
    if (c & 2) { view = V_HOME; SING(SONG_FIND); shakeUntil = millis() + 2000; showMsg("HERE I AM!", "press any button", 8000); }
    else if (c & 1) { if (view != V_HOME) { view = V_HOME; drawFrame(); } SING(SONG_MOCHI); }
  }
  static int lastMin = -1;   // the clock on Home (and "playing for 25m") move on every minute
  if (clockOk() && minuteNow() != lastMin) { if (lastMin >= 0 && view == V_HOME && screenOn) needRedraw = true; lastMin = minuteNow(); }
  songTick();
  bleCheck();
  int b = pressed();
  if (b >= 0) onButton(b);
  pollTouch();

  static uint32_t hungryCheck = 0;   // a meal started (online or offline): a little rumble
  if (millis() - hungryCheck > 30000) {
    hungryCheck = millis();
    static String lastHungry = "";
    String hNow = hungryNow();
    if (hNow.length() && hNow != lastHungry && !asleep() && card.ok) tuneHungry();
    if (hNow != lastHungry && view == V_HOME) needRedraw = true;
    lastHungry = hNow;
  }
  if (msgUntil && millis() > msgUntil) { msgUntil = 0; lastLog = ""; if (view == V_HOME) drawFrame(); }   // the message is done

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
  if (screenOn && view == V_HOME && millis() > msgUntil && asleep() && !singing() && millis() - zTick > 90) { zTick = millis(); drawZs(zTick); }
  static uint32_t nTick = 0;   // singing: notes float up
  if (screenOn && view == V_HOME && singing() && millis() - nTick > 90) { nTick = millis(); drawNotes(millis() - songStart); }
  static bool wasSinging = false;
  if (wasSinging && !singing() && screenOn && view == V_HOME) drawFrame();   // song over: clear the notes
  wasSinging = singing();
  // now and then he just sings a little (awake, not hungry, you're looking at him): every 1-3 hours
  static uint32_t nextSong = 0;
  if (!nextSong) nextSong = millis() + (60 + random(121)) * 60000UL;
  if (millis() > nextSong) {
    nextSong = 0;
    if (screenOn && view == V_HOME && !asleep() && !hungryNow().length() && !coolStart && !singing()) SING(SONG_MOCHI);
  }
  // falling asleep: a lullaby; waking up: a little good-morning jingle
  static int sleptBefore = -1;
  int sleepsNow = asleep();
  if (card.ok && sleptBefore >= 0 && sleepsNow != sleptBefore && screenOn && !singing()) { if (sleepsNow) SING(SONG_LULLABY); else SING(SONG_WAKE); }
  if (card.ok) sleptBefore = sleepsNow;
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
