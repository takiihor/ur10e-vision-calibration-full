"""Show the read-only IPC display on the dedicated HDMI touchscreen."""
from contextlib import closing
from http.client import HTTPConnection, HTTPException
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event
from time import monotonic


URL = 'http://127.0.0.1:18874/'
POLL_S = 2
RETRY_S = 5


def screen_on(root=Path('/sys/class/drm')):
    connected = []
    try:
        for connector in root.glob('card*-*'):
            name = connector.name.split('-', 1)[1]
            if name.startswith(('Virtual-', 'Writeback-')):
                continue
            status = connector / 'status'
            if status.is_file() and status.read_text().strip() == 'connected':
                connected.append(connector)
        return (len(connected) == 1 and connected[0].name.endswith('-HDMI-A-1')
                and (connected[0] / 'enabled').read_text().strip() == 'enabled')
    except OSError:
        return False


def stop_viewer(viewer):
    if viewer is None or viewer.poll() is not None:
        return
    viewer.terminate()
    try:
        viewer.wait(timeout=3)
    except subprocess.TimeoutExpired:
        viewer.kill()
        viewer.wait()


def watch(closing, ready, start, *, clock=monotonic):
    viewer, retry_at = None, 0
    try:
        while not closing.is_set():
            if not ready():
                stop_viewer(viewer)
                viewer, retry_at = None, 0
            else:
                if viewer is not None and viewer.poll() is not None:
                    viewer, retry_at = None, clock() + RETRY_S
                if viewer is None and clock() >= retry_at:
                    try:
                        viewer = start()
                    except (OSError, HTTPException):
                        retry_at = clock() + RETRY_S
            closing.wait(POLL_S)
    finally:
        stop_viewer(viewer)


def start_viewer():
    with closing(HTTPConnection('127.0.0.1', 18874, timeout=1)) as connection:
        connection.request('GET', '/')
        if connection.getresponse().status != 200:
            raise OSError('The status display is unavailable.')
    # Native Wayland clients cannot set the X11 no-focus and keep-below hints.
    env = dict(os.environ, GDK_BACKEND='x11')
    return subprocess.Popen([sys.executable, '-I', __file__, '--view'], env=env)


def view():
    import gi

    gi.require_version('Gtk', '3.0')
    gi.require_version('Gdk', '3.0')
    gi.require_version('WebKit2', '4.1')
    from gi.repository import Gdk, GLib, Gtk, WebKit2

    display = Gdk.Display.get_default()
    if display is None or display.get_n_monitors() != 1:
        return 1
    window = Gtk.Window(title='Anodizing IPC status')
    window.set_decorated(False)
    window.set_accept_focus(False)
    window.set_focus_on_map(False)
    window.set_skip_taskbar_hint(True)
    window.set_skip_pager_hint(True)
    window.set_keep_below(True)
    window.stick()
    window.fullscreen()

    context = WebKit2.WebContext.new_ephemeral()
    context.set_network_proxy_settings(WebKit2.NetworkProxyMode.NO_PROXY, None)
    web = WebKit2.WebView.new_with_context(context)
    web.connect('context-menu', lambda *_: True)
    web.connect('web-process-terminated', lambda *_: Gtk.main_quit())

    def navigation(_web, decision, kind):
        if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION or (
            kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION
            and decision.get_navigation_action().get_request().get_uri() != URL
        ):
            decision.ignore()
            return True
        return False

    web.connect('decide-policy', navigation)
    web.connect('load-failed', lambda *_: Gtk.main_quit() or True)
    window.connect('destroy', lambda *_: Gtk.main_quit())
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: Gtk.main_quit() or False)
    window.add(web)
    window.realize()
    window.get_window().set_pass_through(True)
    window.show_all()
    web.load_uri(URL)
    Gtk.main()
    return 0


if __name__ == '__main__':
    if sys.argv[1:] == ['--view']:
        raise SystemExit(view())
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    watch(stop, lambda: bool(os.environ.get('DISPLAY')) and screen_on(), start_viewer)
