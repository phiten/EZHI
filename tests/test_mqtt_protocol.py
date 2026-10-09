"""The MQTT wire format, checked against what the device actually answered.

Every expectation here comes from a capture of the real inverter on a local
broker (2026-08-10). The serial is scrubbed; nothing else is.
"""
from __future__ import annotations

import json

import pytest
from ezhi_component import mqtt_protocol as p

DEVICE_ID = "D00000000000"


def test_topics_match_the_ones_the_device_subscribes_to():
    """Wrong topic, no answer -- and the device tells nobody."""
    assert p.topic_get(DEVICE_ID) == f"/properties/EZHI/{DEVICE_ID}/get"
    assert p.topic_set(DEVICE_ID) == f"/properties/EZHI/{DEVICE_ID}/set"
    assert p.topic_get_reply(DEVICE_ID) == f"/properties/EZHI/{DEVICE_ID}/get_reply"
    assert p.topic_set_reply(DEVICE_ID) == f"/properties/EZHI/{DEVICE_ID}/set_reply"
    assert p.topic_event(DEVICE_ID) == f"/event/EZHI/{DEVICE_ID}/post"


def test_reply_topics_are_the_two_a_controller_must_listen_on():
    assert p.reply_topics(DEVICE_ID) == (
        p.topic_get_reply(DEVICE_ID), p.topic_set_reply(DEVICE_ID))


def test_get_carries_company_key():
    """The field that decides whether the device answers at all.

    A get without companyKey reaches the device and is never answered -- the
    broker logs the delivery, the device stays silent. Measured 2026-08-10.
    """
    body = json.loads(p.build_get(DEVICE_ID, "systemMode", "1"))
    assert body["companyKey"] == "AmS4SV9oy3gk"


def test_get_has_no_params_field():
    """The vendor app sends none on a read, so neither do we."""
    body = json.loads(p.build_get(DEVICE_ID, "systemMode", "1"))
    assert "params" not in body


def test_get_envelope_is_complete():
    body = json.loads(p.build_get(DEVICE_ID, "deviceInfo", "42"))
    assert body["identifier"] == "deviceInfo"
    assert body["method"] == "get"
    assert body["id"] == "42"
    assert body["deviceId"] == DEVICE_ID
    assert body["type"] == "property"
    assert body["productKey"] == "EZHI"
    assert body["company"] == "apsystems"
    assert body["version"] == "1.0"


def test_set_carries_params_and_company_key():
    body = json.loads(
        p.build_set(DEVICE_ID, "systemMode", {"socMin": "15"}, "7"))
    assert body["method"] == "set"
    assert body["params"] == {"socMin": "15"}
    assert body["companyKey"] == "AmS4SV9oy3gk"
    assert body["id"] == "7"


def test_payloads_are_compact_json():
    """Whitespace is not wrong, but the app sends none and neither do we."""
    assert " " not in p.build_get(DEVICE_ID, "systemMode", "1")


def test_correlation_ids_are_numeric():
    """A non-numeric id has never been on the wire; ids stay digits-only."""
    for _ in range(20):
        assert p.new_corr_id().isdigit()


def test_correlation_ids_do_not_repeat():
    assert len({p.new_corr_id() for _ in range(500)}) == 500


def test_correlation_ids_are_process_unique():
    """The prefix is what keeps a reply that outlived a restart from
    resolving a fresh request with the same counter value."""
    assert p.new_corr_id().startswith(p._ID_PREFIX)


def test_parse_reply_returns_id_code_and_data():
    corr_id, code, data = p.parse_reply(json.dumps({
        "data": {"systemMode": "4", "dischargeProtection": "13"},
        "code": 200, "message": "SUCCESS", "id": "9990011",
        "deviceId": DEVICE_ID, "identifier": "systemMode",
    }))
    assert corr_id == "9990011"
    assert code == 200
    assert data["dischargeProtection"] == "13"


def test_parse_reply_accepts_bytes():
    """What a broker client hands over is not always str."""
    corr_id, code, _ = p.parse_reply(b'{"id":"5","code":200,"data":{}}')
    assert (corr_id, code) == ("5", 200)


def test_parse_reply_reports_a_failure_code_rather_than_raising():
    """The caller knows which command it was and writes the better message."""
    _, code, _ = p.parse_reply('{"id":"5","code":400,"data":{}}')
    assert code == 400


def test_parse_reply_survives_a_reply_without_data():
    _, _, data = p.parse_reply('{"id":"5","code":200}')
    assert data == {}


def test_parse_reply_rejects_junk():
    """Anyone can publish to a topic. Junk must be droppable, not fatal."""
    for junk in ("", "not json", "[1,2,3]", '"a string"', b"\x00\x01"):
        with pytest.raises(ValueError):
            p.parse_reply(junk)


def test_parse_reply_rejects_a_reply_that_cannot_be_correlated():
    with pytest.raises(ValueError):
        p.parse_reply('{"code":200,"data":{}}')


def test_parse_reply_reports_an_unreadable_code_as_none():
    """None, never a guessed 200 -- this decides whether a write counted."""
    _, code, _ = p.parse_reply('{"id":"5","code":"weird","data":{}}')
    assert code is None


# --- the smart meter and the time question (Local Control) -----------------------

SEM_ID = "M00000000000"


def test_the_default_product_key_is_still_the_inverter():
    """Every call that predates the meter must produce exactly what it did."""
    assert p.topic_get(DEVICE_ID) == p.topic_get(DEVICE_ID, "EZHI")
    assert json.loads(p.build_get(DEVICE_ID, "systemMode", "1"))["productKey"] == "EZHI"
    assert json.loads(p.build_set(DEVICE_ID, "onOff", {"status": "0"}, "1"))["productKey"] == "EZHI"


