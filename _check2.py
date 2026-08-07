import urllib.request
html = urllib.request.urlopen('http://127.0.0.1:8088/').read().decode()
for i, line in enumerate(html.split('\n')):
    if 'restoreFile(' in line and 'function' not in line:
        print(f"Line {i}: {repr(line.strip())}")
        # Check for HTML entities
        if '&#39;' in line or '&amp;' in line or '&quot;' in line:
            print("  >>> FOUND HTML ENTITIES - this breaks JavaScript!")
        else:
            print("  >>> No HTML entities found")
