import os, subprocess, time
from pywinauto import Desktop

app = os.path.join(os.environ['LOCALAPPDATA'], 'LedgerLens', 'ledgerlens.exe')
subprocess.run(['powershell.exe','-NoProfile','-Command','Get-Process ledgerlens,ledgerlens-backend -ErrorAction SilentlyContinue | Stop-Process -Force'])
subprocess.Popen([app])
time.sleep(25)
windows = Desktop(backend='uia').windows()
for w in windows:
    if 'LedgerLens' in w.window_text():
        print('WINDOW', w.window_text(), w.element_info.class_name)
        for c in w.descendants():
            name = c.window_text()
            if name:
                print(c.element_info.control_type, repr(name), c.element_info.automation_id)
        break
