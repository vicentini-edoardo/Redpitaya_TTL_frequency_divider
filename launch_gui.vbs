Dim CONDA_ENV
CONDA_ENV = "my_env"

Dim sh, fso, repoDir, userProfile, python
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

repoDir     = fso.GetParentFolderName(WScript.ScriptFullName)
userProfile = sh.ExpandEnvironmentStrings("%USERPROFILE%")

' Probe common conda install locations in priority order
Dim roots(4)
roots(0) = userProfile & "\.conda\envs\" & CONDA_ENV & "\"
roots(1) = userProfile & "\anaconda3\envs\" & CONDA_ENV & "\"
roots(2) = userProfile & "\miniconda3\envs\" & CONDA_ENV & "\"
roots(3) = userProfile & "\AppData\Local\anaconda3\envs\" & CONDA_ENV & "\"
roots(4) = userProfile & "\AppData\Local\miniconda3\envs\" & CONDA_ENV & "\"

Dim i
python = ""
For i = 0 To 4
    If fso.FileExists(roots(i) & "pythonw.exe") Then
        python = roots(i) & "pythonw.exe"
        Exit For
    ElseIf fso.FileExists(roots(i) & "python.exe") Then
        python = roots(i) & "python.exe"
        Exit For
    End If
Next

If python = "" Then
    MsgBox "Could not find Python for conda env """ & CONDA_ENV & """." & vbCrLf & _
           vbCrLf & "Searched under: " & userProfile, vbCritical, "Launcher Error"
    WScript.Quit 1
End If

' ShellExecute handles the window station correctly for GUI apps.
' nShowCmd 1 = normal window (use 0 only if pythonw.exe is available).
Dim app
Set app = CreateObject("Shell.Application")
app.ShellExecute python, "redpitaya_combined_gui_qt.py", repoDir, "open", 1
