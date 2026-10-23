"""All fixtures are fabricated in code. These tests never access real WeChat data."""
import ctypes
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("extract", ROOT / "scripts" / "extract.py")
extract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extract)
TZ = ZoneInfo("Asia/Shanghai")
GROUP_ID = "0" * 12 + "@chatroom"
NOW = dt.datetime(2024, 6, 2, 10, tzinfo=TZ)


class WindowAndConfigTests(unittest.TestCase):
    def test_exact_rolling_window(self):
        start, end = extract.time_window(None, None, 24, TZ, NOW)
        self.assertEqual(start, NOW - dt.timedelta(hours=24))
        self.assertEqual(end, NOW)

    def test_explicit_timezone_conversion(self):
        start, end = extract.time_window("2024-06-01T02:00:00+00:00", "2024-06-02T10:00:00", 24, TZ, NOW)
        self.assertEqual(start.hour, 10)
        self.assertEqual(end, NOW)

    def test_invalid_and_future_windows(self):
        for hours in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                extract.time_window(None, None, hours, TZ, NOW)
        with self.assertRaises(ValueError):
            extract.time_window(None, "2024-06-03T10:00:00", 24, TZ, NOW)
        with self.assertRaises(ValueError):
            extract.time_window("2024-06-02T10:00:00", None, 24, TZ, NOW)

    def test_example_config_requires_replacement(self):
        with self.assertRaisesRegex(ValueError, "placeholder"):
            extract.load_config(ROOT / "config.example.json")

    def test_group_verification_refuses_ambiguity(self):
        one = GROUP_ID + " | Synthetic group\n"
        self.assertEqual(extract.verify_group(one, GROUP_ID), "Synthetic group")
        with self.assertRaisesRegex(extract.ExtractionError, "ambiguous"):
            extract.verify_group(one + "1" * 12 + "@chatroom | Duplicate name\n", GROUP_ID)
        with self.assertRaises(extract.ExtractionError):
            extract.verify_group("", GROUP_ID)


class MessageTests(unittest.TestCase):
    def test_reply_keeps_author_and_context(self):
        body = "<msg><appmsg><type>57</type><title>虚构回复</title><refermsg><displayname>示例甲</displayname><content>虚构问题</content></refermsg></appmsg></msg>"
        result = extract.normalize(body, (57 << 32) | 49)
        self.assertEqual(result["kind"], "reply")
        self.assertEqual(result["quote"]["sender"], "示例甲")
        self.assertEqual(result["quote"]["text"], "虚构问题")
        self.assertFalse(result["quote"]["truncated"])

    def test_quote_truncation_is_explicit(self):
        body = "<appmsg><type>57</type><title>reply</title><refermsg><content>" + "a" * 1201 + "</content></refermsg></appmsg>"
        result = extract.normalize(body, 49)
        self.assertTrue(result["quote"]["truncated"])
        self.assertEqual(len(result["quote"]["text"]), 1200)

    def test_nested_quote_and_share_card(self):
        body = "<appmsg><type>57</type><title>reply</title><refermsg><content>&lt;appmsg&gt;&lt;title&gt;Synthetic card&lt;/title&gt;&lt;/appmsg&gt;</content></refermsg></appmsg>"
        self.assertEqual(extract.normalize(body, 49)["quote"]["text"], "Synthetic card")
        card = "<appmsg><type>5</type><title>Synthetic resource</title><des>Example</des><url>https://example.com/resource</url></appmsg>"
        self.assertEqual(extract.normalize(card, 49)["url"], "https://example.com/resource")

    def test_media_content_not_inferred(self):
        self.assertEqual(extract.normalize("opaque", 3), {"kind": "image", "text": ""})
        self.assertEqual(extract.normalize("opaque", 34)["kind"], "audio")

    def test_deduplication_preserves_local_only_messages(self):
        def row(db, sid):
            return {"id": db + ":1", "server_id": sid, "time": "2024-06-01 12:00:00"}
        rows = [row("one", "42"), row("two", "42"), row("three", "0"), row("four", "0")]
        self.assertEqual(len(extract.deduplicate(rows)), 3)

    def test_bad_query_and_count_fail_closed(self):
        with self.assertRaises(extract.ExtractionError):
            extract.scalar("query failed")
        with self.assertRaises(extract.ExtractionError):
            extract.scalar("1\n2")
        with self.assertRaisesRegex(extract.ExtractionError, "Coverage mismatch"):
            extract.check_count(2, [{}])
        self.assertEqual(extract.scalar("ok\n0"), 0)


class DecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.decoder = extract.Decoder()
        except (OSError, extract.ExtractionError) as error:
            raise unittest.SkipTest(str(error))

    def test_actual_zstd_frame(self):
        lib = self.decoder.lib
        lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
        lib.ZSTD_compressBound.restype = ctypes.c_size_t
        lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        lib.ZSTD_compress.restype = ctypes.c_size_t
        source = "完全虚构的压缩消息".encode()
        capacity = lib.ZSTD_compressBound(len(source))
        buffer = ctypes.create_string_buffer(capacity)
        size = lib.ZSTD_compress(buffer, capacity, source, len(source), 1)
        self.assertFalse(lib.ZSTD_isError(size))
        text, compressed = self.decoder(buffer.raw[:size].hex())
        self.assertEqual(text, source.decode())
        self.assertTrue(compressed)

    def test_plain_invalid_utf8_and_broken_frame(self):
        self.assertEqual(self.decoder("hello".encode().hex()), ("hello", False))
        for value in ("ff", "28b52ffd00", "not-hex"):
            with self.assertRaises(ValueError):
                self.decoder(value)

    def test_sender_prefix_and_decode_errors(self):
        body = "synthetic_user:\n虚构正文".encode().hex()
        raw = f"1|42|1717228800|1|{body}||\n2|43|1717228801|49|ff||\n"
        rows, errors = extract.parse_rows(raw, "synthetic.db", {"synthetic_user": "示例乙"}, self.decoder, TZ)
        self.assertEqual(rows[0]["sender"], "示例乙")
        self.assertEqual(rows[0]["text"], "虚构正文")
        self.assertEqual(rows[1]["kind"], "decode_error")
        self.assertEqual(len(errors), 1)


