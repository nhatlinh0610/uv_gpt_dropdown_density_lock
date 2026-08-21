"""Small framed protocol used by the external Pro proof worker.

The transport is deliberately independent of Blender.  A frame consists of a
length prefix, a fixed binary header, UTF-8 session metadata and a pickle
protocol-5 payload.  The header is checked completely before the payload is
unpickled.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import hashlib
import hmac
import pickle
import struct
from typing import Any, BinaryIO


MAGIC = b"UVGPTPW"
PROTOCOL_VERSION = 1
PICKLE_PROTOCOL = 5
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_IDENTIFIER_BYTES = 1024
MAX_ERROR_TEXT_BYTES = 512

_PREFIX = struct.Struct("<Q")
# magic, version, type, flags, generation, sequence, item_count,
# nonce length, batch length, payload length, sha256(payload)
_HEADER = struct.Struct("<7sBBHQQQHHQ32s")
FRAME_PREFIX_SIZE = _PREFIX.size
HEADER_SIZE = _HEADER.size


class ProtocolError(Exception):
    """Base class for all framing, metadata and payload errors."""


class ProtocolEOF(ProtocolError, EOFError):
    """The stream ended before a complete frame was read."""


class FrameTooLargeError(ProtocolError):
    """A frame or payload exceeds the finite protocol limit."""


class FrameCorruptError(ProtocolError):
    """A frame has inconsistent lengths, magic, version or digest."""


class MetadataError(ProtocolError):
    """A message contains invalid or unsafe metadata."""


class StaleMessageError(MetadataError):
    """A message belongs to an older generation."""


class FutureMessageError(MetadataError):
    """A message claims a generation that the receiver has not created."""


class MessageType(IntEnum):
    HELLO = 1
    READY = 2
    TASK = 3
    RESULT = 4
    ERROR = 5
    CANCEL = 6
    CANCEL_ACK = 7
    SHUTDOWN = 8
    SHUTDOWN_ACK = 9


@dataclass(frozen=True)
class Envelope:
    """Decoded message metadata and its already-validated payload."""

    message_type: MessageType
    session_nonce: str = ""
    generation: int = 0
    batch_id: str = ""
    sequence: int = 0
    item_count: int = 0
    flags: int = 0
    payload: Any = None

    def __post_init__(self) -> None:
        try:
            message_type = MessageType(self.message_type)
        except (TypeError, ValueError) as exc:
            raise MetadataError("unknown message type") from exc
        object.__setattr__(self, "message_type", message_type)
        _validate_uint(self.generation, "generation")
        _validate_uint(self.sequence, "sequence")
        _validate_uint(self.item_count, "item_count")
        _validate_uint(self.flags, "flags", maximum=0xFFFF)
        _encode_identifier(self.session_nonce, "session_nonce")
        _encode_identifier(self.batch_id, "batch_id")


Message = Envelope
_UNSET = object()


def _validate_uint(value: int, name: str, *, maximum: int = 0xFFFFFFFFFFFFFFFF) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise MetadataError(f"{name} must be an unsigned integer")


def _encode_identifier(value: str, name: str) -> bytes:
    if not isinstance(value, str):
        raise MetadataError(f"{name} must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MetadataError(f"{name} is not valid UTF-8") from exc
    if len(encoded) > MAX_IDENTIFIER_BYTES:
        raise MetadataError(f"{name} is too large")
    return encoded


def _decode_identifier(value: bytes, name: str) -> str:
    if len(value) > MAX_IDENTIFIER_BYTES:
        raise FrameCorruptError(f"{name} is too large")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrameCorruptError(f"{name} is not valid UTF-8") from exc


def payload_digest(payload_bytes: bytes) -> bytes:
    """Return the binary SHA-256 digest used by the envelope."""

    return hashlib.sha256(payload_bytes).digest()


def payload_digest_hex(payload_bytes: bytes) -> str:
    return payload_digest(payload_bytes).hex()


def _make_envelope(
    message_or_type: Envelope | MessageType | int,
    payload: Any,
    *,
    session_nonce: str,
    generation: int,
    batch_id: str,
    sequence: int,
    item_count: int,
    flags: int,
) -> Envelope:
    if isinstance(message_or_type, Envelope):
        if payload is not _UNSET:
            raise MetadataError("payload cannot be supplied twice")
        if any(
            value not in ("", 0)
            for value in (session_nonce, generation, batch_id, sequence, item_count, flags)
        ):
            raise MetadataError("metadata cannot override an Envelope")
        return message_or_type
    if payload is _UNSET:
        payload = None
    return Envelope(
        message_type=message_or_type,
        session_nonce=session_nonce,
        generation=generation,
        batch_id=batch_id,
        sequence=sequence,
        item_count=item_count,
        flags=flags,
        payload=payload,
    )


def encode_message(
    message_or_type: Envelope | MessageType | int,
    payload: Any = _UNSET,
    *,
    session_nonce: str = "",
    generation: int = 0,
    batch_id: str = "",
    sequence: int = 0,
    item_count: int = 0,
    flags: int = 0,
) -> bytes:
    """Encode an Envelope into a complete length-prefixed frame."""

    message = _make_envelope(
        message_or_type,
        payload,
        session_nonce=session_nonce,
        generation=generation,
        batch_id=batch_id,
        sequence=sequence,
        item_count=item_count,
        flags=flags,
    )
    nonce_bytes = _encode_identifier(message.session_nonce, "session_nonce")
    batch_bytes = _encode_identifier(message.batch_id, "batch_id")
    try:
        payload_bytes = pickle.dumps(message.payload, protocol=PICKLE_PROTOCOL)
    except Exception as exc:
        raise ProtocolError("payload serialization failed") from exc
    if len(payload_bytes) > MAX_FRAME_BYTES:
        raise FrameTooLargeError("payload exceeds MAX_FRAME_BYTES")
    header = _HEADER.pack(
        MAGIC,
        PROTOCOL_VERSION,
        int(message.message_type),
        message.flags,
        message.generation,
        message.sequence,
        message.item_count,
        len(nonce_bytes),
        len(batch_bytes),
        len(payload_bytes),
        payload_digest(payload_bytes),
    )
    body = header + nonce_bytes + batch_bytes + payload_bytes
    if len(body) > MAX_FRAME_BYTES:
        raise FrameTooLargeError("frame exceeds MAX_FRAME_BYTES")
    return _PREFIX.pack(len(body)) + body


def _decode_body(body: bytes) -> Envelope:
    if len(body) < HEADER_SIZE:
        raise FrameCorruptError("truncated frame header")
    (
        magic,
        version,
        raw_type,
        flags,
        generation,
        sequence,
        item_count,
        nonce_length,
        batch_length,
        payload_length,
        expected_digest,
    ) = _HEADER.unpack_from(body)
    if magic != MAGIC:
        raise FrameCorruptError("bad protocol magic")
    if version != PROTOCOL_VERSION:
        raise FrameCorruptError("unsupported protocol version")
    try:
        message_type = MessageType(raw_type)
    except ValueError as exc:
        raise FrameCorruptError("unknown message type") from exc
    if nonce_length > MAX_IDENTIFIER_BYTES or batch_length > MAX_IDENTIFIER_BYTES:
        raise FrameCorruptError("identifier exceeds limit")
    expected_body_length = HEADER_SIZE + nonce_length + batch_length + payload_length
    if expected_body_length != len(body):
        raise FrameCorruptError("inconsistent frame lengths")
    if payload_length > MAX_FRAME_BYTES:
        raise FrameTooLargeError("payload exceeds MAX_FRAME_BYTES")
    offset = HEADER_SIZE
    nonce = _decode_identifier(body[offset : offset + nonce_length], "session_nonce")
    offset += nonce_length
    batch_id = _decode_identifier(body[offset : offset + batch_length], "batch_id")
    offset += batch_length
    payload_bytes = body[offset : offset + payload_length]
    actual_digest = payload_digest(payload_bytes)
    if not hmac.compare_digest(expected_digest, actual_digest):
        raise FrameCorruptError("payload digest mismatch")
    try:
        payload = pickle.loads(payload_bytes)
    except Exception as exc:
        raise ProtocolError("payload deserialization failed") from exc
    return Envelope(
        message_type=message_type,
        session_nonce=nonce,
        generation=generation,
        batch_id=batch_id,
        sequence=sequence,
        item_count=item_count,
        flags=flags,
        payload=payload,
    )


def decode_message(frame: bytes | bytearray | memoryview) -> Envelope:
    """Validate and decode a complete length-prefixed frame."""

    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise TypeError("frame must be bytes-like")
    raw = bytes(frame)
    if len(raw) < FRAME_PREFIX_SIZE:
        raise ProtocolEOF("truncated frame length prefix")
    (body_length,) = _PREFIX.unpack_from(raw)
    if body_length > MAX_FRAME_BYTES:
        raise FrameTooLargeError("declared frame exceeds MAX_FRAME_BYTES")
    if body_length != len(raw) - FRAME_PREFIX_SIZE:
        raise FrameCorruptError("declared frame length does not match bytes")
    return _decode_body(raw[FRAME_PREFIX_SIZE:])


decode_frame = decode_message
encode_frame = encode_message


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if chunk is None:
            raise ProtocolEOF("stream returned no data")
        if not chunk:
            raise ProtocolEOF("stream ended before complete frame")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ProtocolError("binary stream returned non-bytes")
        chunk_bytes = bytes(chunk)
        chunks.append(chunk_bytes)
        remaining -= len(chunk_bytes)
    return b"".join(chunks)


def read_message(stream: BinaryIO) -> Envelope:
    """Read one complete frame from a binary stream."""

    prefix = _read_exact(stream, FRAME_PREFIX_SIZE)
    (body_length,) = _PREFIX.unpack(prefix)
    if body_length > MAX_FRAME_BYTES:
        raise FrameTooLargeError("declared frame exceeds MAX_FRAME_BYTES")
    body = _read_exact(stream, body_length)
    return _decode_body(body)


def write_message(
    stream: BinaryIO,
    message_or_type: Envelope | MessageType | int,
    payload: Any = _UNSET,
    **metadata: Any,
) -> int:
    """Encode, write and flush one complete frame."""

    frame = encode_message(message_or_type, payload, **metadata)
    written = stream.write(frame)
    if written is not None and written != len(frame):
        raise ProtocolError("binary stream wrote a partial frame")
    stream.flush()
    return len(frame)


def validate_identity(
    message: Envelope,
    *,
    session_nonce: str,
    generation: int,
) -> Envelope:
    """Validate session/generation metadata before accepting a result."""

    if message.session_nonce != session_nonce:
        raise MetadataError("foreign session nonce")
    if message.generation < generation:
        raise StaleMessageError("stale generation")
    if message.generation > generation:
        raise FutureMessageError("future generation")
    return message


__all__ = [
    "Envelope",
    "Message",
    "MessageType",
    "ProtocolError",
    "ProtocolEOF",
    "FrameTooLargeError",
    "FrameCorruptError",
    "MetadataError",
    "StaleMessageError",
    "FutureMessageError",
    "MAGIC",
    "PROTOCOL_VERSION",
    "PICKLE_PROTOCOL",
    "MAX_FRAME_BYTES",
    "FRAME_PREFIX_SIZE",
    "HEADER_SIZE",
    "payload_digest",
    "payload_digest_hex",
    "encode_message",
    "decode_message",
    "encode_frame",
    "decode_frame",
    "read_message",
    "write_message",
    "validate_identity",
]
