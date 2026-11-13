"""Adaptive ICMP presence detection: mocked ping, no real network."""

import subprocess
from unittest.mock import patch

from netpulse.config import Config
from netpulse.db import Database
from netpulse.presence import StateMonitor


def _completed(returncode):
    return subprocess.CompletedProcess(args=["ping"], returncode=returncode)


def make_monitor(tmp_path, **config_overrides):
    db = Database(str(tmp_path / "netpulse.db"))
    config = Config(**config_overrides)
    return StateMonitor(db, config), db


def test_update_devices_registers_device_with_initial_backoff(tmp_path):
    monitor, db = make_monitor(tmp_path, online_check_interval=10)
    monitor.update_devices([{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.10"}])

    assert "aa:bb:cc:dd:ee:ff" in monitor.devices
    assert monitor.backoff["aa:bb:cc:dd:ee:ff"] == 10
    db.close()


@patch("netpulse.presence.subprocess.run")
def test_check_device_online_when_ping_succeeds(mock_run, tmp_path):
    mock_run.return_value = _completed(0)
    monitor, db = make_monitor(tmp_path)
    monitor.update_devices([{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.10"}])

    assert monitor.check_device("aa:bb:cc:dd:ee:ff") is True
    db.close()


@patch("netpulse.presence.subprocess.run")
def test_check_device_offline_when_ping_fails(mock_run, tmp_path):
    mock_run.return_value = _completed(1)
    monitor, db = make_monitor(tmp_path)
    monitor.update_devices([{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.10"}])

    assert monitor.check_device("aa:bb:cc:dd:ee:ff") is False
    db.close()


@patch("netpulse.presence.subprocess.run")
def test_ping_treats_timeout_as_offline(mock_run, tmp_path):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="ping", timeout=1.5)
    monitor, db = make_monitor(tmp_path)
    monitor.update_devices([{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.10"}])

    assert monitor.check_device("aa:bb:cc:dd:ee:ff") is False
    db.close()


@patch("netpulse.presence.subprocess.run")
def test_check_all_updates_state_and_resets_backoff_when_online(mock_run, tmp_path):
    mock_run.return_value = _completed(0)
    monitor, db = make_monitor(tmp_path, online_check_interval=10)
    monitor.update_devices([{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.10"}])

    monitor.last_checks["aa:bb:cc:dd:ee:ff"] = 0  # force it due for a check
    monitor.check_all()

    assert monitor.devices["aa:bb:cc:dd:ee:ff"]["state"] == "online"
    assert monitor.backoff["aa:bb:cc:dd:ee:ff"] == 10
    db.close()


@patch("netpulse.presence.subprocess.run")
def test_check_all_doubles_backoff_while_offline_and_caps_it(mock_run, tmp_path):
    mock_run.return_value = _completed(1)  # always offline
    monitor, db = make_monitor(
        tmp_path, online_check_interval=10, offline_check_interval=120,
        max_offline_backoff=50,
    )
    mac = "aa:bb:cc:dd:ee:ff"
    monitor.update_devices([{"mac": mac, "ip": "192.168.1.10"}])

    seen_backoffs = []
    for _ in range(6):
        monitor.last_checks[mac] = 0  # force due regardless of the real interval
        monitor.check_all()
        seen_backoffs.append(monitor.backoff[mac])

    # Starts doubling from online_check_interval (10): 20, 40, then capped at 50.
    assert seen_backoffs[:3] == [20, 40, 50]
    assert all(b == 50 for b in seen_backoffs[3:])  # never exceeds the cap
    assert monitor.devices[mac]["state"] == "offline"
    db.close()


@patch("netpulse.presence.subprocess.run")
def test_get_device_count_reports_online_and_offline(mock_run, tmp_path):
    monitor, db = make_monitor(tmp_path)
    monitor.update_devices([
        {"mac": "aa:aa:aa:aa:aa:aa", "ip": "192.168.1.1", "state": "online"},
        {"mac": "bb:bb:bb:bb:bb:bb", "ip": "192.168.1.2", "state": "offline"},
        {"mac": "cc:cc:cc:cc:cc:cc", "ip": "192.168.1.3", "state": "offline"},
    ])

    online, offline = monitor.get_device_count()
    assert online == 1
    assert offline == 2
    db.close()
