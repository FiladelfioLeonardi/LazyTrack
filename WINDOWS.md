# LazyTrack on Windows

LazyTrack watches your screen time, coding and habits, and keeps a little pixel pet (Mochi)
healthy when you are. Everything stays on your PC; only your profile card and chat go online
so friends can see you.

## Install (once, about 10 minutes)

1. **Python 3.12.** Download the "Windows installer (64-bit)" from
   https://www.python.org/downloads/release/python-3128/ and run it.
   On the first screen, tick **"Add python.exe to PATH"**, then click Install Now.
2. **LazyTrack.** Unzip the LazyTrack folder somewhere it can stay, for example `Documents\LazyTrack`.
3. Double-click **`install.bat`**. It downloads what LazyTrack needs (about 1 GB, mostly the camera AI),
   so give it a few minutes. At the end it asks if LazyTrack should start with Windows: press **Y**.
4. Double-click **`start.bat`**. Your browser opens LazyTrack:
   **Let's go → make your account** (name, username, email, password) → pick your habits → done.

Keep the black start.bat window open (minimize it); closing it stops LazyTrack.

## Good to know

- **Windows may warn** "Windows protected your PC" the first time you open a .bat file:
  click **More info → Run anyway**. That's because the file isn't from a big company, not because it's dangerous.
- **Camera:** the smoking detector uses the webcam. If Windows asks, allow it. You can turn the
  detector off in Settings → Smoking detector.
- **Friends:** open the Friends tab and add your friend's username.
- **Websites:** Windows doesn't let apps read the address of a browser tab, so LazyTrack reads the tab
  title instead (e.g. "… - YouTube"). Common sites are recognized; others count as "Other websites".
- **Phone:** install Tailscale on the PC and the iPhone (tailscale.com/download, same login on both),
  then run `lt phone` in the LazyTrack folder (click the address bar of the folder, type `cmd`, Enter,
  then type `lt phone`). Scan the QR code with the iPhone camera and add the page to the home screen.

## Something not working?

Run `lt check` the same way as `lt phone` above: it shows which app it sees, the camera and a test
notification. Send a screenshot of that window.
