"""Connection stability, timed rotation and same-country failover regressions."""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import connection_policy as policy
import test_manager_logic as support

manager = support.manager


def n(index, country="JP", **fields):
    node = {"id": f"vpn{index}", "ip": f"198.51.100.{index+1}",
            "country_short": country, "geo_country_short": country,
            "country": country, "geo_region": "Tokyo", "geo_city": "Tokyo",
            "geo_lat": 35.68, "geo_lon": 139.76, "probe_status": "available",
            "latency_ms": index+10, "node_kind": "openvpn", "active": False}
    node.update(fields)
    return node


class GeographyTests(unittest.TestCase):
    def test_other_countries_are_never_selected_even_if_faster(self):
        candidates = policy.nearby_candidates([n(1, "US", latency_ms=1), n(2)], n(0), {"vpn0"})
        self.assertEqual(["vpn2"], [row["id"] for row in candidates])

    def test_unknown_country_is_not_assumed_same_country(self):
        self.assertEqual([], policy.nearby_candidates([n(1)], {"id":"unknown"}, set()))

    def test_same_city_then_region_then_distance_before_latency(self):
        ref = n(0)
        nearby = n(1, latency_ms=1000)
        distant = n(2, geo_city="Osaka", geo_region="Osaka", geo_lat=34.69, geo_lon=135.5, latency_ms=1)
        same_region = n(3, geo_city="Fuchu", geo_lat=35.66, geo_lon=139.47, latency_ms=5)
        candidates = policy.nearby_candidates([distant, same_region, nearby], ref, {"vpn0"})
        self.assertEqual(["vpn1","vpn3","vpn2"], [row["id"] for row in candidates])

    def test_missing_coordinates_still_only_uses_same_country(self):
        ref = {"id":"old", "ip":"198.51.100.200", "country_short":"JP"}
        candidates = policy.nearby_candidates([n(1, geo_lat=None, geo_lon=None), n(2, "US")], ref, set())
        self.assertEqual(["vpn1"], [row["id"] for row in candidates])

    def test_same_exit_is_not_a_replacement(self):
        ref = n(0, exit_ip="203.0.113.9")
        self.assertEqual([], policy.nearby_candidates([n(1, exit_ip="203.0.113.9")], ref, set()))

    def test_invalid_coordinates_are_not_used(self):
        self.assertIsNone(policy.coords({"geo_lat":float("nan"), "geo_lon":0}))
        self.assertIsNone(policy.coords({"geo_lat":91, "geo_lon":0}))
        self.assertIsNotNone(policy.coords({"geo_lat":0, "geo_lon":0}))

    def test_sticky_never_becomes_due(self):
        self.assertFalse(policy.interval_due({"switch_policy":"sticky"}, {"connection_started_at":10}, 100000))

    def test_timer_boundary_and_disabled_connection(self):
        cfg = {"switch_policy":"interval", "rotation_interval_seconds":3600}
        state = {"connection_started_at":100}
        self.assertFalse(policy.interval_due(cfg, state, 3699))
        self.assertTrue(policy.interval_due(cfg, state, 3700))
        self.assertFalse(policy.interval_due(dict(cfg, connection_enabled=False), state, 9999))
        self.assertFalse(policy.interval_due(dict(cfg, routing_mode="fixed_ip"), state, 9999))
        self.assertFalse(policy.interval_due(cfg, {"connection_started_at":0}, 9999))

    def test_timer_reconfiguration_starts_new_interval(self):
        cfg = {"switch_policy":"interval", "rotation_interval_seconds":3600}
        state = {"connection_started_at":100,"rotation_base_at":10000}
        self.assertFalse(policy.interval_due(cfg, state, 10000))
        self.assertTrue(policy.interval_due(cfg, state, 13600))


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.ManagerLogicTests()
        self.fixture.setUp()
        self.enrich = mock.patch.object(manager.vpn_utils,"enrich_ip_info").start()
        self.refill = mock.patch.object(manager,"schedule_background_refill",return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.rows = [n(0, active=True), n(1), n(2,"US",latency_ms=1)]
        manager.write_json(manager.NODES_FILE, self.rows)
        manager.write_json(manager.PUBLIC_PROXIES_FILE, [])
        manager.active_openvpn_node_id = "vpn0"
        manager.active_openvpn_process = support.FakeProcess()
        manager.set_state(tunnel_ready=True,proxy_ready=True,proxy_ok=True,
                          connection_started_at=100, rotation_base_at=0,
                          last_connection_node=dict(self.rows[0],exit_ip=self.rows[0]["ip"],geo_exit_verified=True))

    def tearDown(self):
        self.fixture.tearDown()

    def cfg(self, **fields):
        cfg = manager.load_ui_config()
        cfg.update(fields)
        manager.write_json(manager.DATA_DIR/"ui_auth.json",cfg)
        return cfg

    def test_upgrade_defaults_to_sticky_but_keeps_failure_recovery(self):
        cfg = manager.load_ui_config()
        self.assertEqual("sticky",cfg["switch_policy"])
        self.assertTrue(cfg["failover_enabled"])
        self.assertEqual(3600,cfg["rotation_interval_seconds"])

    def test_all_background_recovery_callers_keep_healthy_current_ip(self):
        with mock.patch.object(manager,"connect_node") as connect:
            for _ in range(10):
                manager.auto_switch_node()
            connect.assert_not_called()
        self.refill.assert_not_called()

    def test_repeated_healthy_ticks_do_not_rotate_in_sticky_mode(self):
        with mock.patch.object(manager,"check_proxy_health",return_value={"ok":True,"ip":self.rows[0]["ip"],"latency_ms":20}), mock.patch.object(manager,"auto_switch_node") as switch:
            for _ in range(10):
                manager.connection_policy_tick()
            switch.assert_not_called()

    def test_transient_failure_then_success_does_not_switch(self):
        with mock.patch.object(manager,"check_proxy_health",side_effect=[{"ok":False,"error":"temporary"},{"ok":True,"ip":self.rows[0]["ip"],"latency_ms":20}]), mock.patch.object(manager,"auto_switch_node") as switch:
            manager.connection_policy_tick()
            manager.connection_policy_tick()
            switch.assert_not_called()
            self.assertEqual(0,manager.consecutive_proxy_failures)

    def test_failure_threshold_preserves_geographic_reference(self):
        with mock.patch.object(manager,"check_proxy_health",return_value={"ok":False,"error":"dead"}), mock.patch.object(manager,"auto_switch_node") as switch:
            manager.connection_policy_tick()
            manager.connection_policy_tick()
            switch.assert_not_called()
            manager.connection_policy_tick()
            self.assertEqual("failure",switch.call_args.kwargs["reason"])
            self.assertEqual("JP",switch.call_args.kwargs["reference_node"]["geo_country_short"])

    def test_local_gateway_failure_does_not_cycle_remote_ips(self):
        with mock.patch.object(manager,"check_proxy_health",return_value={"ok":False,"error":"gateway down","failure_kind":"local_gateway"}), mock.patch.object(manager,"auto_switch_node") as switch:
            for _ in range(5): manager.connection_policy_tick()
            switch.assert_not_called()
            self.assertIn("本机代理",manager.get_state()["switch_message"])

    def test_manual_only_disables_failure_switch(self):
        self.cfg(failover_enabled=False)
        with mock.patch.object(manager,"check_proxy_health",return_value={"ok":False,"error":"dead"}), mock.patch.object(manager,"auto_switch_node") as switch:
            for _ in range(5): manager.connection_policy_tick()
            switch.assert_not_called()
        manager.active_openvpn_process = None
        with mock.patch.object(manager,"connect_node") as connect:
            manager.auto_switch_node()
            connect.assert_not_called()

    def test_failure_selects_same_country_public_proxy_too(self):
        proxy = n(3, node_kind="public_proxy",exit_ip="198.51.100.4",latency_ms=1)
        manager.write_json(manager.PUBLIC_PROXIES_FILE,[proxy])
        with mock.patch.object(manager,"connect_node",return_value="ok") as connect:
            manager.auto_switch_node(reason="failure",reference_node=dict(self.rows[0],geo_exit_verified=True))
            self.assertEqual("vpn3",connect.call_args.args[0])
            self.assertEqual("JP",connect.call_args.kwargs["automatic_reference"]["geo_country_short"])

    def test_no_same_country_backup_keeps_old_tunnel(self):
        manager.write_json(manager.NODES_FILE,[self.rows[0],self.rows[2]])
        with mock.patch.object(manager,"connect_node") as connect, mock.patch.object(manager,"stop_active_openvpn") as stop:
            manager.auto_switch_node(reason="failure",reference_node=dict(self.rows[0],geo_exit_verified=True))
            connect.assert_not_called()
            stop.assert_not_called()
        self.assertIn("不跨国",manager.get_state()["switch_message"])

    def test_retry_goes_to_next_same_country_not_same_failed_node(self):
        manager.write_json(manager.NODES_FILE,[self.rows[0],n(1),n(3)])
        with mock.patch.object(manager,"connect_node",side_effect=[RuntimeError("dead"),"ok"]) as connect:
            manager.auto_switch_node(reason="failure",reference_node=dict(self.rows[0],geo_exit_verified=True))
            self.assertEqual(["vpn1","vpn3"],[call.args[0] for call in connect.call_args_list])

    def test_cooldown_prevents_repeated_failed_switches(self):
        manager.set_state(last_auto_switch_attempt_at=time.time())
        with mock.patch.object(manager,"connect_node") as connect:
            manager.auto_switch_node(reason="failure",reference_node=dict(self.rows[0],geo_exit_verified=True))
            connect.assert_not_called()

    def test_timer_only_switches_after_due_and_selects_same_country(self):
        self.cfg(switch_policy="interval",rotation_interval_seconds=3600)
        with mock.patch.object(manager.time,"time",return_value=3699), mock.patch.object(manager,"connect_node") as connect:
            manager.auto_switch_node(reason="interval",reference_node=self.rows[0])
            connect.assert_not_called()
        with mock.patch.object(manager.time,"time",return_value=3700), mock.patch.object(manager,"connect_node",return_value="ok") as connect:
            manager.auto_switch_node(reason="interval",reference_node=self.rows[0])
            self.assertEqual("vpn1",connect.call_args.args[0])

    def test_save_timer_settings_does_not_immediately_switch(self):
        with mock.patch.object(manager.time,"time",return_value=10000), mock.patch.object(manager,"connect_node") as connect:
            manager.save_switch_settings({"switch_policy":"interval","rotation_interval_seconds":3600,"failover_enabled":True})
            state = manager.get_state()
            self.assertEqual(13600,state["next_rotation_at"])
            self.assertFalse(policy.interval_due(manager.load_ui_config(),state,10000))
            connect.assert_not_called()

    def test_settings_validation_rejects_bad_values(self):
        for payload in ({"switch_policy":"bad"},{"failover_enabled":"false"},
                        {"rotation_interval_seconds":0},{"rotation_interval_seconds":True},
                        {"rotation_interval_seconds":"3600"},{"rotation_interval_seconds":604801}):
            with self.assertRaises(ValueError): manager.save_switch_settings(payload)

    def test_success_resets_timer_and_persists_anchor(self):
        with mock.patch.object(manager.time,"time",return_value=10000):
            manager.record_successful_connection(self.rows[0],self.rows[0]["ip"])
        manager._json_cache.clear()
        self.assertEqual(10000,manager.get_state()["connection_started_at"])
        self.assertEqual("JP",manager.get_state()["last_connection_node"]["geo_country_short"])

    def test_unknown_or_different_actual_exit_is_rejected(self):
        ref = self.rows[0]
        with self.assertRaises(RuntimeError): manager.verify_automatic_exit(n(1),"203.0.113.2",ref)
        def different(items): items[0]["geo_country_short"]="US"
        self.enrich.side_effect=different
        with self.assertRaises(RuntimeError): manager.verify_automatic_exit(n(1),"203.0.113.2",ref)
        def same(items): items[0]["geo_country_short"]="JP"
        self.enrich.side_effect=same
        manager.verify_automatic_exit(n(1),"203.0.113.2",ref)

    def test_slow_geo_lookup_does_not_overwrite_new_manual_anchor(self):
        old=dict(self.rows[0],exit_ip="203.0.113.99")
        new=dict(self.rows[1],geo_exit_verified=True)
        def lookup(items):
            manager.active_openvpn_node_id=new["id"]
            manager.set_state(last_connection_node=new)
            items[0]["geo_country_short"]="JP"
        self.enrich.side_effect=lookup
        manager.reference_geography(old)
        self.assertEqual(new["id"],manager.get_state()["last_connection_node"]["id"])

    def test_failed_public_preflight_preserves_old_upstream(self):
        old = n(3,node_kind="public_proxy",protocol="http",remote_host="198.51.100.4",remote_port=8080)
        target = n(4,node_kind="public_proxy",protocol="http",remote_host="198.51.100.5",remote_port=8080)
        manager.write_json(manager.PUBLIC_PROXIES_FILE,[old,target])
        manager.active_public_proxy_id=old["id"]
        manager.active_openvpn_node_id=""
        manager.proxy_server.set_active_upstream(old)
        with mock.patch.object(manager.public_proxy_pool,"probe_proxy",return_value={"id":target["id"],"probe_status":"unavailable","probe_message":"dead"}), mock.patch.object(manager,"stop_active_openvpn") as stop:
            with self.assertRaises(RuntimeError): manager.connect_node(target["id"])
            stop.assert_not_called()
        self.assertEqual(old["id"],manager.active_public_proxy_id)
        self.assertEqual(old["id"],manager.proxy_server.get_active_upstream()["id"])
        self.assertTrue(manager.get_state()["proxy_ok"])

    def test_failed_vpn_preflight_preserves_old_public_upstream(self):
        rows = self.fixture.write_nodes(2)
        old = n(3,node_kind="public_proxy",protocol="http",remote_host="198.51.100.4",remote_port=8080)
        manager.write_json(manager.PUBLIC_PROXIES_FILE,[old])
        manager.active_public_proxy_id=old["id"]
        manager.active_openvpn_node_id=""
        manager.active_openvpn_process=None
        manager.proxy_server.set_active_upstream(old)
        with mock.patch.object(manager,"run_openvpn_until_ready",return_value=(False,"preflight failed",None)):
            with self.assertRaises(RuntimeError): manager.connect_node(rows[1]["id"])
        self.assertEqual(old["id"],manager.active_public_proxy_id)
        self.assertEqual(old["id"],manager.proxy_server.get_active_upstream()["id"])
        self.assertTrue(manager.get_state()["proxy_ok"])

    def test_successful_public_to_vpn_commit_clears_previous_upstream(self):
        rows=self.fixture.write_nodes(2)
        old=n(3,node_kind="public_proxy",protocol="http",remote_host="198.51.100.4",remote_port=8080)
        manager.write_json(manager.PUBLIC_PROXIES_FILE,[old])
        manager.active_public_proxy_id=old["id"]
        manager.active_openvpn_node_id=""
        manager.active_openvpn_process=None
        manager.proxy_server.set_active_upstream(old)
        process=support.FakeProcess()
        def health():
            self.assertIsNone(manager.proxy_server.get_active_upstream())
            self.assertEqual("",manager.active_public_proxy_id)
            return {"ok":True,"ip":"198.51.100.200","latency_ms":10}
        with mock.patch.object(manager,"run_openvpn_until_ready",side_effect=[(True,"preflight",None),(True,"ready",process)]),mock.patch.object(manager,"setup_policy_routing",return_value=True),mock.patch.object(manager,"cleanup_policy_routing"),mock.patch.object(manager.vpn_utils,"ping_latency_ms",return_value=10),mock.patch.object(manager,"check_proxy_health",side_effect=health):
            manager.connect_node(rows[1]["id"])
        self.assertEqual(rows[1]["id"],manager.active_openvpn_node_id)
        self.assertIsNone(manager.proxy_server.get_active_upstream())

    def test_dead_openvpn_process_recovers_without_waiting_for_three_checks(self):
        manager.active_openvpn_process.running=False
        with mock.patch.object(manager,"check_proxy_health",return_value={"ok":False,"error":"process exited"}),mock.patch.object(manager,"auto_switch_node") as switch:
            manager.connection_policy_tick()
            self.assertEqual(1,switch.call_count)
            self.assertEqual("failure",switch.call_args.kwargs["reason"])

    def test_fixed_public_proxy_does_not_reconnect_healthy_connection(self):
        old=n(3,node_kind="public_proxy",protocol="http",remote_host="198.51.100.4",remote_port=8080)
        manager.write_json(manager.PUBLIC_PROXIES_FILE,[old])
        manager.active_public_proxy_id=old["id"]
        manager.active_openvpn_node_id=""
        manager.active_openvpn_process=None
        manager.proxy_server.set_active_upstream(old)
        cfg=self.cfg(routing_mode="fixed_ip",fixed_node_id=old["id"])
        self.assertEqual(old["id"],manager.current_fixed_node_id(cfg))
        with mock.patch.object(manager,"connect_node") as connect:
            self.assertFalse(manager.reconnect_fixed_node_if_needed(cfg))
            connect.assert_not_called()

    def test_newer_operator_request_stops_automatic_retry(self):
        manager.write_json(manager.NODES_FILE,[self.rows[0],n(1),n(3)])
        def fail_with_newer_request(*args,**kwargs):
            manager.connection_epoch+=2
            raise RuntimeError("older request failed")
        with mock.patch.object(manager,"connect_node",side_effect=fail_with_newer_request) as connect:
            manager.auto_switch_node(reason="failure",reference_node=dict(self.rows[0],geo_exit_verified=True))
            self.assertEqual(1,connect.call_count)

    def test_switch_settings_http_round_trip_and_validation(self):
        with mock.patch.object(manager.Handler,"is_authorized",return_value=True),mock.patch.object(manager.Handler,"get_secret_path",return_value="test"):
            server=manager.ThreadingHTTPServer(("127.0.0.1",0),manager.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                url=f"http://127.0.0.1:{server.server_port}/test/api/switch_settings"
                payload={"switch_policy":"interval","rotation_interval_seconds":7200,"failover_enabled":False}
                request=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"})
                with urllib.request.urlopen(request,timeout=5) as response: result=json.load(response)
                self.assertEqual(7200,result["rotation_interval_seconds"])
                self.assertFalse(result["failover_enabled"])
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(urllib.request.Request(url,data=b'{"rotation_interval_seconds":1}',headers={"Content-Type":"application/json"}),timeout=5)
                self.assertEqual(400,ctx.exception.code)
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)

    def test_public_proxy_health_on_linux_does_not_require_tun(self):
        manager.active_public_proxy_id="proxy"
        manager.active_openvpn_node_id=""
        sock=mock.MagicMock()
        output=mock.Mock(returncode=0,stdout="198.51.100.1\n0.1 200\n")
        with mock.patch.object(manager.sys,"platform","linux"),mock.patch.object(manager.socket,"socket",return_value=sock),mock.patch.object(manager.Path,"exists",return_value=False),mock.patch.object(manager.subprocess,"run",return_value=output) as run:
            result=manager.check_proxy_health()
        self.assertTrue(result["ok"])
        self.assertIn("https://api.ip.sb/ip",run.call_args.args[0])


if __name__ == "__main__": unittest.main()
