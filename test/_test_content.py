"""端到端测试：检查恢复后文件内容"""
import os, time, shutil, urllib.request, json

SHARE = r"E:\tmp\share"
BAK = r"E:\tmp\share_bak"
RYC = r"E:\tmp\share_ryc"
TEST_FILE = os.path.join(SHARE, "e2e_content_test.txt")
CONTENT = "HELLO WORLD - this file has real content 12345"

def api(path):
    return json.loads(urllib.request.urlopen(f'http://127.0.0.1:8088{path}').read())

def api_post(path, data):
    req = urllib.request.Request(
        f'http://127.0.0.1:8088{path}',
        data=json.dumps(data).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    return json.loads(urllib.request.urlopen(req).read())

# 清理之前的测试文件
for p in [TEST_FILE]:
    if os.path.exists(p):
        os.remove(p)

# 1. 创建文件并写入内容
print(f"[1] 创建文件: {TEST_FILE}")
with open(TEST_FILE, 'w') as f:
    f.write(CONTENT)
print(f"    原始内容: {repr(CONTENT)}, 长度: {len(CONTENT)}")

# 2. 等待备份
print("[2] 等待备份 (10秒)...")
time.sleep(10)

bak_file = os.path.join(BAK, "e2e_content_test.txt")
if os.path.exists(bak_file):
    bak_size = os.path.getsize(bak_file)
    bak_content = open(bak_file, 'r').read()
    print(f"    备份文件: {bak_file}")
    print(f"    备份大小: {bak_size}, 内容: {repr(bak_content)}")
else:
    print(f"    备份文件不存在: {bak_file}")

# 3. 删除文件
print("[3] 删除文件...")
os.remove(TEST_FILE)
print("    等待回收站处理 (5秒)...")
time.sleep(5)

# 4. 检查回收站
print("[4] 检查回收站...")
stats = api('/api/stats')
print(f"    回收站文件数: {stats['recycled_count']}")

ryc_files = []
for root, dirs, files in os.walk(RYC):
    for fn in files:
        if not fn.endswith('.meta') and not fn.endswith('.recycle.json'):
            fp = os.path.join(root, fn)
            sz = os.path.getsize(fp)
            content = open(fp, 'rb').read()
            print(f"    回收站文件: {fp}")
            print(f"    大小: {sz}, 内容: {repr(content)}")
            if 'e2e_content_test' in fn:
                ryc_files.append((fp, sz, content))

# 5. 恢复
if ryc_files:
    ryc_path = ryc_files[0][0].replace(os.sep, '/')
    print(f"[5] 恢复文件: {ryc_path}")
    result = api_post('/api/restore', {'path': ryc_path})
    print(f"    恢复结果: {result}")
    
    time.sleep(2)
    
    # 6. 检查恢复后的文件
    if os.path.exists(TEST_FILE):
        restored_size = os.path.getsize(TEST_FILE)
        restored_content = open(TEST_FILE, 'r').read()
        print(f"[6] 恢复后文件:")
        print(f"    大小: {restored_size}")
        print(f"    内容: {repr(restored_content)}")
        if restored_content == CONTENT:
            print("    >>> 内容一致，恢复成功！")
        elif restored_size == 0:
            print("    >>> 文件为空！恢复失败！")
        else:
            print(f"    >>> 内容不一致！期望: {repr(CONTENT)}")
    else:
        print("[6] 恢复后文件不存在！")
else:
    print("[5] 回收站中没有找到测试文件")

# 清理
if os.path.exists(TEST_FILE):
    os.remove(TEST_FILE)
