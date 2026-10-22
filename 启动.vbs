' Hotspot Collection Network - silent launcher
' 1) 直接启动 pythonw（不经 cmd，因此不会闪黑窗口）
' 2) 轮询 /api/health 直到就绪（最多约 30 秒）
' 3) 用 ShellExecute 打开浏览器
Option Explicit
Dim sh, shell, fso, root, pyw, http, i, ready, url

Set sh    = CreateObject("WScript.Shell")
Set shell = CreateObject("Shell.Application")
Set fso   = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root

pyw = "D:\Software\Program-Ming\Python\pythonw.exe"
If Not fso.FileExists(pyw) Then pyw = "pythonw.exe"

' 已在运行则不重复启动
ready = False
For i = 1 To 2
    If Probe() Then ready = True
    If ready Then Exit For
    WScript.Sleep 300
Next
If Not ready Then
    sh.Run Chr(34) & pyw & Chr(34) & " run.py", 0, False
End If

ready = False
For i = 1 To 60
    WScript.Sleep 500
    If Probe() Then
        ready = True
        Exit For
    End If
Next

url = "http://127.0.0.1:9000/"
shell.ShellExecute url, "", "", "open", 1

Function Probe()
    Dim req
    Probe = False
    On Error Resume Next
    Set req = CreateObject("MSXML2.XMLHTTP")
    req.Open "GET", "http://127.0.0.1:9000/api/health", False
    req.Send
    If Err.Number = 0 Then
        If req.Status = 200 Then Probe = True
    End If
    On Error GoTo 0
    Set req = Nothing
End Function
