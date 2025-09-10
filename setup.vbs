' Runs the bundled Python installer silently and waits for completion.
' This helper is used because .bat files cannot run GUI installers completely silent.

Option Explicit

Dim fso, shell, installerPath, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

Dim appDir
appDir = fso.GetParentFolderName(WScript.ScriptFullName)

installerPath = fso.BuildPath(appDir, "python_installer.exe")

If Not fso.FileExists(installerPath) Then
    WScript.Echo "python_installer.exe not found at: " & installerPath
    WScript.Quit 1
End If

' Command-line options for the official python.org Windows installer:
' /quiet - silent
' InstallAllUsers=1 - install for all users
' PrependPath=1 - add Python to PATH
' Include_pip=1 - ensure pip is installed
cmd = """" & installerPath & """" & " /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1"

' Run hidden (0) and wait (True)
Dim exitCode
exitCode = shell.Run(cmd, 0, True)

WScript.Quit exitCode