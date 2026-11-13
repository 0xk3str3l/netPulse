"""JSON and CSV export of current network state."""

import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path

from .db import Database

DEVICE_FIELDS = [
    "mac", "ip", "hostname", "vendor",
    "first_seen", "last_seen", "state", "state_changed",
]


class Exporter:
    """JSON and CSV export of current network state."""

    def __init__(self, db: Database, export_dir: str):
        self.db = db
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)

    def _write_atomic(self, path: Path, write) -> None:
        """Write to a sibling temp file, then atomically replace the target.

        Keeps a concurrent `--export`/cron reader from ever seeing a halved
        JSON document mid-write.
        """
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            write(f)
        os.replace(tmp, path)

    def export_json(self, filename: str = "netpulse_snapshot.json"):
        devices = self.db.get_devices()
        snapshot = {
            "timestamp": time.time(),
            "datetime": datetime.now().isoformat(),
            "device_count": len(devices),
            "online_count": sum(1 for d in devices if d.get("state") == "online"),
            "offline_count": sum(1 for d in devices if d.get("state") == "offline"),
            "devices": devices
        }
        path = self.export_dir / filename
        self._write_atomic(path, lambda f: json.dump(snapshot, f, indent=2, default=str))
        return path

    def export_csv(self, filename: str = "netpulse_devices.csv"):
        devices = self.db.get_devices()
        path = self.export_dir / filename

        def write(f):
            writer = csv.DictWriter(f, fieldnames=DEVICE_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(devices)

        self._write_atomic(path, write)
        return path
