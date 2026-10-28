#!/usr/bin/env python3
"""Galaxy S9 (postmarketOS) framebuffer status UI: touch pages, power key toggles the screen.

Ported from the Mi A3 display.py; DRM replaced by an mmap'd fbdev (/dev/fb0), data comes from the local
dashboard (http://127.0.0.1:8090/api/status). Layout space is 1080x2220; frames render at P x that (4/3 = native panel) and are
scaled to the panel if P differs.
"""
import argparse
import ctypes
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import errno
import fcntl
import glob
import ipaddress
import json
import logging
import mmap
import os
from pathlib import Path
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import textwrap
import time
import urllib.error
import urllib.request

from PIL import Image, ImageChops, ImageDraw, ImageFont

try:
    import pyte           # apk add py3-pyte; the Terminal tab shows a hint without it
except ImportError:
    pyte = None

BG = '#090c11'
PANEL = '#111720'
LINE = '#263241'
TEXT = '#e8eef5'
MUTED = '#91a0b1'
CYAN = '#65e0da'
VIOLET = '#b9a2ff'
GREEN = '#7ce6a6'
AMBER = '#ffcb70'
RED = '#ff5c5c'
FONT = Path('/usr/share/fonts/opensans')
ICON_FONT = Path('/usr/share/fonts/nerd-fonts/SymbolsNerdFont-Regular.ttf')   # apk add font-nerd-fonts-symbols
ICON_MONO_FONT = Path('/usr/share/fonts/nerd-fonts/SymbolsNerdFontMono-Regular.ttf')
FONTS = {}

FB = '/dev/fb0'
BACKLIGHT = '/sys/class/backlight/panel'
TOUCH_NAME, KEY_NAME = 'sec_touchscreen', 'gpio_keys'
TSP_CMD = Path('/sys/class/sec/tsp/cmd')   # Samsung touch IC commands; dead_zone_enable,0 = touches on the curved edges count
KEY_POWER, KEY_VOLDOWN, KEY_VOLUP = 116, 114, 115
DASH_URL = 'http://127.0.0.1:8090'
BATT = Path('/sys/class/power_supply/battery')
WC_CONTROL, WC_ENABLE = BATT / 'wc_control', BATT / 'wc_enable'
WIRELESS_ONLINE, WIRELESS_FILE = Path('/sys/class/power_supply/wireless/online'), Path('/etc/s9/wireless')
CPUFREQ = Path('/sys/devices/system/cpu/cpufreq')
MALI = Path('/sys/devices/platform/17500000.mali')    # dvfs_max_lock / dvfs_min_lock (kHz; the top/bottom table clock releases the lock)
SAMSUNG_MAX_LIMIT = Path('/sys/power/cpufreq_max_limit')
OC_ROOT, OC_FILE, OC_BTN = Path('/'), Path('/etc/s9/overclock'), (30, 1838, 1050, 1998)   # file: 0 = off, else the 4-core cap kHz
OC_KNOBS = (('sys/devices/system/cpu/cpuhotplug/governor/quad_freq', '2314000', '1794000'),   # r7 knob: 4-core big cap
            ('sys/class/thermal/thermal_zone0/sustainable_power', '3500', '2000'),          # BIG IPA power budget
            ('sys/class/thermal/thermal_zone0/trip_point_2_temp', '88000', '83000'))        # BIG throttle target
CPU_PRESENT = Path('/sys/devices/system/cpu/present')
CORE_PREV = {}

W, H = 1080, 2220            # layout/touch space; frames are drawn at P x this and scaled to the panel
P = 2 / 3
IMG = (720, 1480)
TAB_Y = 2070
TABS = ('Overview', 'Processes', 'Network', 'Thermals', 'Power', 'Terminal', 'Dock')
TAB_ICONS = ('\U000F056E', '\U000F0EE0', '\U000F05A9', '\U000F050F', '\U000F0425', '\U000F018D', '\U000F0868')  # Material Design (Nerd Font)
TAB_W = W // len(TABS)
ROW_H, TOP_PROCS, PROW_H = 120, 30, 140
LIST_TOP, LIST_BOTTOM = 150, TAB_Y - 10
PROC_SORT_BTNS = (('cpu_percent', 'CPU', (600, 25, 800, 125)), ('rss_bytes', 'RAM', (820, 25, 1040, 125)))
PERF_BTN, PERF_Y, PERF_H = (800, 25, 1040, 125), 560, 118
THERM_PAGE, REFRESH_BTN, REFRESHES = 3, (580, 25, 780, 125), (1, 2, 5, 10)   # Thermals-only fetch interval (s); 5 elsewhere
TG_CONF = Path('/var/lib/thermal-guard/limits.json')   # re-read by thermal-guard every 5 s
TG_STATUS, TG_BOX = Path('/run/thermal-guard.status'), (30, 1940, 1050, 2030)
TG_PAGE_ROWS = 3   # low_pct (last) is set on the Network > Audio page
TG_LIMITS = (('batt_max', 'Battery max', ' °C', 1, 40, 65, 55), ('soc_max', 'SoC max (CPU/GPU/board)', ' °C', 5, 70, 110, 100),
             ('hold_s', 'Hold time', ' s', 10, 10, 300, 60), ('low_pct', 'Low-battery chimes from (0 = off)', ' %', 5, 0, 50, 30))
PERF_ROWS = (('policy0', 'max', 'Little', 'max'), ('policy0', 'min', 'Little', 'min'),        # (key, which, group, label)
             ('policy4', 'max', 'Big', 'max'), ('policy4', 'min', 'Big', 'min'), ('quad', 'max', 'Big', '4-core cap'),
             ('trip', 'max', 'Big', 'throttle at'),
             ('gpu', 'max', 'GPU', 'max'), ('gpu', 'min', 'GPU', 'min'))
PERF_GROUPS = {'Little': ('Little cores · cpu0-3', CYAN), 'Big': ('Big cores · cpu4-7', VIOLET), 'GPU': ('GPU · Mali-G72', GREEN)}
OV_STORE, BACK_BTN = (30, 860, 525, 1160), (30, 25, 260, 125)
FILES_BTN, FILE_TOP, FILE_ROW, FILE_LINE, FILES_MAX, FILE_PEEK = (880, 25, 1040, 125), 190, 100, 36, 500, 65536   # Partitions -> read-only folder explorer
OV_BATT, OV_CPU, OV_NET, OV_DOCK, OV_PROC = (30, 160, 1050, 500), (30, 530, 525, 830), (30, 1190, 1050, 1560), (30, 1590, 1050, 1700), (30, 1720, 1050, 2040)
NET_ROWS = (('ssh', 'SSH & User'), ('tailscale', 'Tailscale'), ('wireguard', 'WireGuard'), ('usb', 'USB'), ('bluetooth', 'Bluetooth'), ('web', 'Web dashboard'), ('audio', 'Audio'))
NET_ICONS = {'ssh': '\U000F08C0', 'tailscale': '\U000F0582', 'wireguard': '\U000F0565', 'usb': '\U000F0553', 'bluetooth': '\U000F00AF', 'web': '\U000F059F', 'audio': '\U000F057E'}
NET_WIFI = (30, 170, 1050, 690)
WG_START, WG_STOP, WG_AUTO = (30, 940, 525, 1080), (555, 940, 1050, 1080), (30, 1110, 1050, 1250)
WG_HELPER = '/usr/local/sbin/s9-wireguard'


def ov_service_box(i):
    # Five mini-cards occupy the same 960 px interior as the original four.
    x = 60 + i * 194
    return (x, 1430, x + 184, 1530)


def wg_state(wg):
    if wg.get('configured') is False:
        return 'Unconfigured', None
    up = wg.get('up')
    return ('Up' if up is True else 'Down' if up is False else 'Unknown'), up


def wg_action(action):
    if action not in ('start', 'stop', 'autostart-on', 'autostart-off'):
        return False, 'Unsupported action'
    try:
        result = subprocess.run([WG_HELPER, action], capture_output=True, text=True, timeout=18)
        response = json.loads(result.stdout)
        return result.returncode == 0 and response.get('ok') is True, response.get('message') or 'WireGuard action failed'
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return False, 'WireGuard helper unavailable or timed out'

STORE_FILE, STORE_BTN = Path('/etc/s9/store_mode'), (30, 1160, 1050, 1330)
WIRELESS_BTN = (30, 985, 1050, 1125)
def net_box(i):
    return (30, 720 + i * 150, 1050, 855 + i * 150)
TERM_CONF, TERM_OPTIONS_BTN = Path('/etc/s9/term'), (60, 1430, 1020, 1540)
TERM_FILTERS = (
    ('KNOX', 'Knox VPN notices', (r'KNOX:',), 'hide', 'Samsung VPN notices.'),
    ('UFS_PROBES', 'Optional disk checks',
     (r'ufshcd_ioctl: Illegal ufs-IOCTL cmd -2144861675\Z',
      r'ufshcd_ioctl: Illegal ufs-IOCTL cmd -2147216764\Z',
      r'ufshcd_ioctl: Illegal ufs-IOCTL cmd 21397\Z'), 'hide',
     'Optional disk checks unsupported by this older driver.'),
    ('WIFI_NETDEV', 'Other network devices', (r'wdev null\. Do nothing\Z', r'wl_cfg80211_netdev_notifier_call : ?\Z'), 'hide',
     'Wi-Fi notices about USB and other network links.'),
    ('USB_MAC', 'USB network addresses',
     (r'using random self ethernet address\Z', r'using random host ethernet address\Z'), 'hide',
     'Normal automatic USB network address selection.'),
    ('PANEL_ON', 'Screen already on', (r'decon-\d+ already (on|ON)', r'dsim already on'), 'hide',
     'The panel driver noting a wake request while the screen was already on.'),
    ('USB_GADGET', 'USB driver notices', (r'^usb[:_ ]', r'^ccic_notifier_init already registered', r'^dwc3 [0-9a-f]+\.dwc3: (usb: )?dwc3_gadget|^dwc3 [0-9a-f]+\.dwc3: Turn (on|off) gadget',
                                                 r'^gadget_dev_desc_UDC_store', r'^configfs-gadget gadget: ', r'Setting up USB gadget through configfs'), 'hide',
     'USB link, gadget and cable-detect chatter.'),
    ('WIFI_DRIVER', 'Wi-Fi driver notices', (r'^dhd_', r'^DHCP [A-Z]+\[', r'^\d+\.\d+ EL:V\d', r'^MACEVENT: ', r'^ETHER_TYPE_802_1X',
                                              r'^Register interface \[wlan', r'^Invalid frame (length )?\[?',
                                              r'^CFG\w*-(INFO|TRACE)\d?\) ', r'^DHD: ', r'^dhdpcie_', r'^wifi_platform_set_power', r'^Compiled in drivers/net/wireless/bcmdhd',
                                              r'^\[wlan\d\] ', r'^wl version\. ', r'^if name: p2p', r'^Register(ing Vendor| BRCM)|^Vendor: Register BRCM', r'^Created /proc/dhd_',
                                              r'^\[?STATIC_IF\]? |^Static I/F ', r'^Incoming iface = ', r'^\[WIFI_SEC\] ', r'^No filter file\(not an error\)', r'^wl_android_wifi_on',
                                              r'^(START SCAN|E?SCAN COMPLETED|LEGACY_SCAN |scan_prep done|Enqueing escan|n_channels:|scan engine not aborted)',
                                              r'^(event_type \(|event=\d+ status|channel_hint|bssid_hint|key index \(|NOT assoc|(drv|fw) state is not)',
                                              r'^(P2P interface registered|Vif created\.|p2p_dev bsscfg| Connected event)', r'^\[BT\] set_wake_locked'), 'hide',
     'Broadcom Wi-Fi/Bluetooth setup, scan and connect traces, firmware events and DHCP/EAPOL packet traces.'),
    ('AUDIO', 'Audio driver notices', (r'^samsung-(abox|vts)', r'^\[(ABOX_ADAPTATION|MAXIM_DSM_POWER|MAX98512_DEBUG)\]', r'^(star-)?madera',
                                       r'^max98512', r'^Calliope'), 'hide',
     'Speaker amp, codec and audio DSP chatter on every sound.'),
    ('ACPM_RETRY', 'Power firmware retries', (r'acpm_ipc timeout retry 1now',), 'hide',
     'Slow reply from the power core under load, fine on the first retry. Retry 2+ still shows.'),
    ('HQM', 'Samsung telemetry', (r'^\[HQM\] ',), 'hide',
     'Hardware-quality reports for Android, e.g. BOCP: big-core power rail at its current limit.'),
    ('WIRELESS_NACK', 'Wireless charger no-reply', (r'^exynos5-hsi2c 14370000\.hsi2c: ', r'^mfc_'), 'hide',
     'Charger chip is unpowered without a pad; the on/off switch still applies.'),
)
TERM_FILTER_DEFAULTS = {key: default for key, _, _, default, _ in TERM_FILTERS}
TERM_FILTER_BOXES = tuple((60, 190 + i * 185, 1020, 330 + i * 185) for i in range(len(TERM_FILTERS)))
POWER_BTNS = (('reboot', (30, 1865, 520, 2040)), ('poweroff', (560, 1865, 1050, 2040)))
FREEZE_BTN, BOOST_BTN = (30, 1590, 1050, 1710), (30, 1725, 1050, 1845)
BOOST_FILE = Path('/etc/s9/touch-boost')     # 0 = off; missing = on
PANIC_CONF = '/etc/sysctl.d/90-s9-panic.conf'
PANIC_KEYS = ('softlockup_panic', 'panic_on_rcu_stall')   # hard lockups always panic here (CONFIG_BOOTPARAM_HARDLOCKUP_PANIC)
CONFIRM_TEXT = {'reboot': ('Reboot', 'The phone restarts'), 'poweroff': ('Shut down', 'Turn it back on with the power key'),
                'forget': ('Forget', 'Deletes the saved password'), 'forget_active': ('Forget', 'Wi-Fi disconnects; the phone may go offline'),
                'ssh_off': ('Turn off', 'Remote logins over SSH stop'), 'rename': ('Rename', 'Log in with the new name from now on'),
                'oc_warn': ('Overclock', 'Needs a STRONG charger (2 A or more)\nAND extra cooling, e.g. a fan!\nThe phone runs hotter and draws more!', '!! WARNING !!'),
                'oc_pants': ('I accept', 'Put on your big boy pants!\n#»»  I  ««\nFULLY understand the repercussions\nand accept that this could\n*BURN MY HOUSE DOWN\n*or BLOW MY BATTERY!', '!!! FINAL WARNING !!!')}
CONFIRM_TEXT['trip'] = ('Unlock', 'Only for a heatsink on the chip!\nThermal guard stays the failsafe.\nLocks again 10 s after the last change.', 'Change throttle temperature?')
DANGER = {'oc_warn', 'oc_pants'}    # red pop-up with an alert icon; hint lines: *yellow, #huge yellow


def confirm_btns(act):
    """(cancel, ok): the final overclock warning swaps them, so a double tap on the first OK lands on Cancel."""
    return (OK_BTN, CANCEL_BTN) if act == 'oc_pants' else (CANCEL_BTN, OK_BTN)
CANCEL_BTN, OK_BTN = (130, 1130, 510, 1260), (570, 1130, 950, 1260)
CONFIRM_S = 10.0
DOCK_PAGE = 6
DOCKER = '/usr/bin/docker'
DOCKER_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'DOCKER_CONFIG': '/nonexistent'}
SECRET_RE = re.compile(r'(?i)((?:key|token|secret|passw\w*|auth\w*|credential\w*)["\']?\s*[=:]\s*)\S+')
ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]')
ACT_L, ACT_R, ACT_W = (30, 1900, 520, 2040), (560, 1900, 1050, 2040), (30, 1900, 1050, 2040)
TERM_PAGE, TERM_COLS, TERM_ROWS, TERM_CH, TERM_FONT = 5, 80, 42, 27, 21
TERM_TOP, TERM_X = 96, 30   # fixed controls use the top strip; side padding clears the rounded panel corners
TERM_ROTATE_BTN, TERM_KB_BTN = (30, 24, 520, 76), (560, 24, 1050, 76)
TERM_USER = next((l.split(':')[0] for l in open('/etc/passwd') if l.split(':')[2:3] == ['10000']), 'user')   # pmOS default user
KB_TOP, KEY_W, KEY_H = 1250, 108, 135
OSK_TOP = TAB_Y * 2 // 3     # buffyboard fills the bottom third of the height it is given (it ignores -g's Y offset)
KB_ROWS = {'abc': ('qwertyuiop', 'asdfghjkl', 'zxcvbnm'), 'ABC': ('QWERTYUIOP', 'ASDFGHJKL', 'ZXCVBNM'),
           'sym': ('!@#$%^&*()', '-_=+[]{};:', '\'",.?/\\'), 'sym2': ('~`|<>%^&*()', '#$@_-+=:;(', '\'"!?,./')}
KB_SHIFT = {'abc': 'ABC', 'ABC': 'abc', 'sym': 'sym2', 'sym2': 'sym'}
KB_LABELS = {'esc': 'Esc', 'tab': 'Tab', 'ctrl': 'Ctrl', 'left': '<', 'up': '^', 'down': 'v', 'right': '>', 'shift': 'Shift',
             'back': 'Del', 'space': 'space', 'enter': 'Enter'}
KEY_SEQ = {'esc': '\x1b', 'tab': '\t', 'enter': '\r', 'back': '\x7f', 'space': ' '}
# buffyboard's uinput key codes (US layout) -> pty input; it sends Shift/Ctrl/Alt as separate key events
UKEYS = dict(zip(range(2, 14), zip('1234567890-=', '!@#$%^&*()_+')))
UKEYS.update(zip(range(16, 28), zip('qwertyuiop[]', 'QWERTYUIOP{}')))
UKEYS.update(zip(range(30, 42), zip("asdfghjkl;'`", 'ASDFGHJKL:"~')))
UKEYS.update(zip(range(43, 54), zip('\\zxcvbnm,./', '|ZXCVBNM<>?')))
UKEYS[57] = (' ', ' ')
USEQ = {1: '\x1b', 14: '\x7f', 15: '\t', 28: '\r', 96: '\r', 103: '\x1b[A', 108: '\x1b[B', 106: '\x1b[C', 105: '\x1b[D',
        102: '\x1b[H', 107: '\x1b[F', 104: '\x1b[5~', 109: '\x1b[6~', 111: '\x1b[3~'}
SHIFTS, CTRLS, ALTS = {42, 54}, {29, 97}, {56, 100}
PRESS_S = 0.1       # a tapped key stays highlighted at least this long: a quick tap's down + up land in one event batch
BOOST_S = 1.0       # touch boost: little cores held at their max this long after a touch-down
ARROWS = {'up': 'A', 'down': 'B', 'right': 'C', 'left': 'D'}
TERM_PAL = {'black': '#1b1f27', 'red': '#e06c75', 'green': '#7ce6a6', 'brown': '#ffcb70', 'blue': '#61afef', 'magenta': '#c678dd',
            'cyan': '#65e0da', 'white': '#c8d0da', 'brightblack': '#5c6a7a', 'brightred': '#ff8a93', 'brightgreen': '#a5f3c3',
            'brightbrown': '#ffe0a0', 'brightblue': '#8cc4ff', 'brightmagenta': '#e0a0f5', 'brightcyan': '#9ff2ee', 'brightwhite': '#ffffff'}
VOL_STEP, DRAG_PX, SWIPE_PX, LONG_S = 600, 40, 250, 0.8
TERM_COAST_S, TERM_COAST_MAX_LINES, TERM_COAST_MAX_SPEED = 0.24, 3, 20.0


def set_scale(p):
    global P, IMG
    P, IMG = p, (round(W * p), round(H * p))


def font(size, bold=False, mono=False, icon=False):
    key = (size, bold, mono, icon)
    if key not in FONTS:
        style = 'Bold' if bold else 'Regular'
        paths = ([ICON_MONO_FONT, Path.home() / '.local/share/fonts/nerd-fonts/FiraCodeNerdFont-Regular.ttf', ICON_FONT] if icon and mono else
                 [ICON_FONT, Path.home() / '.local/share/fonts/nerd-fonts/FiraCodeNerdFont-Regular.ttf'] if icon else
                 [Path('/usr/share/fonts/droid/DroidSansMono.ttf'), Path('/usr/share/fonts/google-noto/NotoSansMono-Regular.ttf')] if mono else
                 [FONT / f'OpenSans-{style}.ttf', Path(f'/usr/share/fonts/open-sans/OpenSans-{style}.ttf')])   # 2nd: host dev boxes
        path = next((p for p in paths if p.exists()), paths[0])
        FONTS[key] = ImageFont.truetype(str(path), size)
    return FONTS[key]


def fmt(value, digits=0, suffix=''):
    return '—' if value is None else f'{value:.{digits}f}{suffix}'


def gib(value):
    return '—' if value is None else f'{value / 2**30:.1f} GiB'


def uptime_text(up):
    return '—' if up is None else f'{int(up) // 86400}d {int(up) % 86400 // 3600}h' if up >= 86400 else \
        f'{int(up) // 3600}h {int(up) % 3600 // 60}m'


def fmt_mem(n):
    return '—' if n is None else f'{n >> 20}M' if n < 1 << 30 else f'{n / (1 << 30):.1f}G'


def fmt_bytes(n):
    return '—' if n is None else f'{n / 1048576:.0f} MiB' if n < 1 << 30 else f'{n / (1 << 30):.1f} GiB'


def batt_color(b):
    pct, st = b.get('percent'), (b.get('status') or '').lower()
    return CYAN if 'charg' in st and 'not' not in st and 'dis' not in st \
        else GREEN if pct is None or pct >= 50 else AMBER if pct >= 20 else '#ff6b6b'


def store_mode():
    """Samsung 'store_mode' = charge cap (read-only here): '1' means the cap is on."""
    try:
        return (BATT / 'store_mode').read_text().strip()
    except OSError:
        return None


def store_want():
    """Wanted charge cap: /etc/s9/store_mode (missing = on, the boot script's default)."""
    try:
        return STORE_FILE.read_text().strip() != '0'
    except OSError:
        return True


def store_toggle(on):
    """Persist the choice for /etc/local.d/store_mode.start and apply it. The driver only accepts 1 (the cap cannot be
    cleared at runtime), so turning it off takes effect at the next boot."""
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STORE_FILE.write_text('1\n' if on else '0\n')
    if on:
        (BATT / 'store_mode').write_text('1')


def boost_want():
    try:
        return BOOST_FILE.read_text().strip() != '0'
    except OSError:
        return True


def switch_card(d, text, card, box, icon, title, sub, on, color=GREEN):
    x0, y0, x1, y1 = box
    card(x0, y0, x1 - x0, y1 - y0)
    text(85, y0 + 60, icon, 46, CYAN, anchor='mm', icon=True)
    text(125, y0 + 15, title, 38)
    text(125, y0 + 70, sub, 26, MUTED, max_width=740)
    d.rounded_rectangle((880 * P, (y0 + 30) * P, 1010 * P, (y0 + 90) * P), 30 * P, fill=color if on else LINE)
    d.ellipse(((950 if on else 885) * P, (y0 + 35) * P, (1005 if on else 940) * P, (y0 + 85) * P), fill=TEXT)


def freeze_reboot(kdir='/proc/sys/kernel'):
    try:
        return all(Path(kdir, k).read_text().strip() == '1' for k in PANIC_KEYS)
    except OSError:
        return None


def set_freeze_reboot(on, kdir='/proc/sys/kernel', conf=PANIC_CONF):
    v = int(bool(on))
    for k in PANIC_KEYS:
        Path(kdir, k).write_text(str(v))
    Path(kdir, 'panic').write_text('10' if on else '120')
    Path(conf).write_text('# A hung kernel panics and reboots after 10 s instead of sitting frozen (headless server).\n'
                          '# sec_debug.level=0, so a panic reboots instead of entering upload mode. Trace: /sys/fs/pstore/.\n'
                          '# Toggle: display Power page.\n'
                          + ''.join(f'kernel.{k} = {v}\n' for k in PANIC_KEYS) + f"kernel.panic = {10 if on else 120}\n")


def overclock():
    try:
        return OC_FILE.read_text().strip() not in ('', '0')
    except OSError:
        return False


def set_overclock(on):
    """Save the opt-in (power/overclock.start re-applies it at boot) and write the knobs now; returns a toast."""
    try:
        if (OC_ROOT / 'sys/class/thermal/thermal_zone0/type').read_text().strip() != 'BIG':
            return 'Overclock: thermal_zone0 is not BIG, nothing changed'
        OC_FILE.write_text(f'{OC_KNOBS[0][1]}\n' if on else '0\n')
        if on:
            OC_FILE.with_name('overclock-trip').write_text(f'{OC_KNOBS[2][1]}\n')
        for path, hi, lo in OC_KNOBS:
            (OC_ROOT / path).write_text(hi if on else lo)
    except OSError as e:
        return f'Overclock: {e.strerror or e}'
    return 'Overclock on: big 2.31 GHz, throttle at 88 °C' if on else 'Overclock off: stock 1.79 GHz, 83 °C'


def store_label(want, sm):
    return 'ON' if want else 'OFF' if sm in (None, '0') else 'OFF after reboot'


