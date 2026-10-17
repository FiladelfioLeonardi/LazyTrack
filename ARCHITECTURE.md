# How LazyTrack works

This is the "how is it built" guide, written for someone who knows C and C++ but not Python.
For installing and using it, see `README.md`.

---

## 1. The big picture

LazyTrack is **one long-running program on your Mac** plus **two front-ends** that talk to it.

```
                ┌──────────────────────── your Mac ─────────────────────────┐
                │                                                            │
                │   python -m lazytrack run        (ONE process, 4 threads)  │
                │   ┌───────────────┐   ┌───────────────┐   ┌──────────────┐ │
                │   │ main thread   │   │ sync thread   │   │ smoke thread │ │
                │   │ activity      │   │ git commits   │   │ webcam →     │ │
                │   │ tracker       │   │ 42 intra API  │   │ AI model →   │ │
                │   │ (every 5 s)   │   │ GitHub API    │   │ "🚬 or 🌿?"  │ │
                │   └──────┬────────┘   └──────┬────────┘   └──────┬───────┘ │
                │          │ writes            │ writes            │ writes  │
                │          ▼                   ▼                   ▼         │
                │   ┌──────────────────────────────────────────────────────┐ │
                │   │  ~/.lazytrack/lazytrack.db   (SQLite: one file)      │ │
                │   └──────────────────────────────────────────────────────┘ │
                │          ▲ reads / writes                                  │
                │   ┌──────┴────────┐                                        │
                │   │ HTTP server   │  port 8042, JSON API + the web page    │
                │   │ thread        │                                        │
                │   └──────┬────────┘                                        │
                └──────────┼─────────────────────────────────────────────────┘
                           │ HTTP (on the Mac: localhost; from the phone: Tailscale)
            ┌──────────────┴───────────────┐
            ▼                              ▼
   Browser: the dashboard           iPhone app (Swift)
   (dashboard.html, JavaScript)     quick log, food photo, gym, sleep/steps
```

Three ideas to keep in mind:

1. **Everything goes into one SQLite file.** SQLite is a C library that stores a whole SQL database in a single file. No database server is needed. Every part of the app reads and writes that same file.
2. **The dashboard is just a web page.** The Python program serves one HTML file. The JavaScript inside it asks the server for data (`/api/overview`, …) and draws it. There's no build step and no framework.
3. **The phone never stores the truth.** It sends events to the Mac's HTTP API. If the Mac can't be reached, the phone keeps them in a queue and sends them later.

---

## 2. Python in 5 minutes (for a C/C++ person)

You only need these to read the code:

| Python | Roughly in C/C++ |
|---|---|
| Indentation **is** the block. No `{ }` | `{ … }` |
| `def f(x, y=3):` | `auto f(auto x, auto y = 3)` |
| `x: int = 5`, `-> str` | Type *hints*. Documentation only, **not checked** when it runs |
| `None` | `nullptr` / "no value" |
| `list` → `[1, 2, 3]` | `std::vector` (any types mixed) |
| `dict` → `{"a": 1}` | `std::unordered_map<std::string, anything>` |
| `tuple` → `(1, "a")` | `std::tuple` (read-only) |
| `for x in items:` | `for (auto& x : items)` |
| `[f(x) for x in items if x > 0]` | A loop that builds a new vector (a "list comprehension") |
| `class A:` + `def method(self, …)` | `class A` + methods; `self` is `this`, written explicitly |
| `import json`, `from . import db` | `#include` + namespace (`db.conn()` ≈ `db::conn()`). `.` means "same package" |
| `f"{n} cigarettes"` | `printf("%d cigarettes", n)` |
| `try: … except ValueError:` | `try { … } catch (const std::invalid_argument&) {}` |
| `with open(p) as f:` | RAII: the file closes itself at the end of the block |
| `a if cond else b` | `cond ? a : b` |
| `x or default` | `x ? x : default` |
| `"__main__"`, `__init__.py` | Special names: `__init__` is the constructor; `__init__.py` marks a folder as a package |
| `threading.Thread(target=f).start()` | `std::thread(f)` |
| `lambda x: x * 2` | `[](auto x){ return x * 2; }` |

