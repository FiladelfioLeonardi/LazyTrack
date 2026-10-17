' Starts LazyTrack without a black window. Used by the Start menu, desktop and startup shortcuts.
' If it's already running, it just opens it in the browser. "/background" = start without opening the browser.
Option Explicit
Dim sh, fso, dir, background, running, http
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
  If Not background Then sh.Run "http://localhost:8042"
  WScript.Quit
End If

If background Then sh.Environment("PROCESS")("LAZYTRACK_RESTARTED") = "1"
sh.CurrentDirectory = dir
sh.Run """" & dir & "\windows\run-hidden.bat""", 0, False
