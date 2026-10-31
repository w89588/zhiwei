"""Native window/tray adapter. Collection and UI state stay independent of WinForms."""
import logging
import webbrowser
from pathlib import Path

from .autostart import set_autostart, startup_status
from .collector import collector
from .remote_url import validate_server_url

WINDOW_WIDTH, WINDOW_HEIGHT = 840, 640
ASSETS = Path(__file__).parent / 'collector_static'
ICON_PATH = ASSETS / 'app.ico'


class DesktopAPI:
    def __init__(self):
        self._window = None
        self._tray = None

    def copy_text(self, text):
        import clr
        clr.AddReference('System.Windows.Forms')
        from System.Windows.Forms import Clipboard
        from System.Threading import Thread, ThreadStart, ApartmentState
        errors = []

        def copy():
            try:
                Clipboard.SetText(text)
            except Exception as error:
                errors.append(str(error))

        thread = Thread(ThreadStart(copy))
        thread.SetApartmentState(ApartmentState.STA)
        thread.Start()
        thread.Join()
        if errors:
            raise RuntimeError(errors[0])
        return True

    def open_server(self):
        url = collector.config().get('url')
        if not url:
            raise ValueError('请先连接服务端')
        return webbrowser.open(validate_server_url(url), new=2)

    def preferences(self):
        return {'startup': startup_status(), 'tray_ready': bool(self._tray and self._tray.icon)}

    def update_autostart(self, enabled):
        set_autostart(enabled)
        return self.preferences()

    def minimize(self):
        self._window.minimize()

    def hide_window(self):
        # Trigger the same close policy as Alt+F4. Never strand a window without a tray.
        self._window.destroy()

    def quit_app(self):
        self._tray.exit()


class Tray:
    def __init__(self, window, start_hidden=False):
        self.window = window
        self.icon = None
        self.exiting = False
        self.start_hidden = start_hidden
        self.menu = None
        self._image = None

    def install(self):
        from System import Action

        def setup():
            try:
                from System.Drawing import Icon
                from System.Windows.Forms import ContextMenuStrip, NotifyIcon
                self._image = Icon(str(ICON_PATH))
                icon = NotifyIcon()
                icon.Icon = self._image
                icon.Text = '微信解析客户端 · 后台运行'
                self.menu = ContextMenuStrip()
                self.menu.Items.Add('打开客户端').Click += self.show
                self.menu.Items.Add('打开服务端').Click += self.open_server
                self.menu.Items.Add('-')
                self.menu.Items.Add('退出客户端').Click += self.exit
                icon.ContextMenuStrip = self.menu
                icon.DoubleClick += self.show
                icon.Visible = True
                self.icon = icon
                # Only hide once the user has a working route back to the window.
                if self.start_hidden:
                    self.window.native.Hide()
            except Exception:
                logging.exception('Unable to initialize tray')
                self.window.native.Show()

        self.window.native.BeginInvoke(Action(setup))

    def show(self, *args):
        self.window.show()
        self.window.restore()

    def open_server(self, *args):
        try:
            if not DesktopAPI().open_server():
                self.show()
        except Exception:
            self.show()

    def exit(self, *args):
        self.exiting = True
        # NotifyIcon and its menu belong to the UI thread.
        if hasattr(self.window.native, 'Invoke'):
            from System import Action
            self.window.native.Invoke(Action(self.dispose))
        else:
            self.dispose()
        self.window.destroy()

    def dispose(self):
        if self.icon:
            self.icon.Visible = False
            self.icon.Dispose()
        if self.menu:
            self.menu.Dispose()
        if self._image:
            self._image.Dispose()

    def closing(self):
        if self.icon and not self.exiting:
            self.window.native.Hide()
            return False


def lock_window(window):
    """Keep fixed native bounds while allowing the designated titlebar to move."""
    import ctypes
    from System import Action

    def apply():
        native = window.native
        native.MinimumSize = native.Size
        native.MaximumSize = native.Size
        native.MaximizeBox = False
        # Keep Move available; only Size and Maximize conflict with fixed bounds.
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetSystemMenu.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        user32.GetSystemMenu.restype = ctypes.c_void_p
        user32.DeleteMenu.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        menu = user32.GetSystemMenu(native.Handle.ToInt64(), False)
        for command in (0xF000, 0xF030):
            user32.DeleteMenu(menu, command, 0)

    window.native.Invoke(Action(apply))
