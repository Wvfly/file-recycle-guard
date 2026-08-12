"""
文件回收站守护程序 - 主入口

用法:
    python main.py [start|stop|status]

    start    - 启动守护程序
    stop     - 停止守护程序
    status   - 查看运行状态
    web      - 仅启动 Web 管理界面（不监控）
"""

import os
import sys
import signal
import time
import argparse
import threading
import ctypes

from core.config import load_config, Config, get_exe_dir
from core.logger import setup_logger
from core.database import init_database
from core.watcher import start_watcher
from core.cleanup import start_cleanup
from core.sync import start_sync
from web import start_web

# USN 架构组件（延迟导入，避免非 Windows 平台报错）
_usn_available = False
try:
    from core.detector.usn_detector import UsnDetector
    from core.detector.reconciler import Reconciler
    from core.usn.event_store import UsnEventStore
    _usn_available = True
except ImportError:
    pass


class RecycleGuard:
    """守护程序主控制器"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.config: Config = None
        self.logger = None
        self.observer = None
        self.handler = None
        self.cleanup_thread = None
        self.cleanup_stop = None
        self.sync_thread = None
        self.sync_stop = None
        self.web_thread = None
        self.running = False
        self.lock = threading.Lock()
        # USN 组件
        self.usn_detector = None
        self.reconciler = None
        self.event_store = None

    def start(self):
        """启动所有服务"""
        with self.lock:
            if self.running:
                self.logger and self.logger.warning("服务已在运行中")
                return

            # 加载配置
            self.config = load_config(self.config_path)
            # 初始化日志
            self.logger = setup_logger(self.config)
            self.logger.info("=" * 50)
            self.logger.info("文件回收站守护程序 v1.0 启动")
            self.logger.info("=" * 50)
            self.logger.info(f"监控路径: {self.config.watch_paths}")
            self.logger.info(f"备份目录: {self.config.backup_dir}")
            self.logger.info(f"回收站目录: {self.config.recycle_dir}")
            self.logger.info(f"保留天数: {self.config.retention_days}")

            # 初始化数据库
            try:
                db_config = self.config.database
                init_database(
                    host=db_config.host,
                    port=db_config.port,
                    user=db_config.user,
                    password=db_config.password,
                    database=db_config.database,
                )
                self.logger.info(f"数据库已初始化: {db_config.host}:{db_config.port}/{db_config.database}")
            except Exception as e:
                self.logger.error(f"数据库初始化失败: {e}")
                self.logger.warning("将使用文件方式存储元信息（兼容模式）")

            # 确保目录存在
            for d in [self.config.backup_dir, self.config.recycle_dir]:
                if not os.path.exists(d):
                    os.makedirs(d, exist_ok=True)
                    self.logger.info(f"创建目录: {d}")

            # 初始化 USN 检测器（如果可用）
            usn_detector = self._init_usn_detector()

            # 启动文件监控（传入 usn_detector，USN 模式下作为加速器）
            self.observer, self.handler = start_watcher(
                self.config, self.logger, usn_detector=usn_detector
            )

            # 启动定期清理
            self.cleanup_thread, self.cleanup_stop = start_cleanup(
                self.config, self.logger
            )

            # 启动定期同步（解决 SMB 网络共享变更检测问题）
            self.sync_thread, self.sync_stop = start_sync(
                self.config, self.logger
            )

            # 启动 Web 界面（注入 USN 检测器供 /api/usn_health 使用）
            self.web_thread = start_web(
                self.config, self.logger, usn_detector=usn_detector
            )

            self.running = True
            if usn_detector:
                self.logger.info("运行模式: USN Journal (主通道) + watchdog (加速器)")
            else:
                self.logger.info("运行模式: watchdog (主通道)")
            self.logger.info("所有服务已启动，守护程序运行中...")

    def stop(self):
        """停止所有服务"""
        with self.lock:
            if not self.running:
                return

            self.logger.info("正在停止守护程序...")

            # 停止 USN 检测器
            if self.usn_detector:
                self.usn_detector.stop()
                self.logger.info("USN 检测器已停止")

            # 停止 Reconciler
            if self.reconciler:
                self.reconciler.stop()
                self.logger.info("一致性修复器已停止")

            # 停止 EventStore
            if self.event_store:
                self.event_store.close()

            # 停止文件监控
            if self.observer:
                self.observer.stop()
                self.observer.join(timeout=5)
                self.logger.info("文件监控已停止")

            # 停止清理线程
            if self.cleanup_stop:
                self.cleanup_stop.set()

            # 停止同步线程
            if self.sync_stop:
                self.sync_stop.set()

            self.running = False
            self.logger.info("守护程序已停止")

    def _init_usn_detector(self):
        """
        初始化 USN 检测器。

        Returns:
            UsnDetector 实例（如果 USN 可用），否则 None
        """
        if not _usn_available:
            self.logger.info("USN 模块不可用，使用 watchdog 模式")
            return None

        if os.name != 'nt':
            self.logger.info("非 Windows 系统，USN Journal 不可用")
            return None

        usn_cfg = getattr(self.config, 'usn', None)
        if usn_cfg and not usn_cfg.enabled:
            self.logger.info("USN Journal 已在配置中禁用")
            return None

        try:
            # 初始化 USN 事件存储（SQLite）
            state_dir = usn_cfg.state_dir if usn_cfg else ".usn_state"
            db_path = os.path.join(state_dir, "usn_state.db")
            self.event_store = UsnEventStore(db_path)

            # 创建 USN 检测器
            self.usn_detector = UsnDetector(
                config=self.config,
                logger=self.logger,
                event_store=self.event_store,
            )

            # 创建 Reconciler
            self.reconciler = Reconciler(
                config=self.config,
                logger=self.logger,
                event_store=self.event_store,
            )

            # 设置 reconciliation 回调
            self.usn_detector.set_on_reconcile(
                lambda vol, reason: self.reconciler.trigger_reconcile(vol, reason)
            )

            # 启动 Reconciler
            self.reconciler.start()

            # 启动 USN 检测器
            if self.usn_detector.start():
                self.logger.info("USN Journal 检测器已启动")
                return self.usn_detector
            else:
                self.logger.warning("USN Journal 初始化失败，回退到 watchdog 模式")
                return None

        except Exception as e:
            self.logger.error(f"USN 检测器初始化失败: {e}")
            return None

    def run_forever(self):
        """启动并持续运行，直到收到停止信号"""
        self.start()

        def signal_handler(sig, frame):
            self.logger.info(f"收到信号 {sig}，正在退出...")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()


def _process_exists(pid: int) -> bool:
    """检查进程是否存在（Windows 兼容）"""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def main():
    parser = argparse.ArgumentParser(
        description="文件回收站守护程序 - 保护共享文件不被远程删除"
    )
    parser.add_argument(
        "action", nargs="?", default="start",
        choices=["start", "stop", "status", "web"],
        help="操作: start(启动) / stop(停止) / status(状态) / web(仅Web界面)"
    )
    parser.add_argument(
        "-c", "--config", default="config.yaml",
        help="配置文件路径（默认 config.yaml）"
    )

    args = parser.parse_args()

    # 定位程序所在目录（支持 onefile 打包模式）
    # Nuitka onefile 下 sys.executable 指向临时目录，必须用 sys.argv[0]
    script_dir = get_exe_dir()
    os.chdir(script_dir)

    pid_file = "recycle_guard.pid"

    if args.action == "start":
        guard = RecycleGuard(args.config)
        # PID 文件只在 start 时写入，避免 stop/status 覆盖守护进程的 PID
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
        try:
            guard.run_forever()
        finally:
            if os.path.exists(pid_file):
                os.remove(pid_file)

    elif args.action == "stop":
        # 通过 PID 文件停止
        if os.path.exists(pid_file):
            with open(pid_file, "r") as f:
                pid = int(f.read().strip())
            if _process_exists(pid):
                # Windows 下使用 TerminateProcess 替代 os.kill(SIGTERM)
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE
                if handle:
                    kernel32.TerminateProcess(handle, 0)
                    kernel32.CloseHandle(handle)
                print(f"已发送停止信号到进程 {pid}")
            else:
                print(f"进程 {pid} 已不存在")
            os.remove(pid_file)
        else:
            print("未找到运行中的守护程序（PID 文件不存在）")

    elif args.action == "status":
        if os.path.exists(pid_file):
            with open(pid_file, "r") as f:
                pid = int(f.read().strip())
            if _process_exists(pid):
                print(f"守护程序运行中 (PID: {pid})")
            else:
                print("守护程序未运行（PID 文件残留）")
        else:
            print("守护程序未运行")

    elif args.action == "web":
        # 仅启动 Web 界面
        config = load_config(args.config)
        logger = setup_logger(config)
        logger.info("仅启动 Web 管理界面...")
        # 初始化数据库
        try:
            db_config = config.database
            init_database(
                host=db_config.host,
                port=db_config.port,
                user=db_config.user,
                password=db_config.password,
                database=db_config.database,
            )
            logger.info(f"数据库已初始化: {db_config.host}:{db_config.port}/{db_config.database}")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")
            logger.warning("将使用文件方式存储元信息（兼容模式）")
        thread = start_web(config, logger)
        if thread:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
