"""Per-device iptables accounting: rule management, parsing, delta rates.

No real iptables or psutil needed -- subprocess and the clock are mocked.
"""

from types import SimpleNamespace

from netpulse.bandwidth import BandwidthMonitor, _ip_token
from netpulse.config import Config
from netpulse.db import Database

IPTABLES_OUTPUT = """\
Chain NETPULSE_INPUT (1 references)
    pkts      bytes target     prot opt in     out     source               destination
      10      1000        all  --  *      *       192.168.2.5          0.0.0.0/0
      20      2000        all  --  *      *       0.0.0.0/0            192.168.2.5
      30      3000        all  --  *      *       192.168.2.6/32       0.0.0.0/0
"""


def make_monitor(tmp_path):
    db = Database(str(tmp_path / "bandwidth.db"))
    monitor = BandwidthMonitor(db, Config(), "eth0", use_iptables=False)
    monitor.iptables_initialized = True
    return monitor, db


def test_ip_token_strips_mask_and_ignores_anywhere():
    assert _ip_token("192.168.2.5/32") == "192.168.2.5"
    assert _ip_token("0.0.0.0/0") is None
    assert _ip_token("garbage") is None


def test_get_iptables_counts_splits_tx_and_rx(tmp_path, monkeypatch):
    monitor, db = make_monitor(tmp_path)
    monkeypatch.setattr(
        monitor, "_iptables",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=IPTABLES_OUTPUT, stderr=""),
    )

    counts = monitor._get_iptables_counts()
    assert counts["192.168.2.5"] == {"rx": 2000, "tx": 1000}
    assert counts["192.168.2.6"] == {"rx": 0, "tx": 3000}
    db.close()


def test_ensure_device_rules_adds_source_and_destination_counters(tmp_path, monkeypatch):
    monitor, db = make_monitor(tmp_path)
    calls = []

    def fake_iptables(*args, check=False):
        calls.append(args)
        # -C reports "missing" so -A runs; everything else succeeds.
        return SimpleNamespace(returncode=1 if args[0] == "-C" else 0,
                               stdout="", stderr="")

    monkeypatch.setattr(monitor, "_iptables", fake_iptables)
    monitor._ensure_device_rules("192.168.2.5")

    chain = monitor.config.iptables_chain
    assert ("-C", chain, "-s", "192.168.2.5") in calls
    assert ("-A", chain, "-s", "192.168.2.5") in calls
    assert ("-C", chain, "-d", "192.168.2.5") in calls
    assert ("-A", chain, "-d", "192.168.2.5") in calls
    assert "192.168.2.5" in monitor._tracked_ips

    calls.clear()
    monitor._ensure_device_rules("192.168.2.5")  # already tracked: no new rules
    assert calls == []
    db.close()


def test_sample_per_device_records_delta_rates(tmp_path, monkeypatch):
    monitor, db = make_monitor(tmp_path)
    mac = "aa:bb:cc:dd:ee:ff"
    devices = {mac: {"ip": "192.168.2.5"}}
    db.upsert_device(mac=mac, ip="192.168.2.5", state="online")

    monkeypatch.setattr(monitor, "_ensure_device_rules", lambda ip: None)
    monitor._tracked_ips = {"192.168.2.5"}

    samples = [
        {"192.168.2.5": {"rx": 1000, "tx": 500}},
        {"192.168.2.5": {"rx": 3000, "tx": 1500}},
        {"192.168.2.5": {"rx": 2500, "tx": 1000}},  # counter reset -> clamped to 0
    ]
    monkeypatch.setattr(monitor, "_get_iptables_counts", lambda: samples.pop(0))

    class FakeTime:
        def __init__(self):
            self.now = 1000.0

        def time(self):
            self.now += 10.0
            return self.now

    monkeypatch.setattr("netpulse.bandwidth.time", FakeTime())

    monitor.sample_per_device(devices)   # first sample: no previous counter
    monitor.sample_per_device(devices)   # 2000 rx / 1000 tx over 10s
    monitor.sample_per_device(devices)   # reset: rates clamp to 0

    rows = db.get_device_bandwidth(mac, since=0)
    assert [row["rx_rate"] for row in rows] == [0.0, 200.0, 0.0]
    assert [row["tx_rate"] for row in rows] == [0.0, 100.0, 0.0]
    db.close()
