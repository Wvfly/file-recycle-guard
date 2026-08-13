#!/usr/bin/env python
"""
自动化测试运行器
运行所有测试模块，生成测试报告

用法:
    python test/run_all_tests.py              # 运行所有测试
    python test/run_all_tests.py --quick      # 快速模式（仅单元测试，不依赖外部服务）
    python test/run_all_tests.py --module config  # 仅运行指定模块测试
"""
import os
import sys
import time
import unittest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)


def discover_and_run(module_filter=None, quick_mode=False):
    """发现并运行所有测试"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_dir = os.path.join(PROJECT_ROOT, "test")

    if module_filter:
        # 运行指定模块
        test_file = os.path.join(test_dir, f"test_{module_filter}.py")
        if os.path.exists(test_file):
            tests = loader.discover(test_dir, pattern=f"test_{module_filter}.py")
            suite.addTests(tests)
        else:
            print(f"[ERROR] 测试模块不存在: test_{module_filter}.py")
            return False
    else:
        # 运行所有测试模块
        for f in sorted(os.listdir(test_dir)):
            if f.startswith("test_") and f.endswith(".py"):
                if quick_mode and f in ("test_web.py", "test_database.py"):
                    continue  # 快速模式跳过依赖外部服务的测试
                module_pattern = f
                tests = loader.discover(test_dir, pattern=module_pattern)
                suite.addTests(tests)

    # 运行测试
    print("=" * 70)
    print("  文件回收站守护程序 - 自动化测试")
    print("=" * 70)
    print(f"  项目路径: {PROJECT_ROOT}")
    print(f"  测试模式: {'快速模式' if quick_mode else '完整模式'}")
    print(f"  过滤条件: {module_filter or '全部'}")
    print("=" * 70)
    print()

    runner = unittest.TextTestRunner(verbosity=2)
    start_time = time.time()
    result = runner.run(suite)
    elapsed = time.time() - start_time

    # 打印汇总
    print()
    print("=" * 70)
    print("  测试汇总")
    print("=" * 70)
    print(f"  运行测试数: {result.testsRun}")
    print(f"  成功:       {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"  失败:       {len(result.failures)}")
    print(f"  错误:       {len(result.errors)}")
    print(f"  耗时:       {elapsed:.2f}s")
    print("=" * 70)

    # 打印失败详情
    if result.failures:
        print()
        print("--- 失败详情 ---")
        for test, traceback in result.failures:
            print(f"\n[FAIL] {test}")
            print(traceback[-500:])

    if result.errors:
        print()
        print("--- 错误详情 ---")
        for test, traceback in result.errors:
            print(f"\n[ERROR] {test}")
            print(traceback[-500:])

    return result.wasSuccessful()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="自动化测试运行器")
    parser.add_argument("--quick", action="store_true", help="快速模式（仅单元测试）")
    parser.add_argument("--module", type=str, default=None,
                        help="仅运行指定模块（如 config, backup, recycler, watcher, web, database, integration）")
    args = parser.parse_args()

    success = discover_and_run(module_filter=args.module, quick_mode=args.quick)
    sys.exit(0 if success else 1)
