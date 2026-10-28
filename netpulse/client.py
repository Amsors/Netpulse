"""Async TCP/UDP probes with a small Prometheus HTTP endpoint."""

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass, field
import errno
import logging
import math
import secrets
import signal
import time
from typing import Any

from .protocol import (
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

logger = logging.getLogger("netpulse.client")


def _table(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a table")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _port(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= 65535:
        raise ValueError(f"{name} must be an integer port in range")
    return value


def _duration(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


@dataclass(frozen=True)
class _Target:
    name: str
    host: str
    tcp_port: int
    udp_port: int
    secret: str
    tcp_connections: int
    tcp_interval: float
    udp_interval: float
    tcp_timeout: float
    udp_timeout: float
    reconnect_interval: float


@dataclass
class _TCPState:
    target: _Target
    slot: int
    up: int = 0
    disconnects: int = 0
    reconnects: int = 0
    rtt: float = 0.0
    connected_before: bool = False
    failure_reported: bool = False


@dataclass
class _Pending:
    nonce: str
    sent_at: float
    deadline: float
    timer: asyncio.TimerHandle


@dataclass
class _UDPState:
    target: _Target
    transport: asyncio.DatagramTransport | None = None
    pending: dict[int, _Pending] = field(default_factory=dict)
    outcomes: deque[tuple[float, bool]] = field(default_factory=deque)
    sequence: int = 0
    sent: int = 0
    received: int = 0
    lost: int = 0
    up: int = 0
    rtt: float = 0.0
    loss_reported: bool = False
    last_rejected_log: float = -math.inf


class _UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, client: "Client", state: _UDPState):
        self.client = client
        self.state = state

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.client._on_datagram(self.state, data)


# Name, type, help text, and the state attribute to expose.
_UDP_METRICS = (
    ("netpulse_udp_sent_total", "counter", "UDP probes sent.", "sent"),
    ("netpulse_udp_received_total", "counter", "UDP probes answered on time.", "received"),
    ("netpulse_udp_lost_total", "counter", "UDP probes timed out or failed to send.", "lost"),
    ("netpulse_udp_loss_ratio", "gauge", "Fraction of completed UDP probes lost in the rolling window.", None),
    ("netpulse_udp_rtt_seconds", "gauge", "Round-trip time of the last successful UDP probe in seconds.", "rtt"),
    ("netpulse_udp_up", "gauge", "Success of the last completed UDP probe.", "up"),
)
_TCP_METRICS = (
    ("netpulse_tcp_up", "gauge", "Authenticated TCP connection status for this slot.", "up"),
    ("netpulse_tcp_disconnects_total", "counter", "Established TCP connections lost by this slot.", "disconnects"),
    ("netpulse_tcp_reconnects_total", "counter", "Successful TCP reconnections after the first connection.", "reconnects"),
    ("netpulse_tcp_rtt_seconds", "gauge", "Round-trip time of the last successful TCP probe in seconds.", "rtt"),
)


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _tcp_failure_reason(exc: Exception) -> str:
    """Describe a failed probe without printing untrusted response contents."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, EOFError):
        return "peer closed connection"
    if isinstance(exc, UnicodeError):
        return "invalid text encoding"
    if type(exc) is ValueError and exc.args in (
        ("invalid challenge",), ("authentication rejected",), ("invalid pong",)
    ):
        return exc.args[0]
    if isinstance(exc, ValueError):
        return "invalid protocol response"
    if isinstance(exc, OSError) and exc.errno is not None:
        return errno.errorcode.get(exc.errno, "OS error")
    return type(exc).__name__


class Client:
    """Run independent authenticated probes against each configured server."""

    def __init__(self, config: dict):
        root = _table(config, "config")
        client = _table(root.get("client"), "client")
        self.client_id = _text(client.get("id"), "client.id")
        self.metrics_host = _text(client.get("metrics_host", "127.0.0.1"), "client.metrics_host")
        self.metrics_port = _port(client.get("metrics_port", 9108), "client.metrics_port", allow_zero=True)
        self.log_level = parse_log_level(client.get("log_level", "INFO"), "client.log_level")
        self.loss_window = _duration(client.get("loss_window", 60), "client.loss_window")
        defaults = {
            key: _duration(client.get(key, default), f"client.{key}")
            for key, default in (
                ("udp_interval", 1), ("tcp_interval", 1),
                ("udp_timeout", 2), ("tcp_timeout", 2),
                ("reconnect_interval", 1),
            )
        }
        entries = root.get("servers")
        if not isinstance(entries, list) or not entries:
            raise ValueError("servers must be a nonempty array of tables")
        targets = []
        names = set()
        for index, entry in enumerate(entries):
            item = _table(entry, f"servers[{index}]")
            prefix = f"servers[{index}]"
            name = _text(item.get("name"), f"{prefix}.name")
            if name in names:
                raise ValueError(f"{prefix}.name must be unique")
            names.add(name)
            connections = item.get("tcp_connections", 3)
            if type(connections) is not int or connections < 1:
                raise ValueError(f"{prefix}.tcp_connections must be a positive integer")
            timing = {key: _duration(item.get(key, default), f"{prefix}.{key}")
                      for key, default in defaults.items()}
            targets.append(_Target(
                name=name,
                host=_text(item.get("host"), f"{prefix}.host"),
                tcp_port=_port(item.get("tcp_port"), f"{prefix}.tcp_port"),
                udp_port=_port(item.get("udp_port"), f"{prefix}.udp_port"),
                secret=_text(item.get("secret"), f"{prefix}.secret"),
                tcp_connections=connections,
                **timing,
            ))
        self._udp = [_UDPState(target) for target in targets]
        self._tcp = [_TCPState(target, slot) for target in targets
                     for slot in range(target.tcp_connections)]
        self._tasks: set[asyncio.Task] = set()
        self._http_tasks: set[asyncio.Task] = set()
        self.metrics_server: asyncio.AbstractServer | None = None
        self._started = False
        self._closing = False

    async def start(self) -> None:
        """Bind UDP sockets and the metrics listener, then launch the probes."""
        if self._started or self._closing:
            raise RuntimeError("client already started or closed")
        self._started = True
        loop = asyncio.get_running_loop()
        try:
            self.metrics_server = await asyncio.start_server(
                self._handle_http, self.metrics_host, self.metrics_port, limit=MAX_LINE,
            )
            logger.info("Metrics listening on %s", ", ".join(
                str(sock.getsockname()) for sock in self.metrics_server.sockets
            ))
            for state in self._udp:
                self._tasks.add(asyncio.create_task(self._probe_udp(state)))
            for state in self._tcp:
                self._tasks.add(asyncio.create_task(self._probe_tcp(state)))
            logger.info("Client started: %s servers, %s TCP slots",
                        len(self._udp), len(self._tcp))
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Stop probes, pending timeouts, UDP sockets, and the HTTP listener."""
        if self._closing:
            return
        self._closing = True
        logger.info("Client stopping")
        if self.metrics_server is not None:
            self.metrics_server.close()
            await self.metrics_server.wait_closed()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        for state in self._udp:
            for pending in state.pending.values():
                pending.timer.cancel()
            state.pending.clear()
            if state.transport is not None:
                state.transport.close()
                state.transport = None
        for task in self._http_tasks:
            task.cancel()
        if self._http_tasks:
            await asyncio.gather(*self._http_tasks, return_exceptions=True)
            self._http_tasks.clear()
        logger.info("Client stopped")

    def _record_udp(self, state: _UDPState, lost: bool, now: float) -> None:
        if lost:
            state.lost += 1
            state.up = 0
            logger.debug("UDP probe lost: server=%s", state.target.name)
            if not state.loss_reported and not self._closing:
                logger.warning("UDP probe loss: server=%s", state.target.name)
                state.loss_reported = True
        else:
            state.received += 1
            state.up = 1
            logger.debug("UDP probe answered: server=%s rtt=%.6fs",
                         state.target.name, state.rtt)
            if state.loss_reported and not self._closing:
                logger.info("UDP probes recovered: server=%s", state.target.name)
                state.loss_reported = False
        state.outcomes.append((now, lost))
        self._prune(state, now)

    def _prune(self, state: _UDPState, now: float) -> None:
        threshold = now - self.loss_window
        while state.outcomes and state.outcomes[0][0] < threshold:
            state.outcomes.popleft()

    def _expire_udp(self, state: _UDPState, seq: int) -> None:
        pending = state.pending.pop(seq, None)
        if pending is not None:
            pending.timer.cancel()
            self._record_udp(state, True, time.monotonic())

    def _rejected_udp(self, state: _UDPState) -> None:
        # Rate-limit malformed and unauthenticated packets, including at DEBUG.
        if logger.isEnabledFor(logging.DEBUG):
            now = time.monotonic()
            if now - state.last_rejected_log >= 1:
                state.last_rejected_log = now
                logger.debug("Rejected UDP response: server=%s", state.target.name)

    def _on_datagram(self, state: _UDPState, data: bytes) -> None:
        if self._closing:
            return
        try:
            message = decode_datagram(data)
            seq = message.get("seq")
            if (message.get("kind") != "pong" or message.get("client") != self.client_id
                    or type(seq) is not int or not valid_mac(state.target.secret, message)):
                self._rejected_udp(state)
                return
            pending = state.pending.get(seq)
            if pending is None or message.get("nonce") != pending.nonce:
                self._rejected_udp(state)
                return
        except (ValueError, UnicodeError):
            self._rejected_udp(state)
            return
        now = time.monotonic()
        if now >= pending.deadline:
            self._expire_udp(state, seq)
            return
        state.pending.pop(seq)
        pending.timer.cancel()
        state.rtt = now - pending.sent_at
        self._record_udp(state, False, now)

    async def _probe_udp(self, state: _UDPState) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = time.monotonic()
            seq = state.sequence
            state.sequence += 1
            nonce = secrets.token_hex(16)
            deadline = started + state.target.udp_timeout
            timer = loop.call_at(deadline, self._expire_udp, state, seq)
            state.pending[seq] = _Pending(nonce, started, deadline, timer)
            state.sent += 1
            try:
                if state.transport is None:
                    transport, _ = await loop.create_datagram_endpoint(
                        lambda: _UDPProtocol(self, state),
                        remote_addr=(state.target.host, state.target.udp_port),
                    )
                    state.transport = transport
                state.transport.sendto(encode_datagram(
                    state.target.secret, "ping", self.client_id, seq, nonce,
                ))
                logger.debug("UDP probe sent: server=%s seq=%d", state.target.name, seq)
            except (OSError, RuntimeError) as exc:
                logger.debug("UDP probe send failed: server=%s seq=%d reason=%s",
                             state.target.name, seq, type(exc).__name__)
                self._expire_udp(state, seq)
            await asyncio.sleep(max(0, started + state.target.udp_interval - time.monotonic()))

    async def _probe_tcp(self, state: _TCPState) -> None:
        target = state.target
        seq = 0
        while True:
            writer = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(target.host, target.tcp_port, limit=MAX_LINE),
                    target.tcp_timeout,
                )
                challenge = await read_message(reader, target.tcp_timeout)
                nonce = challenge.get("nonce")
                if challenge.get("kind") != "challenge" or not isinstance(nonce, str) or not nonce:
                    raise ValueError("invalid challenge")
                writer.write(encode_line({
                    "kind": "auth", "client": self.client_id,
                    "mac": mac(target.secret, "auth", self.client_id, 0, nonce),
                }))
                await asyncio.wait_for(writer.drain(), target.tcp_timeout)
                if (await read_message(reader, target.tcp_timeout)).get("kind") != "ok":
                    raise ValueError("authentication rejected")
                if state.connected_before:
                    state.reconnects += 1
                state.connected_before = True
                state.up = 1
                logger.info("TCP %s: server=%s slot=%d",
                            "reconnected" if state.reconnects else "connected",
                            target.name, state.slot)
                state.failure_reported = False
                while True:
                    started = time.monotonic()
                    current_seq = seq
                    seq += 1
                    async with asyncio.timeout(target.tcp_timeout):
                        writer.write(encode_line({"kind": "ping", "seq": current_seq}))
                        await writer.drain()
                        reply = await read_message(reader, target.tcp_timeout)
                    if reply.get("kind") != "pong" or type(reply.get("seq")) is not int or reply["seq"] != current_seq:
                        raise ValueError("invalid pong")
                    state.rtt = time.monotonic() - started
                    logger.debug("TCP probe answered: server=%s slot=%d seq=%d rtt=%.6fs",
                                 target.name, state.slot, current_seq, state.rtt)
                    await asyncio.sleep(max(0, started + target.tcp_interval - time.monotonic()))
            except (OSError, TimeoutError, EOFError, ValueError, UnicodeError) as exc:
                reason = _tcp_failure_reason(exc)
                logger.debug("TCP probe failed: server=%s slot=%d reason=%s",
                             target.name, state.slot, reason)
                if not self._closing and not state.failure_reported:
                    if state.up:
                        status = "disconnected"
                    elif state.connected_before:
                        status = "reconnect failed"
                    else:
                        status = "initial connection failed"
                    logger.warning("TCP %s: server=%s slot=%d reason=%s",
                                   status, target.name, state.slot, reason)
                    state.failure_reported = True
            finally:
                if state.up:
                    state.up = 0
                    if not self._closing:
                        state.disconnects += 1
                if writer is not None:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), target.tcp_timeout)
                    except (OSError, TimeoutError):
                        pass
            await asyncio.sleep(target.reconnect_interval)

    def metrics(self) -> str:
        """Return a coherent Prometheus text-format snapshot."""
        now = time.monotonic()
        lines = []
        for name, kind, help_text, attribute in _UDP_METRICS:
            lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"))
            for state in self._udp:
                if attribute is None:
                    self._prune(state, now)
                    lost = sum(is_lost for _, is_lost in state.outcomes)
                    value = lost / len(state.outcomes) if state.outcomes else 0.0
                else:
                    value = getattr(state, attribute)
                lines.append(f'{name}{{server="{_label(state.target.name)}"}} {value}')
        for name, kind, help_text, attribute in _TCP_METRICS:
            lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"))
            for state in self._tcp:
                lines.append(f'{name}{{server="{_label(state.target.name)}",slot="{state.slot}"}} {getattr(state, attribute)}')
        return "\n".join(lines) + "\n"

    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._http_tasks.add(task)
        try:
            request = await asyncio.wait_for(reader.readline(), 5)
            path = request.split(b" ", 2)
            if len(path) >= 2 and path[0] == b"GET" and path[1].split(b"?", 1)[0] == b"/metrics":
                body = self.metrics().encode("utf-8")
                status = b"200 OK"
            else:
                body = b"Not Found\n"
                status = b"404 Not Found"
            writer.write(b"HTTP/1.1 " + status + b"\r\n"
                         b"Content-Type: text/plain; version=0.0.4; charset=utf-8\r\n"
                         b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                         b"Connection: close\r\n\r\n" + body)
            await writer.drain()
        except (OSError, TimeoutError, ValueError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            if task is not None:
                self._http_tasks.discard(task)


async def _run_cli(client: Client) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await client.start()
        await stop.wait()
    finally:
        await client.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(signum)
            except (NotImplementedError, RuntimeError):
                pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Monitor authenticated network probes")
    parser.add_argument("config", help="path to TOML configuration")
    parser.add_argument("--log-level", help="override client.log_level (DEBUG, INFO, WARNING, ERROR, CRITICAL)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        client = Client(config)
        if args.log_level is not None:
            client.log_level = parse_log_level(args.log_level, "--log-level")
    except (OSError, ValueError) as exc:
        if isinstance(exc, OSError):
            parser.error("cannot load configuration")
        parser.error(str(exc))
    logging.basicConfig(
        level=client.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_run_cli(client))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