**Running things.** Python isn't compiled ahead of time. `python file.py` reads the source and runs it directly. That's why "update" just means swapping files and restarting.

**`.venv`** is a private folder holding a Python interpreter plus all the libraries this project uses (OpenCV, PyTorch…), so nothing gets installed system-wide. Think of it like a project-local `/usr/local`. `install.sh` creates it.

**`pip`** is the package manager (like `brew`, but for Python libraries). `requirements.txt` lists what to install.

---

## 3. How it starts

```
double-click start.command
  └─ runs:  .venv/bin/python -m lazytrack run
              └─ "-m lazytrack" runs the folder lazytrack/ as a program:
                 lazytrack/__main__.py  →  calls  cli.main()          (≈ int main())
                   └─ parses "run"  →  cmd_run(cfg, args)
                        1. dashboard.start(cfg)     start the HTTP server thread
                        2. opens the dashboard in your browser (once)
                        3. SyncWorker(...).start()  start the sync thread
                        4. SmokeDetector(...).start() start the camera thread
                        5. loop forever on the MAIN thread:
                             tracker.sample()            what app is in front?
                             summary.maybe_send_weekly() Sunday 20:00 notification
                             sleep 5 s (while letting macOS deliver events)
```

`./lt <command>` is a 3-line shell script that runs the same thing with other commands: `./lt check`, `./lt summary`, `./lt log gym`, `./lt camtest`, `./lt collect smoking`, `./lt train`, `./lt sync`, `./lt phone`.

**Why the tracker must be on the main thread:** macOS only updates "which app is frontmost" for code that runs the Cocoa event loop, and that loop lives on the main thread. `macos.pump_runloop()` sleeps *and* lets that loop run, so the information stays fresh.

---

## 4. The files, one by one

```
lazytrack 2/
├─ start.command, lt, install.sh       shell scripts: start / run a command / first install
├─ requirements.txt                    Python libraries to install
├─ README.md                           how to use it
├─ ARCHITECTURE.md                     this file
├─ lazytrack/                          the Python program (a "package")
│  ├─ __main__.py                      entry point → cli.main()
│  ├─ cli.py                           commands (run, check, phone…) + the sync thread
│  ├─ config.py                        settings: defaults + ~/.lazytrack/config.json
│  ├─ db.py                            SQLite: tables and small helpers
│  ├─ macos.py                         talks to macOS: front app, idle time, video playing, dialogs
│  ├─ activity.py                      screen time tracker
│  ├─ commits.py                       finds your git repos, counts your commits
│  ├─ fortytwo.py                      42 intra API (logtime, projects, blackhole)
│  ├─ github.py                        GitHub API (contributions, streak, repos)
│  ├─ smoke.py                         webcam smoking detector + training tools
│  ├─ summary.py                       numbers for a day / week, weekly summary text
│  ├─ detail.py                        numbers for one stat's detail page
│  ├─ dashboard.py                     the HTTP server and its JSON API
│  ├─ settings.py                      the Settings page backend + restart
│  └─ static/
│     ├─ dashboard.html                the whole web UI (HTML + CSS + JavaScript)
│     └─ icon.png                      home-screen icon
└─ ios/                                the iPhone app (Swift, built with Xcode)
   ├─ project.yml, setup.sh            generates the Xcode project
   └─ LazyTrack/*.swift
```

### config.py: settings

- `DEFAULT_CONFIG` is a big nested dict with every setting and its default.
- `load()` reads `~/.lazytrack/config.json` and merges it **on top of** the defaults (`_merge`, recursively). So your file only needs the things you changed. It's like default arguments for the whole app.
- `LEGACY_DEFAULTS` holds old default values that earlier versions wrote into your file. If your file still contains one unchanged, it gets removed so the improved default applies.

