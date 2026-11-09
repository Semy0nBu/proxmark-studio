Option Explicit
Dim fs, sh, folder, python, script
Set fs = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
python = fs.BuildPath(folder, ".venv\Scripts\pythonw.exe")
script = fs.BuildPath(folder, "run_gui.py")
If Not fs.FileExists(python) Then
  MsgBox "Run start.ps1 once to prepare the application.", 48, "Proxmark Studio"
Else
  sh.CurrentDirectory = folder
  sh.Run Chr(34) & python & Chr(34) & " " & Chr(34) & script & Chr(34), 0, False
End If
