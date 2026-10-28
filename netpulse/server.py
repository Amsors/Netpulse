"""Authenticated TCP and UDP probe server."""

import argparse
import asyncio
import hmac
import logging
import math
import secrets
import signal

from .protocol import (
    MAX_DATAGRAM,
    MAX_LINE,
    decode_datagram,
    encode_datagram,
    encode_line,
    load_config,
    mac,
    parse_log_level,
    read_message,
    valid_mac,
)

logger = logging.getLogger("netpulse.server")


class _ProbeDatagrams(asyncio.DatagramProtocol):
    def __init__(self, clients: dict[str, str]) -> None:
        self.clients = clients
        self.transport: asyncio.DatagramTransport | None = None
        self.closed = asyncio.get_running_loop().create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            message = decode_datagram(data)
        except (ValueError, UnicodeError):
            return

        client = message.get("client")
        if not isinstance(client, str):
            return
        secret = self.clients.get(client)
        if (secret is None or message.get("kind") != "ping"
                or not valid_mac(secret, message)):
            return

        reply = encode_datagram(secret, "pong", client, message["seq"], message["nonce"])
        logger.debug("Authenticated UDP ping received")
        if len(reply) <= MAX_DATAGRAM and self.transport is not None and not self.transport.is_closing():
            self.transport.sendto(reply, addr)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.closed.done():
            self.closed.set_result(None)

    def error_received(self, exc: Exception) -> None:
        logger.debug("UDP socket error: %s", type(exc).__name__)


