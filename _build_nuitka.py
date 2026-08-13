"""Nuitka onefile 构建脚本"""
import io
import subprocess
import sys
import os
import shutil

# GitHub Actions Windows runner 默认代码页不是 UTF-8，强制 stdout/stderr 使用 UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# 清理旧产物
for d in ["dist", "main.build", "main.dist"]:
    if os.path.exists(d):
        shutil.rmtree(d, ignore_errors=True)

cmd = [
    sys.executable, "-m", "nuitka",
    "--onefile",                       # 单个 exe 文件
    "--output-dir=dist",
    "--include-package=core",
    "--include-package=web",
    "--include-data-dir=web/templates=web/templates",  # 模板内置到 exe
    "--enable-plugin=anti-bloat",
    "--nofollow-import-to=service",
    "--windows-console-mode=attach",
    "--windows-company-name=FileRecycleGuard",
    "--windows-product-name=FileRecycleGuard",
    "--windows-file-version=1.0.0.0",
    "--windows-product-version=1.0.0.0",
    "--windows-file-description=File Recycle Guard Daemon",
    "--assume-yes-for-downloads",
    "main.py",
]

print("=" * 60)
print("  Nuitka Onefile Build - File Recycle Guard")
print("=" * 60)
print()
print("Command:", " ".join(cmd))
print()
print("注意: config.yaml 需放在 exe 同目录")
print()

result = subprocess.run(cmd)

if result.returncode == 0:
    # 复制 config.yaml 到 dist/ (exe 旁边)
    if os.path.exists("config.yaml"):
        shutil.copy2("config.yaml", os.path.join("dist", "config.yaml"))
        print("  Copied config.yaml -> dist/")
    print()
    print("=" * 60)
    print("  BUILD SUCCESS!")
    print("=" * 60)
    dist_dir = "dist"
    if os.path.exists(dist_dir):
        # 显示 exe 大小
        exe_path = os.path.join(dist_dir, "main.exe")
        if os.path.exists(exe_path):
            size_mb = os.path.getsize(exe_path) / (1024 * 1024)
            print(f"  main.exe: {size_mb:.1f} MB")
        print(f"  请将 config.yaml 放在 exe 同目录 (dist/)")
    print()
else:
    print()
    print("=" * 60)
    print("  BUILD FAILED!")
    print("=" * 60)
    sys.exit(1)
