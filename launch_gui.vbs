Dim CONDA_ENV
CONDA_ENV = "my_env"

Dim sh, fso, repoDir, base, python
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

repoDir = fso.GetParentFolderName(WScript.ScriptFullName)
base    = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.conda\envs\" & CONDA_ENV & "\"

If fso.FileExists(base & "pythonw.exe") Then
    python = base & "pythonw.exe"
ElseIf fso.FileExists(base & "python.exe") Then
    python = base & "python.exe"
Else
    MsgBox "Python not found in conda env """ & CONDA_ENV & """." & vbCrLf & _
           vbCrLf & "Looked in: " & base, vbCritical, "Launcher Error"
    WScript.Quit 1
End If

' ShellExecute handles the window station correctly for GUI apps.
' nShowCmd 1 = normal window (use 0 only if pythonw.exe is available).
Dim app
Set app = CreateObject("Shell.Application")
app.ShellExecute python, "redpitaya_combined_gui_qt.py", repoDir, "open", 1