def sysfs_int(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def wireless_want():
    return kv_read(WIRELESS_FILE, {'ENABLED': 'on'})['ENABLED'] != 'off'


def wireless_state():
    want, enabled = wireless_want(), sysfs_int(WC_ENABLE)
    online = sysfs_int(WIRELESS_ONLINE)
    return want, enabled, online


def wireless_label():
    want, enabled, online = wireless_state()
    if enabled is None:
        return f"Wireless charging: unavailable · saved {'on' if want else 'off'}", AMBER
    if enabled:
        state = 'On · pad detected' if online else 'On · no pad' if online == 0 else 'On'
    else:
        state = 'Off'
    if bool(enabled) != want:
        state += f" · saved {'on' if want else 'off'}"
    return 'Wireless charging: ' + state, GREEN if enabled and online else CYAN if enabled else MUTED


def wireless_toggle(on):
    """Persist the requested state for boot and apply it through the driver's wc_control switch."""
    conf = kv_read(WIRELESS_FILE, {'ENABLED': 'on'})
    conf['ENABLED'] = 'on' if on else 'off'
    try:
        kv_write(WIRELESS_FILE, conf)
    except OSError as exc:
        return False, f"Could not save wireless charging preference: {exc.strerror or exc}"
    try:
        WC_CONTROL.write_text('1' if on else '0')
    except OSError as exc:
        return False, f"Saved for boot; wireless control failed: {exc.strerror or exc}"
    actual = sysfs_int(WC_ENABLE)
    if actual is None or bool(actual) != on:
        return False, f"Saved for boot; hardware did not confirm {'on' if on else 'off'}"
    return True, f"Wireless charging {'enabled' if on else 'disabled'}"


def tools(image):
    d = ImageDraw.Draw(image)

    def text(x, y, value, size, color=TEXT, bold=False, max_width=None, anchor='la', mono=False, icon=False):
        value = str(value if value is not None else '—')
        f = font(max(8, round(size * P)), bold, mono, icon)
        if max_width:
            while len(value) > 1 and d.textlength(value, font=f) > max_width * P:
                value = value[:-2] + '…'
        d.text((x * P, y * P), value, fill=color, font=f, anchor=anchor)

    def card(x, y, w, h, fill=PANEL, outline=LINE):
        d.rounded_rectangle((x * P, y * P, (x + w) * P, (y + h) * P), 16, fill=fill, outline=outline, width=1)

    def bar(x, y, w, percent, h=16, color=CYAN):
        d.rounded_rectangle((x * P, y * P, (x + w) * P, (y + h) * P), 5, fill=LINE)
        if percent is not None and percent > 0:
            d.rounded_rectangle((x * P, y * P, (x + w * min(100, max(0, percent)) / 100) * P, (y + h) * P), 5, fill=color)

    return d, text, card, bar


def header(image, title, stale, right=None):
    d, text, card, bar = tools(image)
    d.rectangle((0, 0, IMG[0], (LIST_TOP - 10) * P), fill=BG)
    text(40, 36, title, 60, bold=True)
    text(1040, 50, right or ('Offline' if stale else 'Live'), 34, AMBER if stale else MUTED if right else GREEN, anchor='ra')


def btn(text, card, box, label, color=CYAN, size=48):
    card(box[0], box[1], box[2] - box[0], box[3] - box[1], outline=color)
    text((box[0] + box[2]) // 2, (box[1] + box[3]) // 2, label, size, color, bold=True, anchor='mm')


def batt_head(d, text, x, y, size, b):
    """'74%' plus a small battery icon filled to the level."""
    value = fmt(b.get('percent'), suffix='%') if b.get('percent') is not None else '—'
    text(x, y, value, size, bold=True)
    ix = x + d.textlength(value, font=font(round(size * P), True)) / P + 30
    iy, ih, iw, col = y + size * 0.42, size * 0.5, size * 0.85, batt_color(b)
    d.rounded_rectangle((ix * P, iy * P, (ix + iw) * P, (iy + ih) * P), 6, outline=col, width=3)
    d.rectangle(((ix + iw) * P, (iy + ih * .3) * P, (ix + iw + 8) * P, (iy + ih * .7) * P), fill=col)
    if b.get('percent'):
        d.rectangle(((ix + 8) * P, (iy + 8) * P, (ix + 8 + (iw - 16) * min(100, b['percent']) / 100) * P, (iy + ih - 8) * P),
                    fill=col)
    return col


def core_stats():
    """[(cpu, busy %, MHz, cpufreq policy)] from /proc/stat deltas since the previous call (None on the first)."""
    out = []
    for line in Path('/proc/stat').read_text().splitlines()[1:]:
        f = line.split()
        if not f[0].startswith('cpu'):
            break
        n, t = int(f[0][3:]), [int(v) for v in f[1:9]]
        tot, idle = sum(t), t[3] + t[4]
        pt, pi = CORE_PREV.get(n, (tot, idle))
        CORE_PREV[n] = (tot, idle)
        base = f'/sys/devices/system/cpu/cpu{n}/cpufreq'
        try:
            mhz = int(Path(base, 'scaling_cur_freq').read_text()) // 1000
        except (OSError, ValueError):
            mhz = None
        out.append((n, 100 - (idle - pi) * 100 / (tot - pt) if tot > pt else None, mhz, os.path.realpath(base)))
    # hotplugged-off cores vanish from /proc/stat: list them from `present`, grouped by the policy's related_cpus
    try:
        lo, _, hi = CPU_PRESENT.read_text().strip().partition('-')
        missing = set(range(int(lo), int(hi or lo) + 1)) - {c[0] for c in out}
    except (OSError, ValueError):
        missing = set()
    for n in missing:
        pol = next((os.path.dirname(f) for f in glob.glob(str(CPU_PRESENT.parent / 'cpufreq/policy*/related_cpus'))
                    if str(n) in Path(f).read_text().split()), f'offline{n // 4}')
        out.append((n, None, 'off', os.path.realpath(pol)))
    return sorted(out)


def gpu_cells():
    """(n_available, lit flags) from the Mali core_mask file: one cell per available shader core, lit if in the JS0 mask."""
    try:
        masks = dict(re.findall(r'(Available|JS0)\D*: (0x[0-9a-fA-F]+)', (MALI / 'core_mask').read_text()))
        avail, js0 = int(masks['Available'], 16), int(masks['JS0'], 16)
    except (OSError, KeyError, ValueError):
        return []
    return [bool(js0 >> b & 1) for b in range(avail.bit_length()) if avail >> b & 1]


def draw_gpu_cells(d, text, data, y, label=True):
    """One row of shader-core cells, every lit cell filled to the overall GPU load (the driver has no per-core load)."""
    cells, gpu = gpu_cells(), data.get('gpu') or {}
    if not cells:
        return
    pct = gpu.get('utilization_percent')
    if label:
        text(60, y, f"GPU · {len(cells)} cores · {fmt(pct, 0, '%')} · {fmt(gpu.get('clock_mhz'), 0, ' MHz')}", 26, MUTED, max_width=960)
    pitch = 960 / len(cells)
    for i, lit in enumerate(cells):
        x0, y0 = 60 + i * pitch, y + 36 * label
        d.rounded_rectangle((x0 * P, y0 * P, (x0 + pitch - 7) * P, (y0 + 32) * P), 4 * P, outline=CYAN if lit else LINE, width=max(1, int(P)))
        fill = 32 * min(pct or 0, 100) / 100 if lit else 0
        if fill >= 1:
            d.rounded_rectangle((x0 * P, (y0 + 32 - fill) * P, (x0 + pitch - 7) * P, (y0 + 32) * P), 4 * P, fill=AMBER if (pct or 0) >= 80 else CYAN)


def draw_cores(d, text, bar, data, yoffset=0):
    """Per-core busy % and clock: Little cluster left, Big right."""
    text(60, 180 + yoffset, f"CPU cores · total {fmt((data.get('cpu') or {}).get('percent'), 0, '%')} · MHz", 34, MUTED)
    groups = {}
    for c in core_stats():
        groups.setdefault(c[3], []).append(c)
    names = ('Little', 'Big') if len(groups) == 2 else tuple(os.path.basename(k) for k in groups)
    for col, (name, (pol, cs)) in enumerate(zip(names, list(groups.items())[:2])):
        x = 60 + col * 495
        try:
            hw, cap = (int(Path(pol, f).read_text()) for f in ('cpuinfo_max_freq', 'scaling_max_freq'))
            top = f" · max {cap / 1e6:.1f} GHz" + (f" (hw {hw / 1e6:.1f})" if cap < hw else '')
        except (OSError, ValueError):
            top = ''
        text(x, 235 + yoffset, name + top, 28, MUTED)
        for i, (n, pct, mhz, _) in enumerate(cs[:4]):
            y = 285 + yoffset + i * 52
            off = mhz == 'off'
            text(x, y, f'cpu{n}', 30, MUTED if off else TEXT, bold=True)
            bar(x + 100, y + 16, 180, None if off else pct, 14, AMBER if (pct or 0) >= 80 else CYAN)
            text(x + 370, y, 'off' if off else fmt(pct, 0, '%'), 30, MUTED if off else TEXT, anchor='ra')
            text(x + 465, y + 3, '' if off else mhz or '—', 26, MUTED, anchor='ra')


def thermal_readouts():
    """Named zone and power-supply temperatures, sentinels dropped."""
    def read_temp(path, scale):
        try:
            value = int(Path(path).read_text().strip()) / scale
        except (OSError, ValueError, UnicodeError):
            return None, 'unavailable'
        if value == -40:
            return None, 'skip'
        return (value, None) if -20 < value < 150 else (None, 'invalid')

    out = []
    for zone in sorted(glob.glob('/sys/class/thermal/thermal_zone*')):
        try:
            name = Path(zone, 'type').read_text().strip() or os.path.basename(zone)
        except (OSError, UnicodeError):
            name = os.path.basename(zone)
        value, status = read_temp(zone + '/temp', 1000)
        if status != 'skip':
            out.append((f'Zone · {name}', value, status))
    for path in sorted(glob.glob('/sys/class/power_supply/*/temp')):
        value, status = read_temp(path, 10)
        if status != 'skip':
            out.append((f'Power · {os.path.basename(os.path.dirname(path))}', value, status))
    return out


# ---- pages -------------------------------------------------------------------------------------
def page_overview(data, stale=False, cores=False):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, 'Galaxy S9', stale)
    try:
        stamp = dt.datetime.fromisoformat(data.get('collected_at')).astimezone().strftime('%H:%M:%S')
    except (TypeError, ValueError):
        stamp = 'waiting'
    text(900, 50, stamp, 34, MUTED, anchor='ra')

    b = data.get('battery') or {}
    x0, y0, x1, y1 = OV_BATT
    card(x0, y0, x1 - x0, y1 - y0)
    if cores:   # CPU tile tapped: per-core view takes the big card, battery moves into the tile
        draw_cores(d, text, bar, data)
    else:
        col = batt_head(d, text, 60, 175, 130, b)
        ma = b.get('current_ma')
        text(60, 350, (b.get('status') or 'No battery gauge') + (f' · {abs(ma):.0f} mA' if ma is not None else ''), 36, MUTED,
             max_width=560)
        bar(60, 420, 560, b.get('percent'), h=24, color=col)
        for i, (label, value, glyph) in enumerate((('Uptime', uptime_text(data.get('uptime_seconds')), '\U000F0954'),
                                                   ('Temperature', fmt(b.get('temperature_c'), 1, ' °C'), '\U000F050F'))):
            text(700, 205 + i * 150, label, 30, MUTED)
            text(720, 263 + i * 150, glyph, 34, CYAN, anchor='mm', icon=True)
            text(755, 235 + i * 150, value, 52, bold=True)

    cpu, mem, st, gpu = (data.get(k) or {} for k in ('cpu', 'memory', 'storage', 'gpu'))
    load = gpu.get('utilization_percent')
    gpu_value = 'Off' if not gpu.get('available') else 'Asleep' if gpu.get('power_state') == 'suspended' \
        else fmt(load, 0, '%') if load is not None else 'n/a'
    tiles = (('CPU', fmt(cpu.get('percent'), 0, '%'), f"{cpu.get('logical_cores', '—')} cores" +
              (f" · {cpu['temp_c']:.0f} °C" if cpu.get('temp_c') is not None else ''), cpu.get('percent')),
             ('Memory free', gib(mem.get('free_bytes')), f"+{gib(mem.get('cached_bytes'))} cached · {gib(mem.get('used_bytes'))} used",
              (mem.get('used_percent') or 0) + (mem.get('cached_percent') or 0)),
             ('Storage free', gib(st.get('free_bytes')), f"{gib(st.get('used_bytes'))} used", st.get('used_percent')),
             (gpu.get('model') or 'GPU', gpu_value, 'GPU load' + (f" · {gpu['temp_c']:.0f} °C" if gpu.get('temp_c') is not None else ''), load))
    tile_icons = ('\U000F061A', '\U000F035B', '\U000F02CA', '\U000F061A')
    for i, (label, value, sub, pct) in enumerate(tiles):
        x, y = 30 + i % 2 * 525, 530 + i // 2 * 330
        card(x, y, 495, 300)
        if cores and i == 0:
            text(x + 50, y + 45, '\U000F0079', 36, CYAN, anchor='mm', icon=True)
            text(x + 85, y + 25, 'Battery', 34, MUTED)
            col = batt_head(d, text, x + 30, y + 70, 72, b)
            bar(x + 30, y + 190, 435, b.get('percent'), color=col)
            text(x + 30, y + 230, b.get('status') or 'No battery gauge', 30, MUTED, max_width=435)
            continue
        text(x + 30, y + 25, label, 34, MUTED, max_width=435)
        text(x + 48, y + 118, tile_icons[i], 36, CYAN, anchor='mm', icon=True)
        text(x + 80, y + 75, value, 64, bold=True, max_width=395)
        bar(x + 30, y + 190, 435, pct, color=CYAN if i != 1 else '#4b5b70')   # memory: used + cached (grey), used (cyan) on top
        if i == 1 and mem.get('used_percent'):
            d.rounded_rectangle(((x + 30) * P, (y + 190) * P, (x + 30 + 435 * min(100, mem['used_percent']) / 100) * P, (y + 206) * P), 5, fill=CYAN)
        text(x + 30, y + 230, sub, 30, MUTED, max_width=435)

    wifi, services = data.get('wifi') or {}, dict(data.get('services') or {}, web={'up': not stale and web_state().get('WEB') != 'off'}, audio={'up': audio_state()['AUDIO'] != 'off'})
    x0, y0, x1, y1 = OV_NET
    card(x0, y0, x1 - x0, y1 - y0)
    text(78, 1232, '\U000F05A9', 34, CYAN, anchor='mm', icon=True)
    text(110, 1215, 'Wi-Fi', 30, MUTED)
    text(60, 1255, wifi.get('ssid') or ('Disconnected' if wifi.get('connected') is False else 'unknown'), 56, bold=True, max_width=700)
    text(60, 1340, wifi.get('ip_address'), 38, MUTED, max_width=700)
    text(1020, 1230, fmt(wifi.get('signal_percent'), suffix='%'), 64, CYAN, bold=True, anchor='ra')
    text(1020, 1325, f"{wifi.get('rate_mbps') or ''}", 30, MUTED, anchor='ra', max_width=260)
    for i, (key, label) in enumerate(r for r in NET_ROWS if r[0] not in ('web', 'audio')):
        up = (services.get(key) or {}).get('up')
        if key == 'tailscale':
            up = ts_state(services.get(key) or {}) == ('Connected', True)
        if key == 'wireguard':
            _, up = wg_state(services.get(key) or {})
        color = GREEN if up is True else AMBER if up is False else MUTED
        x, y, right, bottom = ov_service_box(i)
        card(x, y, right - x, bottom - y, fill=BG, outline=color)
        text(x + 36, 1480, NET_ICONS[key], 32, color, anchor='mm', icon=True)
        text(x + 116, 1480, {'tailscale': 'Tail', 'wireguard': 'WG', 'bluetooth': 'BT'}.get(key, label.split()[0]), 28, color, bold=True, anchor='mm', max_width=100)

    items = containers_of(data)
    run = sum(i.get('state') == 'running' for i in items)
    bad = any('unhealthy' in (i.get('status') or '') for i in items)
    col = MUTED if not items else '#ff6b6b' if bad else GREEN if run == len(items) else AMBER
    x0, y0, x1, y1 = OV_DOCK
    card(x0, y0, x1 - x0, y1 - y0, outline=col if items else LINE)
    text(80, 1645, '\U000F0868', 34, col, anchor='mm', icon=True)
    text(110, 1628, 'Docker', 34, MUTED)
    text(1020, 1620, f'{run}/{len(items)} running' + (' · unhealthy' if bad else '') if items else 'none', 44, col, bold=True, anchor='ra')

    procs = sorted(data.get('processes') or [], key=lambda p: p.get('cpu_percent') or 0, reverse=True)
    x0, y0, x1, y1 = OV_PROC
    card(x0, y0, x1 - x0, y1 - y0)
    text(80, 1762, '\U000F0279', 34, CYAN, anchor='mm', icon=True)
    text(110, 1745, 'Processes', 34, MUTED)
    text(1020, 1740, str(len(procs)) if procs else '—', 48, bold=True, anchor='ra')
    for i, pr in enumerate(procs[:4]):
        y = 1800 + i * 56
        text(60, y, pr.get('name') or '?', 34, max_width=600)
        text(820, y + 2, fmt(pr.get('cpu_percent'), 0, '%'), 32, anchor='ra')
        text(1020, y + 2, fmt_mem(pr.get('rss_bytes')), 32, MUTED, anchor='ra')
    return image


def procs_of(data, sort='cpu_percent'):
    return sorted(data.get('processes') or [],
                  key=lambda p: (p.get(sort) or 0, p.get('cpu_percent') or 0, p.get('rss_bytes') or 0), reverse=True)[:TOP_PROCS]


def max_scroll(data, sort='cpu_percent'):
    return max(0, len(procs_of(data, sort)) * ROW_H - (LIST_BOTTOM - LIST_TOP))


def page_processes(data, stale, scroll, sort='cpu_percent'):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    rows = procs_of(data, sort)
    if not rows:
        text(40, 200, 'No process data', 40, MUTED)
    for i, r in enumerate(rows):
        y = LIST_TOP + i * ROW_H - scroll
        if y + ROW_H < LIST_TOP or y > LIST_BOTTOM:
            continue
        cpu = r.get('cpu_percent')
        card(30, y, 1020, ROW_H - 14)
        text(60, y + 10, r.get('name'), 38, bold=True, max_width=700)
        text(1020, y + 10, fmt(cpu, 0, '%'), 38, AMBER if (cpu or 0) >= 80 else CYAN if (cpu or 0) >= 30 else TEXT, bold=True, anchor='ra')
        text(60, y + 62, f"{r.get('pid')} · {r.get('user') or '?'} · {fmt_bytes(r.get('rss_bytes'))}", 28, MUTED, max_width=700)
        bar(820, y + 74, 200, cpu, 12)   # full bar = one whole core
    total, span = len(rows) * ROW_H, LIST_BOTTOM - LIST_TOP
    if total > span:
        top = LIST_TOP + span * scroll / total
        d.rounded_rectangle((1066 * P, top * P, 1074 * P, (top + span * span / total) * P), 3, fill=MUTED)
    header(image, 'Processes', stale)
    for key, label, box in PROC_SORT_BTNS:
        btn(text, card, box, label, CYAN if sort == key else MUTED, 36)
    text(40, 108, f"{'Offline · ' if stale else ''}100% = 1 core of {(data.get('cpu') or {}).get('logical_cores') or '?'}", 25, MUTED)
    return image


def sysfs(path, default='—'):
    try:
        return Path(path).read_text().strip() or default
    except OSError:
        return default


def ts_state(ts):
    """(label, up) for the Tailscale row; up None = tailscaled not answering."""
    st = ts.get('state')
    return ({'Running': 'Connected', 'NeedsLogin': 'Logged out', 'Stopped': 'Stopped'}.get(st, st or 'Unknown'),
            ts.get('up') if st else None)


def net_detail(key, data):
    """(title, [(label, value)], hint) for a Network subpage."""
    wifi, svc = data.get('wifi') or {}, data.get('services') or {}
    up = (svc.get(key) or {}).get('up')
    onoff = 'On' if up else 'Off' if up is False else 'Unknown'
    ip = (wifi.get('ip_address') or '').split('/')[0] or '—'
    if key == 'wifi':
        i = wifi.get('interface') or 'wlan0'
        return 'Wi-Fi', [('Network', wifi.get('ssid') or '—'), ('IP address', ip), ('Signal', fmt(wifi.get('signal_percent'), suffix='%')),
                         ('Link rate', wifi.get('rate_mbps') or '—'), ('Interface', i), ('MAC', sysfs(f'/sys/class/net/{i}/address'))], \
            'Change network: wifi-setup SSID PASSWORD (Terminal tab)'
    if key == 'ssh':
        return 'SSH', [('Server', 'Up' if up else 'Down' if up is False else 'Unknown'), ('Port', '22 (dropbear)'), ('Wi-Fi', ip),
                       ('USB', '172.16.43.1')], 'Firewall: home LAN, USB and tailnet only'
    if key == 'tailscale':
        ts = svc.get('tailscale') or {}
        label, _ = ts_state(ts)
        return 'Tailscale', [('State', label), ('Tailnet IP', ts.get('ip') or '—'), ('Hostname', ts.get('name') or '—'),
                             ('Peers online', f"{ts.get('online', 0)} of {ts.get('peers', 0)}" if ts else '—')], \
            'Log in once: sudo tailscale up' if ts.get('state') == 'NeedsLogin' else ''
    if key == 'wireguard':
        wg = svc.get('wireguard') or {}
        state, _ = wg_state(wg)
        handshake = wg.get('latest_handshake')
        age = f'{max(0, int(time.time() - handshake))} s ago' if isinstance(handshake, (int, float)) and handshake > 0 else 'Never / unknown'
        return 'WireGuard', [('State', state), ('Profile', wg.get('profile') or '—'),
                             ('Service', wg.get('service') or '—'), ('Peers', str(wg.get('peers')) if wg.get('peers') is not None else '—'),
                             ('Latest handshake', age), ('Autostart', 'On' if wg.get('autostart') is True else 'Off' if wg.get('autostart') is False else 'Unknown')], \
            'Configure /etc/s9/wireguard and the VPN profile as root.' if not wg.get('configured') else 'Up = interface active. Check handshake for peer connectivity.'
    if key == 'usb':
        return 'USB', [('Link', sysfs('/sys/class/net/ncm0/operstate')), ('Gadget', sysfs(next(Path('/sys/class/udc').glob('*/state'), '/nonexistent'))), ('Phone IP', '172.16.43.1'), ('Host IP', '172.16.43.2 (DHCP)'),
            ('Serial', '/dev/ttyGS0')], 'Network + serial gadget. Set up at boot by usb-net'
    if key == 'bluetooth':
        return 'Bluetooth', [('Power', onoff), ('Address', (svc.get('bluetooth') or {}).get('address') or '—')], ''
    return 'Web dashboard', [('Status', onoff), ('Wi-Fi', f"http://{ip}:8090"), ('Tailnet', 'port 8090')], ''


def page_netsub(key, data, stale, busy=False, toast=None):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, '', stale, right=' ')
    btn(text, card, BACK_BTN, '< Back', CYAN, 40)
    title, rows, hint = net_detail(key, data)
    text(300, 40, title, 52, bold=True, max_width=740)
    card(30, 170, 1020, 40 + len(rows) * 100)
    for i, (k, v) in enumerate(rows):
        text(60, 215 + i * 100, k, 34, MUTED)
        text(1020, 210 + i * 100, v, 42, anchor='ra', max_width=620)
    text(60, 240 + len(rows) * 100, hint, 30, MUTED, max_width=960)
    if key == 'wireguard':
        wg = (data.get('services') or {}).get('wireguard') or {}
        enabled = wg.get('configured') is True and not stale and not busy
        btn(text, card, WG_START, 'Start', GREEN if enabled else MUTED, 44)
        btn(text, card, WG_STOP, 'Stop', AMBER if enabled else MUTED, 44)
        btn(text, card, WG_AUTO, 'Autostart: ' + ('On' if wg.get('autostart') else 'Off'), CYAN if enabled else MUTED, 42)
        text(540, 1330, toast or '', 32, AMBER, anchor='mm', max_width=960)
    return image


def page_network(data, stale):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, 'Network', stale)
    wifi, services = data.get('wifi') or {}, dict(data.get('services') or {}, web={'up': not stale and web_state().get('WEB') != 'off'}, audio={'up': audio_state()['AUDIO'] != 'off'})
    card(30, 170, 1020, 520)
    text(85, 215, '\U000F05A9', 48, CYAN, anchor='mm', icon=True)
    text(125, 195, 'Wi-Fi', 34, MUTED)
    text(60, 250, wifi.get('ssid') or ('Disconnected' if wifi.get('connected') is False else 'unknown'), 64, bold=True, max_width=960)
    text(60, 350, wifi.get('ip_address'), 44, max_width=960)
    sig = wifi.get('signal_percent')
    text(60, 440, f"Signal {fmt(sig, suffix='%')}", 36, MUTED)
    bar(60, 500, 960, sig)
    text(60, 555, f"{wifi.get('interface') or '—'} · {wifi.get('rate_mbps') or '—'}", 34, MUTED, max_width=960)
    text(1030, 190, '›', 52, MUTED, anchor='ra')
    for i, (key, label) in enumerate(NET_ROWS):
        up = (services.get(key) or {}).get('up')
        y = net_box(i)[1]
        card(30, y, 1020, 135)
        state = ('On' if up else 'Off') if key in ('bluetooth', 'web', 'audio') else 'Up' if up else 'Down'
        state = 'Unknown' if up is None else state
        if key == 'tailscale':
            state, up = ts_state(services.get(key) or {})
        if key == 'wireguard':
            state, up = wg_state(services.get(key) or {})
        color = GREEN if up is True else AMBER if up is False else MUTED
        text(85, y + 67, NET_ICONS[key], 48, color, anchor='mm', icon=True)
        text(140, y + 35, label, 52, bold=True, max_width=600)
        text(980, y + 35, state, 48, color, bold=True, anchor='ra')
        text(1030, y + 33, '›', 52, MUTED, anchor='ra')
    text(40, 1810, f"Dashboard: {DASH_URL.replace('127.0.0.1', (wifi.get('ip_address') or 'phone').split('/')[0])}", 34, MUTED, max_width=1000)
    return image


def page_thermals(data, stale, refresh=5):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, 'Thermals & perf', stale, right=' ')
    btn(text, card, PERF_BTN, 'Limits >', CYAN, 40)
    btn(text, card, REFRESH_BTN, f'    {refresh} s', CYAN, 40)
    text(630, 75, '\U000F0450', 40, CYAN, anchor='mm', icon=True)   # mdi-refresh
    card(30, 160, 1020, 340)
    draw_cores(d, text, bar, data)
    gpu = data.get('gpu') or {}
    card(30, 520, 1020, 190)
    text(60, 540, f"GPU · {gpu.get('model') or 'n/a'}", 34, MUTED, max_width=900)
    text(60, 595, fmt(gpu.get('utilization_percent'), 0, '%'), 56, bold=True)
    text(1020, 560, f"{fmt(gpu.get('clock_mhz'), 0, ' MHz')} / {fmt(gpu.get('max_clock_mhz'), 0, ' MHz')}", 34, anchor='ra')
    draw_gpu_cells(d, text, data, 668, False)
    text(1020, 620, f"{gpu.get('governor') or '—'} · {fmt(gpu.get('temp_c'), 0, ' °C')}", 30, MUTED, anchor='ra')
    cpu, mem = data.get('cpu') or {}, data.get('memory') or {}
    card(30, 730, 1020, 300)
    for i, (label, pct, value) in enumerate((('CPU', cpu.get('percent'), fmt(cpu.get('percent'), 0, '%')),
                                             ('RAM', (mem.get('used_percent') or 0) + (mem.get('cached_percent') or 0),
                                              f"{gib(mem.get('free_bytes'))} free · {gib(mem.get('cached_bytes'))} cache"))):
        y = 750 + i * 60
        text(60, y, label, 32, MUTED, bold=True)
        bar(160, y + 14, 440, pct, 18, '#4b5b70' if i else AMBER if (pct or 0) >= 80 else CYAN)
        if i and mem.get('used_percent'):   # RAM like Overview: used (cyan) over used + cached (grey); the rest is truly free
            bar(160, y + 14, 440 * min(100, mem['used_percent']) / 100, 100, 18, CYAN)
        text(1020, y, value, 30, bold=True, anchor='ra', max_width=400)
    for i, pr in enumerate(procs_of(data)[:3]):
        y = 880 + i * 48
        text(60, y, pr.get('name') or '?', 30, max_width=600)
        text(820, y, fmt(pr.get('cpu_percent'), 0, '%'), 30, anchor='ra')
        text(1020, y, fmt_mem(pr.get('rss_bytes')), 30, MUTED, anchor='ra')
    card(*TG_BOX[:2], TG_BOX[2] - TG_BOX[0], TG_BOX[3] - TG_BOX[1])
    text(60, 1985, 'Thermal guard', 40, bold=True, anchor='lm')
    text(1020, 1985, tg_running() and 'On  ›' or 'Stopped  ›', 40, GREEN if tg_running() else AMBER, bold=True, anchor='rm')
    for i, (name, value, status) in enumerate(thermal_readouts()[:11]):
        y = 1050 + i * 76
        card(30, y, 1020, 66)
        text(60, y + 12, name, 32, MUTED, max_width=650)
        text(1020, y + 10, f'{value:.1f} °C' if value is not None else status, 34, TEXT if value is not None else AMBER,
             bold=value is not None, anchor='ra')
    return image


def page_power(data, stale):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, 'Battery & power', stale)
    b = data.get('battery') or {}
    card(30, 170, 1020, 1400)
    col = batt_head(d, text, 60, 195, 125, b)
    text(60, 345, b.get('status') or 'No battery gauge', 36, MUTED, max_width=900)
    bar(60, 405, 960, b.get('percent'), h=26, color=col)
    sm = store_mode()
    rows = (('Voltage', fmt(b.get('voltage_v'), 3, ' V')), ('Current', fmt(b.get('current_ma'), 0, ' mA')),
            ('Temperature', fmt(b.get('temperature_c'), 1, ' °C')), ('Health', b.get('health') or '—'),
            ('Plugged', str(b.get('plugged') or '—')))
    for i, (k, v) in enumerate(rows):
        text(60, 500 + i * 85, k, 34, MUTED)
        text(1020, 495 + i * 85, v, 40, anchor='ra', max_width=500)
    wireless_text, wireless_color = wireless_label()
    btn(text, card, WIRELESS_BTN, wireless_text, wireless_color, 34)
    want = store_want()
    btn(text, card, STORE_BTN, 'Charge cap (stand mode): ' + store_label(want, sm), GREEN if want else AMBER, 40)
    text(540, 1360, 'Tap to toggle. ON holds the charge near 70%. OFF applies after a reboot', 28, MUTED, anchor='ma', max_width=960)
    btn(text, card, TERM_OPTIONS_BTN, 'Terminal options', CYAN, 40)
    fr = freeze_reboot()
    switch_card(d, text, card, FREEZE_BTN, '\U000F0901', 'Reboot on freeze (untested)',
                'on a kernel lockup: panic, reboot after 10 s' if fr is not None else 'unavailable', fr)
    switch_card(d, text, card, BOOST_BTN, '\U000F0241', 'Touch boost',
                'UI runs on the small cores on purpose; full clock 1 s per touch', boost_want())
    for act, box in POWER_BTNS:
        btn(text, card, box, CONFIRM_TEXT[act][0], AMBER, 48)
    return image


def page_term_options(stale):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, '', stale, right='Terminal options')
    btn(text, card, BACK_BTN, '< Back', CYAN, 36)
    text(540, 155, 'Hide known notices here; full kernel logs stay unchanged.', 26, MUTED, anchor='mm')
    conf = term_filter_config()
    for (key, label, _, default, note), box in zip(TERM_FILTERS, TERM_FILTER_BOXES):
        hidden = conf.get(key, default) == 'hide'
        btn(text, card, box, f"{label}: {'hidden' if hidden else 'shown'}", GREEN if hidden else MUTED, 31)
        text(540, box[3] - 24, note, 22, MUTED, anchor='mm', max_width=920)
    return image


def freq_text(khz):
    return '–' if khz is None else f'{khz / 1e6:.2f} GHz' if khz >= 1000000 else f'{khz // 1000} MHz'


def freq_table(key):
    """Sorted kHz steps of a cpufreq policy or the Mali dvfs_table (None if unreadable)."""
    try:
        if key == 'trip':   # BIG throttle target (m°C), stock 83 °C up to 100
            return list(range(83000, 100001, 1000))
        if key == 'quad':   # 4-core big cap: big-cluster steps from the stock cap up
            return [v for v in freq_table('policy4') if v >= int(OC_KNOBS[0][2])] or None
        src = MALI / 'dvfs_table' if key == 'gpu' else CPUFREQ / key / 'scaling_available_frequencies'
        return sorted(int(v) for v in src.read_text().split()) or None
    except TypeError:
        return None
    except (OSError, ValueError):
        return None


def freq_get(key, which):
    """Current kHz max/min limit; the GPU lock reads -1 (or "-1 / ..") when released, i.e. the table end."""
    try:
        if key in ('quad', 'trip'):
            return int((OC_ROOT / OC_KNOBS[0 if key == 'quad' else 2][0]).read_text()) if which == 'max' else None
        if key == 'gpu':
            v = int((MALI / f'dvfs_{which}_lock').read_text().split()[0])
            return v if v > 0 else (freq_table(key) or [None])[-1 if which == 'max' else 0]
        return int((CPUFREQ / key / f'scaling_{which}_freq').read_text())
    except (OSError, ValueError, IndexError):
        return None


def perf_text(key, v):
    return f'{v // 1000} °C' if key == 'trip' and v else freq_text(v)