### db.py: the database

The tables (think of each as an array of structs on disk):

| Table | One row = | Main columns |
|---|---|---|
| `activity` | a stretch of time on one app/site | `app, title, domain, category, ts_start, ts_end` |
| `events` | one logged thing (a cigarette, a gym visit…) | `ts, kind, source, device, note` |
| `commits` | one git commit | `hash, repo, ts, message` |
| `metrics` | one number per day (sleep, steps from the phone) | `day, metric, value` |
| `ft_logtime`, `ft_projects` | 42 data | per day / per project |
| `gh_days` | GitHub contributions on one day | `day, count` |
| `kv` | small key → value store | blackhole date, last sync times… |

Times (`ts`) are **Unix timestamps** in seconds, like C's `time(NULL)` but as a `double`.

`conn()` gives **each thread its own connection** (`threading.local()`, which is like `thread_local` in C++), because a SQLite connection shouldn't be shared between threads. `PRAGMA journal_mode=WAL` lets one thread write while others read.

### macos.py: talking to macOS

Python can call Objective-C APIs through a bridge library called **pyobjc**:

- `frontmost_app()` → `NSWorkspace.sharedWorkspace().frontmostApplication()`, the same API a Swift app would call.
- `idle_seconds()` → `CGEventSourceSecondsSinceLastEventType(…, kCGAnyInputEventType)`: seconds since the last key press, click, scroll or mouse move, of **any** kind.
- `screen_locked()` → asks the login session whether the screen is locked.
- `browser_tab(app)` runs a tiny **AppleScript** (`osascript`) to read the current tab's URL and title from Chrome, Safari, Arc or Brave. Firefox doesn't support this.
- `video_playing()` runs `pmset -g assertions` and looks for an app asking macOS to keep the display on (`PreventUserIdleDisplaySleep`). Video players, browsers playing video, and video calls do exactly that. The result is cached for 10 s.
- `notify()` and `ask()` show a notification or a dialog with buttons, also via AppleScript.

### activity.py: screen time

Every 5 seconds `sample()` runs:

1. Is the screen locked, or are you **away**? Then stop.
2. Get the front app; if it's a browser, also get the URL and title.
3. `categorize()` maps them to a category (`coding`, `youtube`, `social`, `streaming`, `gaming`, `browsing`, `other`) using the rules in the config. The first matching rule wins. Domains match properly: `x.com` matches `www.x.com` but not `dropbox.com`.
4. `record()` saves it as a **segment**. If you're still on the same app, category and title as 5 s ago, it just extends the current row's `ts_end` (`UPDATE`). Otherwise it starts a new row. It's like run-length encoding, so a full day takes hundreds of rows, not tens of thousands.

**"Away" rule (`is_away`):**
- Any key press (arrow keys too), scroll, click or mouse move means you're there.
- No input for **5 minutes** means away, **unless a video or call is playing**. Watching still counts, up to 3 hours with no input at all.
- When it decides you're away, it **cuts the current segment back** to your last activity (or the last moment a video was playing). The 5 minutes it waited don't get counted.

Phone screen time arrives through the API (`POST /api/activity`) as finished segments.

### commits.py

Walks your home folder (4 levels deep, skipping `Library`, `node_modules`, …), finds folders containing `.git`, and runs `git log --all --pretty=...` in each one. It keeps only the commits whose author e-mail is yours, from `git config --global user.email`. `INSERT OR IGNORE` on the commit hash means re-reading the same commits is harmless.

### fortytwo.py: 42 intra

- **OAuth "client credentials":** POST your UID and secret to `/oauth/token` to get an access token valid for about 2 hours, then call `/v2/users/<login>` with `Authorization: Bearer <token>`.
- From the user object it reads `cursus_users` (level, `blackholed_at`) and `projects_users` (status of each project).
- Logtime comes from `/v2/users/<login>/locations_stats`. If that's not allowed, it falls back to adding up the raw login sessions, split at midnight.
- The API allows about 2 requests per second, so on HTTP 429 ("too many") it waits and retries.

