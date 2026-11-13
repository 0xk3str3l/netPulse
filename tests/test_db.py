"""SQLite persistence: upsert, retention/purge, bandwidth history."""

import time

import pytest

from netpulse.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "netpulse.db"), retention_days=90)
    yield database
    database.close()


def test_upsert_device_inserts_new_device(db):
    db.upsert_device(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.10",
                      hostname="laptop", vendor="Apple", state="online")
    devices = db.get_devices()
    assert len(devices) == 1
    assert devices[0]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert devices[0]["ip"] == "192.168.1.10"
    assert devices[0]["hostname"] == "laptop"
    assert devices[0]["vendor"] == "Apple"
    assert devices[0]["state"] == "online"
    assert devices[0]["first_seen"] == devices[0]["last_seen"]


def test_upsert_device_updates_existing_and_keeps_first_seen(db):
    db.upsert_device(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.10", state="online")
    first_seen = db.get_devices()[0]["first_seen"]

    time.sleep(0.01)
    db.upsert_device(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.11", state="online")

    devices = db.get_devices()
    assert len(devices) == 1
    assert devices[0]["ip"] == "192.168.1.11"  # IP updates
    assert devices[0]["first_seen"] == first_seen  # first_seen is sticky
    assert devices[0]["last_seen"] > first_seen


def test_upsert_device_hostname_and_vendor_are_sticky(db):
    db.upsert_device(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.10",
                      hostname="laptop", vendor="Apple", state="online")
    # A later upsert with blank hostname/vendor shouldn't erase the known ones.
    db.upsert_device(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.10", state="online")

    devices = db.get_devices()
    assert devices[0]["hostname"] == "laptop"
    assert devices[0]["vendor"] == "Apple"


def test_upsert_device_records_state_change_history(db):
    db.upsert_device(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.10", state="online")
    db.upsert_device(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.10", state="offline")

    with db.cursor() as cur:
        cur.execute("SELECT old_state, new_state FROM state_history")
        rows = [dict(zip(("old_state", "new_state"), r)) for r in cur.fetchall()]

    assert {"old_state": "online", "new_state": "offline"} in rows


def test_record_and_get_device_bandwidth(db):
    db.upsert_device(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.10", state="online")
    db.record_bandwidth(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.10",
                         rx_bytes=100, tx_bytes=50, rx_rate=10, tx_rate=5)

    samples = db.get_device_bandwidth("aa:bb:cc:dd:ee:ff", since=0)
    assert len(samples) == 1
    assert samples[0]["rx_bytes"] == 100
    assert samples[0]["tx_bytes"] == 50


def test_record_and_get_interface_bandwidth(db):
    db.record_interface_bw("eth0", rx_bytes=1000, tx_bytes=500, rx_rate=100, tx_rate=50)
    samples = db.get_interface_bandwidth("eth0", since=0)
    assert len(samples) == 1
    assert samples[0]["interface"] == "eth0"
    assert samples[0]["rx_rate"] == 100


def test_purge_old_removes_stale_offline_device_but_keeps_online(db):
    now = time.time()
    stale = now - (200 * 86400)  # older than the 90-day retention window

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO devices (mac, ip, first_seen, last_seen, state, state_changed) "
            "VALUES (?, ?, ?, ?, 'offline', ?)",
            ("aa:aa:aa:aa:aa:aa", "192.168.1.1", stale, stale, stale),
        )
        cur.execute(
            "INSERT INTO devices (mac, ip, first_seen, last_seen, state, state_changed) "
            "VALUES (?, ?, ?, ?, 'online', ?)",
            ("bb:bb:bb:bb:bb:bb", "192.168.1.2", stale, stale, stale),
        )
        cur.execute(
            "INSERT INTO bandwidth_samples (timestamp, mac, ip, rx_bytes, tx_bytes) "
            "VALUES (?, ?, ?, 0, 0)",
            (stale, "aa:aa:aa:aa:aa:aa", "192.168.1.1"),
        )

    db.purge_old()

    macs = {d["mac"] for d in db.get_devices()}
    assert "aa:aa:aa:aa:aa:aa" not in macs  # stale + offline -> purged
    assert "bb:bb:bb:bb:bb:bb" in macs  # stale but online -> kept (still "known")
    assert db.get_device_bandwidth("aa:aa:aa:aa:aa:aa", since=0) == []


def test_purge_old_keeps_recent_offline_device(db):
    db.upsert_device(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.10", state="offline")
    db.purge_old()
    assert len(db.get_devices()) == 1
