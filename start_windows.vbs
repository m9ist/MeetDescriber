Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir  = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = appDir
' stderr в файл, а не в скрытую консоль: иначе traceback главного потока и любое
' исключение фонового потока пропадают бесследно. cmd /c нужен только ради ">>".
sh.Run "cmd /c """"C:\Python310\python.exe"" -m app.main 2>> """ & appDir & "\app.err.log""""", 0, False
