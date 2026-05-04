Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "K:\repos\for_meets"
sh.Run "C:\Python310\pythonw.exe -m app.main", 0, False
