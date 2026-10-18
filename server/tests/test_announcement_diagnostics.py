"""Issue #170: structured, actionable announcement diagnostics.

These tests pin the diagnostics contract that the dashboard renders: every
announcement problem is classified (real-ip, wrong-port, pinned-ip,
stale-endpoint, auto-discovery, missing-endpoint, config-unreadable,
verification-pending), attributed to its source (node config file vs. live
gossip), and paired with mode-aware remediation commands. The fail-closed
policy itself is unchanged: the same conflicts still block routing.
"""
import json
import os
import sys
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as app_module  # noqa: E402
from app import app  # noqa: E402


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SERVER = 'us3.tunnelsats.com'
SERVER_IP = '178.156.167.202'
PORT = 23217
EXPECTED = f'{SERVER}:{PORT}'
LND_HOST_PATH = '/home/umbrel/umbrel/app-data/lightning/data/lnd/lnd.conf'
UMBREL_COMPOSE = '/home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml'


@pytest.fixture(autouse=True)
def hermetic_server_resolution():
    """Never hit real DNS; individual tests opt into the original resolver."""
    original = app_module._tunnelsats_server_ips
    app_module._server_ip_cache.clear()
    app_module._server_ip_inflight.clear()
    with patch('app._tunnelsats_server_ips', return_value=(SERVER_IP,)) as mocked:
        yield mocked, original
    app_module._server_ip_cache.clear()
    app_module._server_ip_inflight.clear()


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as test_client:
        yield test_client


def _server_matcher(host):
    return host in (SERVER, SERVER_IP)


def _diag(kind, source='config_file', **fields):
    entry = {
        'impl': 'lnd',
        'kind': kind,
        'source': source,
        'setting': None,
        'value': None,
        'line': None,
        'line_number': None,
        'address': None,
        'port_defaulted': False,
        'expected': EXPECTED,
        'expected_line': f'externalhosts={EXPECTED}',
        'config_path': LND_HOST_PATH,
        'withdraw_command': None,
        'detail': None,
    }
    entry.update(fields)
    return entry


def _withdraw(address, container='lightning_lnd_1'):
    return f'sudo docker exec {container} lncli peers updatenodeannouncement --address_remove={address}'


def _write(path, content):
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write(content)


def _dataplane(**overrides):
    state = app_module.read_dataplane_state()
    state.update({'rules_synced': True, 'last_error': None})
    state.update(overrides)
    return state


def _lnd_container(name='lightning_lnd_1'):
    return [{
        'Names': [f'/{name}'],
        'Id': 'lnd1',
        'NetworkSettings': {'Networks': {'main': {'IPAddress': '10.21.21.9'}}},
    }]


# --- Cycle 1: endpoint parsing and classification -------------------------------------------


class TestEndpointParsing:
    def test_defaults_lightning_port_and_strips_pubkey(self):
        assert app_module.split_announced_endpoint('178.156.167.202') == (SERVER_IP, 9735, True)
        assert app_module.split_announced_endpoint('02abc@203.0.113.5:9736') == ('203.0.113.5', 9736, False)
        assert app_module.split_announced_endpoint('US3.TunnelSats.com.:23217') == (SERVER, PORT, False)

    def test_handles_ipv6_forms(self):
        assert app_module.split_announced_endpoint('[2001:db8::1]:9735') == ('2001:db8::1', 9735, False)
        assert app_module.split_announced_endpoint('[2001:db8::1]') == ('2001:db8::1', 9735, True)
        assert app_module.split_announced_endpoint('2001:db8::1') == ('2001:db8::1', 9735, True)

    @pytest.mark.parametrize('value', [
        '', '   ', 'host:port', '1.2.3.4:0', '1.2.3.4:70000', '1.2.3.4;rm -rf ~',
        '$(id):9735', '[not-ipv6]:9735', '-bad.example.com:9735', 'a b:9735',
        '[fe80::1%eth0]:9735', "[fe80::1%$(reboot)]:9735", 'fe80::1%eth0', '[2001:db8::1]:',
        # str.isdigit() accepts these, int() does not (or they are not ASCII ports at all).
        '203.0.113.5:\u00b2', '203.0.113.5:\u0669\u0667\u0663\u0665', '[2001:db8::1]:\u00b9', '203.0.113.5:0009735',
    ])
    def test_rejects_values_that_cannot_be_announced_safely(self, value):
        assert app_module.split_announced_endpoint(value) is None


