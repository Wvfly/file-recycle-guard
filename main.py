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

from core.config import load_config, Config
from core.logger import setup_logger
from core.database import init_database
from core.watcher import start_watcher
from core.cleanup import start_cleanup
from core.sync import start_sync
from web import start_web


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

            # 启动文件监控
            self.observer, self.handler = start_watcher(self.config, self.logger)

            # 启动定期清理
            self.cleanup_thread, self.cleanup_stop = start_cleanup(
                self.config, self.logger
            )

            # 启动定期同步（解决 SMB 网络共享变更检测问题）
            self.sync_thread, self.sync_stop = start_sync(
                self.config, self.logger
            )

            # 启动 Web 界面
            self.web_thread = start_web(self.config, self.logger)

            self.running = True
            self.logger.info("所有服务已启动，守护程序运行中...")

    def stop(self):
        """停止所有服务"""
        with self.lock:
            if not self.running:
                return

            self.logger.info("正在停止守护程序...")

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

    # 切换到脚本所在目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
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
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"已发送停止信号到进程 {pid}")
                os.remove(pid_file)
            except OSError:
                print(f"进程 {pid} 已不存在")
                os.remove(pid_file)
        else:
            print("未找到运行中的守护程序（PID 文件不存在）")

    elif args.action == "status":
        if os.path.exists(pid_file):
            with open(pid_file, "r") as f:
                pid = int(f.read().strip())
            try:
                os.kill(pid, 0)
                print(f"守护程序运行中 (PID: {pid})")
            except OSError:
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
