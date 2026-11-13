"""MAC vendor (OUI) lookup."""

from netpulse.oui import OUI_VENDORS, lookup_vendor


def test_known_prefix_resolves_to_vendor():
    assert lookup_vendor("00:0C:29:11:22:33") == "VMware"
    assert lookup_vendor("00:00:0C:AA:BB:CC") == "Cisco"


def test_unknown_prefix_returns_unknown():
    assert lookup_vendor("DE:AD:BE:EF:00:01") == "Unknown"


def test_lookup_is_case_insensitive():
    assert lookup_vendor("00:0c:29:11:22:33") == lookup_vendor("00:0C:29:11:22:33")


def test_lookup_uses_only_the_first_three_octets():
    # Same OUI, different host portion -> same vendor.
    assert lookup_vendor("00:0C:29:00:00:00") == lookup_vendor("00:0C:29:FF:FF:FF")


def test_oui_table_has_no_empty_vendor_names():
    assert all(vendor for vendor in OUI_VENDORS.values())


def test_oui_table_keys_are_well_formed():
    assert all(len(k) == 8 and k.count(":") == 2 for k in OUI_VENDORS)