class Server:
    """Serve authenticated probes for clients listed in the configuration."""

    def __init__(self, config: dict) -> None:
        if not isinstance(config, dict):
            raise ValueError("config must be a table")
        settings = config.get("server", {})
        if not isinstance(settings, dict):
            raise ValueError("[server] must be a table")
        clients = config.get("clients")
        if not isinstance(clients, dict) or not clients:
            raise ValueError("[clients] must be a nonempty table of client IDs and secrets")
        for client, secret in clients.items():
            if not isinstance(client, str) or not client or not isinstance(secret, str) or not secret:
                raise ValueError("[clients] requires nonempty client IDs and secrets")
            try:
                client.encode("utf-8")
                secret.encode("utf-8")
            except UnicodeError as exc:
                raise ValueError("[clients] IDs and secrets must be UTF-8 encodable") from exc

        host = settings.get("host", "0.0.0.0")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("[server].host must be a nonempty string")
        self.host = host
        self.log_level = parse_log_level(settings.get("log_level", "INFO"), "[server].log_level")
        self.tcp_port = self._port(settings.get("tcp_port", 9009), "tcp_port")
        self.udp_port = self._port(settings.get("udp_port", 9010), "udp_port")
        self.handshake_timeout = self._timeout(settings.get("handshake_timeout", 5), "handshake_timeout")
        self.heartbeat_idle_timeout = self._timeout(
            settings.get("heartbeat_idle_timeout", 10), "heartbeat_idle_timeout"
        )
        self.clients = dict(clients)
        self.tcp_server: asyncio.Server | None = None
        self.udp_transport: asyncio.DatagramTransport | None = None
        self._udp_protocol: _ProbeDatagrams | None = None
        self._sessions: dict[asyncio.Task, asyncio.StreamWriter] = {}

    @staticmethod
    def _port(value: object, name: str) -> int:
        if type(value) is not int or not 0 <= value <= 65535:
            raise ValueError(f"[server].{name} must be an integer from 0 to 65535")
        return value

    @staticmethod
    def _timeout(value: object, name: str) -> float:
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"[server].{name} must be a positive finite number")
        return float(value)

    async def start(self) -> None:
        """Bind both listeners; release the TCP listener if UDP binding fails."""
        if self.tcp_server is not None or self.udp_transport is not None:
            raise RuntimeError("server is already started")
        try:
            self.tcp_server = await asyncio.start_server(
                self._accept_tcp, self.host, self.tcp_port, limit=MAX_LINE
            )
            loop = asyncio.get_running_loop()
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _ProbeDatagrams(self.clients), local_addr=(self.host, self.udp_port)
            )
            self.udp_transport = transport
            self._udp_protocol = protocol
            for sock in self.tcp_server.sockets or ():
                logger.info("TCP listening on %s", sock.getsockname())
            logger.info("UDP listening on %s", transport.get_extra_info("sockname"))
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Stop listeners and wait for active TCP sessions and UDP socket to close."""
        tcp_server = self.tcp_server
        tcp_addresses = tuple(sock.getsockname() for sock in tcp_server.sockets or ()) if tcp_server else ()
        udp_address = (
            self.udp_transport.get_extra_info("sockname") if self.udp_transport is not None else None
        )
        if tcp_server is not None:
            tcp_server.close()
        if self.udp_transport is not None:
            self.udp_transport.close()
        sessions = tuple(self._sessions)
        for session in sessions:
            self._sessions[session].close()
            session.cancel()
        if sessions:
            await asyncio.gather(*sessions, return_exceptions=True)
        if tcp_server is not None:
            await tcp_server.wait_closed()
            self.tcp_server = None
        if self.udp_transport is not None:
            if self._udp_protocol is not None:
                await self._udp_protocol.closed
            self.udp_transport = None
            self._udp_protocol = None
        for address in tcp_addresses:
            logger.info("TCP listener stopped on %s", address)
        if udp_address is not None:
            logger.info("UDP listener stopped on %s", udp_address)

    def _accept_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.tcp_server is None or not self.tcp_server.is_serving():
            writer.close()
            return
        session = asyncio.create_task(self._handle_tcp(reader, writer))
        self._sessions[session] = writer
        session.add_done_callback(self._sessions.pop)

    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        authenticated = False
        peer = writer.get_extra_info("peername")
        phase = "challenge write"
        try:
            nonce = secrets.token_hex(16)
            writer.write(encode_line({"kind": "challenge", "nonce": nonce}))
            await asyncio.wait_for(writer.drain(), self.handshake_timeout)
            phase = "authentication read"
            auth = await read_message(reader, self.handshake_timeout)
            client = auth.get("client")
            signature = auth.get("mac")
            if (auth.get("kind") != "auth" or not isinstance(client, str)
                    or not isinstance(signature, str) or not signature.isascii()):
                logger.debug("TCP authentication rejected from %s", peer)
                return
            secret = self.clients.get(client)
            if secret is None or not hmac.compare_digest(signature, mac(secret, "auth", client, 0, nonce)):
                logger.debug("TCP authentication rejected from %s", peer)
                return
            phase = "authentication write"
            writer.write(encode_line({"kind": "ok"}))
            await asyncio.wait_for(writer.drain(), self.handshake_timeout)
            authenticated = True
            logger.debug("Authenticated TCP connection from %s", peer)

            while True:
                phase = "heartbeat read"
                probe = await read_message(reader, self.heartbeat_idle_timeout)
                seq = probe.get("seq")
                if probe.get("kind") != "ping" or type(seq) is not int or seq < 0:
                    logger.debug("Invalid TCP heartbeat from %s", peer)
                    return
                phase = "heartbeat write"
                writer.write(encode_line({"kind": "pong", "seq": seq}))
                await asyncio.wait_for(writer.drain(), self.heartbeat_idle_timeout)
        except (EOFError, ValueError, UnicodeError, asyncio.TimeoutError, OSError) as exc:
            logger.debug("TCP %s from %s ended: %s", phase, peer, type(exc).__name__)
        finally:
            if authenticated:
                logger.debug("Authenticated TCP connection closed from %s", peer)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError as exc:
                logger.debug("TCP close from %s failed: %s", peer, type(exc).__name__)


async def _serve(path: str, log_level: str | None = None) -> None:
    server = Server(load_config(path))
    level = parse_log_level(log_level, "--log-level") if log_level is not None else server.log_level
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            continue
        registered.append(signum)
    try:
        await server.start()
        await stop.wait()
    finally:
        await server.close()
        for signum in registered:
            loop.remove_signal_handler(signum)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve authenticated network quality probes")
    parser.add_argument("config", help="path to the TOML server configuration")
    parser.add_argument("--log-level", metavar="LEVEL", help="override [server].log_level")
    args = parser.parse_args()
    if args.log_level is not None:
        try:
            parse_log_level(args.log_level, "--log-level")
        except ValueError as exc:
            parser.error(str(exc))
    try:
        asyncio.run(_serve(args.config, args.log_level))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
