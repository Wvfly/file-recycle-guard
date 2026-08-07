import urllib.request, json
html = urllib.request.urlopen('http://127.0.0.1:8088/').read().decode()
stats = json.loads(urllib.request.urlopen('http://127.0.0.1:8088/api/stats').read().decode())
print(f"Recycled count: {stats['recycled_count']}")
print("---")
for line in html.split('\n'):
    s = line.strip()
    if 'restoreFile(' in s:
        print(f"BTN: {s}")
        if 'disabled' in s:
            print("  >>> DISABLED")
        else:
            print("  >>> ENABLED")
