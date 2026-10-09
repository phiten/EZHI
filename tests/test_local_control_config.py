"""The configuration side of Local Control that needs no Home Assistant."""
from __future__ import annotations

import pytest

from ezhi_component import const, local_control as lc

SEM = "M00000000000"


def test_the_default_offset_agrees_with_the_controller():
    assert const.DEFAULT_LOCAL_CONTROL_OFFSET == lc.DEFAULT_OFFSET_W


@pytest.mark.parametrize("raw", ["M01234567890", " D01234567890 ", "abc123", "A" * 32])
def test_a_device_id_is_accepted_and_trimmed(raw):
    assert const.normalise_device_id(raw) == raw.strip()


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_no_id_is_the_empty_string(raw):
    assert const.normalise_device_id(raw) == ""


@pytest.mark.parametrize("raw", [
    "M0123 4567890",        # a space inside
    "M012/34567890",        # a topic separator
    "+", "#", "M0123#",     # wildcards
    "short",                # too short to be an id
    "A" * 33,
    "ä" * 8,
])
def test_anything_a_topic_would_read_as_structure_is_refused(raw):
    with pytest.raises(ValueError):
        const.normalise_device_id(raw)


def test_a_pasted_id_with_a_trailing_newline_is_just_trimmed():
    assert const.normalise_device_id("M01234567890\n") == "M01234567890"


def test_the_meter_counts_only_on_the_local_mqtt_transport():
    data = {const.CONF_SEM_DEVICE_ID: SEM}
    assert const.sem_device_id(data) == ""                                  # default: cloud
    assert const.sem_device_id({**data, const.CONF_CONTROL_TRANSPORT: "cloud"}) == ""
    assert const.sem_device_id({**data, const.CONF_CONTROL_TRANSPORT: "bluetooth"}) == ""
    assert const.sem_device_id({**data, const.CONF_CONTROL_TRANSPORT: "local_mqtt"}) == SEM


def test_a_stored_garbage_id_counts_as_none():
    data = {const.CONF_CONTROL_TRANSPORT: "local_mqtt", const.CONF_SEM_DEVICE_ID: "a/b"}
    assert const.sem_device_id(data) == ""
    assert const.sem_device_id(None) == ""
    assert const.sem_device_id({const.CONF_CONTROL_TRANSPORT: "local_mqtt"}) == ""
