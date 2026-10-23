#!/usr/bin/env python3
"""Export a bounded group conversation using a locally installed WeChat Toolkit.

This file does not call a model or send messages. Runtime exports are private.
"""
import argparse
import collections
import ctypes
import ctypes.util
import datetime as dt
import importlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


class ExtractionError(RuntimeError):
    pass


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    required = ("group_name", "group_id", "wechat_cli", "toolkit_src", "output_root")
    for field in required:
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise ValueError(f"Missing configuration field: {field}")
        if "<" in config[field] or ">" in config[field]:
            raise ValueError(f"Replace the placeholder in: {field}")
    if not re.fullmatch(r"\d+@chatroom", config["group_id"]):
        raise ValueError("group_id must be the numeric ID returned by WeChat CLI")
    for field in ("wechat_cli", "toolkit_src", "output_root"):
        expanded = Path(config[field]).expanduser()
        if not expanded.is_absolute():
            raise ValueError(f"Use an absolute path or ~/ path for: {field}")
        config[field] = str(expanded.resolve())
    ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    return config


def parse_time(value, tz):
    parsed = dt.datetime.fromisoformat(value)
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)


def time_window(since, until, hours, tz, now=None):
    now = now or dt.datetime.now(tz)
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("hours must be finite and positive")
    end = parse_time(until, tz) if until else now.replace(microsecond=0)
    start = parse_time(since, tz) if since else end - dt.timedelta(hours=hours)
    if start >= end or end > now:
        raise ValueError("Expected start < end <= current time")
    # Database timestamps have whole-second resolution.
    return start.replace(microsecond=0), end.replace(microsecond=0)


def scalar(result):
    values = [int(line.strip()) for line in result.splitlines() if line.strip().isdigit()]
    if len(values) != 1:
        raise ExtractionError("Database did not return one numeric result")
    return values[0]


def verify_group(stdout, group_id):
    matches = []
    for line in stdout.splitlines():
        gid, separator, name = line.partition(" | ")
        if separator and re.fullmatch(r"\d+@chatroom", gid.strip()):
            matches.append((gid.strip(), name.strip()))
    if not any(gid == group_id for gid, _ in matches):
        raise ExtractionError("Configured group ID was not found by the group-name search")
    # The upstream `msg` command selects the first name match. Refuse ambiguity.
    if len(matches) != 1:
        raise ExtractionError("Group-name search is ambiguous; use a unique group name")
    return matches[0][1]


class Decoder:
    MAX_BYTES = 20_000_000

    def __init__(self):
        library = ctypes.util.find_library("zstd")
        if not library:
            for candidate in ("/opt/homebrew/lib/libzstd.dylib", "/usr/local/lib/libzstd.dylib"):
                if Path(candidate).is_file():
                    library = candidate
                    break
        if not library:
            raise ExtractionError("libzstd is missing; install zstd first")
        self.lib = ctypes.CDLL(library)
        self.lib.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self.lib.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        self.lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        self.lib.ZSTD_decompress.restype = ctypes.c_size_t
        self.lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        self.lib.ZSTD_isError.restype = ctypes.c_uint

    def __call__(self, value):
        data = bytes.fromhex(value)
        compressed = data.startswith(b"\x28\xb5\x2f\xfd")
        if compressed:
            size = self.lib.ZSTD_getFrameContentSize(data, len(data))
            if size == 2**64 - 1:  # ZSTD_CONTENTSIZE_UNKNOWN
                size = self.MAX_BYTES
            if size > self.MAX_BYTES:  # Also rejects ZSTD_CONTENTSIZE_ERROR.
                raise ValueError("Invalid or oversized zstd frame")
            buffer = ctypes.create_string_buffer(max(1, size))
            got = self.lib.ZSTD_decompress(buffer, size, data, len(data))
            if self.lib.ZSTD_isError(got):
                raise ValueError("zstd decompression failed")
            data = buffer.raw[:got]
        return data.decode("utf-8"), compressed


