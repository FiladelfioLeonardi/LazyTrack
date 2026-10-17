# LazyTrack on Windows

LazyTrack watches your screen time, coding and habits, and keeps a little pixel pet (Mochi)
healthy when you are. Everything stays on your PC; only your profile card and chat go online
so friends can see you.

## Install (once, about 10 minutes)

1. Double-click **`LazyTrack-Setup.exe`**. If Windows says "Windows protected your PC", click **More info → Run anyway**
   (it's not signed by a big company, that's all).
2. Click **OK**. A page opens in your browser and shows the progress. It downloads about 1 GB (mostly the camera AI),
   so give it a few minutes. You don't need to install Python or anything else.
3. When it's done, LazyTrack opens: **Let's go → make your account** (name, username, email, password) → pick your habits
   and targets → a short guided tour.

LazyTrack is now in the **Start menu** and on the **desktop**, opens in **its own window** (not the browser), starts by itself
with Windows, and keeps tracking quietly in the background when you close the window (no black window). It **updates itself**: you never need the installer again.
To remove it: Settings → Apps → LazyTrack → Uninstall (your data stays unless you choose to delete it).

<details><summary>The manual way (without the installer)</summary>

1. Install Python 3.12 from python.org and tick **"Add python.exe to PATH"**.
2. Unzip the LazyTrack folder somewhere it can stay, double-click **`install.bat`**, then **`start.bat`**
   (keep that black window open).
</details>

## Good to know

- **Camera:** the smoking detector uses the webcam. If Windows asks, allow it. If it can't open the camera,
  turn on Settings → Privacy & security → Camera → "Let desktop apps access your camera". You can turn the
  detector off in Settings → Smoking detector.
- **Friends:** open the Friends tab and add your friend's username.
- **Websites:** Windows doesn't let apps read the address of a browser tab, so LazyTrack reads the tab
  title instead (e.g. "… - YouTube"). Common sites are recognized; others count as "Other websites".
- **iPhone:** see "Your iPhone" below.

## Your iPhone

Your phone shows your LazyTrack (Mochi, stats, logging buttons, friends, chat) by connecting to
your PC, from anywhere, through Tailscale (a free private connection between your own devices).

1. Install **Tailscale** on the PC (tailscale.com/download) and on the iPhone (App Store).
   Log in with the **same account** on both, and switch it **on** on the phone.
2. With LazyTrack running, double-click **`phone.bat`**. If Windows asks to allow Python on
   networks, click **Allow** (then run phone.bat again).
3. A QR code opens. Scan it with the iPhone **Camera** and tap the link: LazyTrack opens in Safari.
4. In Safari: **Share → Add to Home Screen**. Now it's an app icon on your phone.

The PC has to be on with LazyTrack running for the phone to show your stats.

**The question pops up everywhere:** when a camera catches you, "cigarette or joint?" shows on the PC
and in every open LazyTrack page, phone included. Answer anywhere. **Already registered** means you
logged it yourself already: nothing is logged twice, and the camera rests for a while.

## Test it

- **`camtest.bat`**: opens the camera with the live smoking score. Hold a pen like a cigarette:
  the score should go up. Press **Q** in the camera window to close it.
- **`check.bat`**: shows which app LazyTrack sees, idle time, the camera and a test notification.
- **Settings → Smoking detector → Show what the camera sees**: the same live view inside the app,
  while LazyTrack is running (Windows lets only one program use the camera, so close start.bat
  before using camtest.bat).
- **`train.bat`**: if it misses your cigarettes (colored RGB light, weak webcam), this teaches it what
  you look like smoking, in your room and your light. About 5 minutes, guided step by step.

If anything looks wrong, send a screenshot of the black window.
