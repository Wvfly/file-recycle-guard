"""自动修复 .bat 文件的 CRLF 换行符（LF → CRLF），修复后重启自身"""
import sys, os
path = os.path.abspath(sys.argv[1])
with open(path, 'rb') as f:
    data = f.read()
normalized = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
if normalized != data:
    with open(path, 'wb') as f:
        f.write(normalized)
    print(f"[自动修复] {os.path.basename(path)}: LF -> CRLF")
    os.execv(path, [path])
