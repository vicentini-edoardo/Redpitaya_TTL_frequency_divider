' ── Configuration — only edit these two lines ────────────────────────────────
Dim ENV_NAME : ENV_NAME = "my_env"       ' conda env name under %USERPROFILE%\.conda\envs\
Dim PKG_NAME : PKG_NAME = "my_package"  ' argument to:  python -m <PKG_NAME>
' ─────────────────────────────────────────────────────────────────────────────

Dim fso : Set fso = CreateObject("Scripting.FileSystemObject")
Dim wsh : Set wsh = CreateObject("WScript.Shell")

' Repo root is always the folder that contains this .vbs file,
' so the launcher works regardless of where the repo is placed.
Dim repoDir : repoDir = fso.GetParentFolderName(WScript.ScriptFullName)
Dim srcDir  : srcDir  = repoDir & "\src"

' Expand %USERPROFILE% at runtime so the path is username-independent.
Dim envDir : envDir = wsh.ExpandEnvironmentStrings("%USERPROFILE%") & _
                      "\.conda\envs\" & ENV_NAME

' Prefer pythonw.exe (suppresses the console window that python.exe opens).
Dim pyW : pyW = envDir & "\pythonw.exe"
Dim pyN : pyN = envDir & "\python.exe"
Dim pyExe

If fso.FileExists(pyW) Then
    pyExe = pyW
ElseIf fso.FileExists(pyN) Then
    pyExe = pyN
Else
    MsgBox "Python not found in conda env """ & ENV_NAME & """." & vbCrLf & _
           vbCrLf & "Looked in: " & envDir, vbCritical, "Launcher Error"
    WScript.Quit 1
End If

' Shell.Application.ShellExecute is required instead of WScript.Shell.Run(style=0).
' Run(0) creates a hidden window station that breaks Qt's ability to show a window.
' ShellExecute inherits the interactive desktop session, so Qt works correctly.
Dim sh : Set sh = CreateObject("Shell.Application")
sh.ShellExecute pyExe, "-m " & PKG_NAME, srcDir, "open", 1
