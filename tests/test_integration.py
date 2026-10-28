"""Network-level tests: actual listeners, probes and Prometheus exposition."""

import asyncio
import secrets
import unittest

from netpulse.protocol import decode_datagram, encode_datagram, encode_line, read_message, mac
from netpulse.server import Server
from netpulse.client import Client


async def eventually(predicate, timeout=4):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not met before deadline")


async def scrape(port):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    assert response.startswith(b"HTTP/1.1 200"), response
    return response.split(b"\r\n\r\n", 1)[1].decode()


def metric(text, name, labels):
    suffix = "{" + ",".join(f'{key}="{value}"' for key, value in labels.items()) + "}"
    for line in text.splitlines():
        if line.startswith(name + suffix + " "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"missing {name}{suffix} in {text}")


class QualityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        for server in self.servers:
            await server.close()

    async def make_server(self, clients=None, tcp_port=0, udp_port=0):
        server = Server({"server": {"host": "127.0.0.1", "tcp_port": tcp_port,
                                    "udp_port": udp_port, "handshake_timeout": 0.5,
                                    "heartbeat_idle_timeout": 0.6},
                         "clients": clients or {"alice": "secret-alice", "bob": "secret-bob"}})
        await server.start()
        self.servers.append(server)
        return server

    def endpoint(self, server, name, secret):
        return {"name": name, "host": "127.0.0.1",
                "tcp_port": server.tcp_server.sockets[0].getsockname()[1],
                "udp_port": server.udp_transport.get_extra_info("sockname")[1],
                "secret": secret, "tcp_connections": 2}

    async def make_client(self, client_id, endpoints):
        client = Client({"client": {"id": client_id, "metrics_host": "127.0.0.1",
                                     "metrics_port": 0, "udp_interval": 0.08,
                                     "udp_timeout": 0.11, "tcp_interval": 0.08,
                                     "tcp_timeout": 0.17, "reconnect_interval": 0.08,
                                     "loss_window": 1}, "servers": endpoints})
        await client.start()
        self.clients.append(client)
        return client

    async def test_multiple_servers_clients_and_independent_tcp_slots(self):
        first = await self.make_server()
        second = await self.make_server({"alice": "other-secret"})
        alice = await self.make_client("alice", [self.endpoint(first, "first", "secret-alice"),
                                                   self.endpoint(second, "second", "other-secret")])
        bob = await self.make_client("bob", [self.endpoint(first, "first", "secret-bob")])
        port_a = alice.metrics_server.sockets[0].getsockname()[1]
        port_b = bob.metrics_server.sockets[0].getsockname()[1]

        async def ready():
            a, b = await asyncio.gather(scrape(port_a), scrape(port_b))
            return (all(metric(a, "netpulse_udp_received_total", {"server": n}) >= 1
                        and all(metric(a, "netpulse_tcp_up", {"server": n, "slot": str(i)}) == 1
                                for i in range(2)) for n in ("first", "second"))
                    and metric(b, "netpulse_udp_received_total", {"server": "first"}) >= 1)

        deadline = asyncio.get_running_loop().time() + 4
        while asyncio.get_running_loop().time() < deadline:
            if await ready():
                break
            await asyncio.sleep(0.03)
        else:
            self.fail("multi-server probes did not recover")
        a = await scrape(port_a)
        self.assertIn("# TYPE netpulse_udp_lost_total counter", a)
        self.assertEqual(metric(a, "netpulse_udp_lost_total", {"server": "first"}), 0)
        self.assertEqual(metric(a, "netpulse_udp_loss_ratio", {"server": "second"}), 0)
        self.assertGreater(metric(a, "netpulse_udp_rtt_seconds", {"server": "first"}), 0)

    async def test_unresolvable_target_does_not_stop_other_probes(self):
        server = await self.make_server()
        good = self.endpoint(server, "reachable", "secret-alice")
        bad = {**good, "name": "unresolvable", "host": "invalid host"}
        client = await self.make_client("alice", [bad, good])
        await eventually(lambda: metric(client.metrics(), "netpulse_udp_lost_total",
                                        {"server": "unresolvable"}) >= 1
                         and metric(client.metrics(), "netpulse_udp_received_total",
                                    {"server": "reachable"}) >= 1
                         and metric(client.metrics(), "netpulse_tcp_up",
                                    {"server": "reachable", "slot": "0"}) == 1)

    async def test_udp_loss_and_tcp_disconnect_then_reconnect(self):
        server = await self.make_server()
        endpoint = self.endpoint(server, "edge", "secret-alice")
        tcp_port, udp_port = endpoint["tcp_port"], endpoint["udp_port"]
        client = await self.make_client("alice", [endpoint])
        port = client.metrics_server.sockets[0].getsockname()[1]
        await eventually(lambda: all(metric(client.metrics(), "netpulse_tcp_up", {"server": "edge", "slot": str(i)}) == 1 for i in range(2)))
        await eventually(lambda: metric(client.metrics(), "netpulse_udp_received_total", {"server": "edge"}) >= 1)
        await server.close()
        await eventually(lambda: metric(client.metrics(), "netpulse_udp_lost_total", {"server": "edge"}) >= 1
                         and metric(client.metrics(), "netpulse_udp_loss_ratio", {"server": "edge"}) > 0
                         and all(metric(client.metrics(), "netpulse_tcp_up", {"server": "edge", "slot": str(i)}) == 0
                                 for i in range(2)))
        self.assertEqual(metric(await scrape(port), "netpulse_udp_up", {"server": "edge"}), 0)
        await self.make_server(tcp_port=tcp_port, udp_port=udp_port)
        await eventually(lambda: metric(client.metrics(), "netpulse_udp_up", {"server": "edge"}) == 1
                         and all(metric(client.metrics(), "netpulse_tcp_up", {"server": "edge", "slot": str(i)}) == 1
                                 for i in range(2)))
        self.assertGreaterEqual(metric(await scrape(port), "netpulse_tcp_disconnects_total", {"server": "edge", "slot": "0"}), 1)
        self.assertGreaterEqual(metric(await scrape(port), "netpulse_tcp_reconnects_total", {"server": "edge", "slot": "0"}), 1)

    async def test_udp_rejects_unknown_or_invalid_credentials(self):
        server = await self.make_server()
        host, port = server.udp_transport.get_extra_info("sockname")[:2]
        loop = asyncio.get_running_loop()
        replies = asyncio.Queue()

        class DatagramReceiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, address):
                replies.put_nowait(data)

        transport, _ = await loop.create_datagram_endpoint(DatagramReceiver,
                                                              remote_addr=(host, port))
        try:
            for who, secret in (("unknown", "secret"), ("alice", "wrong-secret")):
                transport.sendto(encode_datagram(secret, "ping", who, 1, secrets.token_hex(8)))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(replies.get(), 0.12)
            transport.sendto(encode_datagram("secret-alice", "ping", "alice", 2, "known-nonce"))
            reply = decode_datagram(await asyncio.wait_for(replies.get(), 1))
            self.assertEqual((reply["kind"], reply["seq"], reply["nonce"]),
                             ("pong", 2, "known-nonce"))
        finally:
            transport.close()

    async def test_tcp_rejects_invalid_authentication(self):
        server = await self.make_server()
        port = server.tcp_server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            challenge = await read_message(reader, 1)
            self.assertEqual(challenge["kind"], "challenge")
            writer.write(encode_line({"kind": "auth", "client": "alice",
                                      "mac": mac("wrong-secret", "auth", "alice", 0,
                                                 challenge["nonce"])}))
            await writer.drain()
            with self.assertRaises(EOFError):
                await read_message(reader, 1)
        finally:
            writer.close()
            await writer.wait_closed()