class TestAnnouncementClassification:
    def test_non_tunnelsats_address_is_real_ip(self):
        assert app_module.classify_announced_address(
            '203.0.113.5:9735', SERVER, PORT, _server_matcher
        ) == ('real-ip', '203.0.113.5:9735', False)

    def test_tunnelsats_ip_without_port_is_wrong_port(self):
        # The #170 customer case: the server IP announced on LND's default port.
        assert app_module.classify_announced_address(
            SERVER_IP, SERVER, PORT, _server_matcher
        ) == ('wrong-port', f'{SERVER_IP}:9735', True)

    def test_tunnelsats_hostname_on_another_port_is_wrong_port(self):
        assert app_module.classify_announced_address(
            f'{SERVER}:9735', SERVER, PORT, _server_matcher
        ) == ('wrong-port', f'{SERVER}:9735', False)

    def test_correct_server_and_port_flagged_by_policy_is_pinned_ip(self):
        assert app_module.classify_announced_address(
            f'{SERVER_IP}:{PORT}', SERVER, PORT, _server_matcher
        ) == ('pinned-ip', f'{SERVER_IP}:{PORT}', False)

    def test_other_tunnelsats_endpoint_is_stale_endpoint(self):
        assert app_module.classify_announced_address(
            'de2.tunnelsats.com:35825', SERVER, PORT, _server_matcher
        ) == ('stale-endpoint', 'de2.tunnelsats.com:35825', False)

    def test_unparseable_value_is_real_ip_without_address(self):
        assert app_module.classify_announced_address(
            '', SERVER, PORT, _server_matcher
        ) == ('real-ip', None, False)

    def test_matcher_resolves_only_the_tunnelsats_domain_for_ip_literals(self, hermetic_server_resolution):
        resolver, _original = hermetic_server_resolution
        matcher = app_module._server_host_matcher(SERVER, extra_hosts=['198.51.100.77'])

        assert matcher('mynode.example.com') is False
        resolver.assert_not_called()
        assert matcher('198.51.100.77') is True
        assert matcher(SERVER.upper()) is True
        resolver.assert_not_called()
        assert matcher(SERVER_IP) is True
        assert matcher('203.0.113.5') is False
        resolver.assert_called_once_with(SERVER)

    def test_server_ip_resolution_is_bounded_cached_and_fails_soft(self, hermetic_server_resolution):
        _mock, original = hermetic_server_resolution
        with patch('socket.gethostbyname_ex', return_value=(SERVER, [], [SERVER_IP])) as dns:
            assert original(SERVER) == (SERVER_IP,)
            assert original(f'{SERVER.upper()}.') == (SERVER_IP,)
        dns.assert_called_once_with(SERVER)

        app_module._server_ip_cache.clear()
        with patch('socket.gethostbyname_ex', side_effect=OSError('resolver down')):
            assert original(SERVER) == ()
        assert original('') == ()

    def test_failed_refresh_keeps_the_last_good_server_addresses(self, hermetic_server_resolution):
        _mock, original = hermetic_server_resolution
        with patch('socket.gethostbyname_ex', return_value=(SERVER, [], [SERVER_IP])):
            assert original(SERVER) == (SERVER_IP,)
        stamp, ips = app_module._server_ip_cache[SERVER]
        app_module._server_ip_cache[SERVER] = (stamp - 3600, ips)  # expire the positive entry

        with patch('socket.gethostbyname_ex', side_effect=OSError('resolver down')) as dns:
            # A transient failure must not make the server's own IP look like a foreign address.
            assert original(SERVER) == (SERVER_IP,)
            assert original(SERVER) == (SERVER_IP,)
        dns.assert_called_once_with(SERVER)

    def test_hanging_resolver_is_never_queued_twice(self, hermetic_server_resolution):
        _mock, original = hermetic_server_resolution
        release = threading.Event()
        lookups = []

        def hanging_lookup(host):
            lookups.append(host)
            release.wait(5)
            return (SERVER, [], [SERVER_IP])

        try:
            with patch('socket.gethostbyname_ex', side_effect=hanging_lookup), \
                    patch('app._SERVER_IP_RESOLVE_TIMEOUT_SECONDS', 0.05), \
                    patch('app._SERVER_IP_NEGATIVE_CACHE_TTL_SECONDS', 0.0):
                assert original(SERVER) == ()
                assert original(SERVER) == ()  # waits on the same lookup instead of queueing another
                release.set()
                app_module._server_ip_inflight[SERVER].result(timeout=5)
                assert original(SERVER) == (SERVER_IP,)
        finally:
            release.set()
        assert lookups == [SERVER]
        assert SERVER not in app_module._server_ip_inflight


# --- Cycle 2: diagnostics builders and commands ---------------------------------------------


