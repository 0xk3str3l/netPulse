"""Network utilities and silent ARP-based device discovery."""

import logging
import socket
from ipaddress import ip_address, IPv4Network
from typing import Dict, List, Optional, Tuple

import netifaces
from scapy.all import ARP, Ether, srp, conf

from .oui import lookup_vendor

log = logging.getLogger("netpulse")


def get_default_interface() -> str:
    """Get the first non-loopback interface with a default route."""
    gateways = netifaces.gateways()
    if "default" in gateways and netifaces.AF_INET in gateways["default"]:
        return gateways["default"][netifaces.AF_INET][1]
    for iface in netifaces.interfaces():
        if iface != "lo":
            return iface
    return "eth0"


def get_interface_ip_and_netmask(iface: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        addrs = netifaces.ifaddresses(iface)
        if netifaces.AF_INET in addrs:
            info = addrs[netifaces.AF_INET][0]
            return info.get("addr"), info.get("netmask")
    except Exception:
        pass
    return None, None


def get_interface_cidr(iface: str) -> Optional[str]:
    ip, mask = get_interface_ip_and_netmask(iface)
    if ip and mask:
        try:
            # Convert dotted-decimal netmask to prefix length
            mask_bits = sum(bin(int(x)).count("1") for x in mask.split("."))
            network = ip_address(ip).network_address
            return str(IPv4Network(f"{network}/{mask_bits}", strict=False))
        except Exception:
            return None
    return None


def get_own_mac(iface: str) -> str:
    try:
        addrs = netifaces.ifaddresses(iface)
        if netifaces.AF_LINK in addrs:
            return addrs[netifaces.AF_LINK][0].get("addr", "").lower()
    except Exception:
        pass
    return ""


def resolve_hostname(ip: str) -> str:
    try:
        host, _, _ = socket.gethostbyaddr(ip)
        return host
    except (socket.herror, socket.gaierror):
        return ""


class ARPDiscoverer:
    """Silent ARP sweep to discover live devices on the subnet."""

    def __init__(self, interface: str, timeout: float = 3.0, enabled: bool = True):
        self.interface = interface
        self.timeout = timeout
        self.enabled = enabled
        self.own_mac = get_own_mac(interface) if enabled else ""
        self.own_ip = get_interface_ip_and_netmask(interface)[0] if enabled else None

    def sweep(self, subnet: str) -> List[Dict]:
        """Perform an ARP scan on the given subnet. Returns list of {ip, mac}."""
        if not self.enabled:
            return []
        try:
            conf.iface = self.interface
            conf.verb = 0  # completely silent

            ans, _ = srp(
                Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=subnet),
                timeout=self.timeout,
                iface=self.interface,
                inter=0.01,       # 10ms between packets
                verbose=False,
                retry=1
            )

            devices = []
            seen = set()
            for sent, recv in ans:
                mac = recv[Ether].src.lower()
                ip = recv[ARP].psrc

                # Skip self
                if mac == self.own_mac or ip == self.own_ip:
                    continue
                if mac in seen:
                    continue

                seen.add(mac)
                devices.append({
                    "ip": ip,
                    "mac": mac,
                    "vendor": lookup_vendor(mac)
                })

            return devices

        except Exception as e:
            log.warning(f"ARP sweep failed: {e}")
            return []
