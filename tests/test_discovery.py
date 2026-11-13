"""Discovery: OS ARP-cache parsing, passive/active modes, exclusions, ping args.

These tests never touch the network and do not require scapy/netifaces/psutil.
"""

from types import SimpleNamespace

import netpulse.discovery as disc

WINDOWS_ARP = """\
Interface: 192.168.2.102 --- 0xe
  Internet Address      Physical Address      Type
  192.168.2.1           e0-cb-bc-06-17-bd     dynamic
  192.168.2.50          aa-bb-cc-dd-ee-ff     dynamic
  192.168.2.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
  192.168.2.9           01-00-5e-00-00-09     static
  10.1.1.1              11-22-33-44-55-66     dynamic
"""

LINUX_NEIGH = """\
192.168.2.1 dev eth0 lladdr e0:cb:bc:06:17:bd REACHABLE
192.168.2.50 dev eth0 lladdr aa:bb:cc:dd:ee:ff STALE
10.1.1.1 dev eth0 lladdr 11:22:33:44:55:66 REACHABLE
"""


def make_discoverer(monkeypatch, **kwargs):
    monkeypatch.setattr(disc, "get_own_mac", lambda iface: "")
    monkeypatch.setattr(disc, "get_interface_ip_and_netmask", lambda iface: (None, None))
    kwargs.setdefault("enabled", False)
    return disc.ARPDiscoverer("eth0", **kwargs)


def test_parse_windows_arp_table_filters_subnet_broadcast_and_multicast():
    devices = disc.parse_arp_table(WINDOWS_ARP, "192.168.2.0/24")
    assert {d["ip"] for d in devices} == {"192.168.2.1", "192.168.2.50"}
    assert {d["mac"] for d in devices} == {
        "e0:cb:bc:06:17:bd", "aa:bb:cc:dd:ee:ff",
    }


def test_parse_linux_neigh_output():
    devices = disc.parse_arp_table(LINUX_NEIGH, "192.168.2.0/24")
    assert {d["ip"] for d in devices} == {"192.168.2.1", "192.168.2.50"}


def test_ping_command_is_platform_correct(monkeypatch):
    monkeypatch.setattr(disc, "IS_WINDOWS", True)
    assert disc.ping_command("1.2.3.4", count=2, timeout=0.5) == [
        "ping", "-n", "2", "-w", "500", "1.2.3.4",
    ]

    monkeypatch.setattr(disc, "IS_WINDOWS", False)
    assert disc.ping_command("1.2.3.4", count=2, timeout=0.5) == [
        "ping", "-c", "2", "-W", "1", "-q", "-n", "1.2.3.4",
    ]


def test_passive_mode_reads_the_arp_table_and_never_probes(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=WINDOWS_ARP, stderr="")

    monkeypatch.setattr(disc, "IS_WINDOWS", True)
    monkeypatch.setattr(disc.subprocess, "run", fake_run)
    discoverer = make_discoverer(monkeypatch, mode="passive")

    devices = discoverer.sweep("192.168.2.0/24")

    assert {d["ip"] for d in devices} == {"192.168.2.1", "192.168.2.50"}
    assert calls and all(cmd[0] == "arp" for cmd in calls)
    assert not any("ping" in cmd[0] for cmd in calls)


def test_exclusions_are_applied_to_cache_results(monkeypatch):
    monkeypatch.setattr(disc, "IS_WINDOWS", True)
    monkeypatch.setattr(
        disc.subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=WINDOWS_ARP, stderr=""),
    )
    discoverer = make_discoverer(
        monkeypatch, mode="passive",
        exclude_macs=["AA-BB-CC-DD-EE-FF"],
        exclude_ips=["192.168.2.1"],
    )
    assert discoverer.sweep("192.168.2.0/24") == []


def test_auto_mode_sweeps_when_cache_is_cold(monkeypatch):
    discoverer = make_discoverer(
        monkeypatch, mode="auto", sweep_rate=254, active_sweep_interval=900,
    )
    tables = iter([
        [],
        [{"ip": "192.168.2.7", "mac": "aa:bb:cc:dd:ee:07"}],
    ])
    monkeypatch.setattr(discoverer, "_arp_table", lambda network: next(tables))
    probed = []
    monkeypatch.setattr(discoverer, "_probe", lambda ip: probed.append(ip))
    monkeypatch.setattr(disc.time, "sleep", lambda seconds: None)

    devices = discoverer.sweep("192.168.2.0/24")

    assert len(probed) == 254  # every usable host, no more
    assert [d["ip"] for d in devices] == ["192.168.2.7"]
    assert discoverer._last_active_sweep > 0


def test_auto_mode_skips_the_sweep_when_the_cache_is_warm(monkeypatch):
    discoverer = make_discoverer(
        monkeypatch, mode="auto", active_sweep_interval=900,
    )
    warm = [{"ip": "192.168.2.7", "mac": "aa:bb:cc:dd:ee:07"}]
    monkeypatch.setattr(discoverer, "_arp_table", lambda network: warm)
    probed = []
    monkeypatch.setattr(discoverer, "_probe", lambda ip: probed.append(ip))

    discoverer.sweep("192.168.2.0/24")
    assert probed == []