class TestConfigDiagnostics:
    def _audit(self, content, node_type='lnd'):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, 'lnd.conf' if node_type == 'lnd' else 'config')
            _write(path, content)
            return app_module.audit_node_announcement_config(node_type, path, SERVER, PORT)

    def test_classifies_each_conflict_with_line_numbers_and_commands(self):
        audit = self._audit(
            '[Application Options]\n'
            'alias=customer\n'
            f'externalhosts={EXPECTED}\n'
            f'externalip={SERVER_IP}\n'
            'externalip=203.0.113.5:9735\n'
            f'externalip={SERVER_IP}:{PORT}\n'
            'externalhosts=de2.tunnelsats.com:35825\n'
            'externalip=examplehiddenservice.onion:9735\n'
            'nat=true\n'
        )
        assert audit['conflicts'] == [
            f'externalip={SERVER_IP}',
            'externalip=203.0.113.5:9735',
            f'externalip={SERVER_IP}:{PORT}',
            'externalhosts=de2.tunnelsats.com:35825',
            'nat=true',
        ]

        diagnostics = app_module.build_config_announcement_diagnostics(
            'lnd', audit, SERVER, PORT,
            config_path=LND_HOST_PATH,
            cli_prefix='sudo docker exec lightning_lnd_1',
            is_server_host=_server_matcher,
        )

        assert diagnostics == [
            _diag('wrong-port', setting='externalip', value=SERVER_IP,
                  line=f'externalip={SERVER_IP}', line_number=4,
                  address=f'{SERVER_IP}:9735', port_defaulted=True,
                  withdraw_command=_withdraw(f'{SERVER_IP}:9735')),
            _diag('real-ip', setting='externalip', value='203.0.113.5:9735',
                  line='externalip=203.0.113.5:9735', line_number=5,
                  address='203.0.113.5:9735',
                  withdraw_command=_withdraw('203.0.113.5:9735')),
            # Withdrawing a pinned TunnelSats address would remove a valid route.
            _diag('pinned-ip', setting='externalip', value=f'{SERVER_IP}:{PORT}',
                  line=f'externalip={SERVER_IP}:{PORT}', line_number=6,
                  address=f'{SERVER_IP}:{PORT}'),
            # Hostnames are announced as resolved IPs, so no exact withdraw command.
            _diag('stale-endpoint', setting='externalhosts', value='de2.tunnelsats.com:35825',
                  line='externalhosts=de2.tunnelsats.com:35825', line_number=7,
                  address='de2.tunnelsats.com:35825'),
            _diag('auto-discovery', setting='nat', value='true', line='nat=true', line_number=9),
        ]

    def test_unresolved_server_ip_is_never_offered_for_withdrawal(self, hermetic_server_resolution):
        resolver, _original = hermetic_server_resolution
        resolver.return_value = ()  # The TunnelSats domain did not resolve.
        audit = self._audit(
            '[Application Options]\n'
            f'externalhosts={EXPECTED}\n'
            f'externalip={SERVER_IP}:{PORT}\n'
            'externalip=203.0.113.5:9735\n'
            'externalhosts=myhome.example.com:9735\n'
        )
        diagnostics = app_module.build_config_announcement_diagnostics(
            'lnd', audit, SERVER, PORT,
            config_path=LND_HOST_PATH,
            cli_prefix='sudo docker exec lightning_lnd_1',
        )
        assert [(d['kind'], d['address'], d['withdraw_command'], d['detail']) for d in diagnostics] == [
            # Either IP may be the TunnelSats server itself, so never suggest withdrawing it.
            ('real-ip', f'{SERVER_IP}:{PORT}', None, 'server_unresolved'),
            ('real-ip', '203.0.113.5:9735', None, 'server_unresolved'),
            # Hostnames are compared by name, so DNS does not matter for them.
            ('real-ip', 'myhome.example.com:9735', None, None),
        ]

    def test_cln_conflicts_never_offer_lnd_withdraw_commands(self):
        audit = self._audit('announce-addr=203.0.113.5\nip-discovery=true\n', node_type='cln')
        diagnostics = app_module.build_config_announcement_diagnostics(
            'cln', audit, SERVER, PORT,
            config_path='/cln/config',
            cli_prefix='sudo docker exec core-lightning_lightningd_1',
            is_server_host=_server_matcher,
        )
        common = {'impl': 'cln', 'expected_line': f'announce-addr={EXPECTED}', 'config_path': '/cln/config'}
        assert diagnostics == [
            _diag('real-ip', setting='announce-addr', value='203.0.113.5',
                  line='announce-addr=203.0.113.5', line_number=1,
                  address='203.0.113.5:9735', port_defaulted=True, **common),
            _diag('auto-discovery', setting='ip-discovery', value='true',
                  line='ip-discovery=true', line_number=2, **common),
        ]

    def test_missing_endpoint_is_reported_with_the_line_to_add(self):
        audit = self._audit('[Application Options]\nnat=false\n')
        diagnostics = app_module.build_config_announcement_diagnostics(
            'lnd', audit, SERVER, PORT,
            config_path=LND_HOST_PATH, check_missing_endpoint=True,
            is_server_host=_server_matcher,
        )
        assert diagnostics == [_diag('missing-endpoint', setting='externalhosts')]

    def test_unknown_subscription_endpoint_is_reported_explicitly(self):
        audit = self._audit('[Application Options]\nnat=false\n')
        diagnostics = app_module.build_config_announcement_diagnostics(
            'lnd', audit, '', 0, config_path=LND_HOST_PATH, check_missing_endpoint=True,
        )
        assert diagnostics == [_diag(
            'missing-endpoint', setting='externalhosts',
            expected=None, expected_line=None, detail='endpoint_unknown',
        )]

    def test_unreadable_and_missing_configs(self):
        unreadable = {'readable': False, 'settings': [], 'conflicts': ['config-unreadable'], 'conflict_entries': []}
        missing = {'readable': False, 'settings': [], 'conflicts': [], 'conflict_entries': []}

        assert app_module.build_config_announcement_diagnostics(
            'lnd', unreadable, SERVER, PORT, config_path=LND_HOST_PATH,
        ) == [_diag('config-unreadable', detail='unreadable')]
        assert app_module.build_config_announcement_diagnostics(
            'lnd', missing, SERVER, PORT, detected=True, config_path=LND_HOST_PATH,
        ) == [_diag('config-unreadable', detail='missing')]
        assert app_module.build_config_announcement_diagnostics(
            'lnd', missing, SERVER, PORT, detected=False, config_path=LND_HOST_PATH,
        ) == []


