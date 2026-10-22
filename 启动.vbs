' Hotspot Collection Network - launcher with page chooser
' 1) 弹窗让你选要打开的页面（可用环境变量 HCN_PAGE 跳过，便于测试/快捷方式）
' 2) 静默启动 pythonw（无黑窗口）
' 3) 等 /api/health 就绪后打开所选页面
Option Explicit
Dim sh, shell, fso, root, pyw, i, choice, baseUrl, page

Set sh    = CreateObject("WScript.Shell")
Set shell = CreateObject("Shell.Application")
Set fso   = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root
baseUrl = "http://127.0.0.1:9000"

choice = sh.Environment("Process")("HCN_PAGE")
If choice = "" Then
    choice = InputBox( _
        "启动后打开哪个页面？" & vbCrLf & vbCrLf & _
        "  1 = 大屏（3D 地球 + 线索）" & vbCrLf & _
        "  2 = 设置（源 / 模型 / 代理 / 体检）" & vbCrLf & _
        "  3 = 成本与预算" & vbCrLf & _
        "  4 = 备忘录 / 收藏" & vbCrLf & _
        "  5 = 只启动，不打开网页" & vbCrLf & _
        "  0 = 取消启动" & vbCrLf & vbCrLf & _
        "直接回车 = 1（大屏）", "热点采集网络", "1")
End If
If IsEmpty(choice) Then WScript.Quit 0

Select Case Trim(CStr(choice))
    Case "0" : WScript.Quit 0
    Case "2" : page = "/static/settings.html"
    Case "3" : page = "/static/cost.html"
    Case "4" : page = "/static/notes.html"
    Case "5" : page = ""
    Case Else : page = "/"
End Select

If Not Probe() Then
    pyw = "D:\Software\Program-Ming\Python\pythonw.exe"
    If Not fso.FileExists(pyw) Then pyw = "pythonw.exe"
    sh.Run Chr(34) & pyw & Chr(34) & " run.py", 0, False
End If

For i = 1 To 60
    WScript.Sleep 500
    If Probe() Then Exit For
Next

If page <> "" Then shell.ShellExecute baseUrl & page, "", "", "open", 1

Function Probe()
    Dim req
    Probe = False
    On Error Resume Next
    Set req = CreateObject("MSXML2.XMLHTTP")
    req.Open "GET", baseUrl & "/api/health", False
    req.Send
    If Err.Number = 0 Then
        If req.Status = 200 Then Probe = True
    End If
    On Error GoTo 0
    Set req = Nothing
End Function
