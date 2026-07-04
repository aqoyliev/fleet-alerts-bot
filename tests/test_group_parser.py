from utils.group_parser import extract_vehicle_number


# ── Real-world group titles/descriptions (from onboarding screenshots) ──────────────

def test_unit_keyword_in_title():
    title = "UNIT: 708432 ZANFARA SALIFOU (CD)"
    desc = "UNIT: 708432\nDriver name : ZANFARA SALIFOU\nPhone# 646-250-8883\nTRL # 5324016\nSTRAPS # 12"
    assert extract_vehicle_number(title, desc) == "708432"


def test_leading_number_in_title():
    title = "1274 DHIDINBE, MADAR / YUSUF, ABDIFATAH MUHUMED"
    desc = "DHIDINBE, MADAR / YUSUF, ABDIFATAH MUHUMED\n651-508-2939 / 619-315-1086\nTruck# 1274\nTrailer#"
    assert extract_vehicle_number(title, desc) == "1274"


def test_truck_hash_in_title():
    title = "TRUCK# 212575 LATODDRICK BARBER ( LEASE )"
    desc = "LATODDRICK BARBER\n769-270-4618\nTruck 212575\nTrailer# P5265145"
    assert extract_vehicle_number(title, desc) == "212575"


# ── Keyword variants ────────────────────────────────────────────────────────────────

def test_truck_space_no_hash():
    assert extract_vehicle_number("random name", "Truck 212575") == "212575"


def test_truck_hash_no_space():
    assert extract_vehicle_number("", "Truck#1274") == "1274"


def test_unit_from_description_only():
    assert extract_vehicle_number("Some Driver Group", "UNIT: 708432") == "708432"


def test_case_insensitive():
    assert extract_vehicle_number("unit 4455", "") == "4455"


# ── Must NOT pick up trailer / phone / strap numbers ────────────────────────────────

def test_ignores_trailer_and_phone_when_no_unit():
    # No UNIT/TRUCK keyword and title doesn't start with a number → nothing to match.
    desc = "ZANFARA SALIFOU\nPhone# 646-250-8883\nTRL # 5324016\nSTRAPS # 12"
    assert extract_vehicle_number("ZANFARA SALIFOU", desc) is None


def test_trailer_not_preferred_over_unit():
    desc = "UNIT: 708432\nTRL # 5324016"
    assert extract_vehicle_number("", desc) == "708432"


def test_company_name_with_trucking_is_not_matched():
    # "Trucking" contains "TRUCK" but isn't followed by digits.
    assert extract_vehicle_number("HF Trucking Dispatch", "") is None


# ── Empty / missing ─────────────────────────────────────────────────────────────────

def test_none_inputs():
    assert extract_vehicle_number(None, None) is None


def test_empty_strings():
    assert extract_vehicle_number("", "") is None
