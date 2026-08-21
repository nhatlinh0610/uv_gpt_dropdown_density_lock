"""Focused contract tests for the pure framed Pro process protocol."""

from __future__ import annotations

from io import BytesIO
import importlib.util
from pathlib import Path
import struct
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "uv_gpt" / "pro_process_protocol.py"
SPEC = importlib.util.spec_from_file_location("pro_process_protocol_test_module", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
PROTOCOL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROTOCOL
SPEC.loader.exec_module(PROTOCOL)


class ProProcessProtocolTests(unittest.TestCase):
    def make_frame(self):
        return PROTOCOL.encode_message(
            PROTOCOL.MessageType.TASK,
            {"operation": "sum_squares", "items": [("b", [2, 3]), ("a", [1])]},
            session_nonce="session-1",
            generation=7,
            batch_id="batch-2",
            sequence=11,
            item_count=2,
        )

    def test_round_trip_uses_protocol_five_and_all_identity_fields(self):
        self.assertEqual(PROTOCOL.PICKLE_PROTOCOL, 5)
        frame = self.make_frame()
        decoded = PROTOCOL.decode_message(frame)
        self.assertEqual(decoded.message_type, PROTOCOL.MessageType.TASK)
        self.assertEqual(decoded.session_nonce, "session-1")
        self.assertEqual(decoded.generation, 7)
        self.assertEqual(decoded.batch_id, "batch-2")
        self.assertEqual(decoded.sequence, 11)
        self.assertEqual(decoded.item_count, 2)
        self.assertEqual(decoded.payload["operation"], "sum_squares")

        stream = BytesIO()
        written = PROTOCOL.write_message(
            stream,
            PROTOCOL.MessageType.READY,
            {"ok": True},
            session_nonce="session-1",
            generation=7,
        )
        self.assertEqual(written, len(stream.getvalue()))
        stream.seek(0)
        self.assertEqual(PROTOCOL.read_message(stream).payload, {"ok": True})

    def test_rejects_truncation_and_oversize_before_payload_decode(self):
        frame = self.make_frame()
        with self.assertRaises(PROTOCOL.ProtocolError):
            PROTOCOL.decode_message(frame[:-1])
        with self.assertRaises(PROTOCOL.FrameTooLargeError):
            PROTOCOL.decode_message(struct.pack("<Q", PROTOCOL.MAX_FRAME_BYTES + 1))
        with self.assertRaises(PROTOCOL.FrameTooLargeError):
            PROTOCOL.read_message(BytesIO(struct.pack("<Q", PROTOCOL.MAX_FRAME_BYTES + 1)))

    def test_rejects_magic_version_type_and_length_corruption(self):
        prefix = PROTOCOL.FRAME_PREFIX_SIZE
        header_magic = prefix
        header_version = prefix + len(PROTOCOL.MAGIC)
        header_type = header_version + 1

        bad_magic = bytearray(self.make_frame())
        bad_magic[header_magic] ^= 0x01
        with self.assertRaises(PROTOCOL.FrameCorruptError):
            PROTOCOL.decode_message(bad_magic)

        bad_version = bytearray(self.make_frame())
        bad_version[header_version] = 99
        with self.assertRaises(PROTOCOL.FrameCorruptError):
            PROTOCOL.decode_message(bad_version)

        bad_type = bytearray(self.make_frame())
        bad_type[header_type] = 99
        with self.assertRaises(PROTOCOL.FrameCorruptError):
            PROTOCOL.decode_message(bad_type)

        bad_length = bytearray(self.make_frame())
        declared = struct.unpack_from("<Q", bad_length)[0]
        struct.pack_into("<Q", bad_length, 0, declared + 1)
        with self.assertRaises(PROTOCOL.FrameCorruptError):
            PROTOCOL.decode_message(bad_length)

    def test_rejects_payload_digest_corruption_before_unpickle(self):
        corrupted = bytearray(self.make_frame())
        corrupted[-1] ^= 0x7F
        with self.assertRaises(PROTOCOL.FrameCorruptError):
            PROTOCOL.decode_message(corrupted)

    def test_identity_validation_rejects_foreign_stale_and_future(self):
        message = PROTOCOL.Envelope(
            PROTOCOL.MessageType.RESULT,
            session_nonce="local",
            generation=4,
            batch_id="b",
            sequence=1,
            item_count=1,
            payload={"ok": True},
        )
        with self.assertRaises(PROTOCOL.MetadataError):
            PROTOCOL.validate_identity(message, session_nonce="foreign", generation=4)
        with self.assertRaises(PROTOCOL.StaleMessageError):
            PROTOCOL.validate_identity(message, session_nonce="local", generation=5)
        with self.assertRaises(PROTOCOL.FutureMessageError):
            PROTOCOL.validate_identity(message, session_nonce="local", generation=3)

    def test_unknown_metadata_is_rejected(self):
        with self.assertRaises(PROTOCOL.MetadataError):
            PROTOCOL.Envelope(255, session_nonce="s")
        with self.assertRaises(PROTOCOL.MetadataError):
            PROTOCOL.Envelope(PROTOCOL.MessageType.TASK, generation=-1)
        with self.assertRaises(PROTOCOL.MetadataError):
            PROTOCOL.Envelope(PROTOCOL.MessageType.TASK, session_nonce="x" * 2000)


if __name__ == "__main__":
    unittest.main()
