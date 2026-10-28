"""Exercise CLI logging levels and event logs through real processes and probes."""

import asyncio
import logging
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

from netpulse.client import Client
from netpulse.server import Server


def read_metric(client, name, *, slot=None):
    labels = 'server="edge"' if slot is None else f'server="edge",slot="{slot}"'
    prefix = f"{name}{{{labels}}} "
    for line in client.metrics().splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix):])
    raise AssertionError(f"missing metric {name} for {labels}")


class LogLevelTests(unittest.TestCase):
    def test_log_level_validation(self):
        self.assertEqual(Server({"server": {"log_level": "debug"},
                                 "clients": {"alice": "secret"}}).log_level, logging.DEBUG)
        base = {"client": {"id": "alice", "log_level": "warning"},
                "servers": [{"name": "edge", "host": "127.0.0.1", "tcp_port": 9009,
                             "udp_port": 9010, "secret": "secret"}]}
        self.assertEqual(Client(base).log_level, logging.WARNING)
        for invalid in ("TRACE", 10, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    Server({"server": {"log_level": invalid}, "clients": {"alice": "secret"}})
                with self.assertRaises(ValueError):
                    Client({**base, "client": {**base["client"], "log_level": invalid}})

    def test_cli_config_and_override_filter_real_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            server_config = path / "server.toml"
            server_config.write_text('[server]\nhost="127.0.0.1"\ntcp_port=0\nudp_port=0\n'
                                     'log_level="ERROR"\n[clients]\nalice="sensitive-key"\n')
            client_config = path / "client.toml"
            client_config.write_text('[client]\nid="alice"\nmetrics_port=0\n'
                                     'udp_interval=0.05\ntcp_interval=0.05\n'
                                     'reconnect_interval=0.1\nlog_level="ERROR"\n'
                                     '[[servers]]\nname="unreachable"\nhost="127.0.0.1"\n'
                                     'tcp_port=1\nudp_port=1\nsecret="sensitive-key"\n'
                                     'tcp_connections=1\n')

            def run(module, config, override=()):
                proc = subprocess.Popen([sys.executable, "-m", module, str(config), *override],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    time.sleep(0.35)
                    self.assertIsNone(proc.poll(), "CLI terminated before logging")
                    proc.terminate()
                    _, stderr = proc.communicate(timeout=4)
                    self.assertEqual(proc.returncode, 0, stderr.decode())
                    return stderr.decode()
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()

            self.assertEqual(run("netpulse.server", server_config), "")
            self.assertEqual(run("netpulse.client", client_config), "")
            server_logs = run("netpulse.server", server_config, ("--log-level", "DEBUG"))
            client_logs = run("netpulse.client", client_config, ("--log-level", "DEBUG"))
            self.assertIn("INFO netpulse.server", server_logs)
            self.assertIn("DEBUG netpulse.client", client_logs)
            self.assertNotIn("sensitive-key", server_logs + client_logs)


class LogTransitions(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_recovery_and_no_repeated_warnings(self):
        config = {"server": {"host": "127.0.0.1", "tcp_port": 0, "udp_port": 0,
                             "heartbeat_idle_timeout": 0.5}, "clients": {"alice": "secret"}}
        server = Server(config)
        await server.start()
        tcp_port = server.tcp_server.sockets[0].getsockname()[1]
        udp_port = server.udp_transport.get_extra_info("sockname")[1]
        client = Client({"client": {"id": "alice", "metrics_port": 0,
                                     "udp_interval": 0.06, "udp_timeout": 0.09,
                                     "tcp_interval": 0.06, "tcp_timeout": 0.15,
                                     "reconnect_interval": 0.06},
                         "servers": [{"name": "edge", "host": "127.0.0.1",
                                      "tcp_port": tcp_port, "udp_port": udp_port,
                                      "secret": "secret", "tcp_connections": 1}]})
        replacement = None
        try:
            with self.assertLogs("netpulse.client", level="INFO") as captured:
                await client.start()
                await self._until(lambda: read_metric(client, "netpulse_udp_received_total") >= 1
                                  and read_metric(client, "netpulse_tcp_up", slot=0) == 1)
                await server.close()
                await self._until(lambda: read_metric(client, "netpulse_udp_lost_total") >= 1
                                  and read_metric(client, "netpulse_tcp_up", slot=0) == 0)
                warnings = [record for record in captured.records if record.levelno == logging.WARNING]
                self.assertEqual(len(warnings), 2)  # UDP loss and this TCP slot disconnect.
                await asyncio.sleep(0.24)
                self.assertEqual(len([r for r in captured.records if r.levelno == logging.WARNING]), 2)
                replacement = Server({"server": {"host": "127.0.0.1", "tcp_port": tcp_port,
                                                 "udp_port": udp_port}, "clients": {"alice": "secret"}})
                await replacement.start()
                await self._until(lambda: read_metric(client, "netpulse_udp_up") == 1
                                  and read_metric(client, "netpulse_tcp_up", slot=0) == 1)
            self.assertGreaterEqual(len([r for r in captured.records if r.levelno == logging.INFO]), 4)
            self.assertNotIn("secret", "\n".join(captured.output))
        finally:
            await client.close()
            await server.close()
            if replacement is not None:
                await replacement.close()

    async def _until(self, condition):
        deadline = asyncio.get_running_loop().time() + 3
        while not condition():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("probe transition did not occur")
            await asyncio.sleep(0.02)
