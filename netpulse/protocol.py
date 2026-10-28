"""Small authenticated probe protocol shared by client and server."""

import asyncio
import hashlib
import hmac
import json
import logging
import tomllib
from pathlib import Path

MAX_LINE = 4096
MAX_DATAGRAM = 1024


LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def parse_log_level(value: object, name: str) -> int:
    if not isinstance(value, str) or value.upper() not in LOG_LEVELS:
        raise ValueError(f"{name} must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    return LOG_LEVELS[value.upper()]

def load_config(path: str | Path) -> dict:
    with open(path, "rb") as stream:
        return tomllib.load(stream)


def mac(secret: str, kind: str, client: str, seq: int, nonce: str) -> str:
    payload = f"{kind}|{client}|{seq}|{nonce}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def valid_mac(secret: str, message: dict) -> bool:
    try:
        kind, client, seq, nonce, signature = (
            message[key] for key in ("kind", "client", "seq", "nonce", "mac")
        )
        if not (isinstance(kind, str) and isinstance(client, str)
                and type(seq) is int and seq >= 0 and isinstance(nonce, str)
                and isinstance(signature, str)):
            return False
        return hmac.compare_digest(mac(secret, kind, client, seq, nonce), signature)
    except (KeyError, UnicodeError):
        return False


def encode_line(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"


async def read_message(reader: asyncio.StreamReader, timeout: float) -> dict:
    # readline() enforces the StreamReader limit set by the TCP listener/dialer.
    data = await asyncio.wait_for(reader.readline(), timeout)
    if not data:
        raise EOFError("connection closed")
    if len(data) > MAX_LINE or not data.endswith(b"\n"):
        raise ValueError("invalid TCP frame")
    message = json.loads(data)
    if not isinstance(message, dict):
        raise ValueError("invalid TCP message")
    return message


def encode_datagram(secret: str, kind: str, client: str, seq: int, nonce: str) -> bytes:
    return json.dumps({"kind": kind, "client": client, "seq": seq, "nonce": nonce,
                       "mac": mac(secret, kind, client, seq, nonce)},
                      separators=(",", ":")).encode("utf-8")


def decode_datagram(data: bytes) -> dict:
    if len(data) > MAX_DATAGRAM:
        raise ValueError("oversized UDP probe")
    message = json.loads(data)
    if not isinstance(message, dict):
        raise ValueError("invalid UDP probe")
    return message
