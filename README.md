# LazyTrack

**Easiest install (Windows and Mac):** run the one-file installer, it does everything (its own Python, packages, shortcuts, start with the computer, auto-updates):

- **Windows:** double-click `LazyTrack-Setup.exe`. If Windows says "protected your PC": More info → Run anyway.
- **Mac:** unzip `Install-LazyTrack-Mac.zip`, then right-click **Install LazyTrack** → Open (on macOS 15+: try to open it once, then System Settings → Privacy & Security → Open Anyway).

The installers are built with `installer/build.sh` (into `dist/`). The manual way below still works; for Windows by hand see [WINDOWS.md](WINDOWS.md).

Tracks your life on your Mac without you doing anything:

| What | How |
|---|---|
| App and website time (YouTube, VS Code, Discord…) | Checks the front app and browser tab every 5 s. It stops counting when you're idle for 2 min (30 min for videos). |
| Commits | Scans your git repos every 15 min. |
| 42 logtime, projects, blackhole | Uses the 42 intra API, every hour. |
| Smoking | Webcam detector, then asks "🚬 or 🌿?". |
| Junk food, gym, water… | One-tap buttons on the dashboard, or `./lt log gym`. |
| Weekly summary | Notification on Sunday at 20:00, plus the dashboard. |

Everything is stored locally in `~/.lazytrack/` as a SQLite database plus `config.json`.

## Setup (once)

```bash
cd lazytrack
./install.sh        # creates .venv and installs everything (~1 GB, mostly PyTorch for the detector)
./lt check          # tests everything and triggers the macOS permission prompts
./lt camtest        # live camera view with detection boxes, press q to quit
```

`./lt check` triggers these permission prompts. Allow each one for **Terminal**:

- **Camera**, for the smoking detector.
- **Automation** for Chrome, Safari or Arc. This lets it read the current tab, which is how YouTube is told apart from other sites.
- **Files** for Documents and Desktop, used for the git repo scan.

If you clicked "Don't allow", fix it in **System Settings → Privacy & Security**.

## Start it

- **Double-click `start.command`.** A Terminal window opens and keeps running.
- **Autostart:** add `start.command` to **System Settings → General → Login Items**.
- **Dashboard:** http://localhost:8042

It runs inside Terminal on purpose. Terminal already has the camera permission, while a hidden background service can't show the permission prompt.

## Commands

```bash
./lt run              # start everything
./lt run --no-camera  # everything except the smoking detector
./lt summary          # this week's summary in the terminal (--last for last week)
./lt sync             # sync commits and 42 now
./lt log junk_food    # also: cigarette, joint, alcohol, energy_drink, gym, walk, water
```

## Connect 42 (optional, 2 minutes)

1. Go to https://profile.intra.42.fr/oauth/applications/new.
2. Use any name. For the redirect URI, use `http://localhost`. Scopes: `public`.
3. Copy the UID and SECRET into `~/.lazytrack/config.json`:

```json
"fortytwo": { "login": "yourlogin", "client_id": "u-s4t2ud-…", "client_secret": "s-s4t2ud-…" }
```

4. Run `./lt sync`.

## How the smoking detector works

Frames are analysed in RAM and thrown away. The only images saved are small face-area crops for training (see below). The detector works as a cascade:

1. **Every frame (4 fps):** it checks whether someone is in front of the camera, using face detection and motion. This is almost free.
2. **About 2 times a second while you're there:** it scores only the area around your face and hands, using your personal model once trained, otherwise **YOLO-World**. It runs on your Mac's GPU.
3. **Evidence builds up:** every clear detection adds a point and every near-miss (cigarette partly hidden by a finger, odd angle) adds a bit. The points fade slowly, so a few missed frames don't reset anything. When the evidence bar is full, a dialog asks *Cigarette / Joint / Neither*.
4. **After you answer Cigarette or Joint:** the camera switches off for 10 minutes, so one cigarette isn't counted twice. After **Neither** (or no answer), the camera stays on and keeps watching. It just won't ask again for 2 minutes.

The camera is released, so the green light turns off, in three cases:

- the screen is locked,
- you've been idle for 10 minutes,
- it's in the cooldown after a confirmed cigarette or joint.

**Make it accurate: train your personal model** (about 5 minutes, strongly recommended)

The built-in detector is general-purpose. A model trained on *you* in *your* room is much better:

