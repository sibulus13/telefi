' Launch any program with ZERO console window (for Windows scheduled tasks).
'
' Starting node.exe / powershell.exe / cmd.exe directly from Task Scheduler opens a console
' window, and `powershell -WindowStyle Hidden` still FLASHES one: Windows creates the console
' during process startup, before the program can hide it. WScript.Shell.Run with
' windowStyle 0 (SW_HIDE) hides it in the initial CreateProcess call itself, and wscript.exe
' is a GUI-subsystem program with no console of its own. Child processes inherit the hidden
' console, so nothing they start pops up either.
'
' Usage:   wscript.exe run-hidden.vbs <program> [args...]
' Example: wscript.exe run-hidden.vbs "C:\Python312\python.exe" -m telefi serve
' Exit code = the program's own exit code (waitOnReturn = True), so Task Scheduler's
' LastTaskResult keeps its meaning and the task stays "running" while the job runs.
Option Explicit

Dim objShell, objArgs, cmdLine, i

Set objShell = CreateObject("WScript.Shell")
Set objArgs = WScript.Arguments

If objArgs.Count = 0 Then
    WScript.Quit 2
End If

cmdLine = """" & objArgs(0) & """"
For i = 1 To objArgs.Count - 1
    cmdLine = cmdLine & " """ & objArgs(i) & """"
Next

WScript.Quit objShell.Run(cmdLine, 0, True)