class TestRemediationCommands:
    def test_withdraw_command_quotes_ipv6_and_refuses_unsafe_values(self):
        prefix = 'sudo docker exec lightning_lnd_1'
        assert app_module._withdraw_command(prefix, '203.0.113.5:9735') == _withdraw('203.0.113.5:9735')
        assert app_module._withdraw_command(prefix, '[2001:db8::1]:9735') == (
            "sudo docker exec lightning_lnd_1 lncli peers updatenodeannouncement "
            "'--address_remove=[2001:db8::1]:9735'"
        )
        assert app_module._withdraw_command(prefix, '1.2.3.4:9735; rm -rf ~') is None
        assert app_module._withdraw_command(prefix, None) is None
        assert app_module._withdraw_command('', '203.0.113.5:9735') is None

    def test_cli_prefix_uses_the_real_container_only_in_standard_mode(self):
        with patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False):
            assert app_module._node_cli_prefix('lnd', 'lightning-lnd-1') == 'sudo docker exec lightning-lnd-1'
            assert app_module._node_cli_prefix('lnd', 'core-lightning_lightningd_1') == 'sudo docker exec lightning_lnd_1'
            assert app_module._node_cli_prefix('lnd', 'lnd; reboot') == 'sudo docker exec lightning_lnd_1'
            assert app_module._node_cli_prefix('cln', '') == 'sudo docker exec core-lightning_lightningd_1'
        with patch('app.SECURE_MODE', True), patch('app.K3S_MODE', False):
            assert app_module._node_cli_prefix('lnd', 'lightning-lnd-1') == 'sudo docker exec lightning_lnd_1'
        with patch('app.K3S_MODE', True), patch('app.LND_K8S_NAMESPACE', 'lightning'), \
                patch('app.LND_K8S_POD_SELECTOR', 'app=lnd'):
            assert app_module._node_cli_prefix('lnd', 'lnd') == (
                'kubectl -n lightning exec $(kubectl -n lightning get pod -l app=lnd -o name | head -n 1) --'
            )

    def test_announcement_help_for_docker_modes(self):
        with patch('app.SECURE_MODE', True), patch('app.K3S_MODE', False):
            help_payload = app_module.build_announcement_help('lnd', SERVER, PORT, 'lightning_lnd_1')
        assert help_payload == {
            'impl': 'lnd',
            'mode': 'secure',
            'config_path': LND_HOST_PATH,
            'expected': EXPECTED,
            'expected_line': f'externalhosts={EXPECTED}',
            'verify_command': 'sudo docker exec lightning_lnd_1 lncli getinfo',
            'container_lookup_command': "sudo docker ps --format '{{.Names}}' | grep -iE 'lnd|lightning|tunnelsats'",
            'config_grep_command': (
                "sudo grep -nE '^[[:space:]]*(externalip|externalhosts|nat)[[:space:]]*=' "
                f'{LND_HOST_PATH}'
            ),
            'logs_commands': [
                f'sudo docker compose -f {UMBREL_COMPOSE} logs --tail 100 tunnelsats-daemon',
                f'sudo docker compose -f {UMBREL_COMPOSE} logs --tail 100 tunnelsats-web',
            ],
        }

    def test_announcement_help_for_k3s(self):
        with patch('app.K3S_MODE', True), patch('app.SECURE_MODE', False), \
                patch('app.LND_K8S_NAMESPACE', 'lightning'), patch('app.LND_K8S_POD_SELECTOR', 'app=lnd'), \
                patch('app.K8S_NAMESPACE', 'tunnelsats'):
            help_payload = app_module.build_announcement_help('lnd', SERVER, PORT, 'lnd')
        assert help_payload['mode'] == 'k3s'
        assert help_payload['config_path'] is None
        assert help_payload['config_grep_command'] is None
        assert help_payload['verify_command'] == (
            'kubectl -n lightning exec $(kubectl -n lightning get pod -l app=lnd -o name | head -n 1) -- lncli getinfo'
        )
        assert help_payload['container_lookup_command'] == 'kubectl -n lightning get pods -l app=lnd -o wide'
        assert help_payload['logs_commands'] == ['kubectl -n tunnelsats logs -l app=tunnelsats --tail 100']


