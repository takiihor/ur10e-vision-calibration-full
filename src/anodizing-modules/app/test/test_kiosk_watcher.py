import runpy
from pathlib import Path


watcher = runpy.run_path(str(Path(__file__).parents[2] / 'tools/anodizing-ipc-kiosk.py'))


def connector(root, name, status, enabled):
    directory = root / name
    directory.mkdir(exist_ok=True)
    (directory / 'status').write_text(status + '\n')
    (directory / 'enabled').write_text(enabled + '\n')


def test_only_the_active_single_physical_kiosk_connector_counts(tmp_path):
    active = watcher['screen_on']
    connector(tmp_path, 'card0-HDMI-A-1', 'disconnected', 'disabled')
    assert not active(tmp_path)
    connector(tmp_path, 'card0-HDMI-A-1', 'connected', 'disabled')
    assert not active(tmp_path)
    connector(tmp_path, 'card0-HDMI-A-1', 'connected', 'enabled')
    connector(tmp_path, 'card0-Virtual-1', 'connected', 'enabled')
    assert active(tmp_path)
    connector(tmp_path, 'card0-HDMI-A-2', 'connected', 'disabled')
    assert not active(tmp_path)
    connector(tmp_path, 'card0-HDMI-A-1', 'disconnected', 'disabled')
    connector(tmp_path, 'card0-HDMI-A-2', 'connected', 'enabled')
    assert not active(tmp_path)


def test_hotplug_restarts_a_crashed_viewer_with_backoff_and_stops_on_unplug():
    calls = []
    children = []

    class Screen:
        tick = 0

        def is_set(self):
            return self.tick >= 9

        def wait(self, seconds):
            assert seconds == 2
            self.tick += 1

    screen = Screen()

    class Viewer:
        def poll(self):
            return 1 if self is children[0] and screen.tick >= 2 else None

        def terminate(self):
            calls.append(('stop', screen.tick))

        def wait(self, timeout):
            return 0

    def start():
        calls.append(('start', screen.tick))
        child = Viewer()
        children.append(child)
        return child

    watcher['watch'](screen, lambda: 1 <= screen.tick <= 6 or screen.tick == 8,
                     start, clock=lambda: 2 * screen.tick)
    assert calls == [('start', 1), ('start', 5), ('stop', 7), ('start', 8), ('stop', 9)]
