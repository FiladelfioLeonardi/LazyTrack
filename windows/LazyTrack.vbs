' Starts LazyTrack without a black window. Used by the Start menu, desktop and startup shortcuts.
' If it's already running, it just opens its window. "/background" = start without opening the browser.
Option Explicit
Dim sh, fso, dir, background, running, http, py, rc
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
background = (WScript.Arguments.Count > 0)

running = False
On Error Resume Next
Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
http.setTimeouts 1500, 1500, 1500, 1500
http.open "GET", "http://127.0.0.1:8042/api/auth/state", False
http.send
If Err.Number = 0 Then running = True
On Error GoTo 0

If running Then
  ' already running: show its window (or the browser if the window isn't available)
  If Not background Then
    If fso.FileExists(dir & "\.venv\Scripts\pythonw.exe") Then
      sh.CurrentDirectory = dir
      sh.Run """" & dir & "\.venv\Scripts\pythonw.exe"" -m lazytrack window", 0, False
    Else
      sh.Run "http://localhost:8042"
    End If
  End If
  WScript.Quit
End If

If background Then sh.Environment("PROCESS")("LAZYTRACK_RESTARTED") = "1"
sh.CurrentDirectory = dir
' pythonw has no console at all, so no window can pop up (Windows Terminal ignores "hidden" for console programs).
' It logs to %USERPROFILE%\.lazytrack\lazytrack.log itself. Exit code 3 = restart me (e.g. after an update).
py = dir & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
  sh.Run """" & dir & "\windows\run-hidden.bat""", 0, False
  WScript.Quit
End If
sh.Environment("PROCESS")("PYTHONUTF8") = "1"
Do
  rc = sh.Run("""" & py & """ -m lazytrack run", 0, True)
  sh.Environment("PROCESS")("LAZYTRACK_RESTARTED") = "1"
Loop While rc = 3
