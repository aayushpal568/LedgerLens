import os, subprocess, time
import pyautogui
from pywinauto import Desktop

APP = os.path.join(os.environ['LOCALAPPDATA'], 'LedgerLens', 'ledgerlens.exe')
FOLDER = r'C:\Users\acer\Desktop\data\synthetic-finance-data-main\1\synthetic-finance-data-main\output'
subprocess.run(['powershell.exe','-NoProfile','-Command','Get-Process ledgerlens,ledgerlens-backend -ErrorAction SilentlyContinue | Stop-Process -Force'])
subprocess.Popen([APP]); time.sleep(25)
w = next(x for x in Desktop(backend='uia').windows() if 'LedgerLens' in x.window_text())
w.set_focus(); r = w.rectangle(); time.sleep(1)
# WebDriver logical window was 1441x845; UIA physical window is DPI-scaled.
sx, sy = r.width() / 1441.0, r.height() / 845.0
def click_dom(x, y, width=0, height=0):
    pyautogui.click(r.left + (x + width / 2) * sx, r.top + (y + height / 2) * sy)
    time.sleep(1.5)
click_dom(12, 100, 215, 40)       # Client Directory
click_dom(296, 156, 352, 173)     # First client card
click_dom(1062, 526, 297, 40)     # Choose Folder

time.sleep(2)
found = None
for candidate in Desktop(backend='uia').windows():
    if any(t in candidate.window_text().lower() for t in ('select folder','choose folder','browse for folder')):
        found = candidate; break
print('DIALOG', found.window_text() if found else None, 'SCALE', sx, sy)
if found:
    found.set_focus(); pyautogui.hotkey('alt','d'); pyautogui.write(FOLDER); pyautogui.press('enter'); time.sleep(1)
    for button in found.descendants(control_type='Button'):
        if button.window_text().lower() in ('select folder','select','open'):
            button.click_input(); print('SELECTED'); break
    else:
        pyautogui.hotkey('alt','s'); print('SELECTED_ALT')
time.sleep(3)