### github.py

- **With a token:** one GraphQL request returns the full contribution calendar (the green squares), the totals and your 100 most recently pushed repos.
- **Without a token:** it uses public REST endpoints (profile, repos, last ~300 public events) and estimates contributions from push events.
- `status()` computes today, this week, your current and longest streak, and a 12-week grid for the heatmap.

### smoke.py: the smoking detector

It runs in its own thread so the tracker never waits for the camera. The pipeline, for each frame (about 4 per second):

```
camera frame (in RAM only, never saved)
  │
  ├─ 1. someone there?  face detection (Haar cascade, ~1 ms) + "did pixels change?"
  │
  ├─ 2. crop the area around face, mouth and hands, then score it (every 2nd frame):
  │       • your personal model, if you trained one (./lt train)  → probability 0..1
  │       • otherwise YOLO-World (a general object detector told to look for
  │         "cigarette", "lit cigarette", "cigarette in mouth", "burning cigarette tip"…)
  │       • + glowing tip check: a tiny, very bright orange spot near the face
  │
  ├─ 3. EVIDENCE (the important bit):
  │       evidence = evidence × 0.5^(seconds passed / 20)        ← fades, half-life 20 s
  │       evidence += 1.0  clear hit
  │                 + 0.4  near miss (cigarette half hidden by a finger)
  │                 + 0.5  glowing tip
  │
  └─ 4. evidence ≥ 4 (the "Normal" setting; Careful = 6, Quick = 2.5) → ask "Cigarette / Joint / Neither"
          • Cigarette or Joint → save the event, camera OFF for 10 min
          • Neither / no answer → camera stays on, no new question for 2 min
```

The **evidence** idea is why a finger over the cigarette or a few bad frames don't reset anything. Hits add up and fade slowly, much like a leaky integrator in signal processing.

The camera is released (green light off) when the screen is locked, when you've been idle 10 minutes with no video playing, and during the cooldown.

**Training:** `./lt collect smoking` and `./lt collect not_smoking` save small face-area crops into `~/.lazytrack/dataset/`. `./lt train` fine-tunes a small image classifier (`yolo11n-cls`) on them using your Mac's GPU. Every answer you give in the popup also adds one example. The running app notices the new model file and switches to it.

### summary.py and detail.py: the numbers

They're mostly SQL. The one trick worth understanding is **how much of a segment falls inside a day**:

```sql
SUM( MIN(ts_end, day_end) - MAX(ts_start, day_start) )
WHERE ts_end > day_start AND ts_start < day_end
```

That's interval intersection: clamp each segment to the day, then add up the lengths. The same pattern gives "this week", "today" and "yesterday up to this time", which is how "2 more than yesterday at this time" is computed.

`detail.py` builds the data for one stat's page: today vs yesterday, the 7- and 30-day averages, streaks ("days without" for bad habits, "days in a row" for good ones), the gaps between cigarettes, the timeline (exact moments, or merged time blocks for screen time) and 30 days of history.

### dashboard.py: the HTTP server

It uses Python's built-in `http.server`, with one thread per request. `do_GET` and `do_POST` are just a big `if path == …` dispatch.

| Method + path | What it does |
|---|---|
| `GET /` | the web page (`static/dashboard.html`), read fresh on every request |
| `GET /api/overview?day=YYYY-MM-DD` | everything for the tabs: today, yesterday, yesterday-at-this-time, week, last week, 14 days, 42, GitHub, recent logs |
| `GET /api/detail?kind=cigarette&day=…` | everything for one detail page |
| `POST /api/event` `{"kind":"gym"}` | log something; returns its id (used by Undo) |
| `POST /api/event/delete` `{"id":12}` | remove a log |
| `POST /api/activity` | phone screen time segment |
| `POST /api/metric` | phone sleep / steps for a day |
| `GET/POST /api/settings`, `POST /api/restart` | Settings page. **Only from the Mac itself** (they include your 42 secret) |
| `GET /api/ping` | "are you alive?" |

