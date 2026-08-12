"""naiba-chat 桌面启动器。

内嵌窗口（pywebview）打开聊天界面，后台运行 HTTP 服务，并提供系统托盘图标。
- 关闭窗口：仅隐藏到托盘，服务继续运行（手机仍可访问）。
- 托盘"退出"：停止服务并退出整个程序。
"""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

import server as srv


class Launcher:
    def __init__(self) -> None:
        self.httpd: ThreadingHTTPServer | None = None
        self.window = None
        self.tray = None
        self.should_quit = False

    # ---- HTTP 服务（后台线程） ----
    def _run_server(self, host: str, port: int) -> None:
        self.httpd = ThreadingHTTPServer((host, port), srv.RequestHandler)
        self.httpd.daemon_threads = True
        srv.write_status(host, port, str(srv.APP.config.data["access_token"]))
        try:
            self.httpd.serve_forever(poll_interval=0.3)
        except Exception:
            pass

    def _stop_server(self) -> None:
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass

    # ---- 托盘动作 ----
    def _quit(self, icon=None, item=None) -> None:
        self.should_quit = True
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        # 真正销毁窗口，让 webview.start() 返回
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

    def _show_window(self) -> None:
        if self.window:
            try:
                self.window.show()
                self.window.restore()
            except Exception:
                pass

    def _open_browser(self, url: str):
        def _open():
            webbrowser.open(url)
        return _open

    def _build_tray(self, local_url: str):
        import pystray
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (64, 64), (18, 100, 64))
        draw = ImageDraw.Draw(image)
        draw.ellipse((14, 14, 50, 50), fill=(255, 255, 255))
        draw.ellipse((22, 22, 42, 42), fill=(18, 100, 64))

        menu = pystray.Menu(
            pystray.MenuItem("打开窗口", lambda: self._show_window(), default=True),
            pystray.MenuItem("在浏览器打开", self._open_browser(local_url)),
            pystray.MenuItem("退出", self._quit),
        )
        return pystray.Icon("naiba-chat", image, "naiba-chat", menu)

    def _on_window_closing(self) -> bool:
        # 用户点关闭：若是要退出（托盘点了退出），放行；否则隐藏到托盘
        if self.should_quit:
            return True
        if self.window:
            try:
                self.window.hide()
            except Exception:
                pass
        return False  # 阻止真正关闭

    def run(self) -> None:
        import webview

        instance_lock = srv.acquire_instance_lock()
        srv.APP = srv.NaibaChatApp()
        srv.APP.update_restart_callback = self._quit
        host = str(srv.APP.config.data.get("host", "0.0.0.0"))
        port = int(srv.APP.config.data.get("port", 8765))
        token = str(srv.APP.config.data["access_token"])
        local_url = f"http://127.0.0.1:{port}"
        page_url = f"{local_url}/?token={token}"

        server_thread = threading.Thread(target=self._run_server, args=(host, port), daemon=True)
        server_thread.start()
        for _ in range(50):
            try:
                with __import__("urllib.request").request.urlopen(f"{local_url}/api/health", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.1)

        self.tray = self._build_tray(local_url)
        threading.Thread(target=self.tray.run, daemon=True).start()

        self.window = webview.create_window(
            "naiba-chat",
            page_url,
            width=1280,
            height=860,
            min_size=(900, 600),
            text_select=True,
        )
        self.window.events.closing += self._on_window_closing
        srv.APP.updater.start_auto_update(self._quit)
        try:
            webview.start()
        finally:
            self._stop_server()
            srv.APP.stop()
            try:
                srv.STATUS_PATH.unlink(missing_ok=True)
            except OSError:
                pass
            instance_lock.close()


def main() -> None:
    Launcher().run()


if __name__ == "__main__":
    main()
