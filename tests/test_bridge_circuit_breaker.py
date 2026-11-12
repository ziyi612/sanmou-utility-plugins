"""A stalled Lua bridge ends the cycle, rather than timing out every report."""
from __future__ import annotations
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from capture.game_assets.battle_collector import BattleCollector, install_lua
from capture.game_assets.export_battle_report_runtime import (
    RuntimeBridgeUnavailable, capture_report, decode_envelope, prefetch_reports, run_checked,
)

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None


TEMP = Path('.runtime/test-temp')
IDENTITY = {'avatarId': '42', 'serverId': '1', 'scenarioId': '14'}
CANCELLED = {'ok': False, 'stage': 'timeout', 'cancelled': True,
             'error': 'Lua job timed out after 10.00s; pending execution was cancelled'}


class FailedBridge:
    def __init__(self, *, install_succeeds=False):
        self.calls = []
        self.install_succeeds = install_succeeds

    def execute_on_update(self, code, timeout=10):
        self.calls.append((code, timeout))
        if self.install_succeeds and 'local capabilities' in code:
            return {'ok': True, 'result': json.dumps({'payload': {
                'runtimeIdentity': IDENTITY, 'capabilities': {'RPCGetBattleBlockList': True,
                    'RPCGetChildCombatInfoList': True}, 'segmentLength': 10, 'relationAll': 1000}})}
        return dict(CANCELLED)

    execute = execute_on_update


def seed(collector, count=400):
    collector.state['runtimeIdentity'] = dict(IDENTITY)
    collector.state['reports'] = {str(i): {'battleID': str(i), 'attempts': 0, 'complete': False,
        'lastSeenCycle': 0, 'observations': {}} for i in range(1, count + 1)}


class BridgeCircuitTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='bridge_circuit_', dir=TEMP)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_direct_and_package_imports_share_one_exception_class(self):
        direct = importlib.import_module('runtime_errors')
        package = importlib.import_module('capture.game_assets.runtime_errors')
        self.assertIs(direct.RuntimeBridgeUnavailable, package.RuntimeBridgeUnavailable)
        self.assertIs(RuntimeBridgeUnavailable, package.RuntimeBridgeUnavailable)

    def test_cancelled_lua_job_is_a_fatal_session_error(self):
        runner = FailedBridge()
        with self.assertRaises(RuntimeBridgeUnavailable):
            run_checked(runner, 'return 1')
        self.assertEqual(len(runner.calls), 1)

    def test_known_frida_transport_error_is_typed_without_importing_frida(self):
        error_type = type('TransportError', (Exception,), {'__module__': 'frida'})
        class Runner:
            def execute_on_update(self, code, timeout=10):
                raise error_type('connection lost')
        with self.assertRaises(RuntimeBridgeUnavailable) as caught:
            run_checked(Runner(), 'return 1')
        self.assertIsInstance(caught.exception.__cause__, error_type)

    def test_game_lua_business_error_is_not_a_bridge_error(self):
        class Runner:
            def execute_on_update(self, code, timeout=10):
                return {'ok': False, 'stage': 'lua', 'error': 'battle data unavailable'}
        with self.assertRaises(RuntimeError) as caught:
            run_checked(Runner(), 'return 1')
        self.assertNotIsInstance(caught.exception, RuntimeBridgeUnavailable)

    def test_native_hook_failure_ends_the_component_and_report_queue(self):
        good = {'available': True, 'complete': True, 'eventCount': 1}
        status = {name: dict(good) for name in ('basic', 'detail', 'statics', 'snapshots')}
        status['all'] = {'available': False, 'complete': False, 'eventCount': -1}
        for message in ('Error: access violation accessing 0x1a500090b34', 'Error: system error'):
            with self.subTest(message=message):
                class Runner:
                    def __init__(self):
                        self.calls = []

                    def execute_on_update(self, code, timeout=10):
                        self.calls.append(code)
                        if len(self.calls) == 1:
                            return {'ok': True, 'result': json.dumps({'payload': status})}
                        return {'ok': False, 'stage': 'lua_hook', 'error': message}

                runner = Runner()
                collector = BattleCollector(runner, self.root / str(len(message)))
                seed(collector)
                with self.assertRaises(RuntimeBridgeUnavailable):
                    collector.collect_pending()
                # Status + the first missing component only: no remaining
                # component, final export or another battle touches this bridge.
                self.assertEqual(len(runner.calls), 2)
                self.assertEqual(sum(e['attempts'] for e in collector.state['reports'].values()), 1)

    def test_native_frida_rpc_errors_are_fatal_but_lua_business_text_is_not(self):
        from capture.game_assets.runtime_errors import is_bridge_exception
        error_type = type('RPCException', (Exception,), {'__module__': 'frida.core'})
        for message in ('Error: access violation accessing 0x1a500090b34', 'Error: system error\n    at poll'):
            with self.subTest(message=message):
                self.assertTrue(is_bridge_exception(error_type(message)))
        self.assertFalse(is_bridge_exception(RuntimeError('lua: system error')))
        self.assertFalse(is_bridge_exception(error_type('battle data unavailable')))

    def test_prefetch_stops_at_first_bridge_failure(self):
        runner = FailedBridge()
        with self.assertRaises(RuntimeBridgeUnavailable):
            prefetch_reports(runner, [1, 2, 3])
        self.assertEqual(len(runner.calls), 1)

    def test_capture_does_not_try_remaining_components_or_long_export_after_bridge_failure(self):
        original = self.root / 'battle_1_decrypted.json'
        original.write_bytes(b'previous capture bytes remain intact')
        runner = FailedBridge()
        with self.assertRaises(RuntimeBridgeUnavailable):
            capture_report(runner, 1, self.root)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(original.read_bytes(), b'previous capture bytes remain intact')

    def test_server_component_timeout_stays_partial_without_reconnect(self):
        good = {'available': True, 'complete': True, 'eventCount': 1}
        status = {name: dict(good) for name in ('basic', 'detail', 'all', 'statics')}
        status['snapshots'] = {'available': False, 'complete': False}
        data = {'battleID': 1, 'eventDataList': [{'eventId': 1}]}
        payload = {'battleID': 1, 'source': 'all', 'eventCount': 1,
                   'data': data, 'components': {'basic': data, 'detail': data, 'all': data, 'statics': {}}}
        def checked(runner, code, **kwargs):
            return json.dumps({'payload': payload}) if 'captureVersion=2' in code else 'requested snapshots 1'
        with patch('capture.game_assets.export_battle_report_runtime.read_status', return_value=status), \
             patch('capture.game_assets.export_battle_report_runtime.run_checked', side_effect=checked):
            result = capture_report(None, 1, self.root, wait_seconds=0)
        self.assertTrue(result['exported'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['componentStatus']['snapshots']['error'], 'timeout')

    def test_four_hundred_report_queue_ends_after_one_failed_bridge_call(self):
        runner = FailedBridge()
        progress = []
        collector = BattleCollector(runner, self.root, on_progress=progress.append)
        seed(collector)
        with self.assertRaises(RuntimeBridgeUnavailable):
            collector.collect_pending()
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(sum(e['attempts'] for e in collector.state['reports'].values()), 1)
        self.assertEqual(len(progress), 3)  # Started, typed failure, final duration.
        failed = next(event for event in progress if event['phase'] == 'capture_failed')
        self.assertEqual(failed['phase'], 'capture_failed')
        self.assertTrue(failed['runtime_unavailable'])
        self.assertEqual(failed['error_type'], 'RuntimeBridgeUnavailable')
        self.assertEqual(progress[-1]['phase'], 'capture_finished')
        self.assertEqual(progress[-1]['outcome'], 'failed')

    def test_cycle_rethrows_and_saves_failure_manifest_without_dead_bridge_cleanup(self):
        runner = FailedBridge(install_succeeds=True)
        collector = BattleCollector(runner, self.root, scopes=('personal',))
        seed(collector)
        with self.assertRaises(RuntimeBridgeUnavailable):
            collector.run_cycle()
        self.assertEqual(len(runner.calls), 2)  # Install plus first list; no old report or dead-bridge cleanup.
        self.assertEqual(sum(e['attempts'] for e in collector.state['reports'].values()), 0)
        manifest = json.loads((self.root / 'collector_manifest.json').read_text(encoding='utf-8'))
        self.assertTrue(manifest['runtime_unavailable'])
        self.assertTrue(manifest['cleanupDeferred'])
        self.assertFalse(manifest['complete'])
        self.assertEqual(manifest['scopes'], {})

    def test_bridge_error_crosses_root_and_child_discovery_catches(self):
        caps = {'capabilities': {'RPCGetBattleBlockList': True, 'RPCGetChildCombatInfoList': True},
                'segmentLength': 10, 'relationAll': 1000}
        for child_failure in (False, True):
            with self.subTest(child_failure=child_failure):
                collector = BattleCollector(None, self.root / str(child_failure), scopes=('personal',))
                def page(scope, index, relation, segment, parent=None):
                    if not child_failure or parent is not None:
                        raise RuntimeBridgeUnavailable('bridge unavailable')
                    return ([{'combatInfo': {'battleID': 1}, 'childNum': 1}], None,
                            {'sIndex': 1, 'reqLen': 1}, self.root / 'page.json')
                with patch.object(collector, 'page', side_effect=page) as requests:
                    with self.assertRaises(RuntimeBridgeUnavailable):
                        collector.discover_scope('personal', caps)
                self.assertEqual(requests.call_count, 2 if child_failure else 1)

    def test_parallel_bridge_error_propagates_after_bounded_export_only_recovery(self):
        collector = BattleCollector(None, self.root, parallelism=2)
        seed(collector, 4)
        with patch('capture.game_assets.battle_collector.prefetch_reports', side_effect=RuntimeBridgeUnavailable('lost bridge')), \
             patch('capture.game_assets.battle_collector.capture_report', side_effect=RuntimeBridgeUnavailable('session unavailable')) as capture:
            with self.assertRaises(RuntimeBridgeUnavailable):
                collector.collect_pending()
        self.assertEqual(capture.call_count, 2)
        self.assertTrue(all(c.kwargs['request_missing'] is False for c in capture.call_args_list))
        self.assertEqual(collector.state['reports']['3']['attempts'], 0)
        self.assertEqual(collector.state['reports']['4']['attempts'], 0)
        self.assertTrue(Path(collector.state['prefetchRecoveryErrorLog']).exists())

    def test_owner_id_validates_and_lease_wrapper_uses_same_process_owner(self):
        for value in ('', '   ', False, 123):
            with self.assertRaises(ValueError):
                BattleCollector(None, self.root, owner_id=value)
        first = BattleCollector(None, self.root, owner_id='same-daemon-owner')
        second = BattleCollector(None, self.root, owner_id='same-daemon-owner')
        self.assertEqual(first.owner, second.owner)
        self.assertEqual(first.runner.owner, first.owner)
        self.assertEqual(second.runner.owner, second.owner)

    @unittest.skipIf(LuaRuntime is None, 'optional Lua 5.1 test runtime')
    def test_known_owner_can_recover_its_active_lease_but_other_owner_cannot(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute('''
CommonConst={SEG_LEN=10,BattleBaseRelationType={ALL=1000}}
G_SCENARIO_ID=14
ClientProxy={avatarId=42,GetServerId=function() return 1 end}
ClientProxy.RPCGetBattleBlockList=function() end
ClientProxy.RPCGetBattleBlockListResponse=function() end
''')
        self.assertNotIn('error', decode_envelope(lua.execute(install_lua('same-owner')))['payload'])
        self.assertNotIn('error', decode_envelope(lua.execute(install_lua('same-owner')))['payload'])
        self.assertIn('active lease', decode_envelope(lua.execute(install_lua('different-owner')))['payload']['error'])


if __name__ == '__main__':
    unittest.main()