```bash
./lt collect smoking        # 60 s: smoke or hold a cigarette/joint like you normally do, move around
./lt collect not_smoking    # 60 s: act normal: type, drink, eat, touch your face, phone, pen in mouth
./lt train                  # trains on your Mac's GPU in a few minutes
./lt camtest                # should now say "personal model"
```

Record each one 2–3 times, in different lighting (day, night, lamp) and in different spots, for best results. "Not smoking" photos with your hand near your mouth teach it what *isn't* smoking.

Every time you answer the popup, that crop is added to the dataset (`~/.lazytrack/dataset/`). Run `./lt train` again every week or two and it keeps improving. The running app switches to the new model by itself.

**Tuning:** in `config.json` → `smoke`:

- Too many false alarms: raise `trigger_score` (4 → 6), or `personal_threshold` (0.6 → 0.75) once you have a personal model.
- Too slow to notice, or misses: lower `trigger_score` (4 → 3) or `confidence` (0.15 → 0.1).
- Use `./lt camtest` to see the live score and the evidence bar.

## Customising categories

In `config.json` → `activity.categories`, the first rule that matches wins:

- `apps`: exact app names. Run `./lt check` to see what yours are called.
- `url_contains`: domains, so `x.com` also matches `www.x.com`.
- `title_contains`: text in the tab title.

Anything unmatched counts as `browsing` (browsers) or `other`.

## iPhone app

What it does on the phone:

| What | How |
|---|---|
| Quick log | Big buttons. It works offline and syncs when the Mac is reachable. You can also log via Siri ("Log cigarette in LazyTrack") or Back Tap. |
| Food | Snap a photo. Apple's on-device classifier spots junk food, which is logged after 5 s unless you cancel. The photo is never saved. |
| Gym | Save your gym location once. Any stay of 30+ minutes is logged by itself, even with the app closed. |
| Sleep & steps | Read from Apple Health (iPhone or Watch). Syncs every few hours. |
| App screen time | Shortcuts automations ("When YouTube is opened / closed"). |

### 1. Tailscale (once)

1. Install Tailscale on the Mac (`brew install --cask tailscale` or the App Store) and on the iPhone (App Store).
2. Log in with the **same account** on both.
3. Now the phone can reach your Mac anywhere: at the gym, at 42, or on 4G.

### 2. Build the app onto your iPhone

```bash
cd ios
./setup.sh      # installs XcodeGen if needed, generates the project, opens Xcode
```

In Xcode:

1. Click **LazyTrack** in the left bar, then **Signing & Capabilities**, then pick your **Team**. Your normal Apple ID works.
2. Plug in your iPhone and select it at the top.
3. Press ▶.

On the phone, the first launch may need two things:

- **Settings → Privacy & Security → Developer Mode**: turn it on.
- **Settings → General → VPN & Device Management**: trust your developer certificate.

With a free Apple ID, the app stops opening after 7 days. Press ▶ in Xcode again to renew it. A paid developer account ($99/year) removes this limit. If Xcode complains that HealthKit isn't available for your team, that's the same free-account limit.

### 3. Connect phone to Mac

```bash
./lt phone
```

This command:

- turns on network access (token-protected),
- shows a QR code.

Scan the QR code with the iPhone **Camera** app and tap the banner, and LazyTrack connects itself. Then restart `start.command` on the Mac.

You can also open the dashboard on the phone: `./lt phone` prints a link for that.

### 4. App screen time on iPhone (once per app)

Apple doesn't let apps read Screen Time numbers, so Shortcuts does it.

1. Open the **Shortcuts** app, go to **Automation**, and tap **+**.
2. Choose **App**, pick YouTube, check **Is Opened**, and set it to **Run Immediately**.
3. Add the action **App opened** (LazyTrack) with App name `YouTube`.
4. Repeat with **Is Closed** and the action **App closed**, using the same name.

Do this for each app you care about: Instagram, TikTok, etc.

### Lazier logging

- **Back Tap:** in Shortcuts, make a shortcut called "Log cigarette" with the action **Log habit** set to Cigarette. Then go to Settings → Accessibility → Touch → Back Tap → Double Tap and pick it. Now double-tapping the back of your phone logs it.
- **Siri:** say "Log junk food in LazyTrack".
- **Action Button:** set it to the Log habit shortcut.

Firefox can't be read via AppleScript. In Firefox, YouTube counts as plain `browsing`.
