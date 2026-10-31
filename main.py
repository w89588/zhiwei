"""Launch the local collector and its compact Windows shell."""
import argparse
import socket
import threading
import time

import uvicorn

from helper.collector import collector
from helper.collector_app import app
from helper.desktop import DesktopAPI, ICON_PATH, Tray, WINDOW_HEIGHT, WINDOW_WIDTH, lock_window
from helper.single_instance import SingleInstance
from helper.storage import DATA


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--startup', action='store_true', help='Start quietly in the system tray')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    instance = SingleInstance(DATA)
    if not instance.primary:
        if not args.startup:
            instance.activate()
        instance.close()
        return
    stop = threading.Event()
    server = None
    listener = None
    try:
        if args.port:
            port = args.port
        else:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', log_config=None))
        threading.Thread(target=server.run, daemon=True).start()
        threading.Thread(target=collector.loop, args=(stop,), daemon=True).start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        if not server.started:
            raise RuntimeError('本地服务启动失败')
        url = f'http://127.0.0.1:{port}'
        if args.browser:
            print(url, flush=True)
            try:
                while not stop.wait(1):
                    pass
            except KeyboardInterrupt:
                pass
        else:
            import webview
            bridge = DesktopAPI()
            window = webview.create_window(
                '微信解析客户端', url, width=WINDOW_WIDTH, height=WINDOW_HEIGHT,
                min_size=(WINDOW_WIDTH, WINDOW_HEIGHT), resizable=False,
                frameless=True, easy_drag=False, draggable=False,
                text_select=True, minimized=args.startup, js_api=bridge,
                background_color='#f5f7f8',
            )
            tray = Tray(window, start_hidden=args.startup)
            bridge._window, bridge._tray = window, tray
            window.events.shown += lock_window
            window.events.shown += tray.install
            window.events.closing += tray.closing
            listener = threading.Thread(target=instance.listen, args=(stop, tray.show), daemon=True)
            listener.start()
            webview.start(gui='edgechromium', icon=str(ICON_PATH))
    finally:
        stop.set()
        if server:
            server.should_exit = True
        if listener:
            listener.join(2)
        instance.close()


def run():
    try:
        main()
    except Exception:
        import traceback
        (DATA / 'startup-error.log').write_text(traceback.format_exc(), encoding='utf-8')
        raise


if __name__ == '__main__':
    run()
