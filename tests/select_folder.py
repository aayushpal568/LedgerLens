import sys
import time
from pywinauto import Desktop
from pywinauto.keyboard import send_keys

folder = sys.argv[1]
deadline = time.time() + 15
window = None
while time.time() < deadline:
    candidates = Desktop(backend="uia").windows()
    for candidate in candidates:
        title = candidate.window_text().lower()
        if any(token in title for token in ("select folder", "choose folder", "browse for folder")):
            window = candidate
            break
    if window:
        break
    time.sleep(0.25)

if window is None:
    visible = [(w.window_text(), w.element_info.class_name) for w in Desktop(backend="uia").windows()]
    raise RuntimeError(f"Folder picker dialog was not found; windows={visible}")

window.set_focus()
send_keys("%d")
time.sleep(0.3)
send_keys(folder, with_spaces=True)
send_keys("{ENTER}")
time.sleep(0.8)

buttons = window.descendants(control_type="Button")
for button in buttons:
    label = button.window_text().lower()
    if "select folder" in label or label in {"select", "open"}:
        button.click_input()
        break
else:
    send_keys("%s")