class SyntheticIntegrationTests(unittest.TestCase):
    def setUp(self):
        try:
            extract.Decoder()
        except (OSError, extract.ExtractionError) as error:
            self.skipTest(str(error))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.toolkit = self.base / "toolkit"
        self.toolkit.mkdir()
        self.cli = self.base / "wechat"
        self.cli.write_text(f"#!{sys.executable}\nimport sys\nif sys.argv[1] == 'groups':\n print({GROUP_ID!r} + ' | Synthetic group')\nelse:\n print('[2024-06-01 12:00:00] synthetic baseline')\n")
        self.cli.chmod(0o700)
        self.start = int((NOW - dt.timedelta(hours=24)).timestamp())
        self.end = int(NOW.timestamp())
        # Include records at both boundaries and outside them; the fake backend
        # applies the same SQL predicate instead of returning a canned row count.
        body = "synthetic_user:\n完全虚构的集成测试".encode().hex()
        self.fixture = [
            [1, 101, self.start - 1, 1, body, "", ""],
            [2, 102, self.start, 1, body, "", ""],
            [3, 103, self.start + 1, 1, body, "", ""],
            [4, 104, self.end, 1, body, "", ""],
            [5, 105, self.end + 1, 1, body, "", ""],
        ]
        self.config = self.base / "config.local.json"
        self.config.write_text(json.dumps({"group_name": "Synthetic group", "group_id": GROUP_ID,
                                          "timezone": "Asia/Shanghai", "wechat_cli": str(self.cli),
                                          "toolkit_src": str(self.toolkit), "output_root": str(self.base / "private")}, ensure_ascii=False))
        self.write_backend()

    def write_backend(self, mismatch=False):
        (self.toolkit / "wechat_keys.py").write_text(
            "import re\n"
            f"ROWS = {self.fixture!r}\n"
            "def ensure_keys(): return {'message/message_0.db': 'synthetic-not-a-real-key'}\n"
            "def get_db_base(): return '.'\n"
            "def sqlcipher_query(path, key, sql, timeout=60):\n"
            " assert sql.startswith('PRAGMA query_only=ON;')\n"
            " if 'sqlite_master' in sql: return '1'\n"
            " if 'max(create_time)' in sql: return str(max(r[2] for r in ROWS))\n"
            " start = int(re.search(r'create_time>([0-9]+)', sql).group(1))\n"
            " end = int(re.search(r'create_time<=([0-9]+)', sql).group(1))\n"
            " selected = [r for r in ROWS if start < r[2] <= end]\n"
            f" if 'count(*)' in sql: return str(len(selected) + {int(mismatch)})\n"
            " return '\\n'.join('|'.join(map(str, r)) for r in selected)\n"
        )
        (self.toolkit / "wechat_query.py").write_text(
            "def resolve_contacts(cache): return {'synthetic_user': '示例参与者'}\n"
            "def _find_msg_table(group_id): return 'Msg_' + 'a' * 32\n"
            "def _get_msg_dbs(cache): return ['message/message_0.db']\n"
        )

    def invoke(self, output):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "extract.py"),
                               "--config", str(self.config), "--since", "2024-06-01T10:00:00+08:00",
                               "--until", "2024-06-02T10:00:00+08:00", "--output", str(output)],
                              capture_output=True, text=True)

    def test_pipeline_window_exports_index_permissions_and_no_overwrite(self):
        output = self.base / "export"
        result = self.invoke(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads((output / "metadata.json").read_text())
        self.assertEqual(metadata["raw_count"], 2)
        self.assertEqual(metadata["decode_errors"], [])
        rows = json.loads((output / "messages.json").read_text())
        self.assertEqual([row["server_id"] for row in rows], ["103", "104"])
        self.assertEqual(rows[0]["sender"], "示例参与者")
        index = json.loads((output / "line-index.json").read_text())
        lines = (output / "messages-readable.txt").read_text().splitlines()
        self.assertIn("示例参与者", lines[index[rows[0]["id"]] - 1])
        self.assertFalse((output / "analysis.md").exists())
        combined = "\n".join(path.read_text() for path in output.iterdir())
        self.assertNotIn("synthetic-not-a-real-key", combined)
        if os.name == "posix":
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual((output / "messages.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.invoke(output).returncode, 1)

    def test_query_failure_is_not_an_empty_success(self):
        self.write_backend(mismatch=True)
        output = self.base / "bad-count"
        result = self.invoke(output)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Coverage mismatch", result.stderr)
        self.assertFalse((output / "metadata.json").exists())

    def test_decode_failure_returns_partial(self):
        self.fixture[2][4] = "ff"
        self.write_backend()
        output = self.base / "partial"
        result = self.invoke(output)
        self.assertEqual(result.returncode, 2, result.stderr)
        metadata = json.loads((output / "metadata.json").read_text())
        self.assertEqual(metadata["status"], "partial")
        self.assertEqual(len(metadata["decode_errors"]), 1)


if __name__ == "__main__":
    unittest.main()
