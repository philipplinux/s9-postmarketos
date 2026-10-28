import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import stat
import subprocess
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

def load_helper():
    path = ROOT / 'wireguard' / 's9-wireguard'
    loader = importlib.machinery.SourceFileLoader('wireguard_helper', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module

class WireGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wg = load_helper()

    def test_missing_config_is_unconfigured_and_start_fails_closed(self):
        calls = []
        run = lambda *a, **k: calls.append(a)
        with tempfile.TemporaryDirectory() as temp:
            conf = Path(temp) / 'missing'
            self.assertEqual(self.wg.status(conf=conf, run=run)['state'], 'Unconfigured')
            self.assertFalse(self.wg.control('start', conf=conf, run=run)['ok'])
        self.assertEqual(calls, [])

    def test_parser_rejects_shell_paths_duplicates_unknown_keys(self):
        for text in ('PROFILE=../wg0', 'SERVICE=x;reboot', 'PROFILE=wg0\nPROFILE=wg1',
                     'PROFILE=$(id)', 'UNKNOWN=x', 'SERVICE=-foo', 'PROFILE=wg0\nSERVICE=../../x'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.wg.parse_config(text)
        self.assertEqual(self.wg.parse_config('PROFILE=wg0\nSERVICE=s9-vpn\n'), ('wg0', 's9-vpn'))

    def test_control_rejects_unknown_actions_without_running_commands(self):
        calls = []
        result = self.wg.control('restart;reboot', run=lambda *a, **k: calls.append(a))
        self.assertFalse(result['ok'])
        self.assertEqual(calls, [])

    def test_status_is_public_and_interface_up_even_when_openrc_is_stopped(self):
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            self.assertLessEqual(kwargs['timeout'], 15)
            out = '[{"flags":["UP"]}]' if cmd[0] == '/sbin/ip' else 'PUBLICKEY\t100\n' if cmd[0] == '/usr/bin/wg' else ''
            return SimpleNamespace(returncode=3 if cmd[0] == '/sbin/rc-service' else 0, stdout=out)
        with patch.object(self.wg, 'settings', return_value=('wg0', 's9-vpn')), \
             patch.object(self.wg, 'runlevel_enabled', return_value=False):
            result = self.wg.status(run=run)
        self.assertTrue(result['up'])
        self.assertEqual(result['peers'], 1)
        self.assertNotIn('PUBLICKEY', str(result))
        self.assertFalse(any('dump' in c or 'showconf' in c for c in calls))

    def test_control_uses_only_fixed_service_and_bounded_commands(self):
        calls = []
        original = self.wg.settings
        self.wg.settings = lambda conf: ('wg0', 's9-vpn')
        try:
            result = self.wg.control('start', run=lambda cmd, **kw: (calls.append((cmd, kw)) or SimpleNamespace(returncode=0)))
        finally:
            self.wg.settings = original
        self.assertTrue(result['ok'])
        self.assertEqual(calls[0][0], ['/sbin/rc-service', 's9-vpn', 'start'])
        self.assertEqual(calls[0][1]['timeout'], 15)

    def test_untrusted_metadata_fails_closed(self):
        for mode, uid in ((stat.S_IFREG | 0o666, 0), (stat.S_IFREG | 0o600, 10000),
                          (stat.S_IFLNK | 0o777, 0)):
            with self.subTest(mode=mode, uid=uid), patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=mode, st_uid=uid)):
                with self.assertRaises(ValueError):
                    self.wg.trusted('/etc/s9/wireguard', regular=True)

    def test_timeout_is_bounded_and_does_not_relay_service_output(self):
        for action in ('start', 'stop'):
            calls = []
            def run(cmd, **kwargs):
                calls.append((cmd, kwargs))
                raise subprocess.TimeoutExpired(cmd, kwargs['timeout'],
                    output='sensitive service output', stderr='sensitive service error')
            with self.subTest(action=action), \
                 patch.object(self.wg, 'settings', return_value=('wg0', 'my-vpn')):
                result = self.wg.control(action, run=run)
                self.assertEqual(result, {'ok': False, 'message': 'WireGuard service action timed out'})
                self.assertNotIn('sensitive', str(result))
                self.assertEqual(calls[0][0], ['/sbin/rc-service', 'my-vpn', action])
                self.assertEqual(calls[0][1]['timeout'], 15)
                self.assertEqual(len(calls), 1)

    def test_autostart_uses_exact_fixed_runlevel(self):
        calls = []
        enabled = False
        def run(cmd, **kw):
            nonlocal enabled
            calls.append(cmd)
            enabled = cmd[1] == 'add'
            return SimpleNamespace(returncode=0)
        with patch.object(self.wg, 'settings', return_value=('wg0', 'my-vpn')), \
             patch.object(self.wg, 'runlevel_enabled', side_effect=lambda service: enabled, create=True):
            for action in ('autostart-on', 'autostart-on', 'autostart-off', 'autostart-off'):
                self.assertTrue(self.wg.control(action, run=run)['ok'])
        self.assertEqual(calls, [['/sbin/rc-update', 'add', 'my-vpn', 'default'],
                                 ['/sbin/rc-update', 'del', 'my-vpn', 'default']])

    def test_autostart_already_in_desired_state_does_not_run_rc_update(self):
        for action, enabled in (('autostart-on', True), ('autostart-off', False)):
            calls = []
            with self.subTest(action=action), \
                 patch.object(self.wg, 'settings', return_value=('wg0', 'my-vpn')), \
                 patch.object(self.wg, 'runlevel_enabled', return_value=enabled, create=True):
                result = self.wg.control(action, run=lambda *a, **k: (calls.append(a) or SimpleNamespace(returncode=0)))
                self.assertTrue(result['ok'])
                self.assertEqual(calls, [])

    def test_untrusted_runlevel_entry_blocks_autostart_without_running_commands(self):
        calls = []
        with patch.object(self.wg, 'settings', return_value=('wg0', 'my-vpn')), \
             patch.object(self.wg, 'runlevel_enabled', side_effect=ValueError('Untrusted runlevel')):
            result = self.wg.control('autostart-on', run=lambda *a, **k: calls.append(a))
        self.assertFalse(result['ok'])
        self.assertEqual(calls, [])

    def test_runlevel_entry_must_be_root_owned_symlink_to_configured_service(self):
        cases = ((stat.S_IFREG | 0o600, 0), (stat.S_IFLNK | 0o777, 10000))
        for mode, uid in cases:
            with self.subTest(mode=mode, uid=uid), patch.object(self.wg, 'trusted'), \
                 patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=mode, st_uid=uid)):
                with self.assertRaises(ValueError):
                    self.wg.runlevel_enabled('my-vpn')
        with patch.object(self.wg, 'trusted'), \
             patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)), \
             patch.object(Path, 'resolve', side_effect=[Path('/etc/init.d/other-service'), Path('/etc/init.d/my-vpn')]):
            with self.assertRaises(ValueError):
                self.wg.runlevel_enabled('my-vpn')

    def test_autostart_success_requires_actual_runlevel_state_change(self):
        with patch.object(self.wg, 'settings', return_value=('wg0', 'my-vpn')), \
             patch.object(self.wg, 'runlevel_enabled', return_value=False):
            result = self.wg.control('autostart-on', run=lambda *a, **k: SimpleNamespace(returncode=0))
        self.assertFalse(result['ok'])

    def test_runlevel_absence_and_valid_root_link_report_state(self):
        with patch.object(self.wg, 'trusted'), patch.object(Path, 'lstat', side_effect=FileNotFoundError):
            self.assertFalse(self.wg.runlevel_enabled('my-vpn'))
        with patch.object(self.wg, 'trusted'), \
             patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)), \
             patch.object(Path, 'resolve', return_value=Path('/etc/init.d/my-vpn')):
            self.assertTrue(self.wg.runlevel_enabled('my-vpn'))

    def test_stop_service_error_is_not_masked(self):
        for returncode in (1, 3, -15):
            calls = []
            def run(cmd, **kwargs):
                calls.append(cmd)
                return SimpleNamespace(returncode=returncode,
                    stdout='sensitive service output', stderr='sensitive service error')
            with self.subTest(returncode=returncode), \
                 patch.object(self.wg, 'settings', return_value=('wg0', 'my-vpn')):
                result = self.wg.control('stop', run=run)
                self.assertEqual(result, {'ok': False, 'message': 'WireGuard service action failed'})
                self.assertEqual(calls, [['/sbin/rc-service', 'my-vpn', 'stop']])

    def test_failed_stop_and_down_status_are_independent(self):
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            # A wrapper can remove the interface and then fail a later cleanup.
            return SimpleNamespace(returncode=1, stdout='', stderr='')
        with patch.object(self.wg, 'settings', return_value=('wg0', 'my-vpn')), \
             patch.object(self.wg, 'runlevel_enabled', return_value=False):
            action = self.wg.control('stop', run=run)
            status = self.wg.status(run=run)
        self.assertFalse(action['ok'])
        self.assertEqual(status['state'], 'Down')
        self.assertFalse(status['up'])
        self.assertEqual(calls, [['/sbin/rc-service', 'my-vpn', 'stop'],
                                 ['/sbin/ip', '-j', 'link', 'show', 'dev', 'wg0']])

    def test_already_stopped_openrc_warning_with_zero_exit_is_success(self):
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout='',
                stderr='* WARNING: my-vpn is already stopped')
        with patch.object(self.wg, 'settings', return_value=('wg0', 'my-vpn')):
            result = self.wg.control('stop', run=run)
        self.assertEqual(result, {'ok': True, 'message': 'Saved'})
        self.assertEqual(calls, [['/sbin/rc-service', 'my-vpn', 'stop']])


