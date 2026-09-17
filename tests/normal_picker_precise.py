import os, subprocess, time
import pyautogui
from pywinauto import Desktop

APP = os.path.join(os.environ['LOCALAPPDATA'], 'LedgerLens', 'ledgerlens.exe')
FOLDER = r'C:\Users\acer\Desktop\data\synthetic-finance-data-main\1\synthetic-finance-data-main\output'
subprocess.run(['powershell.exe','-NoProfile','-Command','Get-Process ledgerlens,ledgerlens-backend -ErrorAction SilentlyContinue | Stop-Process -Force'])
subprocess.Popen([APP]); time.sleep(25)
w = next(x for x in Desktop(backend='uia').windows() if 'LedgerLens' in x.window_text())
w.set_focus(); rect=w.rectangle(); time.sleep(1)
# DOM positions measured by WebDriver are relative to the content window.
pyautogui.click(rect.left + 12 + 107, rect.top + 100 + 20)  # Client Directory
# choose newest client card
pyautogui.click(rect.left + 296 + 176, rect.top + 156 + 86); time.sleep(1)
pyautogui.click(rect.left + 1062 + 148, rect.top + 526 + 20); time.sleep(2)
found = None
for candidate in Desktop(backend='uia').windows():
    title = candidate.window_text().lower()
    if any(t in title for t in ('select folder','choose folder','browse for folder')):
        found = candidate; break
print('DIALOG', found.window_text() if found else None)
if found:
    found.set_focus(); pyautogui.hotkey('alt','d'); pyautogui.write(FOLDER); pyautogui.press('enter'); time.sleep(1)
    buttons=found.descendants(control_type='Button')
    selected=False
    for button in buttons:
        if button.window_text().lower() in ('select folder','select','open'):
            button.click_input(); selected=True; break
    if not selected: pyautogui.hotkey('alt','s')
    print('SELECTED')
time.sleep(3)