def freq_set(key, which, khz):
    if key == 'trip':   # overclock only; kept for power/overclock.start, thermal-guard's shutdown stays 5 °C above
        (OC_ROOT / OC_KNOBS[2][0]).write_text(str(khz))
        OC_FILE.with_name('overclock-trip').write_text(f'{khz}\n')
        if tg_conf()['soc_max'] < khz // 1000 + 5:
            tg_set('soc_max', khz // 1000 + 5)
        return
    if key == 'quad':   # overclock only; the file keeps the cap for power/overclock.start
        (OC_ROOT / OC_KNOBS[0][0]).write_text(str(khz))
        OC_FILE.write_text(f'{khz}\n')
        return
    path = MALI / f'dvfs_{which}_lock' if key == 'gpu' else CPUFREQ / key / f'scaling_{which}_freq'
    path.write_text(str(khz))


def perf_step(key, which, sign, to=None):
    """Move one cpufreq/GPU limit a table step, keep min <= max, re-read and report what the kernel really took."""
    label = next(f'{g} {l}' for k, w, g, l in PERF_ROWS if (k, w) == (key, which))
    if key in ('quad', 'trip') and not overclock():
        return f'Turn on Overclock (below) to change {label}'
    table, cur = freq_table(key), freq_get(key, which)
    if not table or cur is None:
        return f'{label}: unavailable'
    new = step_list(table, cur, sign) if to is None else to
    if new == cur:
        return f'{label}: {perf_text(key, cur)}' + (' (end of range)' if to is None else '')
    other = 'min' if which == 'max' else 'max'
    oc = freq_get(key, other)
    try:
        if oc is not None and (new < oc if which == 'max' else new > oc):
            freq_set(key, other, new)
        freq_set(key, which, new)
    except OSError as e:
        return f'{label}: {e.strerror or e}'
    real = freq_get(key, which)
    return f'{label}: {perf_text(key, real)}' + (f' (asked {perf_text(key, new)}, kernel limit)' if real != new else '')


def perf_auto():
    """Release every limit: cpufreq min/max back to the hardware range, GPU locks off, Samsung max limit -1."""
    failed = []
    for pol in dict.fromkeys(k for k, *_ in PERF_ROWS if k not in ('gpu', 'quad', 'trip')):
        for which in ('max', 'min'):
            try:
                freq_set(pol, which, int((CPUFREQ / pol / f'cpuinfo_{which}_freq').read_text()))
            except (OSError, ValueError):
                failed.append(pol)
    table = freq_table('gpu')
    for which, i in (('max', -1), ('min', 0)):
        try:
            freq_set('gpu', which, table[i])     # the table end releases the lock
        except (OSError, TypeError):
            failed.append('gpu')
    try:
        SAMSUNG_MAX_LIMIT.write_text('-1')
    except OSError:
        pass
    if failed:
        return 'Off: could not reset ' + ', '.join(dict.fromkeys(failed))
    return 'Off: no limits set, kernel decides'


def step_list(values, cur, sign):
    """Next value in a sorted list from cur in direction sign (clamped)."""
    i = max((i for i, v in enumerate(values) if v <= cur), default=0)
    return values[max(0, min(len(values) - 1, i + sign))]


def tg_conf():
    try:
        c = json.loads(TG_CONF.read_text())
    except (OSError, ValueError):
        c = {}
    c = c if isinstance(c, dict) else {}
    return {k: max(lo, min(hi, int(c[k]))) if isinstance(c.get(k), (int, float)) and not isinstance(c.get(k), bool) else dflt
            for k, _, _, _, lo, hi, dflt in TG_LIMITS}


def tg_step(i, sign):
    k, _, _, step, lo, hi, _ = TG_LIMITS[i]
    tg_set(k, max(lo, min(hi, tg_conf()[k] + sign * step)))


def tg_set(k, v):
    c = dict(tg_conf(), **{k: v})
    TG_CONF.parent.mkdir(parents=True, exist_ok=True)
    tmp = TG_CONF.with_suffix('.tmp')
    tmp.write_text(json.dumps(c))
    os.replace(tmp, TG_CONF)


TG_RUN = [0.0, False]


def tg_running():
    if time.monotonic() - TG_RUN[0] > 5:
        TG_RUN[:] = [time.monotonic(), sh('rc-service', 'thermal-guard', 'status', timeout=3)[0]]
    return TG_RUN[1]


def tg_btns(i):
    y = 200 + i * 300
    return (60, y + 100, 260, y + 220), (820, y + 100, 1020, y + 220)


def page_guard(stale, toast):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, '', stale, right=' ')
    btn(text, card, BACK_BTN, '< Back', CYAN, 40)
    text(300, 40, 'Thermal guard', 52, bold=True, max_width=480)
    c = tg_conf()
    for i, (k, label, unit, _, _, _, dflt) in enumerate(TG_LIMITS[:TG_PAGE_ROWS]):
        y = 200 + i * 300
        card(30, y, 1020, 260)
        text(60, y + 25, label, 34, MUTED, max_width=700)
        if c[k] != dflt:
            text(1020, y + 25, f'default {dflt}{unit}', 28, MUTED, anchor='ra')
        for box, sym in zip(tg_btns(i), '−+'):
            card(box[0], box[1], box[2] - box[0], box[3] - box[1], fill=BG, outline=CYAN)
            text((box[0] + box[2]) // 2, (box[1] + box[3]) // 2, sym, 72, CYAN, bold=True, anchor='mm')
        text(540, y + 160, f'{c[k]}{unit}', 80, bold=True, anchor='mm')
    up = tg_running()
    card(30, 1400, 1020, 130)
    text(60, 1420, 'Running' if up else 'Stopped', 44, GREEN if up else AMBER, bold=True)
    try:
        st = TG_STATUS.read_text().strip()
    except OSError:
        st = 'no status yet'
    text(60, 1480, st, 32, MUTED, mono=True, max_width=960)
    text(60, 1560, toast or 'Powers the phone off when a sensor stays at or', 32, AMBER if toast else MUTED, max_width=960)
    if not toast:
        text(60, 1605, 'above its limit for the whole hold time.', 32, MUTED)
    return image


def perf_layout():
    """Grouped limit cards: [(group, top, height)] and each PERF_ROWS row's top y."""
    groups, rows, y = [], [], PERF_Y
    for g in dict.fromkeys(r[2] for r in PERF_ROWS):
        n = sum(r[2] == g for r in PERF_ROWS)
        groups.append((g, y, 64 + n * PERF_H + 6))
        rows += [y + 64 + j * PERF_H for j in range(n)]
        y += 64 + n * PERF_H + 6 + 22
    return groups, rows


def perf_btns(i):
    y = perf_layout()[1][i]
    return (720, y + 12, 850, y + 106), (880, y + 12, 1010, y + 106)    # -, +


def page_perf(data, stale, toast, trip_open=False):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, '', stale, right=' ')
    btn(text, card, BACK_BTN, '< Back', CYAN, 40)
    text(300, 40, 'CPU & GPU limits', 52, bold=True, max_width=480)
    btn(text, card, PERF_BTN, 'Off', GREEN, 40)
    card(30, 160, 1020, 340)
    draw_cores(d, text, bar, data)
    groups, rows = perf_layout()
    oc = overclock()
    for g, top, h in groups:
        title, accent = PERF_GROUPS[g]
        card(30, top, 1020, h)
        d.rounded_rectangle((30 * P, (top + 20) * P, 38 * P, (top + 50) * P), 4, fill=accent)
        text(60, top + 16, title, 34, accent, bold=True)
    for i, (key, which, g, label) in enumerate(PERF_ROWS):
        y, accent = rows[i], PERF_GROUPS[g][1]
        live = key not in ('quad', 'trip') or oc
        d.line((60 * P, y * P, 1010 * P, y * P), fill=LINE, width=1)
        text(60, y + 14, label.capitalize(), 28, MUTED)
        cur, table = freq_get(key, which), freq_table(key)
        text(60, y + 46, perf_text(key, cur), 50, TEXT if live else MUTED, bold=True)
        if table and cur is not None and len(table) > 1:     # where the limit sits in the available range
            pos = 100 * sum(v <= cur for v in table[1:]) / (len(table) - 1)
            bar(360, y + 64, 320, pos, 10, accent if live else LINE)
            text(360, y + 26, perf_text(key, table[0]), 22, MUTED)
            text(680, y + 26, perf_text(key, table[-1]), 22, MUTED, anchor='ra')
        if live and key == 'trip' and not trip_open:     # locked: tap, confirm, then 10 s to edit
            box = perf_btns(i)[0][:2] + perf_btns(i)[1][2:]
            card(box[0], box[1], box[2] - box[0], box[3] - box[1], fill=BG, outline=accent)
            text(box[0] + 60, (box[1] + box[3]) // 2, '\U000F033E', 50, accent, anchor='mm', icon=True)    # md-lock
            text(box[0] + 175, (box[1] + box[3]) // 2, 'Unlock', 34, accent, bold=True, anchor='mm')
            continue
        if not live:    # disabled row: say why where the buttons would be
            box = perf_btns(i)[0][:2] + perf_btns(i)[1][2:]
            card(box[0], box[1], box[2] - box[0], box[3] - box[1], fill=BG, outline=AMBER)
            for dy, word in ((-21, 'Needs'), (21, 'Overclock')):
                text((box[0] + box[2]) // 2, (box[1] + box[3]) // 2 + dy, word, 34, AMBER, bold=True, anchor='mm')
            continue
        for box, sym in zip(perf_btns(i), ('−', '+')):
            card(box[0], box[1], box[2] - box[0], box[3] - box[1], fill=BG, outline=accent)
            text((box[0] + box[2]) // 2, (box[1] + box[3]) // 2, sym, 60, accent, bold=True, anchor='mm')
    try:    # Samsung's table tops out at the 4-core max, so reading its top means "no cap"
        cap = int(SAMSUNG_MAX_LIMIT.read_text())
        cap = cap if cap < max(int(v) for v in Path(SAMSUNG_MAX_LIMIT.parent, 'cpufreq_table').read_text().split()) else None
    except (OSError, ValueError):
        cap = None
    if cap:
        text(60, 512, f'Samsung cpufreq_max_limit also caps clocks at {freq_text(cap)}', 26, MUTED, max_width=960)
    else:   # exynos_hpgov maxfreq_table (DT hotplug_governor dual/quad_freq): offlines idle big cores to boost busy ones
        quad = freq_get('quad', 'max')
        text(60, 512, f'Big max follows busy cores: 2.70 GHz (1), 2.31 (2), {freq_text(quad or 1794000)[:-4]} (3-4)', 26, MUTED, max_width=960)
    y = groups[-1][1] + groups[-1][2] + 16
    text(60, y, toast or 'Shown = what the kernel accepted. Limits reset at reboot.', 28, AMBER if toast else MUTED, max_width=960)
    x0, y0, x1, y1 = OC_BTN
    card(x0, y0, x1 - x0, y1 - y0, fill=PANEL, outline=RED if oc else LINE)
    text(85, y0 + 80, '\U000F0026', 46, RED if oc else MUTED, anchor='mm', icon=True)
    text(125, y0 + 25, 'Overclock (at your own risk)', 36, RED if oc else TEXT, bold=oc)
    text(125, y0 + 85, f'4-core cap {freq_text(freq_get("quad", "max"))} (up to 2.70), {perf_text("trip", freq_get("trip", "max"))}. Kept' if oc else
         'Unlocks the 4-core cap up to 2.70 GHz, throttle 88 °C', 26, MUTED, max_width=730)
    d.rounded_rectangle((880 * P, (y0 + 45) * P, 1010 * P, (y0 + 105) * P), 30 * P, fill=RED if oc else LINE)
    d.ellipse(((950 if oc else 885) * P, (y0 + 50) * P, (1005 if oc else 940) * P, (y0 + 100) * P), fill=TEXT)
    return image


def containers_of(data):
    return (data.get('containers') or {}).get('containers') or []


def container_actions(state):
    return [('pause', 'Pause'), ('stop', 'Stop')] if state == 'running' else \
        [('unpause', 'Unpause')] if state == 'paused' else [('start', 'Start')]


def boxes(n):
    return [ACT_W] if n == 1 else [ACT_L, ACT_R]


def docker(*args, timeout=6):
    return subprocess.run([DOCKER, *args], capture_output=True, text=True, timeout=timeout, env=DOCKER_ENV)


def container_detail(name):
    """inspect + one stats sample + last log lines, from this (root) service; the actions go through the dashboard."""
    out = {'name': name}
    try:
        insp = json.loads(docker('inspect', name).stdout)[0]
        st = insp.get('State') or {}
        out.update(state=st.get('Status'), health=(st.get('Health') or {}).get('Status'),
                   restarts=insp.get('RestartCount'), image=(insp.get('Config') or {}).get('Image'))
        if st.get('Running') and st.get('StartedAt'):
            secs = int((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(st['StartedAt'][:19]).replace(tzinfo=dt.timezone.utc)).total_seconds())
            out['uptime'] = (f'{secs // 86400}d {secs % 86400 // 3600}h' if secs >= 86400 else
                             f'{secs // 3600}h {secs % 3600 // 60}m' if secs >= 3600 else f'{secs // 60}m {secs % 60}s')
        else:
            out['uptime'] = f"exited ({st.get('ExitCode')})"
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError):
        pass
    try:
        s = json.loads(docker('stats', '--no-stream', '--format', '{{json .}}', name, timeout=8).stdout)
        out.update(cpu=float(s['CPUPerc'].rstrip('%')), mem=s['MemUsage'].split(' / ')[0], mem_percent=float(s['MemPerc'].rstrip('%')))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        pass
    try:
        r = docker('logs', '--tail', '12', name)
        lines = []
        for raw in (r.stdout + r.stderr).splitlines()[-12:]:
            lines += textwrap.wrap(SECRET_RE.sub(r'\1***', ANSI_RE.sub('', raw)), 58) or ['']
        out['logs'] = lines[-18:]
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def docker_restart(name):
    try:
        r = docker('restart', '-t', '10', name, timeout=40)
        return (True, f'Restarted {name}') if r.returncode == 0 else (False, f'Restart failed: {r.stderr.strip()[:80]}')
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f'Restart failed: {str(exc)[:80]}'


def docker_do(act, name):
    """(ok, message): restart runs here as root; pause/unpause/stop/start go through the dashboard (the login user)."""
    if act == 'restart':
        return docker_restart(name)
    req = urllib.request.Request(DASH_URL + f'/api/docker/{act}', json.dumps({'name': name}).encode(),
                                 {'Content-Type': 'application/json', 'Origin': DASH_URL})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = json.load(r)
            return body.get('ok', False), str(body.get('message', 'done'))[:80]
    except urllib.error.HTTPError as exc:
        try:
            return False, json.load(exc).get('message', 'failed')[:80]
        except ValueError:
            return False, f'HTTP {exc.code}'
    except (OSError, ValueError) as exc:
        return False, str(exc)[:80]


def dock_scroll(data):
    return max(0, len(containers_of(data)) * PROW_H - (LIST_BOTTOM - LIST_TOP))


def page_docker(data, stale, scroll):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    c = data.get('containers') or {}
    items = containers_of(data)
    if not items:
        text(40, 200, 'No containers' if c.get('available') else 'Status unavailable', 40, MUTED)
    for i, it in enumerate(items):
        y = LIST_TOP + i * PROW_H - scroll
        if y + PROW_H < LIST_TOP or y > LIST_BOTTOM:
            continue
        card(30, y, 1020, PROW_H - 14)
        text(60, y + 22, it.get('name') or 'Unnamed', 46, bold=True, max_width=640)
        text(1020, y + 28, it.get('state') or 'Unknown', 38, GREEN if it.get('state') == 'running' else AMBER, anchor='ra')
        text(60, y + 82, f"{it.get('image') or ''} · {it.get('status') or ''}", 30, MUTED, max_width=960)
    total, span = len(items) * PROW_H, LIST_BOTTOM - LIST_TOP
    if total > span:
        top = LIST_TOP + span * scroll / total
        d.rounded_rectangle((1066 * P, top * P, 1074 * P, (top + span * span / total) * P), 3, fill=MUTED)
    header(image, 'Containers', stale, f"{sum(i.get('state') == 'running' for i in items)} / {len(items)} running" if items else None)
    return image


def page_docker_detail(name, info, data, toast):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, '', False, right=' ')
    btn(text, card, BACK_BTN, '< Back', CYAN, 40)
    text(300, 40, name, 52, bold=True, max_width=740)
    base = next((c for c in containers_of(data) if c.get('name') == name), {})
    info = info or {}
    state = info.get('state') or base.get('state') or '…'
    text(40, 150, state, 64, GREEN if state == 'running' else AMBER, bold=True)
    if info.get('health'):
        text(1040, 170, info['health'], 40, MUTED, anchor='ra')
    card(30, 250, 1020, 640)
    for i, (k, v) in enumerate((('Uptime', info.get('uptime') or base.get('status') or '…'),
                                ('Image', info.get('image') or base.get('image') or '…'),
                                ('Restarts', info.get('restarts') if info.get('restarts') is not None else '…'))):
        text(60, 280 + i * 75, k, 34, MUTED)
        text(1020, 275 + i * 75, v, 40, anchor='ra', max_width=760)
    text(60, 515, 'CPU', 34, MUTED)
    text(1020, 510, fmt(info.get('cpu'), 1, '%'), 40, anchor='ra')
    bar(60, 570, 960, info.get('cpu'))
    text(60, 625, 'Memory', 34, MUTED)
    text(1020, 620, info.get('mem') or '—', 40, anchor='ra')
    bar(60, 680, 960, info.get('mem_percent'))
    text(60, 745, 'Updated every 5 s' if info else 'Loading…', 28, MUTED)
    card(30, 910, 1020, 920)
    text(60, 930, 'Last log lines', 34, MUTED)
    y = 990
    for line in (info.get('logs') or ['…' if not info else '(no output)'])[-18:]:
        text(60, y, line, 27, TEXT, mono=True)
        y += 44
    text(540, 1860, toast or 'Hold anywhere to restart', 30, AMBER if toast else MUTED, anchor='mm', max_width=1000)
    acts = container_actions(state)
    for (act, label), box in zip(acts, boxes(len(acts))):
        card(box[0], box[1], box[2] - box[0], box[3] - box[1], fill=AMBER, outline=AMBER)
        text((box[0] + box[2]) // 2, (box[1] + box[3]) // 2, label, 48, BG, bold=True, anchor='mm')
    return image


# ---- Wi-Fi / SSH backends (wpa_cli + wifi-ip, dropbear via OpenRC); all run on worker threads, as root ---------------------
WIFI_IF, IPCONF, WIFI_IP = 'wlan0', Path('/etc/s9/wifi-ip'), '/usr/local/bin/wifi-ip'


def sh(*cmd, timeout=30, stdin=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:80]
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def wpa(*args, timeout=10):
    return sh('wpa_cli', '-i', WIFI_IF, *args, timeout=timeout)


def wpa_status():
    return dict(l.split('=', 1) for l in wpa('status')[1].splitlines() if '=' in l)


def parse_networks(out):
    """wpa_cli list_networks -> saved networks [{id, ssid, active}], active first."""
    rows = []
    for line in out.splitlines()[1:]:
        f = line.split('\t')
        if len(f) >= 2 and f[0].isdigit():
            rows.append({'id': f[0], 'ssid': f[1], 'active': '[CURRENT]' in line})
    return sorted(rows, key=lambda r: (not r['active'], r['ssid'].lower()))


def parse_scan(out, saved):
    """wpa_cli scan_results (bssid, freq, dBm, flags, ssid) -> networks, strongest per SSID, active then saved first."""
    ids, active, nets = {r['ssid']: r['id'] for r in saved}, {r['ssid'] for r in saved if r['active']}, {}
    for line in out.splitlines()[1:]:
        f = line.split('\t')
        if len(f) < 5 or not f[4]:
            continue
        sig = max(0, min(100, 2 * (int(f[2]) + 100)))
        if f[4] not in nets or sig > nets[f[4]]['signal']:
            nets[f[4]] = {'ssid': f[4], 'signal': sig, 'secure': 'WPA' in f[3] or 'WEP' in f[3], 'active': f[4] in active,
                          'id': ids.get(f[4]), 'known': f[4] in ids}
    return sorted(nets.values(), key=lambda n: (not n['active'], not n['known'], -n['signal']))


def wifi_scan():
    wpa('scan')
    time.sleep(4)
    return parse_scan(wpa('scan_results')[1], parse_networks(wpa('list_networks')[1]))


def default_gw():
    m = re.search(r'default via (\S+)', sh('ip', 'route', 'show', 'default', 'dev', WIFI_IF)[1])
    return m and m.group(1)


def wait_gateway(secs):
    """True once the default gateway answers a ping, polling for up to secs."""
    end = time.monotonic() + secs
    while True:
        gw = default_gw()
        if gw and sh('ping', '-c', '1', '-W', '2', gw, timeout=5)[0]:
            return True
        if time.monotonic() > end:
            return False
        time.sleep(2)


def wifi_connect(ssid, pw, nid):
    """Join ssid (saved id nid, or a new network); a failure puts the previous network back."""
    old = next((r['id'] for r in parse_networks(wpa('list_networks')[1]) if r['active']), None)
    new = nid is None
    if new:
        nid = wpa('add_network')[1].splitlines()[-1]
        wpa('set_network', nid, 'ssid', f'"{ssid}"')
    if pw:      # ponytail: psk goes via argv, visible in ps for a moment; wpa_cli stdin interactive mode if other users get shells
        wpa('set_network', nid, 'psk', f'"{pw}"')
    elif new:
        wpa('set_network', nid, 'key_mgmt', 'NONE')
    wpa('select_network', nid)
    end, ok = time.monotonic() + 25, False
    while not ok and time.monotonic() < end:
        time.sleep(1)
        st = wpa_status()
        ok = st.get('wpa_state') == 'COMPLETED' and st.get('ssid') == ssid
    if ok:
        sh(WIFI_IP, 'apply', timeout=60)
        ok = wait_gateway(25)
    if ok:
        wpa('set', 'update_config', '1')
        wpa('save_config')
        return True, f'Connected to {ssid}'
    if new:
        wpa('remove_network', nid)
    if old is not None:
        wpa('select_network', old)
    wpa('enable_network', 'all')
    sh(WIFI_IP, 'apply', timeout=60)
    return False, f'Could not join {ssid}: back on the old network'


def wifi_forget(nid, name):
    wpa('remove_network', nid)
    wpa('set', 'update_config', '1')
    wpa('save_config')
    return True, f'Forgot {name}', parse_networks(wpa('list_networks')[1])


def ip_conf():
    """/etc/s9/wifi-ip -> dict (METHOD, SSID, ADDR, GW, DNS); empty without the file."""
    try:
        return dict(p.split('=', 1) for p in shlex.split(IPCONF.read_text(), comments=True) if '=' in p)
    except (OSError, ValueError):
        return {}


def ip_validate(method, addr, gw, dns):
    """(addr, gw, [dns]) normalised for the IP & DNS page; raises ValueError with a short message."""
    servers = [x for x in dns.replace(' ', ',').split(',') if x]
    for x in servers:
        try:
            ipaddress.ip_address(x)
        except ValueError:
            raise ValueError(f'Bad DNS server: {x}')
    if method == 'auto':
        return '', '', servers
    try:
        net = ipaddress.ip_interface(addr if '/' in addr else addr + '/24')
    except ValueError:
        raise ValueError(f'Bad address: {addr or "empty"}')
    if net.version != 4:
        raise ValueError('Address must be IPv4')
    if gw:
        try:
            g = ipaddress.ip_address(gw)
        except ValueError:
            raise ValueError(f'Bad gateway: {gw}')
        if g not in net.network:
            raise ValueError(f'Gateway {gw} not in {net.network}')
    return str(net), gw, servers


def ip_conf_text(method, ssid, addr, gw, servers):
    return ''.join(f'{k}={shlex.quote(v)}\n' for k, v in (('METHOD', 'dhcp' if method == 'auto' else 'static'), ('SSID', ssid),
                                                          ('ADDR', addr), ('GW', gw), ('DNS', ' '.join(servers))))


def ip_apply(method, addr, gw, dns):
    """Write /etc/s9/wifi-ip, apply it, wait up to 60 s for the gateway; otherwise restore the old config."""
    try:
        addr, gw, servers = ip_validate(method, addr, gw, dns)
    except ValueError as exc:
        return False, str(exc), None
    try:
        old = IPCONF.read_text()
    except OSError:
        old = None

    def write(text):
        IPCONF.parent.mkdir(parents=True, exist_ok=True)
        tmp = IPCONF.with_suffix('.new')
        tmp.write_text(text or '')
        os.replace(tmp, IPCONF)

    write(ip_conf_text(method, wpa_status().get('ssid', ''), addr, gw, servers))
    sh(WIFI_IP, 'apply', timeout=60)
    if wait_gateway(60):
        return True, 'IP settings saved', wifi_info()
    if old is None:
        IPCONF.unlink(missing_ok=True)
    else:
        write(old)
    sh(WIFI_IP, 'apply', timeout=60)
    return False, 'Gateway not reachable: reverted', wifi_info()


def wifi_info():
    st, c = wpa_status(), ip_conf()
    sig = dict(l.split('=', 1) for l in wpa('signal_poll')[1].splitlines() if '=' in l)
    addr = re.search(r'inet (\S+)', sh('ip', '-4', 'addr', 'show', WIFI_IF)[1])
    try:
        dns = re.findall(r'^nameserver (\S+)', Path('/etc/resolv.conf').read_text(), re.M)
        mac = Path('/sys/class/net', WIFI_IF, 'address').read_text().strip()
    except OSError:
        dns, mac = [], '—'
    freq = int(st.get('freq') or 0)
    return {'ssid': st.get('ssid'), 'state': st.get('wpa_state'), 'ip': addr and addr.group(1), 'gw': default_gw(), 'dns': dns,
            'static': c.get('METHOD') == 'static', 'conf': c, 'mac': mac, 'rssi': sig.get('RSSI'), 'rate': sig.get('LINKSPEED'),
            'band': f"{'5' if freq > 4000 else '2.4'} GHz" if freq else '—', 'sec': st.get('key_mgmt') or 'Open'}


USER_APPLY = '/usr/local/bin/user-apply'
USER_RE = re.compile(r'^[a-z_][a-z0-9_-]{0,31}$')


def login_user():
    """The uid 10000 account (pmOS default user)."""
    return next((l.split(':')[0] for l in open('/etc/passwd') if l.split(':')[2:3] == ['10000']), 'user')


def check_username(new):
    """Error text for a bad new username, else None."""
    if not USER_RE.match(new):
        return 'Use a-z, 0-9, _ and -, max 32, no leading digit'
    if any(l.split(':')[0] == new for l in open('/etc/passwd')):
        return f'{new} already exists'


def user_rename(old, new):
    ok, msg = sh(USER_APPLY, old, new)
    return ok, f'Renamed to {new}' if ok else (msg.splitlines() or ['failed'])[-1][:80], new


def login_users():
    rows = [l.split(':') for l in open('/etc/passwd')]
    return [r[0] for r in rows if 1000 <= int(r[2]) < 65534 and not r[6].strip().endswith(('nologin', 'false'))] or ['root']


def set_password(user, password):
    ok, msg = sh('chpasswd', stdin=f'{user}:{password}\n')     # never logged
    return ok, f'Password set for {user}' if ok else (msg.splitlines() or ['failed'])[-1][:80]


def ssh_set(on):
    """dropbear on/off now and at boot (OpenRC); running sessions stay."""
    sh('rc-update', 'add' if on else 'del', 'dropbear', 'default')
    ok, msg = sh('rc-service', 'dropbear', 'start' if on else 'stop')
    return ok, f"SSH {'on' if on else 'off'}" if ok else (msg.splitlines() or ['failed'])[-1][:80]


def wifi_saved():
    return parse_networks(wpa('list_networks')[1])


BT_CONF, WEB_CONF = Path('/etc/s9/bluetooth'), Path('/etc/s9/web')
BT_APPLY, WEB_APPLY = '/usr/local/bin/bt-apply', '/usr/local/bin/web-apply'
AUDIO_CONF, S9_AUDIO, S9_CHIME = Path('/etc/s9/audio'), '/usr/local/bin/s9-audio', '/usr/local/bin/s9-chime'
USB_CONF, USB_NET = Path('/etc/s9/usb'), '/usr/local/sbin/usb-net'
USB_MODES = (('network', 'Network', 'NCM + serial, 172.16.43.1 (default)'), ('rndis', 'Windows network', 'RNDIS + serial, same IP'),
             ('charge', 'Charge only', 'USB gadget off, charging unaffected'))
WEB_PATHS = (('WEB', 'Web dashboard'), ('LAN', 'Wi-Fi (LAN)'), ('TAILSCALE', 'Tailscale'), ('USB', 'USB'))


def kv_read(path, defaults):
    try:
        return dict(defaults, **dict(p.split('=', 1) for p in shlex.split(path.read_text(), comments=True) if '=' in p))
    except (OSError, ValueError):
        return dict(defaults)


def knox_hidden():
    return term_filter_config()['KNOX'] == 'hide'


def term_filter_config():
    return kv_read(TERM_CONF, TERM_FILTER_DEFAULTS)


def term_message_hidden(msg, conf=None):
    conf = term_filter_config() if conf is None else conf
    return any(conf.get(key, default) == 'hide' and any(re.search(pattern, msg) for pattern in patterns)
               for key, _, patterns, default, _ in TERM_FILTERS)


def kv_write(path, conf):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.new')
    tmp.write_text(''.join(f'{k}={shlex.quote(v)}\n' for k, v in conf.items()))
    os.replace(tmp, path)


def bt_state():
    out = sh('bluetoothctl', 'show')[1]
    f = {k: v for k, v in (l.strip().split(': ', 1) for l in out.splitlines() if ': ' in l)}
    return {'on': f.get('Powered') == 'yes', 'vis': f.get('Discoverable') == 'yes', 'alias': f.get('Alias', ''),
            'addr': (re.search(r'Controller (\S+)', out) or [0, ''])[1]}


def bt_set(**kw):
    """bt_set(POWER='on', VISIBLE='off', ALIAS='x'): persist to /etc/s9/bluetooth and apply."""
    kv_write(BT_CONF, dict(kv_read(BT_CONF, {'POWER': 'on', 'VISIBLE': 'off', 'ALIAS': ''}), **kw))
    ok, msg = sh(BT_APPLY, timeout=60)
    return ok, 'Bluetooth updated' if ok else msg[-80:], bt_state()


def usb_state():
    try:
        built = Path('/run/usb-net.built').read_text().strip()      # written by usb-net up
    except OSError:
        built = 'none'
    mode = kv_read(USB_CONF, {'MODE': 'network'})['MODE']
    # ncm must never be re-linked, so a built function other than the wanted one needs a reboot
    return {'mode': mode, 'pending': {'network': 'ncm', 'rndis': 'rndis'}.get(mode, built) not in (built, 'none')}


def usb_set(mode):
    kv_write(USB_CONF, {'MODE': mode})
    ok, msg = sh(USB_NET, 'apply')
    st = usb_state()
    return True, 'Applies after reboot' if st['pending'] else 'Saved' if ok else msg[-80:], st


def web_state():
    return kv_read(WEB_CONF, {'WEB': 'on', 'LAN': 'on', 'TAILSCALE': 'on', 'USB': 'on'})


def web_set(key, on):
    kv_write(WEB_CONF, dict(web_state(), **{key: 'on' if on else 'off'}))
    ok, msg = sh(WEB_APPLY)
    return ok, 'Saved' if ok else msg[-80:], web_state()


def audio_state():
    return kv_read(AUDIO_CONF, {'AUDIO': 'on', 'OUT': 'speaker', 'VOL': '60', 'THERMAL': 'on'})


def audio_set(**kw):
    kv_write(AUDIO_CONF, dict(audio_state(), **kw))
    ok, msg = sh(S9_AUDIO, 'apply', timeout=15)
    return ok, 'Saved' if ok else msg[-80:], audio_state()


def audio_test():
    ok, _ = sh(S9_CHIME, 'test', timeout=15)
    return ok, 'Played' if ok else 'No sound: audio off or busy', audio_state()


def parts_of(data):
    return (data.get('storage') or {}).get('partitions') or []


def parts_scroll(data):
    return max(0, len(parts_of(data)) * PROW_H - (LIST_BOTTOM - LIST_TOP))


def list_dir(path):
    """Folders first, then files (ported from the Mi A3 explorer, read-only); stat follows symlinks, capped at FILES_MAX."""
    ents = []
    with os.scandir(path) as it:
        for e in it:
            try:
                st, isdir = e.stat(), e.is_dir()
            except OSError:
                st, isdir = None, False
            ents.append({'name': e.name, 'dir': isdir, 'size': st and st.st_size, 'mtime': st and st.st_mtime})
            if len(ents) >= FILES_MAX:
                break
    return sorted(ents, key=lambda e: (not e['dir'], e['name'].lower()))


def file_view(path):
    """Read-only preview: {'img': Image} or {'lines': [...]}."""
    if not os.path.isfile(path) or path.startswith(('/proc/', '/sys/', '/dev/')):
        return {'lines': ['No preview for kernel or device files']}
    try:
        with Image.open(path) as im:
            im.thumbnail((1020, 1900))
            return {'img': im.convert('RGB')}
    except Exception:
        pass
    try:
        with open(path, 'rb') as f:
            raw = f.read(FILE_PEEK)
    except OSError as exc:
        return {'lines': [exc.strerror or 'Cannot read']}
    if b'\0' in raw:
        return {'lines': [f'Binary file, {os.path.getsize(path):,} bytes']}
    lines = raw.decode('utf-8', 'replace').expandtabs(4).splitlines() or ['(empty)']
    return {'lines': lines + (['…'] if len(raw) == FILE_PEEK else [])}


def files_scroll(w):
    v = w['view']
    total = v['img'].height if v and 'img' in v else len(v['lines']) * FILE_LINE if v else len(w['ents']) * FILE_ROW
    return max(0, total - (TAB_Y - FILE_TOP) + 40)


def page_files(w, scroll):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    v, path = w['view'], w['path']
    header(image, '', False, right=' ')
    btn(text, card, BACK_BTN, '< Back', CYAN, 40)
    text(300, 50, os.path.basename(v['name']) if v else 'Folders', 52, bold=True, max_width=740)
    text(40, 146, ('…' + path[-43:]) if len(path) > 44 else path, 32, MUTED, mono=True)
    clip = Image.new('RGB', (IMG[0], round((TAB_Y - FILE_TOP) * P)), BG)
    cd, ctext, ccard, _ = tools(clip)
    if v and 'img' in v:
        clip.paste(v['img'].resize((round(v['img'].width * P), round(v['img'].height * P))), (round(30 * P), -round(scroll * P)))
    elif v:
        first = scroll // FILE_LINE
        for n, line in enumerate(v['lines'][first:first + (TAB_Y - FILE_TOP) // FILE_LINE + 2], first):
            ctext(40, n * FILE_LINE - scroll + 4, line or ' ', 28, mono=True, max_width=1010)
    elif not w['ents']:
        ctext(540, 300, 'Empty folder', 48, MUTED, anchor='mm')
    else:
        first = scroll // FILE_ROW
        for n, e in enumerate(w['ents'][first:first + (TAB_Y - FILE_TOP) // FILE_ROW + 2], first):
            y = n * FILE_ROW - scroll
            ccard(30, y, 1020, FILE_ROW - 12, outline=LINE)
            ctext(60, y + 6, e['name'] + ('/' if e['dir'] else ''), 40, CYAN if e['dir'] else TEXT, max_width=700)
            ctext(60, y + 54, dt.datetime.fromtimestamp(e['mtime']).strftime('%Y-%m-%d %H:%M') if e['mtime'] else '', 26, MUTED)
            ctext(1020, y + 22, '›' if e['dir'] else fmt_bytes(e['size']), 36, MUTED, anchor='ra')
    image.paste(clip, (0, round(FILE_TOP * P)))
    return image


def page_partitions(data, stale, scroll):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    rows = parts_of(data)
    if not rows:
        text(40, 200, 'No partition data', 40, MUTED)
    for i, r in enumerate(rows):
        y = LIST_TOP + i * PROW_H - scroll
        if y + PROW_H < LIST_TOP or y > LIST_BOTTOM:
            continue
        mp, pct = r.get('mountpoint'), r.get('used_percent')
        card(30, y, 1020, PROW_H - 14, outline=LINE if mp else BG)
        text(60, y + 10, f"{r.get('name')} · {r.get('label')}" if r.get('label') else r.get('name'), 34, bold=True, max_width=520)
        text(60, y + 62, f"{mp} · {r.get('fstype')}" if mp else 'not mounted', 28, CYAN if mp else MUTED, max_width=520)
        text(1020, y + 10, fmt_bytes(r.get('size_bytes')) if (r.get('size_bytes') or 0) >= 1 << 20 else f"{(r.get('size_bytes') or 0) >> 10} KiB",
             34, anchor='ra')
        if mp and pct is not None:
            text(1020, y + 62, f"{fmt_bytes(r.get('used_bytes'))} · {pct:.0f}%", 28, AMBER if pct >= 90 else MUTED, anchor='ra')
            bar(60, y + 112, 960, pct, 10, AMBER if pct >= 90 else CYAN)
    total, span = len(rows) * PROW_H, LIST_BOTTOM - LIST_TOP
    if total > span:
        top = LIST_TOP + span * scroll / total
        d.rounded_rectangle((1066 * P, top * P, 1074 * P, (top + span * span / total) * P), 3, fill=MUTED)
    header(image, '', stale, right=' ')
    btn(text, card, BACK_BTN, '< Back', CYAN, 40)
    btn(text, card, FILES_BTN, '\U000F024B', CYAN, 56)
    text(300, 50, 'Partitions', 52, bold=True)
    return image


def term_color(name, default):
    if name == 'default':
        return default
    if name in TERM_PAL:
        return TERM_PAL[name]
    return '#' + name if len(name) == 6 else default


def kb_keys(mode):
    """[(key, box)] for the terminal keyboard; drawing and hit testing share it."""
    keys = []
    for i, k in enumerate(('esc', 'tab', 'ctrl', 'left', 'up', 'down', 'right')):
        keys.append((k, (i * 154, KB_TOP, (i + 1) * 154 if i < 6 else 1080, KB_TOP + KEY_H)))
    rows = ('1234567890',) + KB_ROWS[mode][:2] + (KB_ROWS[mode][2],)
    for r, row in enumerate(rows):
        y = KB_TOP + (r + 1) * KEY_H
        x0 = 162 if r == 3 else (1080 - KEY_W * len(row)) // 2
        keys += [(c, (x0 + i * KEY_W, y, x0 + (i + 1) * KEY_W, y + KEY_H)) for i, c in enumerate(row)]
    y = KB_TOP + 4 * KEY_H
    keys += [('shift', (0, y, 162, y + KEY_H)), ('back', (918, y, 1080, y + KEY_H))]
    y += KEY_H
    return keys + [('mode', (0, y, 216, y + KEY_H)), ('space', (216, y, 864, y + KEY_H)), ('enter', (864, y, 1080, y + KEY_H))]


def kb_key_at(mode, x, y):
    """Key under (x, y). In a row's side gaps (the 9-key row is centred) and out on the curved edges: the row's nearest key."""
    row = [(k, b) for k, b in kb_keys(mode) if b[1] <= y <= b[3]]
    return min(row, key=lambda kb: max(kb[1][0] - x, x - kb[1][2], 0), default=(None,))[0]


def key_bytes(key, ctrl=False):
    """On-screen key -> bytes for the pty (pyte ignores DECCKM, so arrows are always CSI)."""
    if key in ARROWS:
        return f"\x1b[{ARROWS[key]}".encode()
    if ctrl and len(key) == 1 and key.isalpha():
        return bytes([ord(key.lower()) - 96])
    if ctrl and key in '[\\]':
        return bytes([ord(key) - 64])
    return KEY_SEQ.get(key, key).encode()


def uinput_bytes(code, held):
    """buffyboard key code + held modifier codes -> bytes for the pty."""
    if code in USEQ:
        s = USEQ[code]
    elif code in UKEYS:
        s = UKEYS[code][bool(held & SHIFTS)]
        if held & CTRLS and (s.isalpha() or s in '@[\\]^_'):
            s = chr(ord(s.upper()) & 0x1f)
    else:
        return b''
    return (('\x1b' if held & ALTS else '') + s).encode()


def cut(box, hole):
    """Parts of box (x0, y0, x1, y1) outside hole: buffyboard owns that rectangle of the framebuffer."""
    if not hole:
        return [box]
    x0, y0, x1, y1 = box
    hx0, hy0, hx1, hy1 = hole
    if x1 <= hx0 or x0 >= hx1 or y1 <= hy0 or y0 >= hy1:
        return [box]
    my0, my1 = max(y0, hy0), min(y1, hy1)
    parts = [(x0, y0, x1, hy0), (x0, hy1, x1, y1), (x0, my0, hx0, my1), (hx1, my0, x1, my1)]
    return [b for b in parts if b[0] < b[2] and b[1] < b[3]]


def kb_height():
    return TAB_Y - (KB_TOP - 6)


def rotated_kb_height():
    return round(W * 0.45)          # a 45% screen-width strip leaves room for a useful landscape terminal


def term_cell_width():
    return font(round(TERM_FONT * P), mono=True).getlength('M') / P


def term_size(rotated=False, keyboard=True, osk=False):
    """Terminal grid for the usable viewport; rotated mode lays out a landscape canvas before turning it clockwise."""
    if rotated:
        cols = (TAB_Y - TERM_TOP - 2 * TERM_X) // term_cell_width()
        bottom = W - rotated_kb_height() if keyboard else W - TERM_X
        rows = (bottom - TERM_X) // TERM_CH
    else:
        cols = (W - 2 * TERM_X) // term_cell_width()
        bottom = (OSK_TOP if osk else KB_TOP) - 6 if keyboard else TAB_Y
        rows = (bottom - TERM_TOP) // TERM_CH
    return max(1, int(cols)), max(1, rows)


def keyboard_touch(x, y, rotated=False):
    """Map physical touch coordinates to the existing portrait keyboard key grid."""
    if not rotated:
        return x, y
    source_x = (y - TERM_TOP) * W / (TAB_Y - TERM_TOP)
    source_y = W - x
    compressed_y = source_y - (W - rotated_kb_height())
    return source_x, KB_TOP - 6 + compressed_y * kb_height() / rotated_kb_height()


def terminal_output_touch(x, y, rotated=False, keyboard=True):
    """True only when a gesture starts over terminal output, never over controls, nav, or keyboard."""
    if rotated:
        right = W - TERM_X
        left = rotated_kb_height() if keyboard else TERM_X
        return left <= x <= right and TERM_TOP + TERM_X <= y <= TAB_Y - TERM_X
    bottom = KB_TOP - 6 if keyboard else TAB_Y
    return TERM_X <= x <= W - TERM_X and TERM_TOP <= y <= bottom


def scroll_terminal_lines(term, count):
    """Move terminal history by rows, leaving volume-key page size unchanged."""
    if not term or not count:
        return False
    screen = term.screen
    position, ratio = screen.history.position, screen.history.ratio
    screen.history = screen.history._replace(ratio=1 / screen.lines)
    try:
        for _ in range(abs(count)):
            before = screen.history.position
            screen.next_page() if count > 0 else screen.prev_page()
            if screen.history.position == before:
                break
    finally:
        screen.history = screen.history._replace(ratio=ratio)
    return screen.history.position != position


class Term:
    """pty.fork()ed login session rendered through pyte; output is read from the main select loop."""

    def __init__(self, cmd=None, cols=TERM_COLS, rows=TERM_ROWS):
        self.cmd, self.cols, self.rows = cmd, cols, rows
        self.screen = pyte.HistoryScreen(cols, rows, history=1000)   # volume keys page through it; any output jumps back to the bottom
        self.screen.write_process_input = lambda data: self.send(data.encode())
        self.stream = pyte.ByteStream(self.screen)
        self.pid = self.fd = None
        self.respawn_at, self.wrote = 0.0, False
        self.kfd = None

    def open_kmsg(self):
        """Echo new kernel warnings/errors into the screen, like a VT console (display only, the shell never sees them)."""
        try:
            self.kfd = os.open('/dev/kmsg', os.O_RDONLY | os.O_NONBLOCK)
            os.lseek(self.kfd, 0, os.SEEK_END)
        except OSError as exc:
            logging.warning('no /dev/kmsg: %s', exc)

    def read_kmsg(self):
        changed = False
        while True:
            try:
                rec = os.read(self.kfd, 8192).decode(errors='replace')
            except BlockingIOError:
                return changed
            except BrokenPipeError:          # ring buffer overran us: next read resumes at the oldest record
                continue
            head, _, msg = rec.partition(';')
            try:
                level = int(head.split(',')[0]) & 7
            except ValueError:
                continue
            msg = msg.split('\n')[0]
            if level <= 4 and not term_message_hidden(msg):   # emerg..warning; filters affect only this Terminal copy
                self.stream.feed(f"\r\n\x1b[{'31' if level <= 3 else '33'}m{msg}\x1b[0m\r\n".encode())
                changed = True

    def spawn(self):
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(TERM='linux', HOME=os.environ.get('HOME', '/root'))
            try:
                cmd = self.cmd or ('/bin/login', '-f', login_user())      # the account may have been renamed since startup
                os.execv(cmd[0], list(cmd))
            finally:
                os._exit(127)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('4H', self.rows, self.cols, 0, 0))
        os.set_blocking(fd, False)
        self.pid, self.fd = pid, fd
        self.screen.reset()

    def resize(self, cols, rows):
        """Resize both pyte's screen and the pty so programs receive SIGWINCH and use the same grid."""
        cols, rows = max(1, int(cols)), max(1, int(rows))
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        self.screen.resize(rows, cols)
        if self.fd is not None:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('4H', rows, cols, 0, 0))

    def read(self):
        """Feed pending pty output to pyte; True when the screen changed. Reaps the child on EOF."""
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return False
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b''
        if data:
            self.stream.feed(data)
            return True
        if os.waitpid(self.pid, os.WNOHANG)[0] == 0:
            return False                  # EIO before the child opened the slave side (or exiting): not over yet
        os.close(self.fd)
        self.pid = self.fd = None
        self.respawn_at = time.monotonic() + 1.0
        logging.info('terminal session ended, respawning')
        return False

    def poll(self, now):
        """Respawn a finished session after a short pause (also throttles a crash loop)."""
        if self.fd is None and now >= self.respawn_at:
            self.spawn()
            return True
        return False

    def send(self, data):
        self.wrote = True
        if self.fd is not None:
            try:
                os.write(self.fd, data)
            except OSError:
                pass

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
            self.pid = self.fd = None


KB_CACHE = {}


def keyboard_image(kb_mode, ctrl, pressed, rotated=False):
    key = (kb_mode, ctrl, pressed, P, rotated)
    if key not in KB_CACHE:             # cache each keyboard state and geometry
        oy = KB_TOP - 6
        width, height = ((TAB_Y - TERM_TOP, rotated_kb_height()) if rotated else (W, kb_height()))
        sx, sy = width / W, height / kb_height()
        kb = Image.new('RGB', (round(width * P), round(height * P)), PANEL)
        kd, ktext, _, _ = tools(kb)
        on = {'ctrl': ctrl, 'shift': kb_mode in ('ABC', 'sym2'), pressed: True}
        for k, (x0, y0, x1, y1) in kb_keys(kb_mode):
            x0, x1, y0, y1 = x0 * sx, x1 * sx, (y0 - oy) * sy, (y1 - oy) * sy
            kd.rounded_rectangle((x0 * P + 3, y0 * P + 3, x1 * P - 3, y1 * P - 3), 8, fill=CYAN if on.get(k) else LINE)
            label = KB_LABELS.get(k) or ('abc' if k == 'mode' and kb_mode.startswith('sym') else '?123' if k == 'mode' else k)
            size = min(32 if len(label) > 1 else 46,
                       round((y1 - y0) * (0.4 if len(label) > 1 else 0.56))) if rotated else (32 if len(label) > 1 else 46)
            ktext((x0 + x1) // 2, (y0 + y1) // 2, label, size, BG if on.get(k) else TEXT, anchor='mm')
        KB_CACHE[key] = kb
    return KB_CACHE[key]


def paste_kb(image, kb_mode, ctrl, pressed, y=None):
    image.paste(keyboard_image(kb_mode, ctrl, pressed), (0, round((KB_TOP - 6 if y is None else y) * P)))


def is_pua(ch):
    if not ch:
        return False
    n = ord(ch[0])
    return 0xE000 <= n <= 0xF8FF or 0xF0000 <= n <= 0xFFFFD or 0x100000 <= n <= 0x10FFFD


TERM_ROWS_CACHE = {}    # drawn terminal lines by content: FreeType text is most of a frame, a keystroke changes ~1 line


def term_row(row, cols, width, left, cw, ch, f, fi):
    key = (tuple(sorted((x, c) for x, c in row.items() if x < cols)), width, left, cw)
    img = TERM_ROWS_CACHE.get(key)
    if img is None:
        if len(TERM_ROWS_CACHE) > 500:      # ponytail: drop-all eviction, a few redraws after a big scroll
            TERM_ROWS_CACHE.clear()
        img = TERM_ROWS_CACHE[key] = Image.new('RGB', (width, round(ch)), BG)
        d, x = ImageDraw.Draw(img), 0
        while x < min(cols, max(row) + 1 if row else 0):   # sparse buffer; ESC[n@ can leave a cell past the edge
            c = row[x]
            key, cells, start = (c.fg, c.bg, c.reverse), [], x
            while x < cols and (row[x].fg, row[x].bg, row[x].reverse) == key:
                cells.append(row[x].data or ' ')
                x += 1
            run = ''.join(cells)
            fg, bg = term_color(key[0], TEXT), term_color(key[1], BG)
            if key[2]:
                fg, bg = bg if key[1] != 'default' else BG, fg
            if bg != BG:
                d.rectangle((left + start * cw, 0, left + x * cw, ch), fill=bg)
            if run.strip():
                ymid, begin = ch / 2, 0
                for i, char in enumerate(cells):
                    if is_pua(char):
                        if begin < i:
                            d.text((left + (start + begin) * cw, ymid), ''.join(cells[begin:i]), fill=fg, font=f, anchor='lm')
                        d.text((left + (start + i) * cw, ymid), char, fill=fg, font=fi, anchor='lm')
                        begin = i + 1
                if begin < len(cells):
                    d.text((left + (start + begin) * cw, ymid), ''.join(cells[begin:]), fill=fg, font=f, anchor='lm')
    return img


def page_terminal(term, kb_mode, ctrl, pressed=None, rotated=False, keyboard=True, osk=False):
    image = Image.new('RGB', IMG, BG)
    if rotated:
        canvas = Image.new('RGB', (round((TAB_Y - TERM_TOP) * P), round(W * P)), BG)
        x0, y0 = TERM_X, TERM_X
        kb_y = W - rotated_kb_height()
    else:
        canvas = Image.new('RGB', (IMG[0], round((TAB_Y - TERM_TOP) * P)), BG)
        x0, y0 = TERM_X, 0
        kb_y = KB_TOP - 6 - TERM_TOP
    d, text, card, bar = tools(canvas)
    if term is None:
        text((TAB_Y - TERM_TOP) // 2, (W // 2) if rotated else 700 - TERM_TOP,
             'Terminal unavailable', 56, AMBER, bold=True, anchor='mm')
        text((TAB_Y - TERM_TOP) // 2, (W // 2) + 100 if rotated else 800 - TERM_TOP,
             'apk add py3-pyte, then restart s9-display', 34, MUTED, anchor='mm')
    else:
        f, cw, ch = font(round(TERM_FONT * P), mono=True), term_cell_width() * P, TERM_CH * P
        fi = font(max(8, round(cw)), mono=True, icon=True)
        left, top, scr = x0 * P, y0 * P, term.screen
        cols, buf = scr.columns, scr.buffer     # locals: pyte's Screen.__getattribute__ is slow per access
        for y in range(scr.lines):
            row = buf.get(y)
            if row:
                canvas.paste(term_row(row, cols, canvas.width, left, cw, ch, f, fi), (0, round(top + y * ch)))
        cur = scr.cursor
        if not cur.hidden and cur.y < scr.lines and cur.x < scr.columns:
            cx, cy = left + cur.x * cw, top + cur.y * ch
            d.rectangle((cx, cy, cx + cw, cy + ch), fill=CYAN)
            char = (scr.buffer.get(cur.y) or {}).get(cur.x, scr.default_char).data or ' '
            d.text((cx, cy + ch / 2), char, fill=BG, font=fi if is_pua(char) else f, anchor='lm')
    if keyboard and not osk:
        kb = keyboard_image(kb_mode, ctrl, pressed, rotated)
        canvas.paste(kb, (0, round(kb_y * P)))
    image.paste(canvas.transpose(Image.Transpose.ROTATE_270) if rotated else canvas, (0, round(TERM_TOP * P)))
    d, text, card, bar = tools(image)
    btn(text, card, TERM_ROTATE_BTN, 'Rotate 90°' if not rotated else 'Rotate 0°', CYAN, 30)
    btn(text, card, TERM_KB_BTN, 'Hide keyboard' if keyboard else 'Show keyboard', GREEN, 30)
    return image


def hit(box, x, y):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


# ---- Wi-Fi / SSH form pages -------------------------------------------------------------------
RESCAN_BTN, IPCFG_BTN, SAVED_BTN, CHANGE_BTN = (780, 25, 1050, 125), (30, 940, 1050, 1050), (30, 1070, 1050, 1180), (30, 1200, 1050, 1310)
CONNECT_BTN, METHOD_BTN, PW_CARD = (30, 1090, 1050, 1230), (30, 150, 1050, 260), (30, 150, 1050, 350)
IP_FIELDS = (('addr', 'IP address'), ('gw', 'Gateway'), ('dns', 'DNS servers'))
IP_BOXES = [(30, 290 + i * 180, 1050, 440 + i * 180) for i in range(3)]
SSH_TOGGLE, USER_BTN, SSH_PW = (700, 165, 1035, 285), (30, 330, 1050, 460), (30, 540, 1050, 670)
PICK_TOP, PICK_H, PICK_MAX = 160, 150, 12
BT_POWER, BT_VIS, BT_NAME, BT_FIELD = (30, 150, 1050, 290), (30, 310, 1050, 450), (30, 470, 1050, 610), (30, 200, 1050, 350)
WEB_BOXES = [(30, 150 + i * 180, 1050, 305 + i * 180) for i in range(4)]
FORM_TOAST_Y = {'rename': 1040, 'pw': 1040, 'ip': 1040, 'ssh': 1040, 'info': 1340, 'btname': 1040, 'bt': 820, 'web': 1090, 'usb': 1000, 'audio': 1400}
FORM_SUBS = ('wifi', 'ssh', 'bluetooth', 'web', 'usb', 'audio', 'wireguard')
AUDIO_BOXES = [(30, 150, 1050, 305), (30, 325, 1050, 480), (30, 500, 1050, 720), (30, 740, 1050, 895), (30, 915, 1050, 1135)]
AUDIO_TEST = (30, 1160, 1050, 1280)
AUDIO_SLIDER = (60, 1020)    # x span of the full-width bars; a tap anywhere in the row sets the value there


def slider_pct(x, span=AUDIO_SLIDER, step=5):
    return max(0, min(100, round((x - span[0]) * 100 / (span[1] - span[0]) / step) * step))
USB_BOXES = [(30, 150 + i * 190, 1050, 320 + i * 190) for i in range(3)]
USB_REBOOT = (30, 760, 1050, 890)


def form_kb(view):
    return view in ('pw', 'ip', 'ssh', 'btname', 'rename')


def form_head(image, title, right=None):
    header(image, '', False, right=' ')
    d, text, card, bar = tools(image)
    btn(text, card, BACK_BTN, '< Back', CYAN, 40)
    text(300, 40, title, 52, bold=True, max_width=480 if right else 740)
    if right:
        btn(text, card, RESCAN_BTN, right, CYAN, 40)
    return d, text, card, bar


def net_rows(image, text, card, rows, top=PICK_TOP):
    """Network list rows: (title, subtitle, right text, colour)."""
    for i, (t, sub, right, color) in enumerate(rows[:PICK_MAX]):
        y = top + i * PICK_H
        card(30, y, 1020, PICK_H - 15)
        text(60, y + 25, t, 48, color, bold=True, max_width=700)
        text(60, y + 90, sub, 32, MUTED, max_width=700)
        text(1020, y + 45, right, 44, color, bold=True, anchor='ra')


def field_box(text, card, box, label, value, on):
    card(box[0], box[1], box[2] - box[0], box[3] - box[1], outline=CYAN if on else LINE)
    text(box[0] + 30, box[1] + 18, label, 30, MUTED)
    text(box[0] + 30, box[1] + 70, value + ('|' if on else ''), 52, max_width=box[2] - box[0] - 60)


def page_form(w, toast, kb_mode, ctrl, pressed, data):
    image = Image.new('RGB', IMG, BG)
    up = ((data.get('services') or {}).get('ssh') or {}).get('up')
    v, info = w['view'], w.get('info') or {}
    if v == 'info':
        d, text, card, bar = form_head(image, 'Wi-Fi')
        st = info.get('state')
        card(30, 150, 1020, 760)
        rssi = info.get('rssi')
        rows = [('Network', info.get('ssid') or ('…' if not info else 'Not connected')),
                ('Signal', f"{max(0, min(100, 2 * (int(rssi) + 100)))}%  ({rssi} dBm)" if rssi else '—'),
                ('IP address', (info.get('ip') or '—')), ('IP mode', 'Static' if info.get('static') else 'DHCP'),
                ('Gateway', info.get('gw') or '—'), ('DNS', ', '.join(info.get('dns') or []) or '—'),
                ('Band / rate', f"{info.get('band', '—')} · {info.get('rate') or '—'} Mbps"), ('Security', info.get('sec') or '—'),
                ('MAC', info.get('mac') or '—')]
        for i, (k, val) in enumerate(rows):
            text(60, 188 + i * 80, k, 32, MUTED)
            text(1020, 180 + i * 80, val, 40, anchor='ra', max_width=700, color=AMBER if st and st != 'COMPLETED' and i == 0 else TEXT)
        btn(text, card, IPCFG_BTN, 'IP & DNS settings', CYAN)
        btn(text, card, SAVED_BTN, 'Saved networks', CYAN)
        btn(text, card, CHANGE_BTN, 'Change network', CYAN)
    elif v == 'pick':
        d, text, card, bar = form_head(image, 'Wi-Fi', 'Rescan')
        nets = w.get('nets')
        if nets is None:
            text(540, 400, 'Scanning…', 52, MUTED, anchor='mm')
        elif not nets:
            text(540, 400, 'No networks found', 52, AMBER, anchor='mm')
        net_rows(image, text, card, [(n['ssid'], ('Connected' if n['active'] else 'Saved' if n['known'] else 'Tap to connect') + (' · secured' if n['secure'] else ' · open'),
                                      f"{n['signal']}%", GREEN if n['active'] else TEXT) for n in nets or ()])
        if nets:
            text(540, 1980, 'Long press a network to enter a new password', 30, MUTED, anchor='mm')
    elif v == 'saved':
        d, text, card, bar = form_head(image, 'Saved networks')
        saved = w.get('saved')
        if saved is not None and not saved:
            text(540, 400, 'None', 52, MUTED, anchor='mm')
        net_rows(image, text, card, [(r['ssid'], 'Connected' if r['active'] else 'Tap to forget', '', GREEN if r['active'] else TEXT) for r in saved or ()])
        if saved:
            text(540, 1980, 'Tap a network to forget it', 30, MUTED, anchor='mm')
    elif v == 'pw':
        d, text, card, bar = form_head(image, w['sel']['ssid'])
        pw = w['pw']
        card(*PW_CARD[:2], PW_CARD[2] - PW_CARD[0], PW_CARD[3] - PW_CARD[1], outline=CYAN)
        text(60, 170, 'Password (tap to ' + ('hide)' if w['show'] else 'show)'), 30, MUTED)
        text(60, 240, (pw if w['show'] else '•' * len(pw)) + '|', 56, max_width=960)
        text(60, 400, f"{len(pw)} characters · WPA needs 8 or more" if w['sel']['secure'] else 'Open network: no password', 32, MUTED)
        btn(text, card, CONNECT_BTN, 'Connect', GREEN if len(pw) >= 8 or not w['sel']['secure'] else MUTED)
    elif v == 'ip':
        d, text, card, bar = form_head(image, 'IP & DNS')
        auto = w['method'] == 'auto'
        btn(text, card, METHOD_BTN, 'IP mode: ' + ('DHCP (automatic)' if auto else 'Static'), CYAN, 44)
        for (key, label), box in zip(IP_FIELDS, IP_BOXES):
            off = auto and key != 'dns'
            field_box(text, card, box, label + (' (automatic)' if off else ' (blank = automatic)' if key == 'dns' else ''),
                      '' if off else w[key], w['field'] == key and not off)
        text(60, 840, 'Wrong settings are reverted if the gateway', 30, MUTED)
        text(60, 885, 'stops answering within 60 seconds.', 30, MUTED)
        btn(text, card, CONNECT_BTN, 'Save and apply', GREEN)
    elif v in ('bt', 'btname'):
        d, text, card, bar = form_head(image, 'Bluetooth name' if v == 'btname' else 'Bluetooth')
        bt = w.get('bt') or {}
        if v == 'btname':
            field_box(text, card, BT_FIELD, 'Shown to other devices', w['name'], True)
            btn(text, card, CONNECT_BTN, 'Save name', GREEN if w['name'].strip() else MUTED)
        else:
            for box, label, val, color in ((BT_POWER, 'Bluetooth', 'On' if bt.get('on') else 'Off', GREEN if bt.get('on') else AMBER),
                                           (BT_VIS, 'Visible to others', 'On' if bt.get('vis') else 'Off', GREEN if bt.get('vis') else MUTED)):
                card(box[0], box[1], box[2] - box[0], box[3] - box[1], outline=color)
                text(60, (box[1] + box[3]) // 2, label, 46, anchor='lm')
                text(1020, (box[1] + box[3]) // 2, val, 52, color, bold=True, anchor='rm')
            card(BT_NAME[0], BT_NAME[1], BT_NAME[2] - BT_NAME[0], BT_NAME[3] - BT_NAME[1])
            text(60, BT_NAME[1] + 25, 'Pairing name', 30, MUTED)
            text(60, BT_NAME[1] + 75, (bt.get('alias') or '—') + '  ›', 46, CYAN, max_width=960)
            text(60, 650, f"Address {bt.get('addr') or '—'}", 32, MUTED)
            text(60, 710, 'Visible is off after every reboot unless', 30, MUTED)
            text(60, 755, 'you turned it on here.', 30, MUTED)
    elif v == 'web':
        d, text, card, bar = form_head(image, 'Web dashboard')
        cfg, svc = w.get('web') or {}, data.get('services') or {}
        ips = {'LAN': ((data.get('wifi') or {}).get('ip_address') or '').split('/')[0], 'TAILSCALE': (svc.get('tailscale') or {}).get('ip'), 'USB': '172.16.43.1'}
        master = cfg.get('WEB', 'on') == 'on'
        for box, (key, label) in zip(WEB_BOXES, WEB_PATHS):
            on = cfg.get(key, 'on') == 'on' and (master or key == 'WEB')
            card(box[0], box[1], box[2] - box[0], box[3] - box[1], outline=GREEN if on else LINE)
            text(60, box[1] + 25, label, 46, TEXT if on or key == 'WEB' else LINE, bold=True)
            sub = ('Off = unreachable from every network' if not master else 'Master switch') if key == 'WEB' else (f"http://{ips[key]}:8090" if ips[key] else 'not connected')
            text(60, box[1] + 90, sub, 34, MUTED if on else LINE, max_width=700)
            text(1020, box[1] + 70, 'On' if on else 'Off', 52, GREEN if on else AMBER if key == 'WEB' else LINE, bold=True, anchor='rm')
        text(60, 880, 'Off still allows SSH tunnels:' if not master else 'Turn all off for SSH only:', 32, MUTED)
        text(60, 930, 'ssh -L 8090:127.0.0.1:8090 user@phone', 32, MUTED, mono=True)
        text(60, 985, 'The phone UI keeps working either way.', 30, MUTED)
    elif v == 'audio':
        d, text, card, bar = form_head(image, 'Audio')
        cfg = w['audio']
        master = cfg['AUDIO'] != 'off'
        low = tg_conf()['low_pct']
        vals = ('On' if master else 'Off', 'Speaker' if cfg['OUT'] != 'headphone' else 'Headphones', cfg['VOL'] + '%',
                'On' if cfg['THERMAL'] != 'off' else 'Off', f'{low}%' if low else 'Off')
        for i, (box, label, val) in enumerate(zip(AUDIO_BOXES, ('Sound', 'Output', 'Volume', 'Heat alert', 'Low battery'), vals)):
            card(box[0], box[1], box[2] - box[0], box[3] - box[1], outline=GREEN if i == 0 and master else LINE)
            ym = (box[1] + box[3]) // 2 if i not in (2, 4) else box[1] + 60
            text(60, ym, label, 46, TEXT if master or i == 0 else LINE, bold=True, anchor='lm')
            if i in (2, 4):        # sliders under the label: volume 0..100, low-battery chime 0..50 (0 = off)
                text(1020, ym, val, 46, CYAN if master else LINE, bold=True, anchor='rm')
                bar(AUDIO_SLIDER[0], box[3] - 75, AUDIO_SLIDER[1] - AUDIO_SLIDER[0], int(cfg['VOL']) if i == 2 else low * 2, 40, CYAN if master else LINE)
            else:
                on = (master, True, True, cfg['THERMAL'] != 'off')[i]
                text(1020, ym, val, 52, (GREEN if on else AMBER) if i != 1 and master else CYAN if master else LINE, bold=True, anchor='rm')
        btn(text, card, AUDIO_TEST, 'Play test sound', CYAN if master else LINE)
        text(60, 1320, 'Heat alert chimes before the thermal guard powers off.', 30, MUTED)
    elif v == 'usb':
        d, text, card, bar = form_head(image, 'USB')
        for box, (key, label, sub) in zip(USB_BOXES, USB_MODES):
            on = w['usb']['mode'] == key
            card(box[0], box[1], box[2] - box[0], box[3] - box[1], outline=GREEN if on else LINE)
            text(60, box[1] + 30, label, 46, bold=True)
            text(60, box[1] + 100, sub, 32, MUTED, max_width=800)
            text(1020, box[1] + 85, 'On' if on else '', 52, GREEN, bold=True, anchor='rm')
        if w['usb']['pending']:
            text(540, 715, 'Applies after reboot', 38, AMBER, bold=True, anchor='mm')
            btn(text, card, USB_REBOOT, 'Reboot now', AMBER)
    elif v == 'rename':
        d, text, card, bar = form_head(image, 'Change username')
        field_box(text, card, BT_FIELD, f"New name for {w['user']}", w['newname'], True)
        text(60, 400, 'a-z, 0-9, _ and -, up to 32 characters', 30, MUTED)
        btn(text, card, CONNECT_BTN, 'Rename', GREEN if w['newname'] else MUTED)
    else:       # ssh
        d, text, card, bar = form_head(image, 'SSH & User')
        card(30, 150, 990, 150)
        on = up
        text(60, 175, 'Server', 32, MUTED)
        text(60, 225, 'Running' if on else 'Stopped' if on is False else 'Unknown', 56, GREEN if on else AMBER, bold=True)
        btn(text, card, SSH_TOGGLE, 'Turn off' if on else 'Turn on', AMBER if on else GREEN, 44)
        btn(text, card, USER_BTN, f"Username: {w['user']}  ›", CYAN, 42)
        text(60, 490, f"New password for {w['user']}", 32, MUTED)
        pw = w['pw']
        field_box(text, card, SSH_PW, 'Tap to ' + ('hide' if w['show'] else 'show'), (pw if w['show'] else '•' * len(pw)), True)
        text(60, 700, f"{len(pw)} characters · 6 or more", 32, MUTED)
        btn(text, card, CONNECT_BTN, 'Set password', GREEN if len(pw) >= 6 else MUTED)
    if toast:
        text(540, FORM_TOAST_Y.get(v, 2010) if v in FORM_TOAST_Y else 2010, toast, 38, AMBER, bold=True, anchor='mm', max_width=1000)
    if form_kb(v):
        paste_kb(image, kb_mode, ctrl, pressed)
    return image


class Touch:
    """Multitouch protocol-B events (slot 0 only) -> tap / long / swipe / scroll gestures."""

    def __init__(self, xmax, ymax):
        self.sx, self.sy = W / (xmax + 1), H / (ymax + 1)
        self.slot = 0
        self.want = self.touching = self.long = False
        self.x = self.y = self.x0 = self.y0 = 0.0
        self.t0 = 0.0
        self.axis = None

    def feed(self, kind, code, value, now):
        if kind == 3:
            if code == 47:
                self.slot = value
            elif self.slot == 0:
                if code == 53:
                    self.x = value * self.sx
                elif code == 54:
                    self.y = value * self.sy
                elif code == 57:
                    self.want = value >= 0
        elif kind == 1 and code == 330:  # BTN_TOUCH
            self.want = bool(value)
        elif kind == 0 and code == 0:
            return self.sync(now)
        return []

    def sync(self, now):
        out = []
        if self.want and not self.touching:
            self.touching, self.long, self.axis = True, False, None
            self.x0, self.y0, self.t0 = self.x, self.y, now
            out.append(('down', self.x0, self.y0))
        elif not self.want and self.touching:
            self.touching = False
            dx = self.x - self.x0
            if self.axis in ('h', 'v'):
                out.append(('scrollend',))
            if self.axis == 'h' and abs(dx) >= SWIPE_PX:
                out.append(('swipe', 'left' if dx < 0 else 'right'))
            elif self.axis is None and not self.long:
                out.append(('tap', self.x0, self.y0))
            out.append(('up',))
        elif self.touching and not self.long:
            dx, dy = self.x - self.x0, self.y - self.y0
            if self.axis is None and max(abs(dx), abs(dy)) > DRAG_PX:
                self.axis = 'h' if abs(dx) > abs(dy) else 'v'
                out.append(('scrollstart',))
            if self.axis == 'v':
                out.append(('scroll', dy))
            elif self.axis == 'h':
                out.append(('scrollx', dx))
        return out

    def tick(self, now):
        if self.touching and self.axis is None and not self.long and now - self.t0 >= LONG_S:
            self.long = True
            return [('long', self.x0, self.y0)]
        return []


class Ui:
    def __init__(self):
        self.page, self.scroll, self.base = 0, 0, 0
        self.proc_sort = 'cpu_percent'
        self.confirm = None     # (act, deadline, thunk or None)
        self.action = None      # set when a confirm was accepted; main() runs it
        self.ov_cores = False
        self.parts = False      # Partitions subpage over Overview
        self.files = None       # folder explorer over Partitions: {path, ents, view, pscroll, dscroll}
        self.perf = self.tg = False       # CPU & GPU limits / Thermal guard subpages over Thermals
        self.trip_until = 0.0             # Throttle-at row editable until then (monotonic)
        self.refresh = 5        # Thermals page fetch interval, cycled by REFRESH_BTN
        self.dk, self.dinfo, self.dact = None, None, None   # Docker: open container, its detail, queued (act, name)
        self.sub = None         # Network subpage key (wifi/ssh/tailscale/usb/bluetooth/web)
        self.toast = None       # (text, deadline)
        self.term: Term | None = None        # Term session (None without pyte)
        self.kb_mode, self.ctrl = 'abc', False
        self.term_rotated, self.term_keyboard = False, True
        self.osk = False                     # buffyboard runs over the keyboard strip and types for us
        self.term_options = False
        self.term_touch_origin, self.term_scroll_active = None, False
        self.term_scroll_units, self.term_scroll_sample, self.term_scroll_velocity = 0, None, 0.0
        self.term_coast = None
        self.w = None           # Wi-Fi / SSH form state (see open_sub)
        self.job, self.busy, self.refetch = None, False, False   # job = (fn, args, cb) for main()'s worker
        self.pressed, self.fired = None, False   # key under the finger; True when touch-down already typed it
        self.press_until, self.press_off = 0.0, None   # highlight minimum end; when 'up' asked to clear it
        self.boost_min, self.boost_until = None, 0.0   # Little min to restore after a touch boost, and when

    def boost(self, now):
        """Touch-down: lift the little cores' min clock for BOOST_S (Android-style input boost).
        A key typed 0.3 s after the last one finds them back at 455 MHz and the frame takes 140 ms instead of 70.
        Skipped on the Limits page, where the user edits that min, and when switched off on the Battery page.
        The UI runs on the small cores on purpose (taskset 0x0f), so this boosts only them."""
        if self.boost_min is None and not self.perf and boost_want():
            cur, top = freq_get('policy0', 'min'), freq_get('policy0', 'max')
            if cur is None or top is None or cur >= top:
                return
            try:
                freq_set('policy0', 'min', top)
            except OSError:
                return
            self.boost_min = cur
        self.boost_until = now + BOOST_S

    def unboost(self):
        if self.boost_min is not None:
            try:
                freq_set('policy0', 'min', self.boost_min)
            except OSError:
                pass
            self.boost_min = None

    def tick(self, now):
        dirty = False
        if self.press_off is not None and now >= self.press_off:
            self.pressed, self.press_off, dirty = None, None, True
        if self.boost_min is not None and now >= self.boost_until:
            self.unboost()
        if self.confirm and now >= self.confirm[1]:
            self.confirm = None
            dirty = True
        if self.toast and now >= self.toast[1]:
            self.toast = None
            dirty = True
        if self.term_coast:
            start, last_target, velocity = self.term_coast
            elapsed = min(TERM_COAST_S, max(0.0, now - start))
            distance = velocity * (elapsed - elapsed * elapsed / (2 * TERM_COAST_S))
            target = int(max(-TERM_COAST_MAX_LINES, min(TERM_COAST_MAX_LINES, distance)))
            delta = target - last_target
            if delta and not scroll_terminal_lines(self.term, delta):
                self.term_coast = None
            else:
                self.term_coast = (start, target, velocity) if elapsed < TERM_COAST_S else None
                dirty |= bool(delta)
        return dirty

    def go(self, page):
        self.unboost()
        self.page, self.scroll, self.ov_cores, self.parts, self.perf, self.tg, self.sub = max(0, min(len(TABS) - 1, page)), 0, False, False, False, False, None
        self.files = None
        self.dk = self.dinfo = self.w = None
        self.term_options = False
        if self.page == TERM_PAGE:
            self.resize_term()

    def run(self, fn, args, cb, msg=None, secs=90):
        """Queue fn(*args) on the worker; cb(result) runs on the UI side when done."""
        if self.busy:
            self.toast = ('Busy…', time.monotonic() + 3)
            return
        self.job, self.busy = (fn, args, cb), True
        self.toast = msg and (msg, time.monotonic() + secs)

    def job_done(self, cb, res):
        self.busy = False
        if self.w is not None:
            cb(res)

    def kbd(self):
        return (self.page == TERM_PAGE and self.term_keyboard) or bool(self.w and form_kb(self.w['view']))

    def resize_term(self):
        if self.term:
            self.term.resize(*term_size(self.term_rotated, self.term_keyboard, self.osk))

    def oc_pants(self):
        self.confirm = ('oc_pants', time.monotonic() + CONFIRM_S, self.oc_on)

    def trip_open(self):
        self.trip_until = time.monotonic() + 10

    def oc_on(self):
        self.toast = (set_overclock(True), time.monotonic() + 4)

    def on_gesture(self, g, data, now):
        """True when the screen needs a redraw."""
        kind = g[0]
        if kind == 'down':
            self.boost(now)
            self.term_touch_origin = (g[1], g[2]) if self.page == TERM_PAGE and terminal_output_touch(
                g[1], g[2], self.term_rotated, self.term_keyboard) else None
            self.term_scroll_active = False
            self.term_scroll_units, self.term_scroll_sample, self.term_scroll_velocity = 0, None, 0.0
            self.term_coast = None
            if self.kbd() and not self.confirm and g[2] < TAB_Y:
                self.fired = self.key_tap(g[1], g[2])      # type on touch-down, not release
                self.press_until, self.press_off = now + PRESS_S, None
                return self.fired
        if kind == 'up':
            busy, self.fired = self.pressed or self.fired, False
            if self.pressed:
                self.press_off = max(now, self.press_until)    # tick() clears it, so the highlight gets drawn
            self.term_touch_origin, self.term_scroll_active = None, False
            return bool(busy)
        if kind == 'tap' and self.fired:
            return False
        if kind == 'tap':
            x, y = g[1], g[2]
            if self.confirm:
                cancel_btn, ok_btn = confirm_btns(self.confirm[0])
                if hit(ok_btn, x, y):
                    c, self.confirm = self.confirm, None
                    if c[2]:
                        c[2]()          # may open a follow-up confirm
                    else:
                        self.action = c[0]
                elif hit(cancel_btn, x, y):
                    self.confirm = None
                return True
            if y >= TAB_Y:
                self.go(int(x // TAB_W))
                return True
            if self.page == 4 and self.term_options:
                if hit(BACK_BTN, x, y):
                    self.term_options = False
                    return True
                conf = term_filter_config()
                for (key, _, _, default, _), box in zip(TERM_FILTERS, TERM_FILTER_BOXES):
                    if hit(box, x, y):
                        conf[key] = 'show' if conf.get(key, default) == 'hide' else 'hide'
                        kv_write(TERM_CONF, conf)
                        return True
                return False
            if self.page == TERM_PAGE:
                if hit(TERM_ROTATE_BTN, x, y):
                    self.term_rotated = not self.term_rotated
                    self.resize_term()
                    return True
                if hit(TERM_KB_BTN, x, y):
                    self.term_keyboard = not self.term_keyboard
                    self.resize_term()
                    return True
                return self.key_tap(x, y)
            if self.page == 2 and self.sub in FORM_SUBS:
                return self.on_form(x, y, data)
            if self.page == DOCK_PAGE:
                if self.dk:
                    state = (self.dinfo or {}).get('state') or next((c.get('state') for c in containers_of(data) if c.get('name') == self.dk), None)
                    acts = container_actions(state)
                    if hit(BACK_BTN, x, y):
                        self.dk = self.dinfo = None
                    for (act, _), box in zip(acts, boxes(len(acts))):
                        if hit(box, x, y):
                            self.dact = (act, self.dk)
                            self.toast = (f'{act} {self.dk}…', now + 40)
                    return True
                i = int((y - LIST_TOP + self.scroll) // PROW_H)
                if LIST_TOP <= y < LIST_BOTTOM and 0 <= i < len(containers_of(data)):
                    self.dk, self.dinfo = containers_of(data)[i]['name'], None
                    return True
                return False
            if self.page == 3:
                if self.tg:
                    if hit(BACK_BTN, x, y):
                        self.tg = False
                    for i in range(TG_PAGE_ROWS):
                        for box, sign in zip(tg_btns(i), (-1, 1)):
                            if hit(box, x, y):
                                try:
                                    tg_step(i, sign)
                                except OSError as e:
                                    self.toast = (f'Could not save: {e.strerror}', now + 4)
                elif hit(TG_BOX, x, y) and not self.perf:
                    self.tg = True
                elif self.perf and hit(BACK_BTN, x, y):
                    self.perf = False
                elif self.perf and hit(PERF_BTN, x, y):
                    self.toast = (perf_auto(), now + 4)
                elif self.perf and hit(OC_BTN, x, y):
                    if overclock():
                        self.toast = (set_overclock(False), now + 4)
                    else:       # two confirms: the second one opens from the first one's OK
                        self.confirm = ('oc_warn', now + CONFIRM_S, self.oc_pants)
                elif self.perf:
                    for i, (key, which, *_) in enumerate(PERF_ROWS):
                        if key == 'trip' and overclock():
                            if now >= self.trip_until:
                                if hit(perf_btns(i)[0][:2] + perf_btns(i)[1][2:], x, y):
                                    self.confirm = ('trip', now + CONFIRM_S, self.trip_open)
                                continue
                            if hit((340, perf_layout()[1][i], 1010, perf_layout()[1][i] + PERF_H), x, y):
                                self.trip_until = now + 10      # each change keeps it open another 10 s
                        for box, sign in zip(perf_btns(i), (-1, 1)):
                            if hit(box, x, y):
                                self.toast = (perf_step(key, which, sign), now + 4)
                        table, top = freq_table(key), perf_layout()[1][i]
                        if table and len(table) > 1 and hit((340, top + 10, 700, top + 110), x, y):    # range bar = slider
                            pick = table[round(max(0, min(1, (x - 360) / 320)) * (len(table) - 1))]
                            self.toast = (perf_step(key, which, 0, to=pick), now + 4)
                elif hit(PERF_BTN, x, y):
                    self.perf = True
                elif hit(REFRESH_BTN, x, y):
                    self.refresh = REFRESHES[(REFRESHES.index(self.refresh) + 1) % len(REFRESHES)]
                    self.refetch = True
                else:
                    return False
                return True
            if self.page == 0 and self.files:
                w = self.files
                i = int((y - FILE_TOP + self.scroll) // FILE_ROW)
                if hit(BACK_BTN, x, y):
                    if w['view']:
                        w['view'], self.scroll = None, w['dscroll']
                    elif w['path'] != '/':
                        self.open_dir(os.path.dirname(w['path']) or '/', now)
                    else:
                        self.files, self.scroll = None, w['pscroll']
                elif not w['view'] and y >= FILE_TOP and 0 <= i < len(w['ents']):
                    full = os.path.join(w['path'], w['ents'][i]['name'])
                    if w['ents'][i]['dir']:
                        self.open_dir(full, now)
                    else:
                        w.update(view={'name': full, **file_view(full)}, dscroll=self.scroll)
                        self.scroll = 0
                else:
                    return False
                return True
            if self.page == 0 and self.parts:
                rows, i = parts_of(data), int((y - LIST_TOP + self.scroll) // PROW_H)
                if hit(BACK_BTN, x, y):
                    self.parts, self.scroll = False, 0
                elif hit(FILES_BTN, x, y):
                    self.open_dir('/', now)
                elif y >= LIST_TOP and 0 <= i < len(rows) and rows[i].get('mountpoint'):
                    self.open_dir(rows[i]['mountpoint'], now)
                else:
                    return False
                return True
            if self.page == 0:
                if hit(OV_STORE, x, y):
                    self.parts, self.scroll = True, 0
                elif hit(OV_CPU, x, y):
                    self.ov_cores = not self.ov_cores
                elif hit(OV_NET, x, y):
                    wg_box = ov_service_box(2)
                    self.go(2)
                    if hit(wg_box, x, y):
                        self.open_sub('wireguard', data)
                elif hit(OV_DOCK, x, y):
                    self.go(DOCK_PAGE)
                elif hit(OV_PROC, x, y):
                    self.go(1)
                else:
                    return False
                return True
            if self.page == 1:
                for key, _, box in PROC_SORT_BTNS:
                    if hit(box, x, y):
                        self.proc_sort, self.scroll = key, 0
                        return True
            if self.page == 2:
                if self.sub and hit(BACK_BTN, x, y):
                    self.sub = None
                elif not self.sub:
                    keys = [k for k, _ in NET_ROWS]
                    self.open_sub('wifi' if hit(NET_WIFI, x, y) else next((k for i, k in enumerate(keys) if hit(net_box(i), x, y)), None), data)
                    if not self.sub:
                        return False
                else:
                    return False
                return True
            if self.page == 4:
                if hit(WIRELESS_BTN, x, y):
                    _ok, msg = wireless_toggle(not wireless_want())
                    self.toast = (msg, now + 4)
                    return True
                if hit(TERM_OPTIONS_BTN, x, y):
                    self.term_options = True
                    return True
                if hit(STORE_BTN, x, y):
                    try:
                        store_toggle(not store_want())
                    except OSError:
                        pass            # label re-reads the file and the kernel, so a refused write shows as unchanged
                    return True
                if hit(BOOST_BTN, x, y):
                    try:
                        BOOST_FILE.write_text('0\n' if boost_want() else '1\n')
                    except OSError as e:
                        self.toast = (f'Could not save: {e.strerror}', now + 4)
                    self.unboost()
                    return True
                if hit(FREEZE_BTN, x, y):
                    try:
                        set_freeze_reboot(not freeze_reboot())
                        self.toast = ('Reboot on freeze ' + ('on' if freeze_reboot() else 'off'), now + 3)
                    except OSError as e:
                        self.toast = (f'Could not save: {e.strerror}', now + 4)
                    return True
                for act, box in POWER_BTNS:
                    if hit(box, x, y):
                        self.confirm = (act, now + CONFIRM_S, None)
                        return True
            return False
        if self.confirm:
            return False
        if kind == 'long' and self.page == 2 and self.sub == 'wifi' and self.w['view'] == 'pick':
            n = self.pick_at(g[2])
            if n:
                self.w.update(view='pw', sel=n, pw='', show=False, field='pw')
            return bool(n)
        if kind == 'long' and self.page == DOCK_PAGE and self.dk:
            self.dact, self.toast = ('restart', self.dk), (f'restart {self.dk}…', now + 40)
            return True
        if kind == 'swipe':
            if self.page == TERM_PAGE:
                return False              # terminal gestures never switch tabs
            self.go(self.page + (1 if g[1] == 'left' else -1))  # drags on the keyboard must not flip tabs
            return True
        if kind == 'scrollstart':
            self.base = self.scroll
            self.term_scroll_active = self.page == TERM_PAGE and self.term_touch_origin is not None
            self.term_scroll_units, self.term_scroll_sample, self.term_scroll_velocity = 0, (now, 0.0), 0.0
        elif kind == 'scroll' and self.page == TERM_PAGE and not self.term_rotated and self.term_scroll_active:
            return self._terminal_drag(-g[1], now)
        elif kind == 'scrollx' and self.page == TERM_PAGE and self.term_rotated and self.term_scroll_active:
            return self._terminal_drag(g[1], now)
        elif kind == 'scrollend':
            if self.term_scroll_active and self.term_scroll_sample and now - self.term_scroll_sample[0] <= 0.15 and abs(self.term_scroll_velocity) >= 5:
                self.term_coast = (now, 0, self.term_scroll_velocity)
            self.term_scroll_active = False
        elif kind == 'scroll' and (self.page == 1 or self.parts or (self.page == DOCK_PAGE and not self.dk)):
            new = max(0, min(self.max_scroll(data), self.base - g[1]))
            changed, self.scroll = new != self.scroll, new
            return changed
        return False

    def _terminal_drag(self, pixels, now):
        """Follow finger movement in screen rows; keep sub-row motion until it crosses a cell."""
        if not self.term:
            return False
        units = pixels / TERM_CH
        target = int(units)
        delta = target - self.term_scroll_units
        if self.term_scroll_sample:
            sample_time, sample_units = self.term_scroll_sample
            elapsed = now - sample_time
            if elapsed > 0:
                speed = (units - sample_units) / elapsed
                speed = max(-TERM_COAST_MAX_SPEED, min(TERM_COAST_MAX_SPEED, speed))
                self.term_scroll_velocity = 0.35 * self.term_scroll_velocity + 0.65 * speed
        self.term_scroll_sample = (now, units)
        self.term_scroll_units = target
        if not delta:
            return False
        moved = scroll_terminal_lines(self.term, delta)
        if not moved:
            self.term_scroll_velocity = 0.0
        return moved

    def key_tap(self, x, y):
        """On-screen keyboard: types into the pty on the Terminal tab, else into the focused form field."""
        if self.page == TERM_PAGE and (not self.term or self.osk):
            return False
        if self.page == TERM_PAGE:
            if not self.term_keyboard:
                return False
            if self.term_rotated and (y < TERM_TOP or x > rotated_kb_height()):
                return False    # nearest-key fallback would grab the Rotate/Keyboard buttons above the strip
            x, y = keyboard_touch(x, y, self.term_rotated)
        key = kb_key_at(self.kb_mode, x, y)
        if key is None:
            return False
        self.pressed = key
        if key == 'shift':
            self.kb_mode = KB_SHIFT[self.kb_mode]
        elif key == 'mode':
            self.kb_mode = 'abc' if self.kb_mode.startswith('sym') else 'sym'
        elif self.page == TERM_PAGE and key == 'ctrl':
            self.ctrl = not self.ctrl
        elif self.page == TERM_PAGE:
            self.term.send(key_bytes(key, self.ctrl))
            self.ctrl = False
        elif key == 'back' or key == 'space' or len(key) == 1:
            f = self.w['field']
            self.w[f] = self.w[f][:-1] if key == 'back' else (self.w[f] + (' ' if key == 'space' else key))[:64]
        else:
            self.pressed = None
            return False
        if self.kb_mode == 'ABC' and key not in ('shift', 'mode', 'ctrl'):
            self.kb_mode = 'abc'     # one-shot shift
        return True

    def open_sub(self, key, data):
        self.sub = key
        if key == 'wifi':
            self.w = {'view': 'info', 'field': 'pw', 'pw': '', 'show': False}
            self.run(wifi_info, (), lambda r: self.w.update(info=r))
        elif key == 'bluetooth':
            self.w = {'view': 'bt', 'field': 'name', 'name': ''}
            self.run(bt_state, (), lambda r: self.w.update(bt=r))
        elif key == 'web':
            self.w = {'view': 'web', 'web': web_state()}
        elif key == 'audio':
            self.w = {'view': 'audio', 'audio': audio_state()}
        elif key == 'usb':
            self.w = {'view': 'usb', 'usb': usb_state()}
        elif key == 'wireguard':
            self.w = {'view': 'wireguard'}
        elif key == 'ssh':
            self.w = {'view': 'ssh', 'field': 'pw', 'pw': '', 'show': False, 'user': login_user()}

    def pick_at(self, y):
        i = int((y - PICK_TOP) // PICK_H)
        nets = self.w.get('nets') or ()
        return nets[i] if PICK_TOP <= y and 0 <= i < min(PICK_MAX, len(nets)) else None

    def toast_msg(self, msg, secs=5):
        self.toast = (msg, time.monotonic() + secs)

    def after(self, res, view=None):
        """Job callback for (ok, message, ...) results: toast, optionally switch view, refresh status."""
        self.toast_msg(res[1])
        if view:
            self.w['view'] = view
        self.refetch = True

    def on_form(self, x, y, data):
        w, v = self.w, self.w['view']
        if hit(BACK_BTN, x, y):
            if v == 'btname':
                w['view'] = 'bt'
            elif v == 'rename':
                w['view'] = 'ssh'
            elif v in ('info', 'ssh', 'bt', 'web', 'usb', 'audio', 'wireguard'):
                self.sub = self.w = None
            else:
                w['view'] = 'info'
                if v == 'saved':
                    self.run(wifi_info, (), lambda r: w.update(info=r))
            return True
        if v == 'wireguard':
            wg = (data.get('services') or {}).get('wireguard') or {}
            # Actions use only the trusted root config, never a name from dashboard JSON.
            if self.busy or w.get('stale') or wg.get('configured') is not True:
                return False
            action = ('start' if hit(WG_START, x, y) else 'stop' if hit(WG_STOP, x, y) else
                      ('autostart-off' if wg.get('autostart') else 'autostart-on') if hit(WG_AUTO, x, y) else None)
            if action:
                self.run(wg_action, (action,), self.after, 'Applying WireGuard…', secs=20)
                return True
            return False
        if v == 'usb':
            for box, (key, _, _) in zip(USB_BOXES, USB_MODES):
                if hit(box, x, y) and w['usb']['mode'] != key:
                    self.run(usb_set, (key,), lambda r: (self.after(r), w.update(usb=r[2])), 'Switching…')
                    return True
            if w['usb']['pending'] and hit(USB_REBOOT, x, y):
                self.confirm = ('reboot', time.monotonic() + CONFIRM_S, None)
                return True
            return False
        if v == 'audio':
            cfg = w['audio']
            done = lambda r: (self.after(r), w.update(audio=r[2]))
            if hit(AUDIO_BOXES[0], x, y):
                self.run(lambda: audio_set(AUDIO='on' if cfg['AUDIO'] == 'off' else 'off'), (), done, 'Saving…')
            elif cfg['AUDIO'] == 'off':
                return False
            elif hit(AUDIO_BOXES[1], x, y):
                self.run(lambda: audio_set(OUT='speaker' if cfg['OUT'] == 'headphone' else 'headphone'), (), done, 'Saving…')
            elif hit(AUDIO_BOXES[2], x, y):
                vol = slider_pct(x)
                self.run(lambda: audio_set(VOL=str(vol)), (), done, 'Saving…')
            elif hit(AUDIO_BOXES[3], x, y):
                self.run(lambda: audio_set(THERMAL='off' if cfg['THERMAL'] != 'off' else 'on'), (), done, 'Saving…')
            elif hit(AUDIO_BOXES[4], x, y):
                try:
                    tg_set('low_pct', slider_pct(x, step=10) // 2)
                except OSError as e:
                    self.toast = (f'Could not save: {e.strerror}', time.monotonic() + 4)
            elif hit(AUDIO_TEST, x, y):
                self.run(audio_test, (), done, 'Playing…')
            else:
                return False
            return True
        if v == 'web':
            for box, (key, _) in zip(WEB_BOXES, WEB_PATHS):
                if hit(box, x, y) and (key == 'WEB' or w['web'].get('WEB') != 'off'):
                    on = w['web'].get(key) == 'off'
                    self.run(web_set, (key, on), lambda r: (self.after(r), w.update(web=r[2])), 'Saving…')
                    return True
            return False
        if v in ('bt', 'btname'):
            bt = w.get('bt') or {}
            if v == 'btname':
                if hit(CONNECT_BTN, x, y) and w['name'].strip():
                    self.run(lambda n=w['name'].strip(): bt_set(ALIAS=n), (), lambda r: (self.after(r, 'bt'), w.update(bt=r[2])), 'Saving…')
                    return True
                return False
            if hit(BT_POWER, x, y):
                self.run(lambda v=not bt.get('on'): bt_set(POWER='on' if v else 'off'), (), lambda r: (self.after(r), w.update(bt=r[2])), 'Please wait…')
            elif hit(BT_VIS, x, y):
                self.run(lambda v=not bt.get('vis'): bt_set(VISIBLE='on' if v else 'off', POWER='on'), (), lambda r: (self.after(r), w.update(bt=r[2])), 'Please wait…')
            elif hit(BT_NAME, x, y):
                w.update(view='btname', name=bt.get('alias') or '', field='name')
            else:
                return False
            return True
        if v == 'rename':
            if hit(CONNECT_BTN, x, y) and w['newname']:
                err = check_username(w['newname'])
                if err:
                    self.toast_msg(err)
                else:
                    old, new = w['user'], w['newname']
                    self.confirm = ('rename', time.monotonic() + CONFIRM_S, lambda: self.run(user_rename, (old, new), lambda r: (self.after(r, 'ssh' if r[0] else None), w.update(user=r[2]) if r[0] else None), 'Renaming…'))
                return True
            return False
        if v == 'ssh':
            up = ((data.get('services') or {}).get('ssh') or {}).get('up')
            if hit(SSH_TOGGLE, x, y):
                if up:
                    self.confirm = ('ssh_off', time.monotonic() + CONFIRM_S, lambda: self.run(ssh_set, (False,), self.after, 'Stopping SSH…'))
                else:
                    self.run(ssh_set, (True,), self.after, 'Starting SSH…')
            elif hit(USER_BTN, x, y):
                w.update(view='rename', newname='', field='newname')
            elif hit(SSH_PW, x, y):
                w['show'] = not w['show']
            elif hit(CONNECT_BTN, x, y) and len(w['pw']) >= 6:
                self.run(set_password, (w['user'], w['pw']), lambda r: (self.after(r), w.update(pw='') if r[0] else None), 'Setting password…')
            else:
                return False
            return True
        if v == 'info':
            if hit(IPCFG_BTN, x, y):
                c = (w.get('info') or {}).get('conf') or {}
                w.update(view='ip', method='static' if c.get('METHOD') == 'static' else 'auto', field='dns',
                         addr=c.get('ADDR') or '', gw=c.get('GW') or '', dns=c.get('DNS') or '')
            elif hit(SAVED_BTN, x, y):
                w.update(view='saved', saved=None)
                self.run(wifi_saved, (), lambda r: w.update(saved=r))
            elif hit(CHANGE_BTN, x, y):
                w.update(view='pick', nets=None)
                self.run(wifi_scan, (), lambda r: w.update(nets=r), 'Scanning…', 8)
            else:
                return False
        elif v == 'pick':
            if hit(RESCAN_BTN, x, y):
                w['nets'] = None
                self.run(wifi_scan, (), lambda r: w.update(nets=r), 'Scanning…', 8)
                return True
            n = self.pick_at(y)
            if not n:
                return False
            if n['active']:
                self.toast_msg('Already connected')
            elif n['known'] or not n['secure']:
                self.run(wifi_connect, (n['ssid'], None, n['id']), lambda r: (self.after(r, 'info'), self.run(wifi_info, (), lambda i: w.update(info=i))),
                         f"Connecting to {n['ssid']}…")
            else:
                w.update(view='pw', sel=n, pw='', show=False, field='pw')
        elif v == 'pw':
            n = w['sel']
            if hit(PW_CARD, x, y):
                w['show'] = not w['show']
            elif hit(CONNECT_BTN, x, y) and (len(w['pw']) >= 8 or not n['secure']):
                self.run(wifi_connect, (n['ssid'], w['pw'] or None, n['id']), lambda r: (self.after(r, 'info'), self.run(wifi_info, (), lambda i: w.update(info=i))),
                         f"Connecting to {n['ssid']}…")
            else:
                return False
        elif v == 'ip':
            if hit(METHOD_BTN, x, y):
                w['method'] = 'static' if w['method'] == 'auto' else 'auto'
                if w['field'] != 'dns' and w['method'] == 'auto':
                    w['field'] = 'dns'
            elif hit(CONNECT_BTN, x, y):
                try:
                    ip_validate(w['method'], w['addr'], w['gw'], w['dns'])
                except ValueError as exc:
                    self.toast_msg(str(exc))
                    return True
                self.run(ip_apply, (w['method'], w['addr'], w['gw'], w['dns']), lambda r: (self.after(r, 'info'), w.update(info=r[2] or w.get('info'))),
                         'Applying… reverts after 60 s if the gateway does not answer', 90)
            else:
                for (key, _), box in zip(IP_FIELDS, IP_BOXES):
                    if hit(box, x, y) and (w['method'] == 'static' or key == 'dns'):
                        w['field'] = key
                        return True
                return False
        elif v == 'saved':
            i = int((y - PICK_TOP) // PICK_H)
            rows = w.get('saved') or ()
            if not (PICK_TOP <= y and 0 <= i < min(PICK_MAX, len(rows))):
                return False
            r = rows[i]
            self.confirm = ('forget_active' if r['active'] else 'forget', time.monotonic() + CONFIRM_S,
                            lambda: self.run(wifi_forget, (r['id'], r['ssid']), lambda res: (self.after(res), w.update(saved=res[2])), 'Forgetting…'))
        return True

    def open_dir(self, path, now):
        try:
            ents = list_dir(path)
        except OSError as exc:
            self.toast = (f"{os.path.basename(path) or '/'}: {exc.strerror or 'cannot open'}", now + 4)
            return
        self.files = {'pscroll': self.scroll, **(self.files or {}), 'path': path, 'ents': ents, 'view': None}
        self.scroll = 0

    def max_scroll(self, data):
        return files_scroll(self.files) if self.files else parts_scroll(data) if self.parts else dock_scroll(data) if self.page == DOCK_PAGE else max_scroll(data, self.proc_sort)

    def scroll_by(self, delta, data):
        if self.page == TERM_PAGE and self.term:
            self.term_coast = None
            h = self.term.screen.history
            pos = h.position
            self.term.screen.next_page() if delta > 0 else self.term.screen.prev_page()
            return self.term.screen.history.position != pos
        if self.page not in (1, DOCK_PAGE) and not self.parts:
            return False
        new = max(0, min(self.max_scroll(data), self.scroll + delta))
        changed, self.scroll = new != self.scroll, new
        return changed

    def render(self, data, stale, now):
        if self.page == 3 and self.tg:
            image = page_guard(stale, self.toast and self.toast[0])
        elif self.page == 3 and self.perf:
            image = page_perf(data, stale, self.toast and self.toast[0], time.monotonic() < self.trip_until)
        elif self.page == 2 and self.sub == 'wireguard':
            self.w['stale'] = stale
            image = page_netsub('wireguard', data, stale, self.busy, self.toast and self.toast[0])
        elif self.page == 2 and self.sub in FORM_SUBS:
            image = page_form(self.w, self.toast and self.toast[0], self.kb_mode, self.ctrl, self.pressed, data)
        elif self.page == 2 and self.sub:
            image = page_netsub(self.sub, data, stale)
        elif self.page == 0 and self.files:
            image = page_files(self.files, self.scroll)
        elif self.page == 0 and self.parts:
            image = page_partitions(data, stale, self.scroll)
        elif self.page == 0:
            image = page_overview(data, stale, self.ov_cores)
        elif self.page == 4 and self.term_options:
            image = page_term_options(stale)
        elif self.page == TERM_PAGE:
            image = page_terminal(self.term, self.kb_mode, self.ctrl, self.pressed, self.term_rotated, self.term_keyboard, self.osk)
        elif self.page == DOCK_PAGE:
            image = page_docker_detail(self.dk, self.dinfo, data, self.toast and self.toast[0]) if self.dk else page_docker(data, stale, self.scroll)
        else:
            image = (None, lambda: page_processes(data, stale, self.scroll, self.proc_sort), lambda: page_network(data, stale),
                     lambda: page_thermals(data, stale, self.refresh), lambda: page_power(data, stale))[self.page]()
        d, text, card, bar = tools(image)
        d.rectangle((0, TAB_Y * P, IMG[0], IMG[1]), fill=PANEL)
        d.line((0, TAB_Y * P, IMG[0], TAB_Y * P), fill=LINE, width=1)
        for i, name in enumerate(TABS):
            cx, on = i * TAB_W + TAB_W // 2, i == self.page
            if on:
                d.rounded_rectangle(((cx - 50) * P, (TAB_Y + 6) * P, (cx + 50) * P, (TAB_Y + 14) * P), 4, fill=CYAN)
            if ICON_FONT.exists():
                text(cx, (TAB_Y + H) // 2 + 4, TAB_ICONS[i], 70, CYAN if on else MUTED, anchor='mm', icon=True)
            else:
                text(cx, (TAB_Y + H) // 2, name[:5], 32, CYAN if on else MUTED, bold=on, anchor='mm')
        if self.confirm:
            image = Image.blend(image, Image.new('RGB', image.size, 'black'), 0.75)
            d, text, card, bar = tools(image)
            act = self.confirm[0]
            danger, (cb, ob) = act in DANGER, confirm_btns(act)
            accent = RED if danger else AMBER
            verb, hint, *title = CONFIRM_TEXT[act]
            if act == 'oc_pants':     # 50 alert triangles around the final warning, fixed staggered grid (no flicker)
                for row, y in enumerate((110, 270, 430, 580, 1420, 1580, 1740, 1900, 2060)):
                    for col in range(5 if row % 2 else 6):
                        text(90 + col * 180 + (90 if row % 2 else 0), y, '\U000F0026', (70, 100, 85)[(row + col) % 3], RED, anchor='mm', icon=True)
            lines = [(l[1:], 110, AMBER) if l[:1] == '#' else (l[1:], 38, AMBER) if l[:1] == '*' else (l, 34, TEXT) if danger else (l, 32, MUTED)
                     for l in hint.split('\n')]
            if danger:      # box grows upwards with the text; the text block ends just above the buttons
                y = 1105 - sum(round(size * 1.2) for _, size, _ in lines)
                card(90, y - 240, 900, 1320 - (y - 240), fill=PANEL, outline=RED)
                text(540, y - 160, '\U000F0026', 110, RED, anchor='mm', icon=True)      # md-alert
                text(540, y - 50, title[0], 56, RED, bold=True, anchor='mm', max_width=820)
            else:
                card(90, 800, 900, 520, fill=PANEL, outline=AMBER)
                text(540, 900, title[0] if title else f'{verb}?', 52, bold=True, anchor='mm', max_width=820)
                y = 1000 - (len(lines) - 1) * 22 - 22
            for line, size, color in lines:
                h = round(size * 1.2) if danger else 44
                text(540, y + h // 2, line, size, color, bold=danger, anchor='mm')
                y += h
            card(cb[0], cb[1], cb[2] - cb[0], cb[3] - cb[1])
            text((cb[0] + cb[2]) // 2, 1195, 'Cancel', 44, anchor='mm')
            card(ob[0], ob[1], ob[2] - ob[0], ob[3] - ob[1], fill=accent, outline=accent)
            text((ob[0] + ob[2]) // 2, 1195, verb, 44, BG, bold=True, anchor='mm')
        return image


# ---- framebuffer + input -----------------------------------------------------------------------
def raw_mode(r_off, g_off, b_off):
    """PIL raw mode for a 32 bpp little-endian framebuffer from its R/G/B bit offsets (0/8/16 -> 'RGBX')."""
    chars = ['X'] * 4
    for c, off in zip('RGB', (r_off, g_off, b_off)):
        chars[off // 8] = c
    return ''.join(chars)


class Display:
    """fbdev panel: mmap /dev/fb0, write rows, pan; blank via backlight bl_power."""

    def __init__(self, path=FB):
        self.fd = os.open(path, os.O_RDWR)
        self.var = bytearray(fcntl.ioctl(self.fd, 0x4600, bytes(160)))   # FBIOGET_VSCREENINFO
        self.xres, self.yres, _, yvirt, _, self.yoff, bpp = struct.unpack_from('7I', self.var)
        if bpp != 32:
            raise RuntimeError(f'need 32 bpp, got {bpp}')
        offs = struct.unpack_from('9I', self.var, 32)
        self.mode = raw_mode(offs[0], offs[3], offs[6])
        try:
            self.stride = int(Path('/sys/class/graphics', os.path.basename(path), 'stride').read_text())
        except (OSError, ValueError):
            self.stride = self.xres * 4
        self.mm = mmap.mmap(self.fd, self.stride * yvirt)
        self.blanked, self.prev, self.hole = False, None, None
        logging.info('fb %dx%d stride %d mode %s (rgb offsets %s)', self.xres, self.yres, self.stride, self.mode, offs[0::3])

    def show(self, image):
        sx, sy = self.xres // image.width, self.yres // image.height
        if (sx * image.width, sy * image.height) != (self.xres, self.yres):     # odd size: scale the whole frame
            image, sx, sy, self.prev = image.resize((self.xres, self.yres), Image.BILINEAR), 1, 1, None
        prev, self.prev = self.prev, image
        boxes = [(0, 0, image.width, image.height)]
        if prev and prev.size == image.size:        # push only what changed, per 185 px band so a key and an echo line stay separate
            diff, boxes = ImageChops.difference(prev, image), []
            for top in range(0, image.height, 185):
                box = diff.crop((0, top, image.width, min(image.height, top + 185))).getbbox()
                if box:
                    boxes.append((max(0, box[0] - 2), max(0, top + box[1] - 2), min(image.width, box[2] + 2), min(image.height, top + box[3] + 2)))
        for x0, y0, x1, y1 in boxes:
            if image.size == (self.xres, self.yres):
                crop = image.crop((x0, y0, x1, y1))
            else:       # resize a padded crop, keep the inner part: the bilinear edge clamp never lands on written pixels
                px0, py0, px1, py1 = max(0, x0 - 8), max(0, y0 - 8), min(image.width, x1 + 8), min(image.height, y1 + 8)
                big = image.crop((px0, py0, px1, py1)).resize(((px1 - px0) * sx, (py1 - py0) * sy), Image.BILINEAR)
                crop = big.crop(((x0 - px0) * sx, (y0 - py0) * sy, (x1 - px0) * sx, (y1 - py0) * sy))
                x0, y0, x1, y1 = x0 * sx, y0 * sy, x1 * sx, y1 * sy
            data, row = crop.tobytes('raw', self.mode), (x1 - x0) * 4
            for px0, py0, px1, py1 in cut((x0, y0, x1, y1), self.hole):
                n, s = (px1 - px0) * 4, (px0 - x0) * 4
                for y in range(py0, py1):
                    o, i = (self.yoff + y) * self.stride + px0 * 4, (y - y0) * row + s
                    self.mm[o:o + n] = data[i:i + n]
        if boxes:
            try:
                fcntl.ioctl(self.fd, 0x4606, bytes(self.var))   # FBIOPAN_DISPLAY: latch the frame
            except OSError:
                pass

    def readback(self):
        row = self.xres * 4
        base = self.yoff * self.stride
        data = b''.join(self.mm[base + y * self.stride:base + y * self.stride + row] for y in range(self.yres))
        return Image.frombytes('RGB', (self.xres, self.yres), data, 'raw', self.mode)

    def blank(self, on):
        self.prev = None
        if on:
            self.show(Image.new('RGB', (8, 8), 'black'))
            self.prev = None
        try:
            Path(BACKLIGHT, 'bl_power').write_text('4' if on else '0')
        except OSError:
            logging.exception('bl_power')
        self.blanked = on

    def close(self):
        self.blank(False)
        self.mm.close()
        os.close(self.fd)


VT_UI, CONSOLE_FONT = 7, '/usr/share/consolefonts/ter-132b.psf.gz'
MODES = ('tty', 'ui', 'off')       # power key cycles: text console (tty1 + buffyboard) > dashboard > screen off
SLEEP_S = 30                       # no touch or key this long: screen off


def chvt(n):
    with open('/dev/tty0', 'wb', buffering=0) as t:
        fcntl.ioctl(t, 0x5606, n)   # VT_ACTIVATE
        fcntl.ioctl(t, 0x5607, n)   # VT_WAITACTIVE


def find_event(name):
    """/dev/input/eventN for the evdev device called `name` (None if absent)."""
    block = None
    for line in Path('/proc/bus/input/devices').read_text().splitlines() + ['']:
        if line.startswith('N: Name='):
            block = line.split('=', 1)[1].strip('"')
        elif line.startswith('H: Handlers=') and block == name:
            for h in line.split()[1:]:
                if h.startswith('event'):
                    return '/dev/input/' + h
    return None


def abs_range(fd, code):
    """(min, max) of an ABS axis via EVIOCGABS."""
    _, lo, hi = struct.unpack('6i', fcntl.ioctl(fd, 0x80184540 + code, bytes(24)))[:3]
    return lo, hi


def fetch():
    with urllib.request.urlopen(DASH_URL + '/api/status', timeout=4) as response:
        return json.load(response)


def events(fd):
    out = []
    try:
        buf = os.read(fd, 24 * 64)
    except BlockingIOError:
        return out
    for i in range(0, len(buf) - 23, 24):
        _, _, kind, code, value = struct.unpack('llHHi', buf[i:i + 24])
        out.append((kind, code, value))
    return out


def sample_data():
    procs = [{'pid': 100 + i, 'name': f'proc{i}', 'cpu_percent': max(0, 90 - i * 3), 'rss_bytes': (50 + i) << 20, 'user': 'root'}
             for i in range(30)]
    return {'collected_at': '2026-01-01T12:00:00+00:00', 'uptime_seconds': 4000, 'processes': procs,
            'battery': {'percent': 71, 'status': 'Discharging', 'temperature_c': 29.9, 'voltage_v': 3.9, 'current_ma': -350,
                        'health': 'Good', 'plugged': False},
            'cpu': {'percent': 12.5, 'logical_cores': 8, 'temp_c': 32}, 'memory': {'used_bytes': 1 << 30, 'total_bytes': 4 << 30, 'used_percent': 25, 'free_bytes': 1 << 29, 'cached_bytes': 5 << 29, 'cached_percent': 62.5},
            'storage': {'used_bytes': 20 << 30, 'free_bytes': 40 << 30, 'used_percent': 33},
            'gpu': {'available': True, 'model': 'Mali-G72', 'utilization_percent': 5, 'clock_mhz': 260, 'max_clock_mhz': 572,
                    'governor': 'simple_ondemand', 'temp_c': 31},
            'wifi': {'connected': True, 'ssid': 'home', 'ip_address': '192.168.1.21', 'signal_percent': 80, 'rate_mbps': '300 Mbps'},
            'services': {'ssh': {'up': True}, 'bluetooth': {'up': False}, 'usb': {'up': True},
                         'wireguard': {'configured': False, 'up': None, 'state': 'Unconfigured'},
                         'tailscale': {'up': False, 'state': 'NeedsLogin', 'ip': None, 'name': 's9', 'peers': 0, 'online': 0}}}


def selftest():
    global TG_CONF, IPCONF, BT_CONF, WEB_CONF, USB_CONF, AUDIO_CONF, sh, WIRELESS_FILE, WC_CONTROL, WC_ENABLE, WIRELESS_ONLINE
    # framebuffer channel order from vscreeninfo offsets
    assert raw_mode(0, 8, 16) == 'RGBX' and raw_mode(16, 8, 0) == 'BGRX' and raw_mode(8, 16, 24) == 'XRGB'
    img = Image.new('RGB', (4, 2), (10, 20, 30))
    assert Image.frombytes('RGB', (4, 2), img.tobytes('raw', 'RGBX'), 'raw', 'RGBX').getpixel((1, 1)) == (10, 20, 30)
    # fmt helpers
    assert fmt(None) == '—' and fmt(3.14159, 1, '%') == '3.1%' and gib(2**30) == '1.0 GiB'
    assert uptime_text(90000) == '1d 1h' and uptime_text(3700) == '1h 1m' and fmt_mem(5 << 20) == '5M'
    assert batt_color({'percent': 10, 'status': 'Discharging'}) == '#ff6b6b'
    assert batt_color({'percent': 10, 'status': 'Charging'}) == CYAN
    # touch gestures: ABS range 0..4095 scales to the layout space
    t = Touch(4095, 4095)
    def press(x, y, now):
        t.feed(3, 53, x, now); t.feed(3, 54, y, now); t.feed(3, 57, 1, now); return t.feed(0, 0, 0, now)
    assert press(2048, 1024, 0.0)[0][0] == 'down'
    t.feed(3, 57, -1, 0.1)
    tap = t.feed(0, 0, 0, 0.1)
    assert tap[0][0] == 'tap' and abs(tap[0][1] - W / 2) < 1 and abs(tap[0][2] - H / 4) < 1 and tap[-1] == ('up',), tap
    press(100, 100, 1.0)
    assert t.tick(2.0) == [('long', t.x0, t.y0)]
    t.feed(3, 57, -1, 2.1)
    assert t.feed(0, 0, 0, 2.1) == [('up',)]
    press(3000, 500, 3.0); t.feed(3, 53, 500, 3.1)
    horizontal = t.feed(0, 0, 0, 3.1)
    assert ('scrollstart',) in horizontal and horizontal[-1][0] == 'scrollx'
    t.feed(3, 57, -1, 3.2)
    assert t.feed(0, 0, 0, 3.2) == [('scrollend',), ('swipe', 'left'), ('up',)]
    press(500, 1500, 4.0); t.feed(3, 54, 2500, 4.1)
    g = t.feed(0, 0, 0, 4.1)
    assert ('scrollstart',) in g and g[-1][0] == 'scroll'
    t.feed(3, 57, -1, 4.2)
    assert t.feed(0, 0, 0, 4.2) == [('scrollend',), ('up',)]
    # Docker tab: list -> detail, buttons per state, long press = restart
    assert [x[0] for x in container_actions('running')] == ['pause', 'stop']
    assert container_actions('paused')[0][0] == 'unpause' and container_actions('exited')[0][0] == 'start'
    for st, want in (('running', 'pause'), ('running', 'stop'), ('paused', 'unpause'), ('exited', 'start')):
        dd = {'containers': {'available': True, 'containers': [{'name': 'demo', 'image': 'x', 'state': st, 'status': 's'}]}}
        u = Ui()
        u.go(DOCK_PAGE)
        page_docker(dd, False, 0)
        assert u.on_gesture(('tap', 500, LIST_TOP + 40), dd, 0) and u.dk == 'demo'
        page_docker_detail('demo', {'state': st, 'logs': ['a'] * 20, 'cpu': 3.0}, dd, None).size
        acts = container_actions(st)
        box = globals()["boxes"](len(acts))[[a for a, _ in acts].index(want)]
        u.on_gesture(('tap', (box[0] + box[2]) // 2, (box[1] + box[3]) // 2), dd, 0)
        assert u.dact == (want, 'demo'), (st, want, u.dact)
        u.dact = None
        u.on_gesture(('long', 500, 500), dd, 0)
        assert u.dact == ('restart', 'demo')
        u.on_gesture(('tap', 100, 70), dd, 0)
        assert u.dk is None
    # UI navigation, scrolling, confirm flow
    data = sample_data()
    ui = Ui()
    for i in range(len(TABS)):
        assert ui.on_gesture(('tap', i * TAB_W + 10, TAB_Y + 50), data, 0) and ui.page == i
        assert ui.render(data, False, 0).size == IMG
    ui.go(0)
    assert ui.on_gesture(('tap', 100, 600), data, 0) and ui.ov_cores and ui.render(data, False, 0).size == IMG
    assert ui.on_gesture(('tap', 100, 1300), data, 0) and ui.page == 2
    ui.go(0); assert ui.on_gesture(('tap', 100, 1800), data, 0) and ui.page == 1
    ui.go(0); assert ui.on_gesture(('tap', 100, 1640), data, 0) and ui.page == DOCK_PAGE
    ui.go(2)    # Network subpages: every row and the Wi-Fi card open one, Back returns
    for key in ['wifi'] + [k for k, _ in NET_ROWS]:
        box = NET_WIFI if key == 'wifi' else net_box([k for k, _ in NET_ROWS].index(key))
        assert ui.on_gesture(('tap', box[0] + 50, box[1] + 50), data, 0) and ui.sub == key
        assert ui.render(data, False, 0).size == IMG and net_detail(key, data)[1]
        assert ui.on_gesture(('tap', 100, 70), data, 0) and ui.sub is None
    assert ts_state(data['services']['tailscale']) == ('Logged out', False) and 'tailscale up' in net_detail('tailscale', data)[2]
    assert ts_state({}) == ('Unknown', None)
    assert store_label(True, '1') == 'ON' and store_label(False, '1') == 'OFF after reboot' and store_label(False, '0') == 'OFF'
    ui.go(1)
    assert ui.on_gesture(('tap', 700, 70), data, 0) is True or ui.proc_sort == 'cpu_percent'
    assert ui.on_gesture(('tap', 900, 70), data, 0) and ui.proc_sort == 'rss_bytes'
    ui.on_gesture(('scrollstart',), data, 0)
    assert ui.on_gesture(('scroll', -300), data, 0) and ui.scroll == 300
    assert ui.on_gesture(('scroll', 9999), data, 0) and ui.scroll == 0
    assert ui.scroll_by(VOL_STEP, data) and ui.scroll == min(VOL_STEP, max_scroll(data, 'rss_bytes'))
    assert ui.on_gesture(('swipe', 'left'), data, 0) and ui.page == 2
    ui.go(4)
    assert ui.on_gesture(('tap', 100, 1950), data, 5.0) and ui.confirm[0] == 'reboot'
    assert ui.render(data, False, 5.0).size == IMG
    assert ui.tick(5.0 + CONFIRM_S + 1) and ui.confirm is None
    ui.on_gesture(('tap', 800, 1950), data, 5.0)
    assert ui.confirm[0] == 'poweroff'
    ui.on_gesture(('tap', 300, 1200), data, 5.0)
    assert ui.confirm is None and ui.action is None
    ui.on_gesture(('tap', 800, 1950), data, 5.0)
    ui.on_gesture(('tap', 700, 1200), data, 5.0)
    assert ui.action == 'poweroff'
    stale = Ui()
    assert stale.render({}, True, 0).size == IMG        # empty data must still draw
    # CPU & GPU limits: stepping, min<=max, clamped readback (fake sysfs in a temp dir)
    import tempfile
    global CPUFREQ, MALI, SAMSUNG_MAX_LIMIT, OC_ROOT, OC_FILE, TG_CONF, BOOST_FILE
    old = CPUFREQ, MALI, SAMSUNG_MAX_LIMIT
    with tempfile.TemporaryDirectory() as tmp:
        CPUFREQ, MALI, SAMSUNG_MAX_LIMIT = Path(tmp, 'cpufreq'), Path(tmp, 'mali'), Path(tmp, 'max_limit')
        (CPUFREQ / 'policy0').mkdir(parents=True)
        MALI.mkdir()
        (CPUFREQ / 'policy0' / 'scaling_available_frequencies').write_text('1794000 1690000 455000')
        (CPUFREQ / 'policy0' / 'scaling_max_freq').write_text('1794000')
        (CPUFREQ / 'policy0' / 'scaling_min_freq').write_text('455000')
        (CPUFREQ / 'policy0' / 'cpuinfo_max_freq').write_text('1794000')
        (CPUFREQ / 'policy0' / 'cpuinfo_min_freq').write_text('455000')
        (MALI / 'dvfs_table').write_text(' 572000 546000 260000\n')
        (MALI / 'core_mask').write_text('Current core mask (JS0) : 0x5771777\nCurrent core mask (JS1) : 0x1\nAvailable core mask : 0x5771777\n')
        assert gpu_cells() == [True] * 18
        (MALI / 'core_mask').write_text('Current core mask (JS0) : 0x5\nAvailable core mask : 0x7\n')
        assert gpu_cells() == [True, False, True]
        assert page_perf({'gpu': {'utilization_percent': 50, 'clock_mhz': 546}}, False, None).size == IMG
        (MALI / 'dvfs_max_lock').write_text('-1')
        (MALI / 'dvfs_min_lock').write_text('-1')
        assert perf_step('policy0', 'max', -1).endswith('1.69 GHz') and freq_get('policy0', 'max') == 1690000
        perf_step('policy0', 'max', 1)
        assert 'end of range' in perf_step('policy0', 'max', 1)
        perf_step('policy0', 'max', -1); perf_step('policy0', 'min', 1)
        assert freq_get('policy0', 'min') == 1690000
        perf_step('policy0', 'max', -1)                 # below min: min follows
        assert freq_get('policy0', 'max') == freq_get('policy0', 'min') == 455000
        BOOST_FILE, old_boost = Path(tmp, 'touch-boost'), BOOST_FILE
        bu = Ui(); perf_step('policy0', 'max', 1)       # touch boost lifts min to max, restores it after BOOST_S
        bu.boost(0.0); bu.boost(0.5)
        assert freq_get('policy0', 'min') == 1690000 and bu.boost_min == 455000
        bu.tick(1.0); assert freq_get('policy0', 'min') == 1690000
        bu.tick(1.5); assert freq_get('policy0', 'min') == 455000 and bu.boost_min is None
        bu.perf = True; bu.boost(2.0); assert freq_get('policy0', 'min') == 455000   # never on the Limits page
        bu.perf, bu.page = False, 4
        assert bu.on_gesture(('tap', 540, 1785), {}, 3.0) and not boost_want()      # Battery page switch: opt out
        bu.boost(4.0); assert freq_get('policy0', 'min') == 455000
        BOOST_FILE = old_boost
        perf_step('policy0', 'max', -1)
        assert freq_get('gpu', 'max') == 572000 and freq_get('gpu', 'min') == 260000
        perf_step('gpu', 'max', -1)
        assert freq_get('gpu', 'max') == 546000 and (MALI / 'dvfs_max_lock').read_text() == '546000'
        assert 'policy4' in perf_auto()                 # fake sysfs has no policy4
        assert (freq_get('policy0', 'max'), freq_get('policy0', 'min'), freq_get('gpu', 'max')) == (1794000, 455000, 572000)
        assert SAMSUNG_MAX_LIMIT.read_text() == '-1'
        perf_step('gpu', 'max', -1)
        ui = Ui()
        ui.go(3)
        assert ui.on_gesture(('tap', 680, 70), data, 0) and ui.refresh == 10 and ui.refetch and not ui.perf
        ui.refresh, ui.refetch = 5, False
        assert ui.on_gesture(('tap', 900, 70), data, 0) and ui.perf and ui.render(data, False, 0).size == IMG
        assert ui.on_gesture(('tap', perf_btns(6)[0][0] + 5, perf_btns(6)[0][1] + 5), data, 1.0) and ui.toast and 'GPU max' in ui.toast[0]
        assert ui.render(data, False, 1.0).size == IMG and ui.tick(100)
        # overclock: off = no confirm, on = two confirms, cancel on the second keeps it off
        old_oc, (OC_ROOT, OC_FILE, TG_CONF) = (OC_ROOT, OC_FILE, TG_CONF), (Path(tmp), Path(tmp, 'overclock'), Path(tmp, 'limits.json'))
        for path, _, lo in OC_KNOBS:
            Path(tmp, path).parent.mkdir(parents=True, exist_ok=True)
            Path(tmp, path).write_text(lo)
        Path(tmp, 'sys/class/thermal/thermal_zone0/type').write_text('BIG\n')
        assert not overclock() and 'on' in set_overclock(True) and overclock()
        assert [Path(tmp, p).read_text() for p, _, _ in OC_KNOBS] == ['2314000', '3500', '88000']
        assert 'off' in set_overclock(False) and not overclock()
        assert Path(tmp, OC_KNOBS[0][0]).read_text() == '1794000'
        (CPUFREQ / 'policy4').mkdir(exist_ok=True)
        (CPUFREQ / 'policy4' / 'scaling_available_frequencies').write_text('2704000 2314000 1794000 1690000')
        assert 'Turn on Overclock' in perf_step('quad', 'max', 1)
        set_overclock(True)
        assert perf_step('quad', 'max', 1).endswith('2.70 GHz') and OC_FILE.read_text() == '2704000\n' and overclock()
        assert 'end of range' in perf_step('quad', 'max', 1)
        perf_step('quad', 'max', -1); perf_step('quad', 'max', -1)
        assert 'end of range' in perf_step('quad', 'max', -1) and freq_get('quad', 'max') == 1794000     # never below stock
        assert perf_step('trip', 'max', 0, to=100000) == 'Big throttle at: 100 °C' and tg_conf()['soc_max'] == 105
        assert Path(tmp, 'overclock-trip').read_text() == '100000\n' and Path(tmp, OC_KNOBS[2][0]).read_text() == '100000'
        assert 'end of range' in perf_step('trip', 'max', 1)
        ui.perf, ui.trip_until, ti = True, 0.0, PERF_ROWS.index(('trip', 'max', 'Big', 'throttle at'))
        assert ui.on_gesture(('tap', perf_btns(ti)[1][0] + 5, perf_btns(ti)[1][1] + 5), data, 0) and ui.confirm[0] == 'trip'
        assert ui.render(data, False, 0).size == IMG and Path(tmp, OC_KNOBS[2][0]).read_text() == '100000'   # locked: + did nothing
        ui.on_gesture(('tap', OK_BTN[0] + 5, OK_BTN[1] + 5), data, 0)
        ui.on_gesture(('tap', perf_btns(ti)[0][0] + 5, perf_btns(ti)[0][1] + 5), data, time.monotonic())
        assert Path(tmp, OC_KNOBS[2][0]).read_text() == '99000' and ui.trip_until > time.monotonic() + 9
        ui.perf, ui.toast = False, None
        assert page_perf(data, False, None).size == IMG
        set_overclock(False)
        Path(tmp, 'sys/class/thermal/thermal_zone0/type').write_text('LITTLE\n')
        assert 'not BIG' in set_overclock(True) and not overclock()
        Path(tmp, 'sys/class/thermal/thermal_zone0/type').write_text('BIG\n')
        ui.perf, ui.toast = True, None
        oc_tap = ('tap', OC_BTN[0] + 50, OC_BTN[1] + 50)
        assert ui.on_gesture(oc_tap, data, 0) and ui.confirm[0] == 'oc_warn' and ui.render(data, False, 0).size == IMG
        assert ui.on_gesture(('tap', OK_BTN[0] + 5, OK_BTN[1] + 5), data, 0) and ui.confirm[0] == 'oc_pants'
        assert ui.render(data, False, 0).size == IMG
        assert ui.on_gesture(('tap', OK_BTN[0] + 5, OK_BTN[1] + 5), data, 0) and not ui.confirm and not overclock()   # double tap = Cancel
        ui.on_gesture(oc_tap, data, 0); ui.on_gesture(('tap', OK_BTN[0] + 5, OK_BTN[1] + 5), data, 0)
        ui.on_gesture(('tap', CANCEL_BTN[0] + 5, CANCEL_BTN[1] + 5), data, 0)                                        # accept sits left
        assert not ui.confirm and 'on' in ui.toast[0] and overclock() and ui.render(data, False, 0).size == IMG
        assert ui.on_gesture(oc_tap, data, 0) and not ui.confirm and not overclock()     # off: no confirm
        OC_ROOT, OC_FILE, TG_CONF = old_oc
        ui.perf = False
    CPUFREQ, MALI, SAMSUNG_MAX_LIMIT = old
    # Storage tile -> Partitions subpage, scroll, back
    data['storage']['partitions'] = [{'name': f'sda{i}', 'label': 'L', 'size_bytes': 1 << 30, 'mountpoint': '/m' if i < 3 else None,
                                      'fstype': 'ext4', 'used_bytes': 1 << 29, 'used_percent': 50} for i in range(26)]
    ui = Ui()
    assert ui.on_gesture(('tap', 100, 1000), data, 0) and ui.parts and ui.render(data, False, 0).size == IMG
    assert ui.scroll_by(300, data) and ui.scroll == 300
    assert ui.on_gesture(('tap', 100, 60), data, 0) and not ui.parts and ui.scroll == 0
    ui.on_gesture(('tap', 100, 1000), data, 0); ui.go(0); assert not ui.parts
    # Partitions -> folder explorer: top-right button opens /, mounted row opens its mountpoint, unmounted does nothing
    ui.on_gesture(('tap', 100, 1000), data, 0)
    assert ui.on_gesture(('tap', 900, 60), data, 0) and ui.files['path'] == '/' and ui.render(data, False, 0).size == IMG
    assert ui.on_gesture(('tap', 100, 60), data, 0) and ui.files is None and ui.parts
    assert not ui.on_gesture(('tap', 100, LIST_TOP + 5 * PROW_H + 10), data, 0) and ui.files is None
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / 'sub').mkdir(); (Path(td) / 'a.txt').write_text('hi')
        data['storage']['partitions'][0]['mountpoint'] = td
        assert ui.on_gesture(('tap', 100, LIST_TOP + 10), data, 0) and ui.files['path'] == td
        assert [e['name'] for e in ui.files['ents']] == ['sub', 'a.txt']
        assert ui.on_gesture(('tap', 100, FILE_TOP + FILE_ROW + 10), data, 0) and ui.files['view']['lines'] == ['hi'] and ui.render(data, False, 0).size == IMG
        assert ui.on_gesture(('tap', 100, 60), data, 0) and not ui.files['view']
        assert ui.on_gesture(('tap', 100, FILE_TOP + 10), data, 0) and ui.files['path'] == td + '/sub'
        assert ui.on_gesture(('tap', 100, 60), data, 0) and ui.on_gesture(('tap', 100, 60), data, 0) and ui.files['path'] == os.path.dirname(td)
    # buffyboard uinput -> bytes; framebuffer writes skip its rectangle
    assert uinput_bytes(30, set()) == b'a' and uinput_bytes(30, {42}) == b'A' and uinput_bytes(46, {29}) == b'\x03'
    assert uinput_bytes(2, {54}) == b'!' and uinput_bytes(103, set()) == b'\x1b[A' and uinput_bytes(16, {56}) == b'\x1bq'
    assert uinput_bytes(26, {29}) == b'\x1b' and uinput_bytes(240, set()) == b''
    assert cut((0, 0, 10, 10), None) == [(0, 0, 10, 10)] and cut((0, 0, 10, 10), (20, 0, 30, 10)) == [(0, 0, 10, 10)]
    assert cut((0, 0, 10, 10), (0, 5, 10, 20)) == [(0, 0, 10, 5)] and cut((0, 0, 10, 10), (0, 0, 4, 10)) == [(4, 0, 10, 10)]
    # terminal keyboard -> bytes; keys tile the area without overlap
    assert key_bytes('a') == b'a' and key_bytes('a', True) == b'\x01' and key_bytes('c', True) == b'\x03'
    assert key_bytes('enter') == b'\r' and key_bytes('back') == b'\x7f' and key_bytes('esc') == b'\x1b' and key_bytes('space') == b' '
    assert key_bytes('up') == b'\x1b[A' and key_bytes('left') == b'\x1b[D' and key_bytes('|') == b'|'
    for mode in KB_ROWS:
        boxes = [b for _, b in kb_keys(mode)]
        assert len(boxes) == 7 + 10 + sum(len(r) for r in KB_ROWS[mode]) + 2 + 3 and max(b[3] for b in boxes) <= TAB_Y, mode
    assert stale.go(TERM_PAGE) is None and stale.render({}, False, 0).size == IMG    # no pyte/no session: hint page
    # Glyph advances depend on the installed font and raster scale. The grid must
    # fit the viewport and leave less than one unused cell, not match a fixed count.
    cw = term_cell_width()
    for rotated, keyboard, width, rows in (
            (False, True, W - 2 * TERM_X, 42), (False, False, W - 2 * TERM_X, 73),
            (True, True, TAB_Y - TERM_TOP - 2 * TERM_X, 20),
            (True, False, TAB_Y - TERM_TOP - 2 * TERM_X, 37)):
        cols, actual_rows = term_size(rotated, keyboard)
        assert cols * cw <= width < (cols + 1) * cw and actual_rows == rows
    qbox = next(box for key, box in kb_keys('abc') if key == 'q')
    qx, qy = (qbox[0] + qbox[2]) // 2, (qbox[1] + qbox[3]) // 2
    qrot = (rotated_kb_height() - (qy - (KB_TOP - 6)) * rotated_kb_height() / kb_height(),
            TERM_TOP + qx * (TAB_Y - TERM_TOP) / W)
    assert all(abs(a - b) < 1e-6 for a, b in zip(keyboard_touch(*qrot, rotated=True), (qx, qy)))
    if pyte:
        t = Term()
        sent = []
        t.fd, t.send = None, sent.append
        t.stream.feed(b'$ ls\r\n\x1b[31mred\x1b[0m \x1b[6n')    # cursor-position query must be answered
        assert t.screen.display[0].startswith('$ ls') and t.screen.display[1].startswith('red') and sent == [b'\x1b[2;5R'], sent
        ui = Ui()
        ui.term = t
        ui.go(TERM_PAGE)
        assert (t.cols, t.rows) == term_size(False, True)
        assert ui.on_gesture(('tap', 54 + 5, KB_TOP + 2 * KEY_H + 10), data, 0) and sent[-1] == b'q'
        ui.on_gesture(('tap', 3 * 154 - 100, KB_TOP + 10), data, 0)           # Ctrl, then a letter
        assert ui.ctrl
        ui.on_gesture(('tap', 54 + 5, KB_TOP + 2 * KEY_H + 10), data, 0)
        assert sent[-1] == b'\x11' and not ui.ctrl, sent
        n = len(sent)
        assert ui.on_gesture(('down', 59, KB_TOP + 2 * KEY_H + 10), data, 0) and sent[-1] == b'q' and ui.pressed == 'q'
        assert not ui.on_gesture(('tap', 59, KB_TOP + 2 * KEY_H + 10), data, 0) and len(sent) == n + 1   # tap after down must not retype
        assert ui.on_gesture(('up',), data, 0) and ui.pressed == 'q' and not ui.fired   # quick tap: still lit
        assert not ui.tick(PRESS_S / 2) and ui.pressed == 'q'
        assert ui.tick(PRESS_S) and ui.pressed is None
        ya = KB_TOP + 3 * KEY_H + 60       # 'asdf' row is centred: its side gaps and the curved edges hit the nearest key
        assert (kb_key_at('abc', 5, ya), kb_key_at('abc', 1079, ya), kb_key_at('abc', 300, ya)) == ('a', 'l', 'd')
        assert kb_key_at('abc', 540, KB_TOP - 40) is None
        assert ui.render(data, False, 0).size == IMG
        assert not ui.on_gesture(('swipe', 'left'), data, 0) and ui.page == TERM_PAGE
        assert [page_terminal(t, 'abc', False, rotated=r, keyboard=k).size for r, k in
                ((False, True), (False, False), (True, True), (True, False))] == [IMG] * 4
        ui.term_rotated = True
        assert ui.key_tap(*qrot) and sent[-1] == b'q'
        ui.term_rotated = False
        assert ui.on_gesture(('tap', (TERM_ROTATE_BTN[0] + TERM_ROTATE_BTN[2]) // 2,
                              (TERM_ROTATE_BTN[1] + TERM_ROTATE_BTN[3]) // 2), data, 0)
        assert ui.term_rotated and (t.cols, t.rows) == term_size(True, True)
        rotated_ui = ui.render(data, False, 0)
        assert rotated_ui.getpixel((round((5 * TAB_W + TAB_W // 2) * P), round((TAB_Y + 10) * P))) == (101, 224, 218)
        rb = ((TERM_ROTATE_BTN[0] + TERM_ROTATE_BTN[2]) // 2, (TERM_ROTATE_BTN[1] + TERM_ROTATE_BTN[3]) // 2)
        n = len(sent)      # rotated: touch-down on the buttons above the key strip must not type Esc/1/Shift
        assert not ui.on_gesture(('down', 60, 50), data, 0) and not ui.on_gesture(('down', *rb), data, 0) and len(sent) == n
        ui.on_gesture(('up',), data, 0)
        assert ui.on_gesture(('tap', *rb), data, 0) and not ui.term_rotated
        assert ui.on_gesture(('tap', *rb), data, 0) and ui.term_rotated
        assert ui.on_gesture(('tap', (TERM_KB_BTN[0] + TERM_KB_BTN[2]) // 2,
                              (TERM_KB_BTN[1] + TERM_KB_BTN[3]) // 2), data, 0)
        assert not ui.term_keyboard and (t.cols, t.rows) == term_size(True, False)
        assert ui.on_gesture(('tap', (TERM_ROTATE_BTN[0] + TERM_ROTATE_BTN[2]) // 2,
                              (TERM_ROTATE_BTN[1] + TERM_ROTATE_BTN[3]) // 2), data, 0)
        assert not ui.term_rotated and (t.cols, t.rows) == term_size(False, False)
        assert ui.on_gesture(('tap', (TERM_KB_BTN[0] + TERM_KB_BTN[2]) // 2,
                              (TERM_KB_BTN[1] + TERM_KB_BTN[3]) // 2), data, 0)
        assert ui.term_keyboard and (t.cols, t.rows) == term_size(False, True)
        edge = Term()       # ESC[n@ (insert chars) leaves a cell at x == columns: rendering used to spin forever
        edge.stream.feed(b'x' * edge.cols + b'\x1b[1G\x1b[5@')
        assert max(edge.screen.buffer[edge.screen.cursor.y]) >= edge.cols and page_terminal(edge, 'abc', False).size == IMG
        # Output swipes page through history; keyboard gestures cannot page or switch tabs.
        swipe_ui = Ui()
        swipe_ui.go(TERM_PAGE)
        swipe_term = Term()
        swipe_term.stream.feed(('\r\n'.join(f'line {i}' for i in range(100)) + '\r\n').encode())
        swipe_ui.term = swipe_term
        swipe_ui.resize_term()
        bottom_pos = swipe_term.screen.history.position
        swipe_ui.on_gesture(('down', 500, 500), data, 0)
        swipe_ui.on_gesture(('scrollstart',), data, 0)
        assert not swipe_ui.on_gesture(('scroll', TERM_CH // 2), data, 0)
        assert swipe_term.screen.history.position == bottom_pos
        assert swipe_ui.on_gesture(('scroll', TERM_CH), data, 0)
        one_row = swipe_term.screen.history.position
        assert bottom_pos - one_row == 1
        assert swipe_ui.on_gesture(('scroll', TERM_CH * 3), data, 0)
        three_rows = swipe_term.screen.history.position
        assert bottom_pos - three_rows == 3
        assert swipe_ui.on_gesture(('scroll', TERM_CH), data, 0)
        assert swipe_term.screen.history.position == bottom_pos - 1
        assert swipe_term.screen.history.ratio == 0.5
        assert swipe_ui.on_gesture(('scroll', 0), data, 0)
        assert swipe_term.screen.history.position == bottom_pos
        swipe_ui.on_gesture(('up',), data, 0)
        # Volume keys keep pyte's normal half-screen paging behavior.
        assert swipe_ui.scroll_by(-VOL_STEP, data)
        page_rows = (swipe_term.screen.lines + 1) // 2
        assert bottom_pos - swipe_term.screen.history.position == page_rows
        assert swipe_term.screen.history.ratio == 0.5
        assert swipe_ui.scroll_by(VOL_STEP, data) and swipe_term.screen.history.position == bottom_pos
        swipe_ui.on_gesture(('down', 500, KB_TOP + 20), data, 0)
        swipe_ui.on_gesture(('scrollstart',), data, 0)
        swipe_ui.on_gesture(('scroll', -TERM_CH), data, 0)
        assert swipe_term.screen.history.position == bottom_pos
        swipe_ui.on_gesture(('up',), data, 0)
        swipe_ui.term_rotated = True
        swipe_ui.on_gesture(('down', 100, 500), data, 0)    # keyboard key can type, but never scroll history
        swipe_ui.on_gesture(('swipe', 'right'), data, 0)
        assert swipe_term.screen.history.position == bottom_pos
        swipe_ui.on_gesture(('up',), data, 0)
        swipe_ui.on_gesture(('down', kb_height() + 50, 500), data, 0)
        swipe_ui.on_gesture(('scrollstart',), data, 0)
        swipe_ui.on_gesture(('scroll', -TERM_CH), data, 0)  # physical vertical motion is not vertical in the rotated terminal
        assert swipe_term.screen.history.position == bottom_pos
        swipe_ui.on_gesture(('up',), data, 0)
        swipe_ui.on_gesture(('down', kb_height() + 50, 500), data, 0)
        swipe_ui.on_gesture(('scrollstart',), data, 0)
        assert swipe_ui.on_gesture(('scrollx', -TERM_CH), data, 0)
        one_rotated_row = swipe_term.screen.history.position
        assert bottom_pos - one_rotated_row == 1
        assert swipe_ui.on_gesture(('scrollx', -3 * TERM_CH), data, 0)
        assert bottom_pos - swipe_term.screen.history.position == 3
        assert swipe_ui.on_gesture(('scrollx', -TERM_CH), data, 0)
        assert swipe_term.screen.history.position == bottom_pos - 1
        swipe_ui.on_gesture(('scrollend',), data, 0)
        assert not swipe_ui.on_gesture(('swipe', 'left'), data, 0) and swipe_term.screen.history.position == bottom_pos - 1
        swipe_ui.on_gesture(('up',), data, 0)
        swipe_ui.term_rotated = False
        swipe_ui.resize_term()
        swipe_ui.on_gesture(('down', 500, 500), data, 1.0)
        swipe_ui.on_gesture(('scrollstart',), data, 1.0)
        swipe_ui.on_gesture(('scroll', TERM_CH * 2), data, 1.05)
        after_drag = swipe_term.screen.history.position
        assert swipe_ui.on_gesture(('scrollend',), data, 1.06) is False and swipe_ui.term_coast
        assert swipe_ui.tick(1.17) and swipe_term.screen.history.position < after_drag
        swipe_ui.tick(1.5)
        assert swipe_ui.term_coast is None
        swipe_ui.on_gesture(('up',), data, 0)
        # pyte and the PTY agree on the resized grid.
        master, slave = pty.openpty()
        resize_term = Term()
        resize_term.fd = master
        resize_term.resize(41, 11)
        assert (resize_term.screen.columns, resize_term.screen.lines) == (41, 11)
        assert struct.unpack('4H', fcntl.ioctl(master, termios.TIOCGWINSZ, b'\0' * 8))[:2] == (11, 41)
        os.close(master); os.close(slave)
        history_term = Term(cols=20, rows=3)
        history_term.stream.feed(b'line0\r\nline1\r\nline2\r\nline3\r\nline4\r\nline5\r\nline6\r\n')
        at_bottom = page_terminal(history_term, 'abc', False, keyboard=False)
        history_term.screen.prev_page()
        paged_up = page_terminal(history_term, 'abc', False, keyboard=False)
        assert paged_up.tobytes() != at_bottom.tobytes() and history_term.screen.cursor.hidden
        history_term.screen.next_page()
        assert not history_term.screen.cursor.hidden
        assert page_terminal(history_term, 'abc', False, keyboard=False).tobytes() == at_bottom.tobytes()
        glyph_term = Term(cols=4, rows=1)
        glyph_term.stream.feed('q\u0301\ue0b0\U000f033d'.encode())
        glyphs = ((1, '\ue0b0'), (2, '\U000f033d'))
        assert glyph_term.screen.buffer[0][0].data == 'q\u0301'
        glyph_image = page_terminal(glyph_term, 'abc', False)
        x0, y0, cw, ch = round(TERM_X * P), round(TERM_TOP * P), round(term_cell_width() * P), round(TERM_CH * P)
        baseline = Image.new('RGB', glyph_image.size, BG)
        bd = ImageDraw.Draw(baseline)
        for i, char in glyphs:
            assert is_pua(char)
            bd.text((TERM_X * P + i * term_cell_width() * P, TERM_TOP * P + TERM_CH * P / 2), char,
                    fill=TEXT, font=font(round(TERM_FONT * P), mono=True), anchor='lm')
            box = (x0 + i * cw, y0, x0 + (i + 1) * cw, y0 + ch)
            assert ImageChops.difference(glyph_image.crop(box), baseline.crop(box)).getbbox()
    else:
        print('pyte missing: terminal tests skipped')
    # Wi-Fi / SSH backends: parsers, validation, conf round trip
    scan = 'bssid / frequency / signal level / flags / ssid\n' + '\n'.join(f'aa\t{f}\t{d}\t{fl}\t{n}' for f, d, fl, n in (
        (2412, -60, '[WPA2-PSK-CCMP][ESS]', 'Home'), (5180, -45, '[WPA2-PSK-CCMP][ESS]', 'Home'), (2437, -90, '[ESS]', 'Cafe'), (2462, -70, '[WPA2-PSK-CCMP]', 'Attic')))
    saved = parse_networks('network id / ssid / bssid / flags\n0\tAttic\tany\t[CURRENT]\n1\tHome\tany\t')
    assert [r['ssid'] for r in saved] == ['Attic', 'Home'] and saved[0]['active']
    nets = parse_scan(scan, saved)
    assert [n['ssid'] for n in nets] == ['Attic', 'Home', 'Cafe'] and nets[1]['signal'] == 100 and nets[2]['secure'] is False and nets[1]['id'] == '1'
    assert ip_validate('static', '192.168.1.5', '192.168.1.1', '1.1.1.1, 9.9.9.9') == ('192.168.1.5/24', '192.168.1.1', ['1.1.1.1', '9.9.9.9'])
    assert ip_validate('auto', 'x', 'y', '') == ('', '', [])
    for bad in (('static', '192.168.1.5', '10.9.9.1', ''), ('static', 'nope', '', ''), ('auto', '', '', '1.2.3'), ('static', '::1', '', '')):
        try:
            ip_validate(*bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    old = IPCONF
    IPCONF = Path('/tmp/s9-ipconf-selftest')
    IPCONF.write_text(ip_conf_text('static', "My 'net", '192.168.1.5/24', '192.168.1.1', ['1.1.1.1']))
    assert ip_conf() == {'METHOD': 'static', 'SSID': "My 'net", 'ADDR': '192.168.1.5/24', 'GW': '192.168.1.1', 'DNS': '1.1.1.1'}
    IPCONF.unlink()
    IPCONF = old
    # Wi-Fi form: info -> pick -> password typing -> connect job; IP validation; forget confirm with a job
    ui = Ui()
    data['services']['ssh'] = {'up': True}
    ui.go(2)
    ui.on_gesture(('tap', 100, 300), data, 0)
    assert ui.sub == 'wifi' and ui.w['view'] == 'info' and ui.job[0] is wifi_info and ui.busy
    ui.job = None; ui.busy = False
    ui.w['info'] = {'ssid': 'Attic', 'state': 'COMPLETED', 'ip': '192.168.1.21/24', 'gw': '192.168.1.1', 'dns': ['192.168.1.1'], 'static': False, 'conf': {},
                    'mac': 'aa', 'rssi': '-50', 'rate': '100', 'band': '5 GHz', 'sec': 'WPA2-PSK'}
    assert ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 500, 1250), data, 0)
    assert ui.w['view'] == 'pick' and ui.job[0] is wifi_scan
    ui.job = None; ui.busy = False
    ui.w['nets'] = nets + [dict(nets[2], ssid='New', secure=True, known=False, id=None)]
    assert ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 500, PICK_TOP + 3 * PICK_H + 20), data, 0)       # locked unknown network -> password view
    assert ui.w['view'] == 'pw' and ui.w['sel']['ssid'] == 'New'
    for key in ('S', 'e', 'c'):
        ui.kb_mode = 'ABC' if key == 'S' else 'abc'
        k = next(b for kk, b in kb_keys(ui.kb_mode) if kk == key)
        assert ui.on_gesture(('down', k[0] + 5, k[1] + 5), data, 0)
        ui.on_gesture(('up',), data, 0)
    assert ui.w['pw'] == 'Sec' and ui.kb_mode == 'abc'
    ui.on_gesture(('tap', 500, 1150), data, 0)
    assert ui.job is None                                                    # too short
    ui.w['pw'] = 'secret123'
    assert ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 500, 1150), data, 0)
    assert ui.job[0] is wifi_connect and ui.job[1] == ('New', 'secret123', None)
    fn, args, cb = ui.job
    ui.job = None; ui.busy = False
    cb((True, 'Connected to New'))
    assert ui.w['view'] == 'info' and ui.refetch and ui.job[0] is wifi_info
    ui.job = None; ui.busy = False
    ui.on_gesture(('tap', 500, 1000), data, 0)
    assert ui.w['view'] == 'ip' and ui.w['method'] == 'auto'
    ui.on_gesture(('tap', 500, 200), data, 0)
    ui.w.update(addr='192.168.1.9', gw='10.9.9.9')
    ui.on_gesture(('tap', 500, 1150), data, 0)
    assert ui.job is None and 'not in' in ui.toast[0]
    ui.w['gw'] = '192.168.1.1'
    assert ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 500, 1150), data, 0)
    assert ui.job[0] is ip_apply and ui.job[1][:3] == ('static', '192.168.1.9', '192.168.1.1')
    ui.job = None; ui.busy = False
    ui.on_gesture(('tap', 100, 60), data, 0); ui.on_gesture(('tap', 500, 1100), data, 0)
    ui.job = None; ui.busy = False
    ui.w['saved'] = saved
    assert ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 500, PICK_TOP + PICK_H + 20), data, 0)
    assert ui.confirm[0] == 'forget' and ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 760, 1195), data, 0)
    assert ui.job[0] is wifi_forget and ui.job[1] == ('1', 'Home') and ui.confirm is None
    ui.job = None; ui.busy = False
    # SSH form: switch user, type password, off needs confirm
    ui = Ui()
    ui.go(2)
    ui.sub, ui.w = 'ssh', {'view': 'ssh', 'field': 'pw', 'pw': '', 'show': False, 'user': 'a'}
    ui.w['pw'] = 'abc123'
    assert ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 500, 1150), data, 0)
    assert ui.job[0] is set_password and ui.job[1] == ('a', 'abc123')
    ui.job = None; ui.busy = False
    ui.on_gesture(('tap', 800, 200), data, 0)
    assert ui.confirm[0] == 'ssh_off' and ui.job is None
    ui.on_gesture(('tap', 760, 1195), data, 0)
    assert ui.job[0] is ssh_set and ui.job[1] == (False,)
    # Bluetooth / Web pages
    global TERM_CONF
    TERM_CONF = Path('/tmp/s9-term-selftest')
    TERM_CONF.unlink(missing_ok=True)
    ui = Ui()
    ui.go(4)
    assert knox_hidden() and ui.render(data, False, 0).size == IMG
    assert ui.on_gesture(('tap', 540, 1480), {}, 0) and ui.term_options
    assert ui.render(data, False, 0).size == IMG
    assert ui.on_gesture(('tap', 540, 260), {}, 0) and not knox_hidden()
    ufs_box = TERM_FILTER_BOXES[1]
    assert ui.on_gesture(('tap', (ufs_box[0] + ufs_box[2]) // 2, (ufs_box[1] + ufs_box[3]) // 2), {}, 0)
    assert term_filter_config()['KNOX'] == 'show' and term_filter_config()['UFS_PROBES'] == 'show'
    saved_filters = term_filter_config()
    saved_filters.update(KNOX='hide', UFS_PROBES='hide')
    kv_write(TERM_CONF, saved_filters)
    assert term_message_hidden('KNOX: sample warning')
    for command in ('-2144861675', '-2147216764', '21397'):
        assert term_message_hidden(f'exynos-ufs 11120000.ufs: ufshcd_ioctl: Illegal ufs-IOCTL cmd {command}')
    assert not term_message_hidden('exynos-ufs 11120000.ufs: ufshcd_ioctl: Illegal ufs-IOCTL cmd 213970')
    assert term_message_hidden('wdev null. Do nothing')
    assert term_message_hidden('using random self ethernet address')
    assert term_message_hidden('using random host ethernet address')
    assert not term_message_hidden('ufshcd_ioctl: NULL buffer')
    for line in ('LEGACY_SCAN sync ID: 0, bssidx: 0', 'key index (0) (0xfac04)', 'START SCAN', 'ESCAN COMPLETED',
                 ' Connected event of connected device e=0 s=0, ignore it', '[BT] set_wake_locked value = 1'):
        assert term_message_hidden(line), line
    for msg in ('usb: enable_store: already disabled', 'usb: gether_disconnect No of ZLPS (0)', 'usb_notify: send_otg_notify otg_notify is null',
                'ccic_notifier_init already registered', 'dhd_prot_ioctl: SET PM to 2', 'DHCP ACK[REPLY][wlan0] [RX]'):
        assert term_message_hidden(msg), msg
    assert not term_message_hidden('xhci-hcd: usb device error')
    conf = dict(TERM_FILTER_DEFAULTS)
    assert term_message_hidden('000000.146 EL:V02:3:1 56 5 0x40dc 0 1285a2', conf) and term_message_hidden('MACEVENT: WLC_E_IF, opcode:0x1  ifidx:0 role:0', conf)
    assert not term_message_hidden('wlan0: link up', conf)
    assert term_message_hidden('[0:    kworker/0:0:21161] acpm_ipc timeout retry 1now = 1919151633178,timeout = 1919145498371', conf)
    assert not term_message_hidden('acpm_ipc timeout retry 2now = 1,timeout = 0', conf)
    assert term_message_hidden('exynos5-hsi2c 14370000.hsi2c: HSI2C NO ACK occured', conf)
    assert term_message_hidden('mfc_reg_write: i2c write error, reg: 0x50, ret: -6 (called by mfc_send_packet)', conf)
    assert not term_message_hidden('exynos5-hsi2c 10950000.hsi2c: HSI2C NO ACK occured', conf)
    assert ui.on_gesture(('tap', BACK_BTN[0] + 10, BACK_BTN[1] + 10), {}, 0) and not ui.term_options
    TERM_CONF.unlink()
    old_wireless = WIRELESS_FILE, WC_CONTROL, WC_ENABLE, WIRELESS_ONLINE
    with tempfile.TemporaryDirectory() as tmp:
        WIRELESS_FILE, WC_CONTROL = Path(tmp, 'wireless'), Path(tmp, 'wc_control')
        WC_ENABLE, WIRELESS_ONLINE = Path(tmp, 'wc_enable'), Path(tmp, 'online')
        WC_ENABLE.write_text('1')
        WIRELESS_ONLINE.write_text('1')
        assert wireless_state() == (True, 1, 1)
        assert wireless_label()[0] == 'Wireless charging: On · pad detected'
        power_ui = Ui()
        power_ui.go(4)
        assert power_ui.render(data, False, 0).size == IMG
        WC_ENABLE.write_text('0')
        assert power_ui.on_gesture(('tap', 500, 1050), data, 0) and power_ui.toast is not None and power_ui.toast[0] == 'Wireless charging disabled'
        assert WIRELESS_FILE.read_text().strip() == 'ENABLED=off' and WC_CONTROL.read_text() == '0'
        assert wireless_label()[0] == 'Wireless charging: Off'
        WC_ENABLE.write_text('1')
        WIRELESS_ONLINE.write_text('0')
        assert power_ui.on_gesture(('tap', 500, 1050), data, 0) and power_ui.toast is not None and power_ui.toast[0] == 'Wireless charging enabled'
        assert wireless_label()[0] == 'Wireless charging: On · no pad'
        assert power_ui.on_gesture(('tap', 500, 1050), data, 0) and power_ui.toast is not None and 'did not confirm off' in power_ui.toast[0]
        assert wireless_want() is False and WC_CONTROL.read_text() == '0'
    WIRELESS_FILE, WC_CONTROL, WC_ENABLE, WIRELESS_ONLINE = old_wireless
    old = BT_CONF, WEB_CONF
    BT_CONF, WEB_CONF = Path('/tmp/s9-bt-selftest'), Path('/tmp/s9-web-selftest')
    kv_write(WEB_CONF, dict(web_state(), TAILSCALE='off'))
    assert web_state() == {'WEB': 'on', 'LAN': 'on', 'TAILSCALE': 'off', 'USB': 'on'}
    kv_write(BT_CONF, {'ALIAS': "Phil's S9", 'POWER': 'on', 'VISIBLE': 'off'})
    assert kv_read(BT_CONF, {})['ALIAS'] == "Phil's S9"
    BT_CONF.unlink(); WEB_CONF.unlink()
    # USB page: tapping a mode runs usb_set; ncm built + rndis wanted = reboot pending
    old_usb = USB_CONF
    USB_CONF = Path('/tmp/s9-usb-selftest')
    assert usb_state()['mode'] == 'network'
    kv_write(USB_CONF, {'MODE': 'charge'}); assert usb_state()['mode'] == 'charge'; USB_CONF.unlink()
    ui = Ui(); ui.go(2); ui.open_sub('usb', data)
    ui.w['usb'] = {'mode': 'network', 'pending': True}
    assert ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 500, USB_BOXES[2][1] + 50), data, 0)
    assert ui.job[0] is usb_set and ui.job[1] == ('charge',)
    ui.job = None; ui.busy = False
    ui.on_gesture(('tap', 500, 800), data, 0)
    assert ui.confirm[0] == 'reboot'
    USB_CONF = old_usb
    BT_CONF, WEB_CONF = old
    ui = Ui()
    ui.go(2)
    ui.on_gesture(('tap', 100, net_box([k for k, _ in NET_ROWS].index('bluetooth'))[1] + 20), data, 0)
    assert ui.sub == 'bluetooth' and ui.job[0] is bt_state
    ui.job = None; ui.busy = False
    ui.w['bt'] = {'on': True, 'vis': False, 'alias': 'S9', 'addr': 'AA:BB'}
    assert ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 500, 380), data, 0)
    assert ui.job and ui.busy
    ui.job = None; ui.busy = False
    ui.on_gesture(('tap', 500, 540), data, 0)
    assert ui.w['view'] == 'btname' and ui.w['name'] == 'S9' and ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 500, 1150), data, 0)
    assert ui.job and ui.busy
    ui.job = None; ui.busy = False
    ui.on_gesture(('tap', 100, 60), data, 0)
    assert ui.w['view'] == 'bt'
    ui.on_gesture(('tap', 100, 60), data, 0)
    assert ui.sub is None
    ui.on_gesture(('tap', 100, net_box([k for k, _ in NET_ROWS].index('web'))[1] + 20), data, 0)
    assert ui.sub == 'web' and ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 500, 250), data, 0)
    assert ui.job and ui.busy
    ui.job = None; ui.busy = False
    # Audio page: config round-trip, volume +, sound toggle, test; rows 2-4 ignored when off
    old_audio, old_sh = AUDIO_CONF, sh
    AUDIO_CONF = Path('/tmp/s9-audio-selftest')
    sh = lambda *c, **k: (True, '')
    assert audio_state() == {'AUDIO': 'on', 'OUT': 'speaker', 'VOL': '60', 'THERMAL': 'on'} and audio_set(VOL='70')[2]['VOL'] == '70' and audio_test()[1] == 'Played'
    ui.on_gesture(('tap', 100, 60), data, 0)
    assert ui.sub is None
    ui.on_gesture(('tap', 100, net_box([k for k, _ in NET_ROWS].index('audio'))[1] + 20), data, 0)
    assert ui.sub == 'audio' and ui.w['view'] == 'audio' and ui.render(data, False, 0).size == IMG
    assert slider_pct(0) == 0 and slider_pct(540) == 50 and slider_pct(1040) == 100
    ui.on_gesture(('tap', 942, 590), data, 0)
    assert ui.job[0]() [2]['VOL'] == '90'
    ui.job = None; ui.busy = False
    ui.on_gesture(('tap', 500, 770), data, 0)
    assert ui.job[0]()[2]['THERMAL'] == 'off'
    ui.job = None; ui.busy = False
    old_tg, TG_CONF = TG_CONF, Path(tempfile.mkdtemp()) / 'limits.json'
    ui.on_gesture(('tap', 540, 1060), data, 0)
    assert tg_conf()['low_pct'] == 25 and ui.render(data, False, 0).size == IMG
    TG_CONF.unlink(); TG_CONF.parent.rmdir(); TG_CONF = old_tg
    ui.job = None; ui.busy = False
    ui.on_gesture(('tap', 500, 230), data, 0)
    assert ui.job[0]()[2]['AUDIO'] == 'off'
    ui.job = None; ui.busy = False
    ui.w['audio'] = audio_state()
    assert ui.render(data, False, 0).size == IMG and not ui.on_gesture(('tap', 940, 590), data, 0) and ui.job is None
    ui.on_gesture(('tap', 100, 60), data, 0)
    assert ui.sub is None
    AUDIO_CONF.unlink(); AUDIO_CONF, sh = old_audio, old_sh
    # Thermal guard page: step writes limits.json atomically, Back returns
    old_tg, TG_CONF = TG_CONF, Path(tempfile.mkdtemp()) / 'limits.json'   # fresh dir: a root-owned leftover broke a fixed path
    assert tg_conf() == {'batt_max': 55, 'soc_max': 100, 'hold_s': 60, 'low_pct': 30}
    ui.go(3)
    assert ui.render(data, False, 0).size == IMG and ui.on_gesture(('tap', 500, 1985), data, 0) and ui.tg and ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 920, 330), data, 0); ui.on_gesture(('tap', 160, 930), data, 0)
    assert json.loads(TG_CONF.read_text()) == {'batt_max': 56, 'soc_max': 100, 'hold_s': 50, 'low_pct': 30}
    ui.on_gesture(('tap', 100, 70), data, 0)
    assert not ui.tg
    TG_CONF.unlink(); TG_CONF.parent.rmdir(); TG_CONF = old_tg
    ui.go(2)
    # rename user: validation, keyboard entry, confirm then job
    assert check_username('Bad') and check_username('1x') and check_username('root') and check_username('a' * 33) and check_username('ok-name_1') is None
    ui.sub, ui.w = 'ssh', {'view': 'ssh', 'field': 'pw', 'pw': '', 'show': False, 'user': 'a'}
    ui.on_gesture(('tap', 500, 400), data, 0)
    assert ui.w['view'] == 'rename'
    ui.on_gesture(('down', 59, KB_TOP + 2 * KEY_H + 10), data, 0); ui.on_gesture(('up',), data, 0)
    assert ui.w['newname'] == 'q' and ui.render(data, False, 0).size == IMG
    ui.on_gesture(('tap', 500, 1150), data, 0)
    assert ui.confirm[0] == 'rename' and ui.job is None
    ui.on_gesture(('tap', 760, 1195), data, 0)
    assert ui.job[0] is user_rename and ui.job[1] == ('a', 'q')
    assert all(c[0] >= 0 for c in core_stats())
    assert len(core_stats()) == int(CPU_PRESENT.read_text().strip().split("-")[-1]) + 1  # offline cores still listed
    with tempfile.TemporaryDirectory() as td:
        kd, cf = Path(td), Path(td, 'panic.conf')
        for k in PANIC_KEYS + ('panic',):
            (kd / k).write_text('0')
        assert freeze_reboot(kd) is False
        set_freeze_reboot(True, kd, cf)
        assert freeze_reboot(kd) and (kd / 'panic').read_text() == '10' and 'kernel.panic_on_rcu_stall = 1' in cf.read_text()
        set_freeze_reboot(False, kd, cf)
        assert freeze_reboot(kd) is False and 'kernel.panic = 120' in cf.read_text()
        assert freeze_reboot(Path(td, 'missing')) is None
    print('selftest ok')


def pump(term, until, timeout=8.0):
    """Read the session's output until `until(screen text)` holds."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if select.select([term.fd], [], [], 0.2)[0]:
            term.read()
        if until('\n'.join(term.screen.display)):
            return
    raise AssertionError('timeout, screen:\n' + '\n'.join(term.screen.display))


PROMPT_ENDS = ('$', '#', '\uf432')    # ash host:cwd$, or the starship character (nf-oct-chevron_right)


def selftest_terminal(out):
    assert pyte, 'apk add py3-pyte'
    login = Term()          # default session: autologin as TERM_USER, no login prompt
    login.spawn()
    pump(login, lambda t: t.rstrip().endswith(PROMPT_ENDS))
    assert 'login:' not in '\n'.join(login.screen.display)
    t = login
    ui = Ui()
    ui.term = t
    ui.go(TERM_PAGE)
    for line in ('uname -a', 'echo "pyte on $(tput cols 2>/dev/null || echo 56) cols"', 'id', 'ls /usr/share/fonts'):
        for ch in line:       # through the on-screen key path where a key exists, else raw bytes
            t.send(key_bytes('space' if ch == ' ' else ch))
        ui.on_gesture(('tap', 900, KB_TOP + 5 * KEY_H + 10), {}, 0)   # Enter key
    pump(t, lambda s: 'opensans' in s)
    t.send(b'seq 200\r')
    pump(t, lambda s: '199' in s and s.rstrip().endswith(PROMPT_ENDS))     # not just the echoed 'seq 200'
    assert ui.scroll_by(-VOL_STEP, {}) and '199' not in '\n'.join(t.screen.display)   # volume up pages back
    assert ui.scroll_by(VOL_STEP, {}) and '199' in '\n'.join(t.screen.display)
    t.send(b'clear; ls /usr/share/fonts\r')
    pump(t, lambda s: 'opensans' in s and '200' not in s)
    image = ui.render({}, False, 0)
    try:
        display = Display()
        display.show(image)
        display.readback().save(out)
        display.mm.close()
        os.close(display.fd)
    except OSError:
        image.save(out)
    t.close()
    print('terminal selftest ok')


def main():
    ctypes.CDLL(None).prctl(15, b's9-phone-ui', 0, 0, 0)     # PR_SET_NAME: show as s9-phone-ui in ps/top, not python3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview', type=Path, help='render the live dashboard data to a PNG and exit')
    parser.add_argument('--page', type=int, default=0)
    parser.add_argument('--readback', type=Path, help='read fb0 back through mmap, save PNG, print stats, exit')
    parser.add_argument('--exclusive-power-key', action='store_true', help='EVIOCGRAB the key device (no system poweroff)')
    parser.add_argument('--scale', type=float, default=4 / 3, help='render scale P (4/3 = native 1440x2960, 2/3 = half-res upscaled)')
    parser.add_argument('--selftest', action='store_true')
    parser.add_argument('--selftest-terminal', type=Path, metavar='PNG',
                        help='drive the autologin session through the Terminal tab, save an fb readback (stop the service first)')
    args = parser.parse_args()
    set_scale(args.scale)
    if args.selftest:
        return selftest()
    if args.selftest_terminal:
        return selftest_terminal(args.selftest_terminal)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    if args.preview:
        ui = Ui()
        ui.go(args.page)
        ui.render(fetch(), False, 0).save(args.preview)
        return
    display = Display()
    if args.readback:
        shot = display.readback()
        shot.save(args.readback)
        small = shot.resize((90, 185))
        colors = small.getcolors(1 << 20)
        bg = sum(c for c, v in colors if v == (9, 12, 17))
        ref = Ui().render(fetch(), False, 0).resize(shot.size, Image.BILINEAR)
        diff = sum(ImageChops.difference(shot, ref).resize((90, 185)).convert('L').tobytes()) / (90 * 185)
        print(f'readback {shot.size} distinct(sampled)={len(colors)} bg_fraction={bg / (90 * 185):.2f} mean_diff_vs_render={diff:.2f}')
        return
    touch = key_fd = touch_fd = None
    keyname = find_event(KEY_NAME)
    if keyname:
        key_fd = os.open(keyname, os.O_RDONLY | os.O_NONBLOCK)
        if args.exclusive_power_key:
            fcntl.ioctl(key_fd, 0x40044590, 1)   # EVIOCGRAB; released on close
            logging.info('Exclusive key input acquired on %s', keyname)
    else:
        logging.error('no %s device: power key disabled', KEY_NAME)
    tname = find_event(TOUCH_NAME)
    if tname:
        touch_fd = os.open(tname, os.O_RDONLY | os.O_NONBLOCK)
        (x0, xmax), (y0, ymax) = abs_range(touch_fd, 53), abs_range(touch_fd, 54)
        logging.info('touch %s ABS_MT_POSITION_X %d..%d Y %d..%d -> layout %dx%d', tname, x0, xmax, y0, ymax, W, H)
        touch = Touch(xmax, ymax)
        try:
            TSP_CMD.write_text('dead_zone_enable,0')   # ponytail: lost if the IC resets (ic_reset_count); re-apply on unblank if that bites
        except OSError:
            logging.warning('could not turn off the touch edge dead zone')
    else:
        logging.error('no %s device: touch disabled', TOUCH_NAME)

    stop, toggle = [], []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    signal.signal(signal.SIGINT, lambda *a: stop.append(1))
    signal.signal(signal.SIGUSR1, lambda *a: toggle.append(1))
    ui, data, stale, pool = Ui(), {}, True, ThreadPoolExecutor(1)
    dpool, afut, dfut, next_detail = ThreadPoolExecutor(2), None, None, 0.0
    jfut, jcb = None, None
    ui.go(args.page)
    if pyte:
        ui.term = Term()
        ui.resize_term()
        ui.term.spawn()
        ui.term.open_kmsg()
    fut, next_fetch, frames = None, 0.0, []
    dirty = True
    # the dashboard draws on tty7 in graphics mode, so fbcon (cursor, kernel messages) never paints over it
    ui_tty = os.open(f'/dev/tty{VT_UI}', os.O_RDWR | os.O_NOCTTY)

    def show_vt(n):
        if n != VT_UI:      # the kernel won't switch away from a KD_GRAPHICS console in VT_AUTO mode
            fcntl.ioctl(ui_tty, 0x4B3A, 0)  # KDSETMODE KD_TEXT
        chvt(n)
        if n == VT_UI:
            fcntl.ioctl(ui_tty, 0x4B3A, 1)  # KD_GRAPHICS

    subprocess.run(['setfont', '-C', '/dev/tty1', CONSOLE_FONT], check=False)
    mode, kb = None, None
    osk, osk_fd, osk_held = None, None, set()

    def stop_osk():
        nonlocal osk, osk_fd, dirty
        if osk:
            osk.terminate()
            osk.wait()
            osk = None
        if osk_fd is not None:
            os.close(osk_fd)
            osk_fd = None
        osk_held.clear()
        display.hole, display.prev, ui.osk, dirty = None, None, False, True
        ui.resize_term()

    def sync_osk():
        """Terminal tab with keyboard shown: buffyboard draws and types in the keyboard strip, far snappier than our own."""
        nonlocal osk
        # ponytail: portrait only; rotated keeps the drawn keyboard until buffyboard -r is mapped onto that strip
        on = mode == 'ui' and ui.page == TERM_PAGE and ui.term and ui.term_keyboard and not ui.term_rotated and not ui.confirm
        s = display.xres / W
        box = (0, round(OSK_TOP * s), display.xres, round(TAB_Y * s)) if on else None
        if box == display.hole:
            return
        stop_osk()
        if box:
            display.hole, ui.osk = box, True
            ui.resize_term()
            # buffyboard turns its touch input off on a graphics VT, checked only at startup and on VT switches:
            # start it while tty7 is in text mode, back to graphics once it is up (fbcon's flash gets redrawn below)
            fcntl.ioctl(ui_tty, 0x4B3A, 0)  # KD_TEXT
            osk = subprocess.Popen(['buffyboard', '-g', f'{display.xres}x{box[3]}'])
            for _ in range(40):
                if find_event('buffyboard'):
                    break
                time.sleep(0.05)
            time.sleep(0.3)
            fcntl.ioctl(ui_tty, 0x4B3A, 1)  # KD_GRAPHICS
            display.prev = None

    def set_mode(m):
        nonlocal mode, kb, dirty, next_fetch, last_input
        if m != 'ui':
            stop_osk()
        if kb and m != 'tty':       # buffyboard reads the touchscreen too: only run it on the console
            kb.terminate()
            kb.wait()
            kb = None
        if m == 'off':
            show_vt(VT_UI)          # leave tty1 untouched by the black frame; switching back redraws it
            display.blank(True)
        else:
            if display.blanked:
                display.blank(False)
            show_vt(1 if m == 'tty' else VT_UI)
            if m == 'tty' and kb is None:
                kb = subprocess.Popen(['buffyboard'])
        mode, dirty, last_input = m, m == 'ui', time.monotonic()
        if m == 'ui':
            next_fetch = 0.0        # data is only polled while the dashboard is on screen: refresh now
        logging.info('mode %s', m)

    set_mode('tty')
    while not stop:
        now = time.monotonic()
        if fut is None and now >= next_fetch and mode == 'ui':
            fut = pool.submit(fetch)
        if fut and fut.done():
            try:
                data, stale = fut.result(), False
            except Exception as exc:
                stale = True
                logging.warning('fetch failed: %s', exc)
            fut, next_fetch = None, now + (1 if ui.page == THERM_PAGE and ui.perf else ui.refresh if ui.page == THERM_PAGE else 5)
            dirty |= ui.page != TERM_PAGE      # the Terminal tab shows no fetched data: stay idle
        if toggle:
            toggle.clear()
            set_mode(MODES[(MODES.index(mode) + 1) % 3])
        term_fd = ui.term.fd if ui.term else None
        kmsg_fd = ui.term.kfd if ui.term else None
        if osk and osk_fd is None and (path := find_event('buffyboard')):
            osk_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        fds = [f for f in (key_fd, touch_fd, term_fd, kmsg_fd, osk_fd) if f is not None]
        for f in select.select(fds, [], [], 1 if mode == 'off' else 0.025 if ui.term_coast or ui.press_off else 0.1)[0]:
            if f in (key_fd, touch_fd, osk_fd):
                last_input = now
            if f == term_fd:
                dirty |= ui.term.read() and ui.page == TERM_PAGE
                continue
            if f == kmsg_fd:
                dirty |= ui.term.read_kmsg() and ui.page == TERM_PAGE
                continue
            if f == osk_fd:
                for kind, code, value in events(f):
                    if kind != 1:
                        continue
                    if code in SHIFTS | CTRLS | ALTS:
                        (osk_held.add if value else osk_held.discard)(code)
                    elif value:
                        ui.term.send(uinput_bytes(code, osk_held))
                continue
            for kind, code, value in events(f):
                if f == key_fd:
                    if kind == 1 and value == 1 and code == KEY_POWER:
                        set_mode(MODES[(MODES.index(mode) + 1) % 3])
                    elif kind == 1 and value in (1, 2) and code in (KEY_VOLDOWN, KEY_VOLUP) and mode == 'ui':
                        dirty |= ui.scroll_by(VOL_STEP if code == KEY_VOLDOWN else -VOL_STEP, data)
                else:
                    for g in touch.feed(kind, code, value, now):
                        if mode == 'off':
                            if g[0] == 'tap':
                                set_mode('tty')
                        elif mode == 'ui':
                            dirty |= ui.on_gesture(g, data, now)
        if mode != 'off' and now - last_input >= SLEEP_S:
            set_mode('off')
        if touch and mode == 'ui':
            for g in touch.tick(now):
                dirty |= ui.on_gesture(g, data, now)
        if ui.term and ui.term.wrote:       # a key was just typed: catch the shell's echo so one frame shows highlight + char
            ui.term.wrote = False
            if ui.term.fd is not None and select.select([ui.term.fd], [], [], 0.02)[0]:
                dirty |= ui.term.read() and ui.page == TERM_PAGE
        dirty |= ui.tick(now)
        if ui.term:
            dirty |= ui.term.poll(now) and ui.page == TERM_PAGE
        if ui.dact and afut is None:
            logging.info('docker %s', ui.dact)
            afut, ui.dact = dpool.submit(docker_do, *ui.dact), None
        if afut and afut.done():
            ok, msg = afut.result()
            logging.info('docker action: %s %s', ok, msg)
            ui.toast, afut, next_detail, next_fetch, dirty = (msg, now + 4), None, 0.0, 0.0, True
        if ui.dk and ui.page == DOCK_PAGE and mode == 'ui' and dfut is None and now >= next_detail:
            dfut = dpool.submit(container_detail, ui.dk)
        if dfut and dfut.done():
            info, dfut, next_detail = dfut.result(), None, now + 5
            if info.get('name') == ui.dk:
                ui.dinfo, dirty = info, dirty or ui.page == DOCK_PAGE
        if ui.job and jfut is None:
            fn, args, jcb = ui.job
            ui.job, jfut = None, dpool.submit(fn, *args)
        if jfut and jfut.done():
            try:
                res = jfut.result()
            except Exception as exc:
                logging.warning('job failed: %s', exc)
                res, jcb = 'Failed', ui.toast_msg
            jfut = None
            ui.job_done(jcb, res)
            dirty = True
        if ui.refetch:
            ui.refetch, next_fetch = False, 0.0
        if ui.action:
            logging.info('running %s', ui.action)
            subprocess.Popen([ui.action])
            ui.action = None
        sync_osk()
        if dirty and mode == 'ui':
            t0 = time.monotonic()
            display.show(ui.render(data, stale, now))
            frames.append(time.monotonic() - t0)
            if len(frames) in (1, 10) or len(frames) % 100 == 0:
                logging.info('frame %d: %.0f ms (avg %.0f ms)', len(frames), frames[-1] * 1e3, sum(frames) / len(frames) * 1e3)
            dirty = False
    ui.unboost()
    stop_osk()
    if kb:
        kb.terminate()
    display.close()
    show_vt(1)


if __name__ == '__main__':
    main()
