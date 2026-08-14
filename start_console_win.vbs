' 总控台 Windows 启动器 —— 隐藏窗口后台启动 server_win.py（不自动开浏览器）。
' 用法：双击本文件，或放入 shell:startup 开机自启；也可 wscript.exe start_console_win.vbs。
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "cmd /c cd /d """ & scriptDir & """ && python -X utf8 server_win.py --no-browser"
sh.Run cmd, 0, False
