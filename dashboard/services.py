"""Read-only SSH (dropbear via OpenRC), Tailscale, USB gadget and Bluetooth status."""
import json
import subprocess
import time
from pathlib import Path

_cached = None
_cached_at = 0
_cached_key = None

_USB_NOT_CONFIGURED = {
    'not attached', 'not-attached', 'attached', 'powered', 'reconnecting',
    'unauthenticated', 'default', 'addressed', 'address', 'suspended',
}


def _usb_info(sys_root):
    """Report USB data readiness from the read-only UDC state sysfs node."""
    try:
        controllers = sorted((Path(sys_root) / 'class' / 'udc').iterdir())
    except OSError:
        return {'up': None}
    states = []
    for controller in controllers:
        try:
            states.append((controller / 'state').read_text().strip().lower())
        except OSError:
            states.append(None)
    if 'configured' in states:
        return {'up': True}
    if states and all(s in _USB_NOT_CONFIGURED for s in states):
        return {'up': False}
    return {'up': None}


def services_info(run=subprocess.run, sys_root='/sys'):
    global _cached, _cached_at, _cached_key
    cache_key = (str(sys_root), run)
    if _cached is not None and _cached_key == cache_key and time.monotonic() - _cached_at < 1:
        return _cached
    result = {'ssh': {'up': None}, 'tailscale': {'up': None}, 'wireguard': {'up': None}, 'usb': _usb_info(sys_root), 'bluetooth': {'up': None, 'address': None}}
    try:
        show = run(['bluetoothctl', 'show'], capture_output=True, text=True, timeout=2)
        if show.returncode == 0 and 'Powered:' in show.stdout:
            result['bluetooth']['up'] = 'Powered: yes' in show.stdout
            result['bluetooth']['address'] = show.stdout.split()[1] if show.stdout.startswith('Controller') else None
    except (OSError, subprocess.SubprocessError):
        pass
    try:  # rc-service status: exit 0 = started, 3 = stopped
        code = run(['rc-service', 'dropbear', 'status'], capture_output=True, text=True, timeout=2).returncode
        result['ssh']['up'] = {0: True, 3: False}.get(code)
    except (OSError, subprocess.SubprocessError):
        pass
    try:  # BackendState: NeedsLogin (logged out) / Stopped / Starting / Running; tailscaled down = unknown
        out = run(['tailscale', 'status', '--json'], capture_output=True, text=True, timeout=2)
        if out.returncode == 0:
            st = json.loads(out.stdout)
            peers = st.get('Peer') or {}
            result['tailscale'] = {'up': st.get('BackendState') == 'Running', 'state': st.get('BackendState'),
                                   'ip': next((a for a in st.get('TailscaleIPs') or [] if '.' in a), None),
                                   'name': (st.get('Self') or {}).get('HostName'),
                                   'peers': len(peers), 'online': sum(1 for q in peers.values() if q.get('Online'))}
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        pass
    try:
        out = run(['sudo', '-n', '/usr/local/sbin/s9-wireguard', 'status'], capture_output=True, text=True, timeout=7)
        if out.returncode == 0:
            wg = json.loads(out.stdout)
            if isinstance(wg, dict):
                result['wireguard'] = {key: wg.get(key) for key in (
                    'up', 'state', 'configured', 'profile', 'service', 'autostart', 'peers', 'latest_handshake')}
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    _cached, _cached_at, _cached_key = result, time.monotonic(), cache_key
    return result


if __name__ == '__main__':
    class R:  # fake completed process
        def __init__(self, rc, out=''): self.returncode, self.stdout = rc, out
    ts = '{"BackendState":"Running","TailscaleIPs":["100.64.0.7","fd7a::7"],"Self":{"HostName":"s9"},"Peer":{"a":{"Online":true},"b":{}}}'
    fake = lambda cmd, **k: R(0, 'Controller 6C:C7:EC:8E:BE:59 (public)\n\tPowered: yes\n') if cmd[0] == 'bluetoothctl' else R(0, ts) if cmd[0] == 'tailscale' else R(3)
    r = services_info(fake, '/nonexistent')
    assert r['ssh']['up'] is False and r['bluetooth'] == {'up': True, 'address': '6C:C7:EC:8E:BE:59'} and r['usb']['up'] is None
    assert r['tailscale'] == {'up': True, 'state': 'Running', 'ip': '100.64.0.7', 'name': 's9', 'peers': 2, 'online': 1}
    print('ok')
