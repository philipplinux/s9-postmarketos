"""Linux-only UI behavior checks; no device, subprocess or network is contacted."""
import importlib.util
from functools import lru_cache
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(sys.platform.startswith('linux'), 'UI imports Linux framebuffer modules')
class WireGuardUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('s9_display', ROOT / 'display' / 'display.py')
        cls.ui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ui)
        from PIL import ImageFont
        fonts = os.environ.get('S9_TEST_FONTS')
        if fonts:
            fonts = Path(fonts)
            @lru_cache(maxsize=None)
            def local_font(size, bold=False, mono=False, icon=False):
                name = ('SymbolsNerdFontMono-Regular.ttf' if icon and mono else
                        'SymbolsNerdFont-Regular.ttf' if icon else 'DroidSansMono.ttf' if mono else
                        'OpenSans-Bold.ttf' if bold else 'OpenSans-Regular.ttf')
                return ImageFont.truetype(str(fonts / name), size)
            cls.ui.font = local_font
            cls.ui.ICON_FONT = fonts / 'SymbolsNerdFont-Regular.ttf'
            return
        # Local fallback only; the phone continues using its packaged Open Sans/Nerd fonts.
        cls.ui.font = lambda size, bold=False, mono=False, icon=False: ImageFont.truetype(
            '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf' if mono else
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', size)

    @unittest.skipUnless(os.environ.get('S9_TEST_FONTS'), 'set S9_TEST_FONTS to the five packaged phone fonts')
    def test_selftest_accepts_real_font_metrics_with_pyte(self):
        self.assertIsNotNone(self.ui.pyte, 'install pyte to qualify the terminal branch')
        self.ui.set_scale(4 / 3)
        self.ui.selftest()

    def test_terminal_grid_uses_fractional_font_advance_without_clipping(self):
        # 1914 / 12.75 fits 150 cells; 1914 / 12.59765625 fits 151.
        # Both advances fit 80 cells in the 1020-pixel portrait viewport.
        for advance, landscape_cols in ((12.75, 150), (12.59765625, 151)):
            with self.subTest(advance=advance), patch.object(self.ui, 'term_cell_width', return_value=advance):
                self.assertEqual(self.ui.term_size(False, True), (80, 42))
                self.assertEqual(self.ui.term_size(False, False), (80, 73))
                self.assertEqual(self.ui.term_size(True, True), (landscape_cols, 20))
                self.assertEqual(self.ui.term_size(True, False), (landscape_cols, 37))

    def data(self, configured=True):
        data = self.ui.sample_data()
        data['services']['wireguard'] = {'configured': configured, 'up': False, 'autostart': False, 'profile': 'wg0', 'service': 'wg-quick.wg0'}
        return data

    def test_wg_mini_opens_detail_other_minis_keep_network_navigation(self):
        for i in range(5):
            ui = self.ui.Ui()
            x, y, _, _ = self.ui.ov_service_box(i)
            self.assertTrue(ui.on_gesture(('tap', x+20, y+20), self.data(), 0))
            self.assertEqual(ui.page, 2)
            self.assertEqual(ui.sub, 'wireguard' if i == 2 else None)

    def test_start_stop_autostart_queue_worker_without_running_helper(self):
        for box, action in [(self.ui.WG_START, 'start'), (self.ui.WG_STOP, 'stop'), (self.ui.WG_AUTO, 'autostart-on')]:
            ui = self.ui.Ui()
            ui.go(2)
            ui.open_sub('wireguard', self.data())
            self.assertTrue(ui.on_gesture(('tap', box[0]+20, box[1]+20), self.data(), 0))
            self.assertTrue(ui.busy)
            self.assertIs(ui.job[0], self.ui.wg_action)
            self.assertEqual(ui.job[1], (action,))
            ui.job_done(ui.job[2], (True, 'Saved'))
            self.assertFalse(ui.busy)
            self.assertTrue(ui.refetch)

    def test_unconfigured_stale_and_busy_block_actions(self):
        for configured, stale, busy in [(False, False, False), (True, True, False), (True, False, True)]:
            ui = self.ui.Ui()
            ui.go(2)
            data = self.data(configured)
            ui.open_sub('wireguard', data)
            ui.busy = busy
            ui.render(data, stale, 0)
            self.assertFalse(ui.on_gesture(('tap', 100, 1000), data, 0))
            self.assertIsNone(ui.job)

    def test_back_returns_to_network(self):
        ui = self.ui.Ui()
        ui.go(2)
        ui.open_sub('wireguard', self.data())
        self.assertTrue(ui.on_gesture(('tap', 100, 70), self.data(), 0))
        self.assertIsNone(ui.sub)
        self.assertIsNone(ui.w)

    def test_pages_render_in_all_states(self):
        for wg in ({'configured': False}, {'configured': True, 'up': False}, {'configured': True, 'up': True}, {}):
            data = self.data()
            data['services']['wireguard'] = wg
            for page in (0, 2):
                ui = self.ui.Ui()
                ui.go(page)
                self.assertEqual(ui.render(data, False, 0).size, self.ui.IMG)
            ui.open_sub('wireguard', data)
            self.assertEqual(ui.render(data, False, 0).size, self.ui.IMG)

if __name__ == '__main__':
    unittest.main()
