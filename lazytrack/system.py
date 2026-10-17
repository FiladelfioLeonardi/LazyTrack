"""Picks the right version of the operating-system pieces: macos.py on a Mac, windows.py on
Windows. Everything else in LazyTrack is the same on both and imports this file.

Both files offer the same functions (like a header with two .c files behind it):
  frontmost_app()      name of the app in front          browser_tab(app) -> (url, title)
  idle_seconds()       time since the last input          video_playing() -> True/False/None
  screen_locked()      is the screen locked               pump_runloop(seconds)
  notify(title, msg)   a notification                     ask(msg, buttons, default) -> button or None
  BROWSERS, BROWSER_SCRIPTS   which apps are browsers
"""
import sys

if sys.platform == "win32":
    from .windows import *  # noqa: F401,F403
    from .windows import BROWSERS, BROWSER_SCRIPTS  # noqa: F401
    IS_WINDOWS = True
else:
    from .macos import *  # noqa: F401,F403
    from .macos import BROWSERS, BROWSER_SCRIPTS  # noqa: F401
    IS_WINDOWS = False
