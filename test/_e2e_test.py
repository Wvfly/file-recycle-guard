"""端到端测试：创建文件->等备份->删除->恢复->验证内容"""
import os, time, urllib.request, json

SHARE = r"E:\tmp\share"
BAK = r"E:\tmp\share_bak"
RYC = r"E:\tmp\share_ryc"
TEST_FILE = os.path.join(SHARE, "restore_verify.txt")
CONTENT = "THIS IS REAL CONTENT 1234567890 ABCDEFGHIJKLMNOP"

def api_get(path):
    return json.loads(urllib.request.urlopen(f'http://127.0.0.1:8088{path}').read())

def api_post(path, data):
    req = urllib.request.Request(
        f'http://127.0.0.1:8088{path}',
        data=json.dumps(data).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    return json.loads(urllib.request.urlopen(req).read())

# 清理
if os.path.exists(TEST_FILE):
    os.remove(TEST_FILE)
    
bak_file = os.path.join(BAK, "restore_verify.txt")
if os.path.exists(bak_file):
    os.remove(bak_file)

# 1. 创建文件
print("=" * 50)
print("[1] 创建文件并写入内容")
with open(TEST_FILE, 'w') as f:
    f.write(CONTENT)
print(f"    文件: {TEST_FILE}")
print(f"    内容: {repr(CONTENT)}, 长度: {len(CONTENT)}")

# 2. 等待备份（sync每30秒一次 + watchdog事件）
print("[2] 等待备份 (35秒)...")
time.sleep(35)

if os.path.exists(bak_file):
    bak_content = open(bak_file, 'r').read()
    print(f"    备份存在: {bak_file}")
    print(f"    备份内容: {repr(bak_content)}, 长度: {len(bak_content)}")
else:
    print(f"    备份不存在: {bak_file}")

# 3. 删除文件
print("[3] 删除文件...")
os.remove(TEST_FILE)
print("    等待回收站处理 (8秒)...")
time.sleep(8)

# 4. 检查回收站
print("[4] 检查回收站...")
stats = api_get('/api/stats')
print(f"    回收站文件数: {stats['recycled_count']}")

ryc_path = None
for root, dirs, files in os.walk(RYC):
    for fn in files:
        if 'restore_verify' in fn:
            fp = os.path.join(root, fn)
            sz = os.path.getsize(fp)
            content = open(fp, 'rb').read()
            print(f"    回收站文件: {fp}")
            print(f"    大小: {sz}, 内容: {repr(content)}")
            ryc_path = fp

if not ryc_path:
    print("    !!! 回收站中没有找到测试文件")
    # 查看日志
    print("\n[LOG] 最新日志:")
    with open(r"E:\PycharmProjects\file-recycle-guard\logs\recycle_guard.log", 'r', encoding='utf-8') as lf:
        lines = lf.readlines()
        for line in lines[-20:]:
            print(f"    {line.rstrip()}")
    exit(1)

# 5. 恢复
ryc_rel = ryc_path.replace(os.sep, '/')
print(f"[5] 恢复文件: {ryc_rel}")
result = api_post('/api/restore', {'path': ryc_rel})
print(f"    恢复结果: {result}")
time.sleep(2)

# 6. 验证恢复后内容
if os.path.exists(TEST_FILE):
    restored = open(TEST_FILE, 'r').read()
    print(f"[6] 恢复后文件:")
    print(f"    大小: {os.path.getsize(TEST_FILE)}")
    print(f"    内容: {repr(restored)}")
    if restored == CONTENT:
        print("    >>> 内容一致，恢复成功！")
    elif len(restored) == 0:
        print("    >>> 文件为空！恢复失败！")
    else:
        print(f"    >>> 内容不一致！期望: {repr(CONTENT)}")
else:
    print("[6] 恢复后文件不存在！")

# 清理
if os.path.exists(TEST_FILE):
    os.remove(TEST_FILE)