**Security:** by default it listens on `127.0.0.1`, which only your Mac can reach. `./lt phone` switches it to `0.0.0.0` (reachable from other devices) and creates a random **token**. Any request not coming from the Mac itself must include that token, or it gets `401`. The token lives in `config.json`.

**Restart** (`settings.restart_soon`) uses `os.execv`. Exactly like C's `execv()`, it replaces the running process with a fresh copy of itself in the same Terminal window.

### static/dashboard.html: the web UI

One file with three parts: HTML (the skeleton), CSS (the Pocket Pet look) and JavaScript (the logic). The JavaScript works like this:

- **Routing:** the part of the URL after `#` says what to show, e.g. `#tab=habits&view=detail&k=cigarette&day=2026-10-15`. Changing it fires `hashchange`, which calls `render()`.
- **`render()`** fetches the JSON (`fetch("/api/overview")`, the browser's HTTP call) and fills the page by building HTML strings (template literals, JavaScript's f-strings). The functions are `renderHome`, `renderHabits`, `renderScreen`, `renderSchool`, `loadDetail` and `loadSettings`.
- **It refreshes every 60 s**, quietly (no animations).
- **Charts** use the Chart.js library. A small plugin (`valueLabels`) writes the numbers on top of the bars.
- **The pet:** `PET` holds 10×10 pixel maps as strings, one per mood. `drawPet()` turns each character into a colored square. The mood is chosen from today's numbers in `renderHome()`.
- **Animations** only run when you open a page (`animNow`), and are skipped if your system has "Reduce motion" turned on.

### The iPhone app (`ios/`)

Swift and SwiftUI, roughly one file per job:

| File | Job |
|---|---|
| `Core.swift` | settings, the **Outbox** (every request is first saved to a JSON file, then sent; if the Mac is offline it retries later, like a persistent queue), small helpers |
| `Views.swift` | the screens: quick log, food photo, setup |
| `HealthSync.swift` | reads sleep and steps from Apple Health and sends one number per day |
| `GymManager.swift` | geofence: iOS wakes the app when you enter or leave the gym area; 30+ minutes inside = gym logged |
| `FoodClassifier.swift` | Apple's on-device image classifier (Vision); junk-food words, so it gets logged |
| `Intents.swift` | App Intents: "Log habit" for Siri, Shortcuts, Back Tap, plus "App opened/closed" for phone screen time |
| `LazyTrackApp.swift` | app entry point, the `lazytrack://setup` link handler |

Note: I couldn't compile the iPhone app myself (that needs a Mac with Xcode), so its first build may need a small fix or two.

---

## 5. Two walkthroughs

### "I smoke a cigarette at my desk"

1. The smoke thread sees a face, crops around it, YOLO scores 0.4: a clear hit, so evidence goes up by 1. Your finger covers the cigarette, so the next scores are 0.12: near misses, +0.4 each. You pull and the tip glows: +0.5.
2. After a few seconds the evidence passes 4, so `macos.ask()` shows the dialog. You click **Cigarette**.
3. `db.add_event("cigarette", source="camera")` inserts a row into `events`. The crop is saved for training. The camera turns off for 10 minutes.
4. Within a minute the dashboard's auto-refresh calls `/api/overview`. `summary.compute()` counts `events` rows with `kind='cigarette'` since midnight. Mochi gets smoky, and the Lungs bar drops.

### "I open the Habits tab and tap Cigarettes"

1. The URL becomes `#tab=habits&view=detail&k=cigarette`, so `render()` calls `loadDetail("cigarette")`.
2. That does `GET /api/detail?kind=cigarette&day=…`, and `detail.detail()` runs a few dozen small SQL queries (today, yesterday, 30 days of history, gaps, streak).
3. The JSON comes back, and JavaScript draws the yellow hero card, the facts, the timeline (one square per `events` row at its exact time) and the 30-day chart.