class TestLiveGossipDiagnostics:
    def test_remaining_live_addresses_are_classified_with_withdraw_commands(self):
        diagnostics = app_module.build_live_announcement_diagnostics(
            'lnd',
            ['203.0.113.5:9735', f'{SERVER_IP}:9735', f'missing expected announcement {EXPECTED}'],
            SERVER, PORT,
            config_path=LND_HOST_PATH,
            cli_prefix='sudo docker exec lightning_lnd_1',
            is_server_host=_server_matcher,
        )
        assert diagnostics == [
            _diag('real-ip', 'live_gossip', value='203.0.113.5:9735', address='203.0.113.5:9735',
                  withdraw_command=_withdraw('203.0.113.5:9735')),
            _diag('wrong-port', 'live_gossip', value=f'{SERVER_IP}:9735', address=f'{SERVER_IP}:9735',
                  withdraw_command=_withdraw(f'{SERVER_IP}:9735')),
            _diag('missing-endpoint', 'live_gossip'),
        ]

    def test_unresolved_server_live_addresses_get_no_withdraw_command(self, hermetic_server_resolution):
        resolver, _original = hermetic_server_resolution
        resolver.return_value = ()  # The TunnelSats domain did not resolve.
        diagnostics = app_module.build_live_announcement_diagnostics(
            'lnd', [f'{SERVER_IP}:{PORT}', '203.0.113.5:9735'], SERVER, PORT,
            config_path=LND_HOST_PATH, cli_prefix='sudo docker exec lightning_lnd_1',
        )
        assert [(d['kind'], d['withdraw_command'], d['detail']) for d in diagnostics] == [
            ('real-ip', None, 'server_unresolved'),
            ('real-ip', None, 'server_unresolved'),
        ]

    def test_initializing_node_is_pending_not_a_conflict(self):
        diagnostics = app_module.build_live_announcement_diagnostics(
            'lnd', ['lnd_initializing'], SERVER, PORT, config_path=LND_HOST_PATH,
        )
        assert diagnostics == [_diag('verification-pending', 'live_gossip', detail='lnd_initializing')]
        assert app_module.build_live_announcement_diagnostics(
            'lnd', ['secure_mode_manual_required'], SERVER, PORT,
        ) == []

    def test_cln_getinfo_parser_brackets_ipv6_hosts(self):
        """CLN lists IPv6 hosts without brackets; '2001:db8::1:9735' is itself a valid IPv6 address."""
        raw_getinfo = json.dumps({'address': [
            {'type': 'ipv4', 'address': '203.0.113.5', 'port': 9735},
            {'type': 'ipv6', 'address': '2001:db8::1', 'port': 9735},
            {'type': 'ipv6', 'address': '[2001:db8::2]', 'port': 9736},
            {'type': 'dns', 'address': SERVER, 'port': PORT},
        ]})
        assert app_module._parse_cln_getinfo_addresses(raw_getinfo) == [
            '203.0.113.5:9735', '[2001:db8::1]:9735', '[2001:db8::2]:9736', EXPECTED,
        ]

    def test_cln_live_ipv6_announcement_keeps_its_real_port(self):
        raw_getinfo = json.dumps({'address': [
            {'type': 'ipv6', 'address': '2001:db8::1', 'port': 9735},
            {'type': 'dns', 'address': SERVER, 'port': PORT},
        ]})
        with patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False), \
                patch('app.container_id_by_match', return_value='cln1'), \
                patch('app.docker_exec', return_value=(True, raw_getinfo)):
            verified, _removed, remaining = app_module.clean_and_verify_cln_announcements(SERVER, PORT, True)
        assert verified is False
        assert remaining == ['[2001:db8::1]:9735']

        diagnostics = app_module.build_live_announcement_diagnostics(
            'cln', remaining, SERVER, PORT, is_server_host=_server_matcher,
        )
        assert [(d['kind'], d['address'], d['port_defaulted'], d['withdraw_command']) for d in diagnostics] == [
            ('real-ip', '[2001:db8::1]:9735', False, None),
        ]


# --- Cycle 3: /api/local/status integration -------------------------------------------------