def normalize(body, local_type):
    typ, subtype = local_type & 0xffffffff, local_type >> 32
    if typ == 1:
        return {"kind": "text", "text": body.strip()}
    kinds = {3: "image", 34: "audio", 42: "contact", 43: "video", 47: "emoji", 10000: "system"}
    if typ in kinds:
        return {"kind": kinds[typ], "text": ""}
    if typ != 49:
        return {"kind": "other", "text": ""}
    root = ET.fromstring(body[body.find("<"):])
    app = root if root.tag == "appmsg" else root.find(".//appmsg")
    if app is None:
        return {"kind": "other", "text": ""}
    subtype = int(app.findtext("type") or subtype)
    title = app.findtext("title", "").strip()
    if subtype in (8, 62):
        return {"kind": "emoji" if subtype == 8 else "pat", "text": ""}
    if subtype == 57:
        result = {"kind": "reply", "text": title}
        ref = app.find("refermsg")
        if ref is not None:
            quoted = ref.findtext("content", "")
            if quoted.lstrip().startswith("<"):
                try:
                    qroot = ET.fromstring(quoted)
                    quoted = qroot.findtext(".//title") or qroot.findtext(".//desc") or "[引用非文本消息]"
                except ET.ParseError:
                    quoted = "[引用非文本消息]"
            result["quote"] = {"sender": ref.findtext("displayname", ""), "text": quoted[:1200], "truncated": len(quoted) > 1200}
        return result
    if subtype == 87:
        return {"kind": "announcement", "text": app.findtext("textannouncement", "")}
    if subtype == 53:
        return {"kind": "signup", "text": title}
    result = {"kind": "share", "text": title, "description": app.findtext("des", "").strip()}
    url = app.findtext("url", "")
    if url and "support.weixin.qq.com" not in url:
        result["url"] = url
    feed = app.find("finderFeed")
    if feed is not None:
        result["text"] = feed.findtext("desc", "") or title
        result["creator"] = feed.findtext("nickname", "")
    return result


def parse_rows(raw, database, contacts, decode, tz):
    records, errors = [], []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) != 7 or not parts[0].isdigit():
            continue  # Count validation below detects malformed or lost records.
        lid, sid, timestamp, typ, content, alternative, uid_hex = parts
        record = {"id": f"{database}:{lid}", "server_id": sid,
                  "time": dt.datetime.fromtimestamp(int(timestamp), tz).strftime("%Y-%m-%d %H:%M:%S"),
                  "local_type": int(typ)}
        try:
            uid = bytes.fromhex(uid_hex).decode("utf-8")
            record["sender"] = contacts.get(uid, uid)
            body, compressed = decode(content or alternative)
            match = re.match(r"^([^\n<]{1,100}):\n", body)
            if match:
                uid = match.group(1)
                record["sender"] = contacts.get(uid, uid)
                body = body[match.end():]
            record.update(normalize(body, int(typ)))
            record["compressed"] = compressed
        except (ValueError, ET.ParseError) as error:
            record.setdefault("sender", "[无法解析发送人]")
            record.update(kind="decode_error", text="")
            errors.append({"id": record["id"], "error": type(error).__name__})
        records.append(record)
    return records, errors


def check_count(expected, records):
    if len(records) != expected:
        raise ExtractionError(f"Coverage mismatch: expected {expected}, fetched {len(records)}")


def deduplicate(records):
    unique, seen = [], set()
    for record in sorted(records, key=lambda item: (item["time"], item["id"])):
        identity = ("server", record["server_id"]) if record["server_id"] not in ("", "0") else ("local", record["id"])
        if identity not in seen:
            seen.add(identity)
            unique.append(record)
    return unique


