"""JSON and CSV export of current network state."""

import csv
import json
import time
from datetime import datetime
from pathlib import Path

from .db import Database


class Exporter:
    """JSON and CSV export of current network state."""

    def __init__(self, db: Database, export_dir: str):
        self.db = db
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)

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
        with open(path, "w") as f:
            json.dump(snapshot, f, indent=2, default=str)
        return path

    def export_csv(self, filename: str = "netpulse_devices.csv"):
        devices = self.db.get_devices()
        path = self.export_dir / filename
        if not devices:
            return path
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=devices[0].keys())
            writer.writeheader()
            writer.writerows(devices)
        return path
