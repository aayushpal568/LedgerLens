import os
import subprocess
import time
import pyautogui
from pywinauto import Desktop

app = os.path.join(os.environ['LOCALAPPDATA'], 'LedgerLens', 'ledgerlens.exe')
folder = os.path.join(os.path.dirname(__file__), 'final_ui_synthetic')
subprocess.run(['powershell.exe','-NoProfile','-Command','Get-Process ledgerlens,ledgerlens-backend -ErrorAction SilentlyContinue | Stop-Process -Force'])
subprocess.Popen([app])
time.sleep(25)
w = next(x for x in Desktop(backend='uia').windows() if 'LedgerLens' in x.window_text())
w.set_focus(); time.sleep(1)
# Installed window is offset (89,30); DOM Choose Folder center is about (1211,546).
# Navigate to Client Directory then first client workspace using measured layout.
pyautogui.click(210, 169); time.sleep(1)
pyautogui.click(450, 240); time.sleep(1)
pyautogui.click(1300, 576); time.sleep(2)
windows = [(x.window_text(), x.element_info.class_name) for x in Desktop(backend='uia').windows()]
print(windows)
# If a native picker opened, select the synthetic folder through its real controls.
for x in Desktop(backend='uia').windows():
    if any(t in x.window_text().lower() for t in ('select folder','choose folder','browse for folder')):
        x.set_focus(); pyautogui.hotkey('alt','d'); pyautogui.write(folder); pyautogui.press('enter'); time.sleep(1); pyautogui.hotkey('alt','s'); print('SELECTED'); break
else:
    print('NO_DIALOG')
time.sleep(2)
subprocess.run(['powershell.exe','-NoProfile','-Command','Get-Process ledgerlens,ledgerlens-backend -ErrorAction SilentlyContinue | Stop-Process -Force'])
