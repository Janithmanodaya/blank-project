Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "cmd.exe /k " & Chr(34) & base & "\start.bat" & Chr(34)
CreateObject("WScript.Shell").Run cmd, 1, False