"""测试页面恢复按钮是否可点击"""
import urllib.request
import json
import time
import os

BASE = "http://127.0.0.1:8088"

def get(url):
    r = urllib.request.urlopen(url)
    return r.read().decode()

def post(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={'Content-Type': 'application/json'})
    r = urllib.request.urlopen(req)
    return json.loads(r.read().decode())

# 1. Check stats
print("=== 1. Current stats ===")
stats = json.loads(get(f"{BASE}/api/stats"))
print(f"Recycled count: {stats['recycled_count']}")

# 2. Get page HTML and check for buttons
print("\n=== 2. Check page HTML for restore buttons ===")
html = get(f"{BASE}/")
# Find all btn-restore lines (excluding CSS)
for line in html.split('\n'):
    stripped = line.strip()
    if 'btn-restore' in stripped and 'onclick' in stripped:
        print(f"BUTTON: {stripped}")
        if 'disabled' in stripped:
            print("  >>> BUTTON IS DISABLED!")
        else:
            print("  >>> Button is ENABLED (clickable)")
    elif 'btn-restore' in stripped and 'background' in stripped:
        pass  # CSS, skip

# 3. Create a test file
test_file = r"E:\tmp\share\_btn_click_test.txt"
print(f"\n=== 3. Creating test file: {test_file} ===")
with open(test_file, 'w') as f:
    f.write("button click test")
print("File created")

# 4. Wait for sync backup
print("\n=== 4. Waiting 35 seconds for backup sync... ===")
time.sleep(35)

# Check backup exists
backup_path = r"E:\tmp\share_bak\_btn_click_test.txt"
if os.path.exists(backup_path):
    print(f"Backup exists: {backup_path}")
else:
    print(f"WARNING: Backup NOT found at {backup_path}")

# 5. Delete the file
print(f"\n=== 5. Deleting test file ===")
os.remove(test_file)
print("File deleted, waiting 5 seconds for recycle...")
time.sleep(5)

# 6. Check stats again
print("\n=== 6. Stats after delete ===")
stats = json.loads(get(f"{BASE}/api/stats"))
print(f"Recycled count: {stats['recycled_count']}")

# 7. Check page HTML for buttons
print("\n=== 7. Check page HTML for restore buttons ===")
html = get(f"{BASE}/")
found_button = False
for line in html.split('\n'):
    stripped = line.strip()
    if 'btn-restore' in stripped and 'onclick' in stripped:
        found_button = True
        print(f"BUTTON: {stripped}")
        if 'disabled' in stripped:
            print("  >>> BUTTON IS DISABLED! (this is the problem)")
        else:
            print("  >>> Button is ENABLED (clickable)")
    elif 'btn-restore' in stripped and 'background' not in stripped and 'padding' not in stripped:
        if 'onclick' not in stripped:
            print(f"OTHER btn-restore line: {stripped}")

if not found_button:
    print("NO RESTORE BUTTONS FOUND ON PAGE!")

# 8. Try API restore
print("\n=== 8. Try API restore ===")
# Find the recycle path from HTML
for line in html.split('\n'):
    if 'restoreFile(' in line:
        # Extract path from onclick="restoreFile('PATH', this)"
        start = line.index("restoreFile('") + len("restoreFile('")
        end = line.index("'", start)
        recycle_path = line[start:end]
        print(f"Restore path: {recycle_path}")
        result = post(f"{BASE}/api/restore", {"path": recycle_path})
        print(f"API result: {result}")
        break

# Cleanup
if os.path.exists(test_file):
    os.remove(test_file)
    
print("\n=== DONE ===")
