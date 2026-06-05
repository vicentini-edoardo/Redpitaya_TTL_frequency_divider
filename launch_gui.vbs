Dim CONDA_ENV
CONDA_ENV = "my_env"

Dim sh, fso, repoDir, srcDir, base, python
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

repoDir = fso.GetParentFolderName(WScript.ScriptFullName)
srcDir  = repoDir & "\src"
base    = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.conda\envs\" & CONDA_ENV & "\"

If fso.FileExists(base & "pythonw.exe") Then
    python = base & "pythonw.exe"
Else
    python = base & "python.exe"
End If

' ShellExecute handles the window station correctly for GUI apps.
' nShowCmd 1 = normal window (use 0 only if pythonw.exe is available).
Dim app
Set app = CreateObject("Shell.Application")
app.ShellExecute python, "-m my_package", srcDir, "open", 1
