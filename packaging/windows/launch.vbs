' Console-less launcher for TransDub Studio (Windows).
' Runs `uv run pythonw sp.py` with the working dir set to this script's folder.
Dim fso, sh, here, uv, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)

uv = sh.ExpandEnvironmentStrings("%USERPROFILE%\.local\bin\uv.exe")
If Not fso.FileExists(uv) Then uv = "uv"   ' fall back to PATH

sh.CurrentDirectory = here
' 0 = hidden window, False = don't wait
cmd = """" & uv & """ run pythonw sp.py"
sh.Run cmd, 0, False
