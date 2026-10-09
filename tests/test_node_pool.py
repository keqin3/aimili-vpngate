"""Regression tests for small cached pools, retirement and HTTP fast paths."""
import gzip
import json
import threading
import time
import unittest
import urllib.request
from unittest import mock

import node_pool
import test_manager_logic as support

manager = support.manager


def node(index, kind="residential", **updates):
    result = {"id": f"n{index}", "ip": f"198.18.{index // 250}.{index % 250 + 1}",
              "ip_type": kind, "probe_status": "available", "probed_at": time.time(),
              "consecutive_failures": 0, "node_kind": "openvpn"}
    result.update(updates)
    return result


class SelectionTests(unittest.TestCase):
    def test_default_targets_and_pending_are_bounded(self):
        rows = [node(i) for i in range(350)] + [node(i, "hosting") for i in range(350, 470)]
        rows += [node(i, "unknown") for i in range(470, 900)]
        kept, counts = node_pool.select_pool([], rows, set(), set())
        self.assertEqual(250, len(kept))
        self.assertEqual(200, counts["residential"])
        self.assertEqual(50, counts["hosting"])
        self.assertEqual(0, counts["pending"])

    def test_unknown_and_mobile_do_not_count_as_residential(self):
        kept, counts = node_pool.select_pool([], [node(i, "unknown") for i in range(90)]
                                             + [node(100, "mobile")], set(), set())
        self.assertEqual(60, len(kept))
        self.assertEqual(200, counts["residential_missing"])

    def test_deduplicates_same_ip_across_ports_and_transports(self):
        original = node(1)
        duplicate = node(2, ip=original["ip"], node_kind="public_proxy", remote_port=8080,
                         exit_ip=original["ip"])
        kept, _ = node_pool.select_pool([], [original, duplicate], set(), set())
        self.assertEqual(["n1"], [n["id"] for n in kept])

    def test_verified_exit_ip_deduplicates_different_servers(self):
        kept, _ = node_pool.select_pool([], [node(1, exit_ip="203.0.113.2"),
                                             node(2, exit_ip="203.0.113.2")], set(), set())
        self.assertEqual(1, len(kept))

    def test_existing_batch_wins_over_faster_new_candidates(self):
        old = node(1, latency_ms=500)
        kept, _ = node_pool.select_pool([old], [node(2, latency_ms=1)], {old["ip"]}, set(), residential=1)
        self.assertEqual([old], kept)

    def test_failures_require_threshold_and_never_remove_protected(self):
        old = node(1, probe_status="unavailable", consecutive_failures=1)
        kept, _ = node_pool.select_pool([old], [], set(), set())
        self.assertEqual([old], kept)
        old["consecutive_failures"] = 2
        kept, _ = node_pool.select_pool([old], [], set(), set())
        self.assertEqual([], kept)
        kept, _ = node_pool.select_pool([old], [], set(), {"n1"})
        self.assertEqual([old], kept)

    def test_rotation_excludes_all_seen_addresses_but_preserves_protected(self):
        old = [node(1), node(2)]
        history = {n["ip"] for n in old}
        kept, _ = node_pool.select_pool(old, [node(3, ip=old[0]["ip"]), node(4)],
                                       history, {"n2"}, rotate=True)
        self.assertEqual(["n2", "n4"], [n["id"] for n in kept])

    def test_systemic_openvpn_failure_does_not_retire_remote_ip(self):
        old = node(1, probe_status="unavailable", consecutive_failures=5,
                   probe_message="[ERR_OVPN_TUN_NOT_AVAILABLE] missing TUN")
        kept, _ = node_pool.select_pool([old], [], set(), set())
        self.assertEqual([old], kept)

    def test_repeated_unclassified_candidates_are_retired(self):
        kept, _ = node_pool.select_pool([node(1, "unknown", pool_classification_attempts=3)], [], set(), set())
        self.assertEqual([], kept)

    def test_ipv6_alias_normalization(self):
        self.assertEqual({"2001:db8::1"}, node_pool.ip_keys({"ip": "2001:0db8:0:0:0:0:0:1", "exit_ip": "[2001:db8::1]"}))

    def test_source_hint_is_not_a_verified_residential_exit(self):
        kept, counts = node_pool.select_pool([], [node(1, node_kind="public_proxy")], set(), set())
        self.assertEqual(0, counts["residential"])
        self.assertEqual("unknown", kept[0]["ip_type"])


class PoolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.ManagerLogicTests()
        self.fixture.setUp()
        manager.pool_job_thread = None
        cfg = manager.load_ui_config()
        cfg["connection_enabled"] = False
        manager.write_json(manager.DATA_DIR / "ui_auth.json", cfg)
        self.fetch = mock.patch.object(manager, "fetch_candidates", return_value=[]).start()
        self.public_fetch = mock.patch.object(manager.public_proxy_pool, "fetch_all_sources", return_value=([], {}, 0, [])).start()
        self.probe = mock.patch.object(manager, "test_combined_nodes", return_value=[]).start()
        self.enrich = mock.patch.object(manager.vpn_utils, "enrich_ip_info").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.fixture.tearDown()

    def store(self, rows):
        manager.write_json(manager.NODES_FILE, rows)
        manager.write_json(manager.PUBLIC_PROXIES_FILE, [])

    def test_full_pool_never_refetches_even_with_old_feed_cache(self):
        rows = [node(i) for i in range(200)] + [node(i, "hosting") for i in range(200, 250)]
        self.store(rows)
        manager.run_pool_maintenance()
        self.fetch.assert_not_called()
        self.public_fetch.assert_not_called()
        self.assertEqual(250, len(manager.read_all_nodes()))
        self.assertFalse(manager.maintenance_lock.locked())
        self.assertFalse(manager.is_connecting)

    def test_only_failure_deficit_is_replaced(self):
        rows = [node(i) for i in range(200)] + [node(i, "hosting") for i in range(200, 250)]
        rows[0].update(probe_status="unavailable", consecutive_failures=2)
        self.store(rows)
        manager.write_json(manager.DATA_DIR / "pool_candidates.json", [node(300)])
        manager.run_pool_maintenance()
        ids = {n["id"] for n in manager.read_all_nodes()}
        self.assertNotIn("n0", ids)
        self.assertIn("n300", ids)
        self.assertEqual(250, len(ids))
        self.fetch.assert_not_called()

    def test_candidate_exhaustion_keeps_current_batch(self):
        self.store([node(1)])
        manager.run_pool_maintenance(rotate=True)
        self.assertEqual(["n1"], [n["id"] for n in manager.read_nodes()])
        self.assertIn("没有未使用过的新 IP", manager.get_state()["pool_message"])

    def test_rotation_and_history_survive_cache_reset(self):
        self.store([node(1)])
        manager.write_json(manager.DATA_DIR / "pool_candidates.json", [node(2)])
        manager.run_pool_maintenance(rotate=True)
        self.assertEqual(["n2"], [n["id"] for n in manager.read_nodes()])
        manager._json_cache.clear()
        self.assertIn(node(1)["ip"], manager.pool_history())
        self.fetch.return_value = [node(1), node(3)]
        manager.run_pool_maintenance(rotate=True)
        self.assertEqual(["n3"], [n["id"] for n in manager.read_nodes()])

    def test_probe_and_classification_are_bounded(self):
        self.store([node(i, "unknown", probe_status="not_checked", probed_at=0) for i in range(60)])
        manager.set_state(pool_last_feed_at=time.time())
        manager.run_pool_maintenance()
        self.assertEqual(20, len(self.enrich.call_args.args[0]))
        self.assertEqual(20, len(self.probe.call_args.args[0]))
        self.assertEqual(200, manager.get_state()["pool_counts"]["residential_missing"])

    def test_feed_failure_keeps_healthy_cached_nodes(self):
        self.store([node(1)])
        self.fetch.side_effect = RuntimeError("offline")
        self.public_fetch.side_effect = RuntimeError("offline")
        manager.run_pool_maintenance(refresh=True)
        self.assertEqual(["n1"], [n["id"] for n in manager.read_nodes()])
        self.assertIn("offline", manager.get_state()["pool_last_error"])

    def test_json_cache_is_copy_safe_and_invalidated_on_write(self):
        path = manager.DATA_DIR / "example.json"
        manager.write_json(path, {"v": [1]})
        snapshot = manager.read_json(path, {})
        snapshot["v"].append(2)
        self.assertEqual({"v": [1]}, manager.read_json(path, {}))
        manager.write_json(path, {"v": [3]})
        self.assertEqual({"v": [3]}, manager.read_json(path, {}))

    def test_json_cache_detects_external_replacement(self):
        path = manager.DATA_DIR / "example.json"
        manager.write_json(path, {"v": 1})
        manager.read_json(path, {})
        replacement = path.with_suffix(".replacement")
        replacement.write_text('{"v":200}', encoding="utf-8")
        replacement.replace(path)
        self.assertEqual({"v": 200}, manager.read_json(path, {}))

    def test_http_cached_page_is_compressed_and_never_fetches_sources(self):
        self.store([node(i, owner="x" * 80, config_text="PRIVATE_CONFIG_MUST_NOT_LEAK") for i in range(200)])
        with mock.patch.object(manager.Handler, "is_authorized", return_value=True), mock.patch.object(manager.Handler, "get_secret_path", return_value="test"):
            server = manager.ThreadingHTTPServer(("127.0.0.1", 0), manager.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/test/api/nodes?page=2&page_size=50"
                request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip;q=0.5"})
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual("gzip", response.headers.get("Content-Encoding"))
                    raw = gzip.decompress(response.read())
                data = json.loads(raw)
                self.assertEqual(200, data["total"])
                self.assertEqual(50, len(data["nodes"]))
                self.assertEqual("n50", data["nodes"][0]["id"])
                self.assertNotIn(b"PRIVATE_CONFIG", raw)
                self.fetch.assert_not_called()
                self.public_fetch.assert_not_called()
                with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept-Encoding": "gzip;q=0"}), timeout=5) as response:
                    self.assertIsNone(response.headers.get("Content-Encoding"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_unknown_mobile_rotation_does_not_erase_current_batch(self):
        self.store([node(1)])
        manager.write_json(manager.DATA_DIR / "pool_candidates.json", [node(2, "unknown")])
        def classify(items):
            for item in items:
                item["ip_type"] = "mobile"
        self.enrich.side_effect = classify
        manager.run_pool_maintenance(rotate=True)
        self.assertEqual(["n1"], [n["id"] for n in manager.read_nodes()])
        self.assertIn(node(2)["ip"], manager.pool_history())

    def test_proxy_exit_matching_older_batch_is_rejected(self):
        self.store([node(1)])
        manager.write_json(manager.DATA_DIR / "pool_history.json", ["203.0.113.3"])
        proxy = node(2, "unknown", node_kind="public_proxy", probed_at=0)
        manager.write_json(manager.DATA_DIR / "pool_candidates.json", [proxy])
        def probe(ids):
            rows = manager.read_public_proxies()
            rows[0].update(exit_ip="203.0.113.3", ip_type="residential", probe_status="available")
            manager.write_json(manager.PUBLIC_PROXIES_FILE, rows)
            return rows
        self.probe.side_effect = probe
        manager.run_pool_maintenance(rotate=True)
        self.assertEqual(["n1"], [n["id"] for n in manager.read_nodes()])
        self.assertEqual([], manager.read_public_proxies())

    def test_upgrade_migration_preserves_unrelated_settings_and_is_idempotent(self):
        from pathlib import Path
        script = (Path(manager.__file__).parent / "upgrade.sh").read_text(encoding="utf-8")
        block = script.split("python3 - \"${env_file}\" <<'PY'")[-1].split("\nPY\n")[0]
        target = manager.DATA_DIR / "service.env"
        target.write_text("CUSTOM=keep\nHOSTING_RETAIN_LIMIT=100\n", encoding="utf-8")
        with mock.patch("sys.argv", ["migration", str(target)]):
            exec(compile(block, "migration", "exec"), {})
        first = target.read_text(encoding="utf-8")
        self.assertIn("CUSTOM=keep", first)
        self.assertIn("HOSTING_RETAIN_LIMIT=50", first)
        self.assertIn("RESIDENTIAL_RETAIN_LIMIT=200", first)
        with mock.patch("sys.argv", ["migration", str(target)]):
            exec(compile(block, "migration", "exec"), {})
        self.assertEqual(first, target.read_text(encoding="utf-8"))

    def test_busy_job_does_not_reset_other_connection_flag(self):
        manager.is_connecting = True
        manager.run_pool_maintenance()
        self.assertTrue(manager.is_connecting)
        self.assertFalse(manager.maintenance_lock.locked())
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
