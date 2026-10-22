' Hotspot Collection Network - silent server launcher (no window, no browser)
Option Explicit
Dim sh, fso, root, target
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root
target = Chr(34) & root & "\tools\run_hidden.cmd" & Chr(34)
sh.Run target, 0, False