def test_meter_topics_use_the_meters_product_key():
    assert p.topic_get(SEM_ID, p.SEM_PRODUCT_KEY) == f"/properties/SEM/{SEM_ID}/get"
    assert p.topic_set(SEM_ID, p.SEM_PRODUCT_KEY) == f"/properties/SEM/{SEM_ID}/set"
    assert p.reply_topics(SEM_ID, p.SEM_PRODUCT_KEY) == (
        f"/properties/SEM/{SEM_ID}/get_reply", f"/properties/SEM/{SEM_ID}/set_reply")
    assert p.topic_event(SEM_ID, p.SEM_PRODUCT_KEY) == f"/event/SEM/{SEM_ID}/post"


def test_meter_envelope_carries_its_own_product_key_and_the_company_key():
    body = json.loads(p.build_get(SEM_ID, "localLink", "7", p.SEM_PRODUCT_KEY))
    assert body["productKey"] == "SEM"
    assert body["deviceId"] == SEM_ID
    assert body["companyKey"] == "AmS4SV9oy3gk"
    assert "params" not in body
    setter = json.loads(p.build_set(
        SEM_ID, "localLink", {"status": "0", "config": {}}, "8", p.SEM_PRODUCT_KEY))
    assert setter["productKey"] == "SEM"
    assert setter["params"] == {"status": "0", "config": {}}


def test_a_nested_config_survives_the_envelope_as_an_object():
    """The group's config is an object on the wire. Stringified, the device
    would store Python's repr of a dict and the group would silently not form."""
    cfg = {"meter": SEM_ID, "device": {DEVICE_ID: "1.00"}}
    body = json.loads(p.build_set(DEVICE_ID, "systemMode", {"config": cfg}, "9"))
    assert body["params"]["config"] == cfg


def test_ntp_topics():
    assert p.topic_ntp_get(DEVICE_ID) == f"/ntp/EZHI/{DEVICE_ID}/get"
    assert p.topic_ntp_reply(SEM_ID, "SEM") == f"/ntp/SEM/{SEM_ID}/get_reply"


def test_parse_ntp_request_reads_id_and_timezone():
    raw = json.dumps({"id": "123", "params": {"timezone": "Europe/Berlin"}})
    assert p.parse_ntp_request(raw) == ("123", "Europe/Berlin")


def test_the_meter_asks_for_no_timezone():
    """The SEM sends an empty one; that must not be an error."""
    assert p.parse_ntp_request(json.dumps({"id": 5, "params": {"timezone": ""}})) == ("5", "")
    assert p.parse_ntp_request(json.dumps({"id": 5})) == ("5", "")


def test_parse_ntp_request_rejects_junk():
    for junk in ("not json", "[]", json.dumps({"params": {}})):
        with pytest.raises(ValueError):
            p.parse_ntp_request(junk)


def test_resolve_timezone_prefers_what_the_device_asked_for():
    assert p.resolve_timezone("Europe/Berlin", "America/New_York") == "Europe/Berlin"


def test_resolve_timezone_falls_back_for_empty_and_unknown_names():
    assert p.resolve_timezone("", "Europe/Berlin") == "Europe/Berlin"
    assert p.resolve_timezone("Mars/Olympus", "Europe/Berlin") == "Europe/Berlin"
    assert p.resolve_timezone("", "") == "UTC"
    assert p.resolve_timezone("nope", "also/nope") == "UTC"


def test_ntp_reply_has_the_shape_the_cloud_answered_with():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 8, 8, 30, 15, tzinfo=timezone.utc)
    body = json.loads(p.build_ntp_reply(DEVICE_ID, "123", "Europe/Berlin", "EZHI", now))
    assert body["type"] == "ntp"
    assert body["method"] == "get_reply"
    assert body["id"] == "123"
    assert body["deviceId"] == DEVICE_ID
    assert body["productKey"] == "EZHI"
    assert body["code"] == 200
    assert body["message"] == "success"
    assert body["companyKey"] == "AmS4SV9oy3gk"
    assert body["data"] == {
        "date": "20261008083015",       # UTC, like the cloud's
        "timezone": "Europe/Berlin",
        "timeOffset": "7200000",        # CEST, in milliseconds
    }


def test_ntp_reply_offset_follows_the_season():
    from datetime import datetime, timezone

    winter = datetime(2026, 1, 8, 8, 0, 0, tzinfo=timezone.utc)
    body = json.loads(p.build_ntp_reply(DEVICE_ID, "1", "Europe/Berlin", "EZHI", winter))
    assert body["data"]["timeOffset"] == "3600000"


def test_ntp_reply_with_an_unknown_zone_still_answers_in_utc():
    body = json.loads(p.build_ntp_reply(DEVICE_ID, "1", "Mars/Olympus", "EZHI"))
    assert body["data"]["timezone"] == "UTC"
    assert body["data"]["timeOffset"] == "0"


def test_parse_event_returns_identifier_and_data():
    raw = json.dumps({"identifier": "outputDataSecond", "type": "event",
                      "data": {"p": "30.0000"}})
    assert p.parse_event(raw) == ("outputDataSecond", {"p": "30.0000"})
    assert p.parse_event(json.dumps({"identifier": "si"})) == ("si", {})


def test_parse_event_rejects_junk():
    for junk in ("nope", "[]", json.dumps({"data": {}}), json.dumps({"identifier": ""})):
        with pytest.raises(ValueError):
            p.parse_event(junk)
