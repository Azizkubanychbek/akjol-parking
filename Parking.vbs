' Akjol parking launcher: starts the server hidden (if not running), the payment screen and the operator window.
Option Explicit
Dim sh, fso, dir, i, wasRunning
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)

wasRunning = Alive()
If Not wasRunning Then
  sh.Run "cmd /c """ & dir & "\start.bat""", 0, False
  For i = 1 To 40
    WScript.Sleep 500
    If Alive() Then Exit For
  Next
  sh.Run "cmd /c """ & dir & "\open_screen.bat""", 0, False
End If

If WScript.Arguments.Count = 0 Then
  ' operator window as a standalone app (no address bar)
  sh.Run "cmd /c start """" msedge --app=http://127.0.0.1:8100/ --start-maximized", 0, False
End If

Function Alive()
  On Error Resume Next
  Dim x
  Set x = CreateObject("MSXML2.ServerXMLHTTP")
  x.setTimeouts 1000, 1000, 1000, 1000
  x.Open "GET", "http://127.0.0.1:8100/api/screen", False
  x.Send
  Alive = (Err.Number = 0 And x.Status = 200)
End Function
