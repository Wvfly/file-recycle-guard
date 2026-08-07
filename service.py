"""
Windows 服务包装器 - 将守护程序安装为 Windows 服务

使用方法:
    pip install pywin32
    python service.py install
    python service.py start
    python service.py stop
    python service.py remove
"""

import os
import sys
import time
import servicemanager
import win32serviceutil
import win32service
import win32event
import win32evtlogutil


# 将项目根目录加入路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from core.config import load_config
from core.logger import setup_logger
from core.watcher import start_watcher
from core.cleanup import start_cleanup
from core.sync import start_sync
from web import start_web


class RecycleGuardService(win32serviceutil.ServiceFramework):
    """Windows 服务类"""

    _svc_name_ = "FileRecycleGuard"
    _svc_display_name_ = "文件回收站守护程序"
    _svc_description_ = ("监控共享文件夹，当远程用户删除文件时，"
                         "自动将文件移入回收站，支持 Web 管理界面恢复。")
    _svc_deps_ = ["EventLog"]

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.observer = None
        self.cleanup_stop = None
        self.sync_stop = None
        self.config = None
        self.logger = None

    def SvcStop(self):
        """服务停止回调"""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.logger and self.logger.info("服务正在停止...")

        if self.observer:
            self.observer.stop()
        if self.cleanup_stop:
            self.cleanup_stop.set()
        if self.sync_stop:
            self.sync_stop.set()

        win32event.SetEvent(self.stop_event)
        self.ReportServiceStatus(win32service.SERVICE_STOPPED)

    def SvcDoRun(self):
        """服务主循环"""
        try:
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, "")
            )

            # 加载配置和日志（Windows 服务工作目录为 System32，必须用绝对路径）
            os.chdir(BASE_DIR)
            self.config = load_config(os.path.join(BASE_DIR, "config.yaml"))
            self.logger = setup_logger(self.config)
            self.logger.info("文件回收站守护程序 - Windows 服务模式启动")

            # 确保目录存在
            for d in [self.config.backup_dir, self.config.recycle_dir]:
                if not os.path.exists(d):
                    os.makedirs(d, exist_ok=True)

            # 启动文件监控
            self.observer, handler = start_watcher(self.config, self.logger)

            # 启动定期清理
            cleanup_thread, self.cleanup_stop = start_cleanup(
                self.config, self.logger
            )

            # 启动定期同步（解决 SMB 网络共享变更检测问题）
            sync_thread, self.sync_stop = start_sync(
                self.config, self.logger
            )

            # 启动 Web 界面
            start_web(self.config, self.logger)

            self.logger.info("所有服务已启动")

            # 等待停止信号
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)

        except Exception as e:
            self.logger and self.logger.error(f"服务异常: {e}")
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_ERROR_TYPE,
                servicemanager.PYS_SERVICE_STOPPED,
                (self._svc_name_, str(e))
            )


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(RecycleGuardService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(RecycleGuardService)
