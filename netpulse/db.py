"""SQLite persistence for device state and bandwidth history."""

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List


class Database:
    """SQLite persistence for device state and bandwidth history."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS devices (
        mac TEXT PRIMARY KEY,
        ip TEXT NOT NULL,
        hostname TEXT DEFAULT '',
        vendor TEXT DEFAULT 'Unknown',
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL,
        state TEXT DEFAULT 'unknown',
        state_changed REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS bandwidth_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        mac TEXT NOT NULL,
        ip TEXT NOT NULL,
        rx_bytes REAL DEFAULT 0,
        tx_bytes REAL DEFAULT 0,
        rx_rate REAL DEFAULT 0,
        tx_rate REAL DEFAULT 0,
        FOREIGN KEY (mac) REFERENCES devices(mac)
    );

    CREATE TABLE IF NOT EXISTS interface_bandwidth (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        interface TEXT NOT NULL,
        rx_bytes REAL DEFAULT 0,
        tx_bytes REAL DEFAULT 0,
        rx_rate REAL DEFAULT 0,
        tx_rate REAL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS state_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        mac TEXT NOT NULL,
        old_state TEXT NOT NULL,
        new_state TEXT NOT NULL,
        FOREIGN KEY (mac) REFERENCES devices(mac)
    );

    CREATE INDEX IF NOT EXISTS idx_bw_mac ON bandwidth_samples(mac, timestamp);
    CREATE INDEX IF NOT EXISTS idx_bw_iface ON interface_bandwidth(interface, timestamp);
    CREATE INDEX IF NOT EXISTS idx_state_mac ON state_history(mac, timestamp);
    """

    def __init__(self, db_path: str, retention_days: int = 90):
        self.db_path = db_path
        self.retention_days = retention_days
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self.lock:
            for stmt in self.SCHEMA.split(";"):
                stmt = stmt.strip()
                if stmt:
                    self.conn.execute(stmt)
            self.conn.commit()

    @contextmanager
    def cursor(self):
        with self.lock:
            cur = self.conn.cursor()
            try:
                yield cur
            finally:
                self.conn.commit()
                cur.close()

    def upsert_device(self, mac: str, ip: str, hostname: str = "",
                      vendor: str = "Unknown", state: str = "online"):
        now = time.time()
        with self.cursor() as cur:
            cur.execute("SELECT state, state_changed, first_seen FROM devices WHERE mac = ?", (mac,))
            row = cur.fetchone()
            old_state = row["state"] if row else None
            state_changed = row["state_changed"] if row else now

            if old_state != state:
                state_changed = now
                if old_state:
                    cur.execute(
                        "INSERT INTO state_history (timestamp, mac, old_state, new_state) VALUES (?, ?, ?, ?)",
                        (now, mac, old_state, state)
                    )

            cur.execute("""
                INSERT INTO devices (mac, ip, hostname, vendor, first_seen, last_seen, state, state_changed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    ip = excluded.ip,
                    hostname = CASE WHEN excluded.hostname != '' THEN excluded.hostname ELSE hostname END,
                    vendor = CASE WHEN excluded.vendor != 'Unknown' THEN excluded.vendor ELSE vendor END,
                    last_seen = excluded.last_seen,
                    state = excluded.state,
                    state_changed = excluded.state_changed
            """, (mac, ip, hostname, vendor, now if not row else row["first_seen"],
                  now, state, state_changed))

    def record_bandwidth(self, mac: str, ip: str, rx_bytes: float, tx_bytes: float,
                         rx_rate: float, tx_rate: float):
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO bandwidth_samples (timestamp, mac, ip, rx_bytes, tx_bytes, rx_rate, tx_rate)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (time.time(), mac, ip, rx_bytes, tx_bytes, rx_rate, tx_rate))

    def record_interface_bw(self, interface: str, rx_bytes: float, tx_bytes: float,
                            rx_rate: float, tx_rate: float):
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO interface_bandwidth (timestamp, interface, rx_bytes, tx_bytes, rx_rate, tx_rate)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (time.time(), interface, rx_bytes, tx_bytes, rx_rate, tx_rate))

    def get_devices(self) -> List[Dict]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM devices ORDER BY last_seen DESC")
            return [dict(row) for row in cur.fetchall()]

    def get_device_bandwidth(self, mac: str, since: float) -> List[Dict]:
        with self.cursor() as cur:
            cur.execute("""
                SELECT * FROM bandwidth_samples
                WHERE mac = ? AND timestamp >= ?
                ORDER BY timestamp ASC
            """, (mac, since))
            return [dict(row) for row in cur.fetchall()]

    def get_interface_bandwidth(self, interface: str, since: float) -> List[Dict]:
        with self.cursor() as cur:
            cur.execute("""
                SELECT * FROM interface_bandwidth
                WHERE interface = ? AND timestamp >= ?
                ORDER BY timestamp ASC
            """, (interface, since))
            return [dict(row) for row in cur.fetchall()]

    def purge_old(self):
        cutoff = time.time() - (self.retention_days * 86400)
        with self.cursor() as cur:
            cur.execute("DELETE FROM bandwidth_samples WHERE timestamp < ?", (cutoff,))
            cur.execute("DELETE FROM interface_bandwidth WHERE timestamp < ?", (cutoff,))
            cur.execute("DELETE FROM state_history WHERE timestamp < ?", (cutoff,))
            cur.execute("""
                DELETE FROM devices WHERE last_seen < ?
                AND state = 'offline'
            """, (cutoff,))

    def close(self):
        self.conn.close()
