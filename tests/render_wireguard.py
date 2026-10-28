"""Headless local preview with synthetic public data; no phone/network access."""
import argparse
from pathlib import Path

from test_wireguard_ui import WireGuardUiTests

parser = argparse.ArgumentParser()
parser.add_argument('output', type=Path)
parser.add_argument('--selftest', action='store_true')
parser.add_argument('--fonts', type=Path, help='Directory containing local copies of the phone fonts')
args = parser.parse_args()
WireGuardUiTests.setUpClass()
display = WireGuardUiTests.ui
display.set_scale(4 / 3)
if args.fonts:
    from functools import lru_cache
    from PIL import ImageFont
    @lru_cache(maxsize=None)
    def local_font(size, bold=False, mono=False, icon=False):
        name = ('SymbolsNerdFontMono-Regular.ttf' if icon and mono else
                'SymbolsNerdFont-Regular.ttf' if icon else 'DroidSansMono.ttf' if mono else
                'OpenSans-Bold.ttf' if bold else 'OpenSans-Regular.ttf')
        return ImageFont.truetype(str(args.fonts / name), size)
    display.font = local_font
    display.ICON_FONT = args.fonts / 'SymbolsNerdFont-Regular.ttf'
if args.selftest:
    display.selftest()
args.output.mkdir(parents=True, exist_ok=True)
for state, wg in (
    ('unconfigured', {'configured': False, 'up': None}),
    ('down', {'configured': True, 'up': False}),
    ('up', {'configured': True, 'up': True}),
):
    data = display.sample_data()
    data['services']['wireguard'] = dict(
        profile='wg0', service='wg-quick.wg0', autostart=False, peers=1,
        latest_handshake=None, **wg)
    for page, name in ((0, 'overview'), (2, 'network')):
        ui = display.Ui()
        ui.go(page)
        ui.render(data, False, 0).save(args.output / f'{name}-{state}.png')
    ui.open_sub('wireguard', data)
    ui.render(data, False, 0).save(args.output / f'wireguard-{state}.png')
print('Headless previews:', args.output)
