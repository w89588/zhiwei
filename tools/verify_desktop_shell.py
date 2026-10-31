"""Exercise a real, isolated WinForms/WebView2 shell without user credentials."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webview
from helper.desktop import DesktopAPI, ICON_PATH, Tray, WINDOW_HEIGHT, WINDOW_WIDTH, lock_window


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--startup', action='store_true')
    parser.add_argument('--report', required=True)
    parser.add_argument('--url', help='Optional local collector UI for bridge verification')
    args = parser.parse_args()
    bridge = DesktopAPI()
    window = webview.create_window(
        '微信解析客户端 · 窗口验证', url=args.url,
        html=None if args.url else '<html lang="zh-CN"><body>隔离窗口验证</body></html>',
        width=WINDOW_WIDTH, height=WINDOW_HEIGHT, min_size=(WINDOW_WIDTH, WINDOW_HEIGHT),
        resizable=False, frameless=True, easy_drag=False, draggable=False,
        minimized=args.startup, js_api=bridge,
    )
    tray = Tray(window, start_hidden=args.startup)
    bridge._window, bridge._tray = window, tray
    window.events.shown += lock_window
    window.events.shown += tray.install
    window.events.closing += tray.closing
    report = {}

    def snapshot():
        from System import Action
        result = {}
        def read():
            native = window.native
            result.update(x=native.Location.X, y=native.Location.Y,
                          width=native.Size.Width, height=native.Size.Height,
                          minimum=[native.MinimumSize.Width, native.MinimumSize.Height],
                          maximum=[native.MaximumSize.Width, native.MaximumSize.Height],
                          visible=native.Visible, maximize=native.MaximizeBox,
                          border=str(native.FormBorderStyle), scale=native._scale,
                          window_state=str(native.WindowState),
                          tray=bool(tray.icon and tray.icon.Visible),
                          menu=tray.menu.Items.Count if tray.menu else 0)
        window.native.Invoke(Action(read))
        return result

    def drag(selector, moves):
        """Send DOM pointer movement through pywebview's real native move bridge."""
        before = snapshot()
        origin = [before['x'] / before['scale'], before['y'] / before['scale']]
        window.evaluate_js('''(() => {
            const element = document.querySelector(%s);
            const rect = element.getBoundingClientRect();
            const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
            const origin = %s;
            element.dispatchEvent(new MouseEvent('mousedown', {
                bubbles: true, button: 0, buttons: 1, clientX: x, clientY: y,
                screenX: origin[0] + x, screenY: origin[1] + y
            }));
            window.dispatchEvent(new MouseEvent('mousemove', {
                buttons: 1, clientX: x + 40, clientY: y + 20,
                screenX: origin[0] + x + 40, screenY: origin[1] + y + 20
            }));
            window.dispatchEvent(new MouseEvent('mouseup', {button: 0}));
        })()''' % (json.dumps(selector), json.dumps(origin)))
        time.sleep(.3)
        after = snapshot()
        if moves:
            assert abs(after['x'] - before['x'] - 40 * before['scale']) <= 1, (selector, before, after)
            assert abs(after['y'] - before['y'] - 20 * before['scale']) <= 1, (selector, before, after)
        else:
            assert [after['x'], after['y']] == [before['x'], before['y']], (selector, before, after)
        assert [after['width'], after['height']] == [before['width'], before['height']]
        return {'selector': selector, 'moves': moves, 'before': [before['x'], before['y']],
                'after': [after['x'], after['y']]}

    def verify():
        try:
            assert window.events.loaded.wait(20), 'WebView did not load'
            for _ in range(50):
                if tray.icon: break
                time.sleep(.1)
            before = snapshot()
            assert before['tray'] and before['menu'] == 4, before
            assert before['visible'] is not args.startup, before
            tray.show()
            time.sleep(.3)
            if args.url:
                for _ in range(50):
                    if window.evaluate_js('!document.getElementById("window-controls").hidden && !document.getElementById("autostart").disabled'):
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError('The real desktop bridge did not enable native controls')
                report['real_ui_bridge'] = True
            shown = snapshot()
            scale = shown['scale']
            assert shown['visible'] and shown['border'] == 'None' and not shown['maximize'], shown
            assert shown['minimum'] == shown['maximum'] == [round(WINDOW_WIDTH * scale), round(WINDOW_HEIGHT * scale)], shown
            if args.url:
                report['drag_regions'] = [drag(selector, True) for selector in
                    ('.titlebar-drag-region', '.brand strong', '.brand img', '.version')]
                report['non_drag_regions'] = [drag(selector, False) for selector in
                    ('#minimize-window', '#hide-window', '#page-title', '#url')]
                window.evaluate_js('document.getElementById("minimize-window").click()')
                time.sleep(.3)
                assert snapshot()['window_state'] == 'Minimized'
                tray.show()
                time.sleep(.2)
                window.evaluate_js('document.querySelector(".titlebar-drag-region").dispatchEvent(new MouseEvent("dblclick", {bubbles: true}))')
                assert snapshot()['window_state'] == 'Normal'
                report['titlebar_buttons_and_double_click'] = True
            window.resize(1200, 900)
            time.sleep(.2)
            resized = snapshot()
            assert [resized['width'], resized['height']] == shown['maximum'], resized
            if args.url:
                window.evaluate_js('document.getElementById("hide-window").click()')
            else:
                bridge.hide_window()
            time.sleep(.3)
            hidden = snapshot()
            assert not hidden['visible'] and hidden['tray'], hidden
            tray.show()
            time.sleep(.2)
            assert snapshot()['visible']
            report.update(ok=True, startup=args.startup, initial=before, restored=shown, resize_attempt=resized, closed=hidden)
        except Exception as error:
            report.update(ok=False, error=str(error), state=snapshot())
        finally:
            Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            tray.exit()

    webview.start(verify, gui='edgechromium', icon=str(ICON_PATH))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