---

## 6. Where your data lives

Everything is in `~/.lazytrack/` (a hidden folder in your home folder):

| File | What |
|---|---|
| `lazytrack.db` | all the data (SQLite) |
| `config.json` | your settings and the phone token |
| `dataset/` | training photos for your personal smoking model |
| `smoke-personal.pt` | your trained model (once you've run `./lt train`) |

To look inside the database yourself (`sqlite3` is built into macOS):

```bash
sqlite3 ~/.lazytrack/lazytrack.db
sqlite> .tables
sqlite> SELECT datetime(ts,'unixepoch','localtime'), kind, source FROM events ORDER BY ts DESC LIMIT 10;
sqlite> SELECT category, ROUND(SUM(ts_end-ts_start)/3600.0,1) AS hours FROM activity
   ...> WHERE ts_start > strftime('%s','now','localtime','start of day','utc') GROUP BY category;
sqlite> .quit
```

---

## 6b. Accounts and habits

**`accounts.py`**: one account per LazyTrack (the person whose computer it is). Two tables: `users` (username, display name, password *hash*, `setup_done`) and `sessions` (random token, which user). Passwords go through `hashlib.scrypt`, a deliberately slow hash, so the password itself is never stored. Logging in sets a cookie `lt_session` that lasts a year. All the data that was there before belongs to that account; "Delete account, keep data" only clears those two tables.

How a request is let in (`_authed` in `dashboard.py`): a paired device with the phone token → yes. An account exists → only with a valid session cookie. No account yet → like before (the computer itself is trusted). The page `/` is always served, because the page itself shows the login screen.

**`habits.py`**: which habits are tracked is data, not code. The list lives in the `kv` table as JSON (key `habits`), next to `screen_stats` (what the stat sheet shows from screen time) and `modules` (42 and GitHub on or off). `CATALOG` holds the built-in habits; custom ones get a key starting with `c_`. Each habit carries its own HP (`hp`, and `cap` for good ones), so the HP math in the dashboard just loops over the list. Removing a habit only removes it from the list; old rows in `events` stay, so adding it back brings the history back.

**The flow in `dashboard.html`**: one overlay with panels side by side (`pHello`, `pAuth`, `pS1`–`pS4`). `flow.show(id)` slides one out and the next in; `flow.leave()` slides the overlay away and the app in. `setup.begin("edit")` reuses the same panels from Settings.

## 7. Changing things yourself

| You want to… | Change |
|---|---|
| add a new habit (e.g. `cold showers`) | nothing to code: Settings → Habits → "Add or change habits" → "Add your own". To add one to the built-in list for everyone, add a line to `CATALOG` in `habits.py` |
| put an app or site in a category | `~/.lazytrack/config.json` → `activity.categories` (`apps` = exact app names, `url_contains` = domains) |
| change when you count as away | Settings page → "Count me as away after", or `activity.idle_seconds` |
| make the detector ask more or less | Settings page → Careful / Normal / Quick |
| see what's going on | run `./lt -v run` for detailed logs, or `./lt check` to test permissions and detection |

After changing a `.py` file, restart (close `start.command` and open it again). After changing `dashboard.html`, just reload the page.

## Mochi's life (life.py)

HP carries over from day to day. Every night the finished day is settled: each tracked thing is
scored against the user's own target (less of: on target = 0, under heals, over hurts; more of:
last 7 days vs a daily or weekly goal), times its size (small 3 / medium 6 / big 10), plus 2 HP
for being alive, capped at -25 / +15 a day. At 0 HP the pet dies and a baby (next generation)
starts at 100. Targets for "less of" get ~10% tighter each week they're met on 5+ days.
State lives in the kv table: `life` (gen, born, hp, settled, deaths, log, best) and `rules`
(target/goal, per, size, auto, on). API: GET /api/life, POST /api/life/rules, POST /api/life/seen;
/api/overview includes `life` (with today's live score) and `life_day` for past days.