def write_exports(out, rows, metadata):
    def write_json(name, value):
        (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_json("messages.json", rows)
    lines = [f"{metadata['group']}｜({metadata['start']}, {metadata['end']}]",
             "本机已同步记录；图片未OCR，音视频未转录；引用不重复计数。", ""]
    compact, index = [], {}
    for row in rows:
        if row["kind"] not in ("text", "reply", "share", "signup", "announcement"):
            continue
        index[row["id"]] = len(lines) + 1
        prefix = f"[{row['time']}] 【{row['sender']}】[{row['kind']}] "
        lines.extend((prefix + row["text"]).splitlines())
        for field, label in (("description", "卡片摘要"), ("url", "链接"), ("creator", "作者")):
            if row.get(field) and row[field] != row["text"]:
                lines.extend(f"  {label}: {row[field]}".splitlines())
        if row.get("quote"):
            quote = row["quote"]
            lines.extend(f"  ↳ 引用【{quote['sender']}】: {quote['text']}".splitlines())
            if quote.get("truncated"):
                lines.append("  [引用超过1200字符，已截断]")
        compact.append(prefix + row["text"].replace("\n", " / "))
    (out / "messages-readable.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "messages-compact.txt").write_text("\n".join(compact) + "\n", encoding="utf-8")
    write_json("line-index.json", index)
    write_json("metadata.json", metadata)


def run(config, start, end, out):
    tz = ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    env = dict(os.environ, TZ=str(tz))
    groups = subprocess.run([config["wechat_cli"], "groups", "search", config["group_name"]],
                            capture_output=True, text=True, env=env, check=True, timeout=90)
    group_name = verify_group(groups.stdout, config["group_id"])
    days = max(1, math.ceil((dt.datetime.now(tz) - start).total_seconds() / 86400) + 1)
    cli = subprocess.run([config["wechat_cli"], "msg", config["group_name"], str(days), "--limit", "20000"],
                         capture_output=True, text=True, env=env, check=True, timeout=120)
    scoped = []
    for chunk in re.split(r"(?=^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\])", cli.stdout, flags=re.M):
        match = re.match(r"\[([^\]]+)\]", chunk)
        if match and start < parse_time(match.group(1), tz) <= end:
            scoped.append(chunk.rstrip())

    sys.path.insert(0, config["toolkit_src"])
    keys = importlib.import_module("wechat_keys")
    query = importlib.import_module("wechat_query")
    cache = keys.ensure_keys()
    if not cache:
        raise ExtractionError("WeChat database credentials unavailable")
    contacts = query.resolve_contacts(cache)
    base, table, decode = keys.get_db_base(), query._find_msg_table(config["group_id"]), Decoder()
    if not re.fullmatch(r"Msg_[0-9a-f]{32}", table):
        raise ExtractionError("Unsupported message table identifier")
    rows, errors, databases, counts = [], [], [], {}
    start_ts, end_ts, latest = int(start.timestamp()), int(end.timestamp()), 0
    for relative in query._get_msg_dbs(cache):
        path, key = os.path.join(base, relative), cache[relative]
        def sql(statement):
            return keys.sqlcipher_query(path, key, "PRAGMA query_only=ON;\n" + statement, timeout=60)
        if not scalar(sql(f"SELECT count(*) FROM sqlite_master WHERE name='{table}';")):
            continue
        databases.append(relative)
        condition = f"create_time>{start_ts} AND create_time<={end_ts}"
        counts[relative] = scalar(sql(f"SELECT count(*) FROM {table} WHERE {condition};"))
        latest = max(latest, scalar(sql(f"SELECT coalesce(max(create_time),0) FROM {table};")))
        raw = sql(f""".separator '|'
SELECT m.local_id,m.server_id,m.create_time,m.local_type,hex(m.message_content),hex(m.compress_content),hex(n.user_name)
FROM {table} m LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id
WHERE m.create_time>{start_ts} AND m.create_time<={end_ts}
ORDER BY m.create_time,m.local_id;""")
        records, failures = parse_rows(raw, relative, contacts, decode, tz)
        check_count(counts[relative], records)
        rows.extend(records)
        errors.extend(failures)
    if not databases:
        raise ExtractionError("Target group message table was not found")
    rows = deduplicate(rows)
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    (out / "cli-scoped.txt").write_text("\n".join(scoped) + "\n", encoding="utf-8")
    metadata = {"status": "partial" if errors else "ok", "group": group_name, "group_id": config["group_id"],
                "timezone": str(tz), "start": start.isoformat(), "end": end.isoformat(), "interval": "(start,end]",
                "raw_count": len(rows), "pre_dedup_rows": sum(counts.values()),
                "kinds": dict(collections.Counter(row["kind"] for row in rows)),
                "daily": dict(collections.Counter(row["time"][:10] for row in rows)), "cli_scoped_count": len(scoped),
                "first": rows[0]["time"] if rows else None, "latest": rows[-1]["time"] if rows else None,
                "latest_local_group_message": dt.datetime.fromtimestamp(latest, tz).isoformat() if latest else None,
                "source_databases": databases, "decode_errors": errors, "output_dir": str(out),
                "limitations": ["Local synchronized messages only", "No image OCR or audio/video transcription",
                                "Forwarded collections: card summary only", "Quote text capped at 1200 characters",
                                "No cross-run checkpoint; deduplication is within this run"]}
    write_exports(out, rows, metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 2 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.local.json"))
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--since", help="Exclusive ISO start (naive timestamps use configured timezone)")
    parser.add_argument("--until", help="Inclusive ISO end; default: current time")
    parser.add_argument("--output", type=Path, help="New output directory; existing directories are refused")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = load_config(args.config)
        tz = ZoneInfo(config.get("timezone", "Asia/Shanghai"))
        start, end = time_window(args.since, args.until, args.hours, tz)
        out = args.output.expanduser().resolve() if args.output else Path(config["output_root"]) / end.strftime("%Y-%m-%d_%H%M%S")
        if out.exists():
            raise ExtractionError("Output directory already exists; choose a new path")
        if not Path(config["wechat_cli"]).is_file():
            raise ExtractionError("Configured WeChat CLI does not exist")
        if not all((Path(config["toolkit_src"]) / name).is_file() for name in ("wechat_keys.py", "wechat_query.py")):
            raise ExtractionError("toolkit_src must contain wechat_keys.py and wechat_query.py")
        return run(config, start, end, out)
    except subprocess.CalledProcessError:
        print("Extraction failed: WeChat CLI returned an error. Check it locally; do not publish its logs.", file=sys.stderr)
    except (OSError, ValueError, KeyError, ImportError, ExtractionError, subprocess.TimeoutExpired) as error:
        print(f"Extraction failed: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