class TestStatusDiagnostics:
    def test_secure_mode_names_the_wrong_port_setting_from_the_config_file(self, client):
        """The #170 customer case end to end, without any lncli access."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            _write(lnd_path, (
                '[Application Options]\n'
                'alias=customer\n'
                f'externalhosts={EXPECTED}\n'
                f'externalip={SERVER_IP}\n'
            ))
            _write(os.path.join(tmp_dir, app_module.META_FILE),
                   json.dumps({'vpnPort': PORT, 'serverDomain': SERVER}))
            with patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path), \
                    patch('app.SECURE_MODE', True), patch('app.lnd_exists', return_value=True), \
                    patch('app.cln_exists', return_value=False), \
                    patch('app._get_wireguard_state', return_value=('Connected', '')), \
                    patch('app.read_dataplane_state', return_value=_dataplane(target_container='lightning_lnd_1')), \
                    patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')):
                payload = client.get('/api/local/status').get_json()

        assert payload['rules_synced'] is False
        assert payload['last_error'] == app_module.ANNOUNCEMENT_CONFLICT_ERROR
        assert payload['last_error_code'] == 'announcement_conflict'
        assert payload['announcement_conflicts'] == [f'lnd:externalip={SERVER_IP}']
        assert payload['announcement_verification_source'] == 'config_file'
        assert payload['announcement_diagnostics'] == [
            _diag('wrong-port', setting='externalip', value=SERVER_IP,
                  line=f'externalip={SERVER_IP}', line_number=4,
                  address=f'{SERVER_IP}:9735', port_defaulted=True,
                  withdraw_command=_withdraw(f'{SERVER_IP}:9735')),
        ]
        assert payload['announcement_help']['verify_command'] == 'sudo docker exec lightning_lnd_1 lncli getinfo'
        assert payload['announcement_help']['mode'] == 'secure'

    def test_secure_mode_reports_the_missing_tunnelsats_line(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            _write(lnd_path, '[Application Options]\nexternalhosts=someonion.onion:9735\n')
            _write(os.path.join(tmp_dir, app_module.META_FILE),
                   json.dumps({'vpnPort': PORT, 'serverDomain': SERVER}))
            with patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path), \
                    patch('app.SECURE_MODE', True), patch('app.lnd_exists', return_value=True), \
                    patch('app.cln_exists', return_value=False), \
                    patch('app._get_wireguard_state', return_value=('Connected', '')), \
                    patch('app.read_dataplane_state', return_value=_dataplane()), \
                    patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')):
                payload = client.get('/api/local/status').get_json()

        assert payload['announcement_conflicts'] == [f'lnd:missing-externalhosts={EXPECTED}']
        assert payload['last_error_code'] == 'announcement_conflict'
        assert payload['announcement_diagnostics'] == [_diag('missing-endpoint', setting='externalhosts')]

    def _secure_status(self, client, lnd_conf, extra_patches=()):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            _write(lnd_path, lnd_conf)
            _write(os.path.join(tmp_dir, app_module.META_FILE),
                   json.dumps({'vpnPort': PORT, 'serverDomain': SERVER}))
            patches = [
                patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path),
                patch('app.SECURE_MODE', True), patch('app.lnd_exists', return_value=True),
                patch('app.cln_exists', return_value=False),
                patch('app._get_wireguard_state', return_value=('Connected', '')),
                patch('app.read_dataplane_state', return_value=_dataplane()),
                patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')),
                *extra_patches,
            ]
            for active in patches:
                active.start()
            try:
                response = client.get('/api/local/status')
            finally:
                for active in reversed(patches):
                    active.stop()
        return response

    def test_malformed_port_is_explained_without_breaking_status(self, client):
        response = self._secure_status(
            client, f'[Application Options]\nexternalhosts={EXPECTED}\nexternalip=203.0.113.5:\u00b2\n'
        )
        payload = response.get_json()
        assert response.status_code == 200
        assert payload['last_error_code'] == 'announcement_conflict'
        assert payload['announcement_conflicts'] == ['lnd:externalip=203.0.113.5:\u00b2']
        assert payload['announcement_diagnostics'] == [
            _diag('real-ip', setting='externalip', value='203.0.113.5:\u00b2',
                  line='externalip=203.0.113.5:\u00b2', line_number=3),
        ]

    def test_diagnostics_failure_never_changes_the_fail_closed_verdict(self, client):
        response = self._secure_status(
            client,
            f'[Application Options]\nexternalhosts={EXPECTED}\nexternalip={SERVER_IP}\n',
            extra_patches=(
                patch('app.build_config_announcement_diagnostics', side_effect=RuntimeError('boom')),
                patch('app.build_announcement_help', side_effect=RuntimeError('boom')),
            ),
        )
        payload = response.get_json()
        assert response.status_code == 200
        assert payload['rules_synced'] is False
        assert payload['last_error'] == app_module.ANNOUNCEMENT_CONFLICT_ERROR
        assert payload['last_error_code'] == 'announcement_conflict'
        assert payload['announcement_conflicts'] == [f'lnd:externalip={SERVER_IP}']
        assert payload['announcement_diagnostics'] == []
        assert payload['announcement_help'] == {}

    def test_standard_mode_reports_live_gossip_leftovers_with_the_real_container(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            _write(lnd_path, f'[Application Options]\nexternalhosts={EXPECTED}\nnat=false\n')
            _write(os.path.join(tmp_dir, app_module.META_FILE),
                   json.dumps({'vpnPort': PORT, 'serverDomain': SERVER}))
            live_result = (False, [], ['203.0.113.5:9735', f'missing expected announcement {EXPECTED}'])
            with patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path), \
                    patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False), \
                    patch('app._get_wireguard_state', return_value=('Connected', '')), \
                    patch('app.list_containers', return_value=_lnd_container('lightning-lnd-1')), \
                    patch('app.read_dataplane_state', return_value=_dataplane(
                        target_container='lightning-lnd-1', target_impl='lnd')), \
                    patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')), \
                    patch('app.clean_and_verify_lnd_announcements', return_value=live_result):
                payload = client.get('/api/local/status').get_json()

        assert payload['last_error'] == app_module.ANNOUNCEMENT_VERIFICATION_ERROR
        assert payload['last_error_code'] == 'announcement_unverified'
        assert payload['announcement_conflicts'] == []
        assert payload['announcement_verification_source'] == 'live_gossip'
        assert payload['announcement_diagnostics'] == [
            _diag('real-ip', 'live_gossip', value='203.0.113.5:9735', address='203.0.113.5:9735',
                  withdraw_command=_withdraw('203.0.113.5:9735', container='lightning-lnd-1')),
            _diag('missing-endpoint', 'live_gossip'),
        ]
        assert payload['announcement_help']['mode'] == 'standard'
        assert payload['announcement_help']['verify_command'] == 'sudo docker exec lightning-lnd-1 lncli getinfo'

    def test_standard_mode_initializing_lnd_is_reported_as_pending(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            _write(lnd_path, f'[Application Options]\nexternalhosts={EXPECTED}\n')
            _write(os.path.join(tmp_dir, app_module.META_FILE),
                   json.dumps({'vpnPort': PORT, 'serverDomain': SERVER}))
            with patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path), \
                    patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False), \
                    patch('app._get_wireguard_state', return_value=('Connected', '')), \
                    patch('app.list_containers', return_value=_lnd_container()), \
                    patch('app.read_dataplane_state', return_value=_dataplane()), \
                    patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')), \
                    patch('app.clean_and_verify_lnd_announcements', return_value=(False, [], ['lnd_initializing'])):
                payload = client.get('/api/local/status').get_json()

        assert payload['last_error_code'] == 'announcement_unverified'
        assert payload['announcement_diagnostics'] == [
            _diag('verification-pending', 'live_gossip', detail='lnd_initializing'),
        ]

    def test_clean_protected_node_reports_no_diagnostics(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            _write(lnd_path, f'[Application Options]\nexternalhosts={EXPECTED}\nnat=false\n')
            _write(os.path.join(tmp_dir, app_module.META_FILE),
                   json.dumps({'vpnPort': PORT, 'serverDomain': SERVER}))
            with patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path), \
                    patch('app.SECURE_MODE', True), patch('app.lnd_exists', return_value=True), \
                    patch('app.cln_exists', return_value=False), \
                    patch('app._get_wireguard_state', return_value=('Connected', '')), \
                    patch('app.read_dataplane_state', return_value=_dataplane()), \
                    patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')):
                payload = client.get('/api/local/status').get_json()

        assert payload['rules_synced'] is True
        assert payload['last_error'] is None
        assert payload['last_error_code'] is None
        assert payload['announcement_diagnostics'] == []

    @pytest.mark.parametrize('daemon_error,expected_code', [
        ('No WireGuard config found in /data', 'no_wireguard_config'),
        ('SecureMode: No Lightning node detected (probed 10.21.21.9 and 10.21.21.96)', 'no_lightning_node'),
        ('Failed to bring up WireGuard interface tunnelsatsv2', 'dataplane_error'),
    ])
    def test_daemon_errors_get_stable_codes(self, client, daemon_error, expected_code):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch('app.DATA_DIR', tmp_dir), \
                    patch('app.LND_CONFIG_PATH', os.path.join(tmp_dir, 'missing-lnd.conf')), \
                    patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False), \
                    patch('app._get_wireguard_state', return_value=('Disconnected', '')), \
                    patch('app.list_containers', return_value=[]), \
                    patch('app.read_dataplane_state', return_value=_dataplane(
                        rules_synced=False, last_error=daemon_error)), \
                    patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')):
                payload = client.get('/api/local/status').get_json()

        assert payload['last_error'] == daemon_error
        assert payload['last_error_code'] == expected_code
        assert payload['announcement_diagnostics'] == []

    def test_pending_target_switch_has_its_own_code(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            _write(os.path.join(tmp_dir, app_module.META_FILE),
                   json.dumps({'vpnPort': PORT, 'serverDomain': SERVER, 'nodeType': 'cln'}))
            with patch('app.DATA_DIR', tmp_dir), \
                    patch('app.LND_CONFIG_PATH', os.path.join(tmp_dir, 'missing-lnd.conf')), \
                    patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False), \
                    patch('app._get_wireguard_state', return_value=('Connected', '')), \
                    patch('app.list_containers', return_value=[]), \
                    patch('app.read_dataplane_state', return_value=_dataplane(target_impl='lnd')), \
                    patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')):
                payload = client.get('/api/local/status').get_json()

        assert payload['last_error'] == app_module.TARGET_SWITCH_PENDING_ERROR
        assert payload['last_error_code'] == 'target_switch_pending'


# --- Cycle 4: configure-node responses ------------------------------------------------------


class TestConfigureNodeDiagnostics:
    def test_secure_mode_manual_steps_list_actual_lines_and_prefilled_commands(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            _write(lnd_path, (
                '[Application Options]\n'
                f'externalip={SERVER_IP}\n'
                'externalhosts=old.example.com:9735\n'
            ))
            _write(os.path.join(tmp_dir, app_module.META_FILE),
                   json.dumps({'vpnPort': PORT, 'serverDomain': SERVER}))
            with patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path), \
                    patch('app.SECURE_MODE', True):
                response = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

        payload = response.get_json()
        assert response.status_code == 200
        assert payload['manual_mode'] is True
        assert payload['gossip_command'] == (
            'sudo docker exec lightning_lnd_1 lncli peers updatenodeannouncement '
            '--address_remove=<your-old-ip>:9735'
        )
        assert payload['gossip_commands'] == [_withdraw(f'{SERVER_IP}:9735')]
        assert payload['diagnostics'] == [
            _diag('wrong-port', setting='externalip', value=SERVER_IP,
                  line=f'externalip={SERVER_IP}', line_number=2,
                  address=f'{SERVER_IP}:9735', port_defaulted=True,
                  withdraw_command=_withdraw(f'{SERVER_IP}:9735')),
            _diag('real-ip', setting='externalhosts', value='old.example.com:9735',
                  line='externalhosts=old.example.com:9735', line_number=3,
                  address='old.example.com:9735'),
            _diag('missing-endpoint', setting='externalhosts'),
        ]

    @patch(
        'app.container_ids_by_match',
        side_effect=lambda pattern, containers=None: ['mock'] if pattern == app_module.LND_CONTAINER_PATTERN else [],
    )
    def test_standard_mode_conflict_confirmation_includes_diagnostics(self, _mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            original = f'[Application Options]\nexternalip={SERVER_IP}\n'
            _write(lnd_path, original)
            _write(os.path.join(tmp_dir, app_module.META_FILE),
                   json.dumps({'vpnPort': PORT, 'serverDomain': SERVER}))
            with patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path), \
                    patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False):
                response = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})
            with open(lnd_path, encoding='utf-8') as fp:
                assert fp.read() == original

        payload = response.get_json()
        assert response.status_code == 409
        assert payload['requires_confirmation'] is True
        assert payload['conflicts'] == [f'externalip={SERVER_IP}']
        assert [d['kind'] for d in payload['diagnostics']] == ['wrong-port']
        assert payload['diagnostics'][0]['line_number'] == 2


# --- Documentation contracts ----------------------------------------------------------------


def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding='utf-8') as fp:
        return fp.read()


def test_conflict_error_no_longer_claims_a_real_ip():
    assert 'real-IP' not in app_module.ANNOUNCEMENT_CONFLICT_ERROR
    assert not app_module.ANNOUNCEMENT_VERIFICATION_ERROR.startswith('LND ')


def test_lnd_cli_examples_use_verifiable_umbrel_container_names():
    for parts in (('web', 'index.html'), ('web', 'js', 'app.js'), ('server', 'app.py'), ('FAQ.md',)):
        source = _read(*parts)
        assert 'docker exec lnd ' not in source, parts
        assert 'docker exec -it lightning_lnd_1' not in source, parts


def test_faq_documents_container_lookup_logs_and_wrong_port():
    html = _read('web', 'index.html')
    faq_md = _read('FAQ.md')
    for source in (html, faq_md):
        assert "docker ps --format '{{.Names}}'" in source
        assert 'logs --tail 100 tunnelsats-daemon' in source
        assert '--address_remove=' in source
        assert ':9735' in source
        assert 'without the' in source and 'pubkey' in source
