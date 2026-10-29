"""Private durable inbox for incoming Telegram events; no Telegram or model calls."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


class EventSpool:
    def __init__(
        self, path: Path, account_id: int, peers: set[int], max_events: int = 10000
    ):
        if not peers or account_id <= 0 or max_events < 1:
            raise ValueError("account, peers and positive retention are required")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL, received REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoints (peer INTEGER PRIMARY KEY, message_id INTEGER);
        """)
        binding = json.dumps([account_id, sorted(peers)])
        old = self.db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
        if old and old[0] != binding:
            self.db.close()
            raise ValueError("spool account/peer binding differs; use a new spool")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('binding', ?)", (binding,))
        self.db.execute(
            "INSERT OR IGNORE INTO meta VALUES ('stream_id', ?)", (str(uuid.uuid4()),)
        )
        self.db.commit()
        self.peers, self.max_events = peers, max_events
        self.stream_id = self.db.execute(
            "SELECT value FROM meta WHERE key='stream_id'"
        ).fetchone()[0]

    def checkpoint(self, peer: int):
        row = self.db.execute(
            "SELECT message_id FROM checkpoints WHERE peer=?", (peer,)
        ).fetchone()
        return row[0] if row else None

    def seed(self, peer: int, message_id: int):
        if peer not in self.peers:
            raise ValueError("peer outside scope")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO checkpoints VALUES (?,?)", (peer, message_id)
            )

    def append(self, payload: dict) -> bool:
        if payload["chat_id"] not in self.peers or payload.get("outgoing"):
            return False
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(encoded.encode()).hexdigest()
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO events(event_key,payload,received) VALUES (?,?,?)",
                (key, encoded, time.time()),
            )
            self.db.execute(
                "INSERT INTO checkpoints VALUES (?,?) ON CONFLICT(peer) DO UPDATE SET "
                "message_id=MAX(message_id, excluded.message_id)",
                (payload["chat_id"], payload["message_id"]),
            )
            # seq is never reset. A lagging consumer gets an explicit retention gap.
            boundary = self.db.execute(
                "SELECT seq FROM events ORDER BY seq DESC LIMIT 1 OFFSET ?",
                (self.max_events - 1,),
            ).fetchone()
            if boundary:
                removed = self.db.execute(
                    "SELECT MAX(seq) FROM events WHERE seq<?", (boundary[0],)
                ).fetchone()[0]
                if removed is not None:
                    self.db.execute(
                        "INSERT OR REPLACE INTO meta VALUES ('retention_floor', ?)",
                        (str(removed),),
                    )
                    self.db.execute("DELETE FROM events WHERE seq<=?", (removed,))
        return result.rowcount == 1

    def read(self, after_seq: int, limit: int = 20) -> dict:
        if after_seq < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid cursor or limit")
        low, high = self.db.execute("SELECT MIN(seq), MAX(seq) FROM events").fetchone()
        floor = self.db.execute(
            "SELECT value FROM meta WHERE key='retention_floor'"
        ).fetchone()
        if after_seq and floor and after_seq < int(floor[0]):
            return {
                "error": "cursor_expired",
                "oldest_seq": low,
                "latest_seq": high,
                "stream_id": self.stream_id,
            }
        rows = self.db.execute(
            "SELECT seq,payload FROM events WHERE seq>? ORDER BY seq LIMIT ?",
            (after_seq, limit),
        ).fetchall()
        return {
            "stream_id": self.stream_id,
            "events": [dict(json.loads(p), seq=s) for s, p in rows],
            "next_cursor": rows[-1][0] if rows else after_seq,
            "latest_seq": high or 0,
            "oldest_seq": low or 0,
        }

    def close(self):
        self.db.close()
