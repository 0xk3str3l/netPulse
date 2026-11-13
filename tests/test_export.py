"""JSON/CSV export shape."""

import csv
import json

from netpulse.db import Database
from netpulse.export import Exporter


def make_exporter(tmp_path, with_device=True):
    db = Database(str(tmp_path / "netpulse.db"))
    if with_device:
        db.upsert_device(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.10",
                          hostname="laptop", vendor="Apple", state="online")
    exporter = Exporter(db, str(tmp_path / "exports"))
    return exporter, db


def test_export_json_shape(tmp_path):
    exporter, db = make_exporter(tmp_path)
    path = exporter.export_json()

    assert path.exists()
    data = json.loads(path.read_text())
    assert data["device_count"] == 1
    assert data["online_count"] == 1
    assert data["offline_count"] == 0
    assert data["devices"][0]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert "timestamp" in data and "datetime" in data
    db.close()


def test_export_json_default_filename(tmp_path):
    exporter, db = make_exporter(tmp_path)
    path = exporter.export_json()
    assert path.name == "netpulse_snapshot.json"
    db.close()


def test_export_json_custom_filename(tmp_path):
    exporter, db = make_exporter(tmp_path)
    path = exporter.export_json(filename="custom.json")
    assert path.name == "custom.json"
    assert path.exists()
    db.close()


def test_export_csv_shape(tmp_path):
    exporter, db = make_exporter(tmp_path)
    path = exporter.export_csv()

    assert path.exists()
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert rows[0]["hostname"] == "laptop"
    assert set(rows[0].keys()) >= {"mac", "ip", "hostname", "vendor", "state"}
    db.close()


def test_export_csv_default_filename(tmp_path):
    exporter, db = make_exporter(tmp_path)
    path = exporter.export_csv()
    assert path.name == "netpulse_devices.csv"
    db.close()


def test_export_csv_with_no_devices_does_not_write_a_file(tmp_path):
    # Documents existing behavior: export_csv() returns the would-be path
    # without creating it when there are no devices, since csv.DictWriter
    # needs the first row's keys as fieldnames and there isn't one.
    exporter, db = make_exporter(tmp_path, with_device=False)
    path = exporter.export_csv()
    assert not path.exists()
    db.close()


def test_export_dir_is_created(tmp_path):
    export_dir = tmp_path / "exports" / "nested"
    db = Database(str(tmp_path / "netpulse.db"))
    Exporter(db, str(export_dir))
    assert export_dir.is_dir()
    db.close()