class CollectorTests(unittest.TestCase):
    def test_dashboard_requests_only_status_and_publishes_only_public_fields(self):
        import json
        spec = importlib.util.spec_from_file_location('wg_test_services', ROOT / 'dashboard' / 'services.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == 'sudo':
                return SimpleNamespace(returncode=0, stdout=json.dumps({'up': True, 'configured': True, 'private_key': 'EXCLUDED'}))
            return SimpleNamespace(returncode=3, stdout='')
        result = module.services_info(run, '/nonexistent')
        self.assertTrue(result['wireguard']['up'])
        self.assertNotIn('EXCLUDED', str(result))
        self.assertEqual([c for c in calls if c[0] == 'sudo'], [['sudo', '-n', '/usr/local/sbin/s9-wireguard', 'status']])

if __name__ == '__main__':
    unittest.main()

class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Compile only pure declarations/functions: no framebuffer or Linux imports.
        import ast
        source = ast.parse((ROOT / 'display' / 'display.py').read_text(encoding='utf-8'))
        wanted = {'NET_ROWS', 'OV_NET', 'TAB_Y', 'WG_START', 'WG_STOP', 'WG_AUTO'}
        body = []
        for node in source.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in wanted for t in node.targets):
                body.append(node)
            elif isinstance(node, ast.FunctionDef) and node.name in ('ov_service_box', 'net_box', 'wg_state', 'hit'):
                body.append(node)
        cls.ns = {}
        exec(compile(ast.Module(body=body, type_ignores=[]), '<layout>', 'exec'), cls.ns)

    def test_wireguard_follows_tailscale_and_network_rows_fit(self):
        keys = [k for k, _ in self.ns['NET_ROWS']]
        self.assertEqual(keys[keys.index('tailscale') + 1], 'wireguard')
        self.assertLess(self.ns['net_box'](len(keys)-1)[3], self.ns['TAB_Y'])

    def test_five_nonoverlapping_overview_cards_inside_same_width(self):
        boxes = [self.ns['ov_service_box'](i) for i in range(5)]
        self.assertGreaterEqual(boxes[0][0], 60)
        self.assertLessEqual(boxes[-1][2], 1020)
        self.assertTrue(all(a[2] < b[0] for a, b in zip(boxes, boxes[1:])))
        self.assertTrue(all(b[1:] != () and b[3] <= 1560 for b in boxes))

    def test_unconfigured_is_distinct_from_down_and_unknown(self):
        f = self.ns['wg_state']
        self.assertEqual(f({'configured': False}), ('Unconfigured', None))
        self.assertEqual(f({'configured': True, 'up': False}), ('Down', False))
        self.assertEqual(f({'configured': True, 'up': True}), ('Up', True))
        self.assertEqual(f({}), ('Unknown', None))
