"""Topics and envelope for the EZHI's own MQTT channel.

The inverter speaks MQTT to the vendor cloud, and it carries the same JSON
envelope the Bluetooth channel does -- identifier/method/params out, a `code`
and a `data` block back, correlated by `id`. Pointing the device at a local
broker therefore turns the vendor's own control channel into a local one, with
no reverse engineering left to do: this module is that wire format.

Verified against a capture of the real device on a local broker (2026-08-10,
`docs/ezhi-mqtt-topics-2026-08-10.md` in the config repo -- kept out of this
repository, it contains a serial and a password). What the capture settled:

* the device subscribes to seven topics, of which `/properties/<p>/<s>/get`
  and `.../set` are the ones a controller publishes on;
* it answers on `.../get_reply` and `.../set_reply`, `code` 200 for success;
* telemetry is a *push* on `/event/<p>/<s>/post`, not something to poll --
  `systemMode` on the other hand is never pushed and has to be asked for;
* **companyKey is required**. A `get` without it reaches the device (the
  broker logs the delivery) and is simply never answered. That silence cost
  an evening; it is the one field that looks like decoration and is not.

Pure: no I/O, no Home Assistant, no broker client. Everything here can be
checked without a device.
"""
from __future__ import annotations

import json
import uuid
from itertools import count
from typing import Any

# One truth for the vendor's four envelope constants, which both transports
# need and neither owns. ble_protocol.py had them first.
from .ble_protocol import COMPANY, COMPANY_KEY, PRODUCT_KEY, VERSION

# The smart meter (SEM3-WL-2) speaks the same envelope on the same broker, under
# its own product key. Verified against a real unit on a local broker
# (2026-10-07): `get`/`set` of `localLink` are answered, and it pushes
# `outputDataSecond` on /event/SEM/<id>/post.
SEM_PRODUCT_KEY = "SEM"

# The device answers a command it accepted with this. As on the other
# transports, it means "parsed and accepted", never "and it took effect".
SUCCESS_CODE = 200

# Every id in the capture is numeric -- the vendor app sends 9-10 digit
# numbers, and the ids we put on the wire ourselves were numeric too and were
# answered. A non-numeric id has never been tried against real firmware, so
# the ids built below stay digits-only rather than finding out during a write.
_ID_PREFIX = str(uuid.uuid4().int % 1_000_000)
_ID_COUNTER = count()


def topic_get(device_id: str, product_key: str = PRODUCT_KEY) -> str:
    """Where a controller asks for a property."""
    return f"/properties/{product_key}/{device_id}/get"


def topic_set(device_id: str, product_key: str = PRODUCT_KEY) -> str:
    """Where a controller writes a property."""
    return f"/properties/{product_key}/{device_id}/set"


def topic_get_reply(device_id: str, product_key: str = PRODUCT_KEY) -> str:
    """Where the device answers a get."""
    return f"/properties/{product_key}/{device_id}/get_reply"


def topic_set_reply(device_id: str, product_key: str = PRODUCT_KEY) -> str:
    """Where the device answers a set."""
    return f"/properties/{product_key}/{device_id}/set_reply"


def topic_event(device_id: str, product_key: str = PRODUCT_KEY) -> str:
    """Where the device pushes telemetry (outputData, si, light, alarm, ...)."""
    return f"/event/{product_key}/{device_id}/post"


def topic_ntp_get(device_id: str, product_key: str = PRODUCT_KEY) -> str:
    """Where the device asks for the time -- on every (re)connect."""
    return f"/ntp/{product_key}/{device_id}/get"


def topic_ntp_reply(device_id: str, product_key: str = PRODUCT_KEY) -> str:
    """Where the device waits for the answer to that question."""
    return f"/ntp/{product_key}/{device_id}/get_reply"


def reply_topics(device_id: str, product_key: str = PRODUCT_KEY) -> tuple[str, str]:
    """The two topics a controller has to be listening on before it asks."""
    return (topic_get_reply(device_id, product_key),
            topic_set_reply(device_id, product_key))


def new_corr_id() -> str:
    """A correlation id: numeric, unique in this process and across restarts.

    The random per-process prefix is not decoration either. A reply that
    outlives its request -- a retained message, a device answering after a
    restart -- must not resolve a fresh request that happens to have reached
    the same counter value again. The counter alone, restarting at 0 with the
    process, would allow exactly that.
    """
    return f"{_ID_PREFIX}{next(_ID_COUNTER):04d}"


def build_get(
    device_id: str, identifier: str, corr_id: str, product_key: str = PRODUCT_KEY
) -> str:
    """JSON to publish on topic_get() -- deliberately without `params`.

    The vendor app sends no params on a read and that is what was verified;
    an empty `params: {}` was on the wire once, in the same message that was
    missing companyKey, so it has never been cleanly tested on its own.
    """
    return _envelope(device_id, identifier, "get", corr_id, None, product_key)


def build_set(
    device_id: str,
    identifier: str,
    params: dict,
    corr_id: str,
    product_key: str = PRODUCT_KEY,
) -> str:
    """JSON to publish on topic_set()."""
    return _envelope(device_id, identifier, "set", corr_id, params, product_key)


def _envelope(
    device_id: str,
    identifier: str,
    method: str,
    corr_id: str,
    params: dict | None = None,
    product_key: str = PRODUCT_KEY,
) -> str:
    body = {
        "identifier": identifier,
        "method": method,
        "company": COMPANY,
        "companyKey": COMPANY_KEY,
        "id": corr_id,
        "type": "property",
        "productKey": product_key,
        "version": VERSION,
        "deviceId": device_id,
    }
    if params is not None:
        body["params"] = params
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def parse_reply(payload: str | bytes) -> tuple[str, int | None, dict]:
    """(correlation id, code, data) from a *_reply payload.

    Raises ValueError on anything that is not a JSON object with an `id`.
    The subscriber logs and drops those rather than failing: a broker will
    hand over whatever a third party published to the topic, and one stray
    message must not take the transport down.

    `code` is returned as-is rather than checked here -- the caller knows
    which command it belongs to and can say so in the error.
    """
    try:
        body = json.loads(payload)
    except (TypeError, ValueError) as err:
        raise ValueError(f"reply is not JSON: {err}") from err
    if not isinstance(body, dict):
        raise ValueError(f"reply is not a JSON object: {type(body).__name__}")
    corr_id = body.get("id")
    if corr_id is None:
        raise ValueError("reply has no id, cannot be correlated")
    data = body.get("data")
    return str(corr_id), _as_code(body.get("code")), data if isinstance(data, dict) else {}


def _as_code(value: Any) -> int | None:
    """The code as an int. None when it is missing or unreadable, never a guess."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- the time question ---------------------------------------------------------
#
# On every (re)connect the inverter asks the broker for the time, on
# /ntp/<productKey>/<id>/get, and the vendor cloud answers on .../get_reply. A
# local broker does not, so nothing answers unless something is told to. What
# the device does without an answer is not established for every feature (Local
# Control was measured to start without one, 2026-10-08), but its clock stays
# at the epoch until it gets one -- events then carry deviceTime 19700101000000
# -- and anything keyed to the time of day has nothing to go on.
#
# Request and reply formats were read off a capture of the real cloud answering
# (2026-10-06/07) and then used against the real device for weeks.

def parse_ntp_request(payload: str | bytes) -> tuple[str, str]:
    """(correlation id, requested timezone) from a /ntp/.../get payload.

    The timezone is "" when the device sends none -- the SEM does. Raises
    ValueError on anything that is not a JSON object with an `id`.
    """
    try:
        body = json.loads(payload)
    except (TypeError, ValueError) as err:
        raise ValueError(f"time request is not JSON: {err}") from err
    if not isinstance(body, dict):
        raise ValueError(f"time request is not a JSON object: {type(body).__name__}")
    corr_id = body.get("id")
    if corr_id is None:
        raise ValueError("time request has no id, cannot be answered")
    params = body.get("params")
    tz = params.get("timezone") if isinstance(params, dict) else ""
    return str(corr_id), tz if isinstance(tz, str) else ""


def resolve_timezone(requested: str, fallback: str) -> str:
    """The IANA name to answer with: what the device asked for, if we know it.

    The EZHI asks for its own zone and gets it back; the SEM asks for none, so
    it is given the fallback (Home Assistant's). A name the tz database does
    not know -- or a missing database -- falls through to UTC rather than
    raising: an unanswered question is worse than an answer in the wrong zone.
    """
    from zoneinfo import ZoneInfo

    for name in (requested, fallback):
        if not name:
            continue
        try:
            ZoneInfo(name)
        except Exception:  # noqa: BLE001 - unknown key, bad key, no tzdata
            continue
        return name
    return "UTC"


def build_ntp_reply(
    device_id: str,
    corr_id: str,
    tz_name: str,
    product_key: str = PRODUCT_KEY,
    now=None,
) -> str:
    """JSON to publish on topic_ntp_reply().

    `date` is UTC as YYYYMMDDhhmmss, exactly like the cloud's; `timeOffset` is
    the zone's current offset in milliseconds ("7200000" in a German summer).
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    now = now or datetime.now(timezone.utc)
    try:
        offset = now.astimezone(ZoneInfo(tz_name)).utcoffset()
    except Exception:  # noqa: BLE001 - see resolve_timezone
        tz_name, offset = "UTC", None
    offset_ms = int(offset.total_seconds() * 1000) if offset is not None else 0
    return json.dumps(
        {
            "company": COMPANY,
            "version": VERSION,
            "id": corr_id,
            "deviceId": device_id,
            "type": "ntp",
            "method": "get_reply",
            "productKey": product_key,
            "data": {
                "date": now.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S"),
                "timezone": tz_name,
                "timeOffset": str(offset_ms),
            },
            "code": 200,
            "message": "success",
            "companyKey": COMPANY_KEY,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def parse_event(payload: str | bytes) -> tuple[str, dict]:
    """(identifier, data) from a /event/.../post payload.

    Raises ValueError on anything that is not a JSON object with an
    `identifier`; `data` is {} when the event carries none.
    """
    try:
        body = json.loads(payload)
    except (TypeError, ValueError) as err:
        raise ValueError(f"event is not JSON: {err}") from err
    if not isinstance(body, dict):
        raise ValueError(f"event is not a JSON object: {type(body).__name__}")
    identifier = body.get("identifier")
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("event has no identifier")
    data = body.get("data")
    return identifier, data if isinstance(data, dict) else {}
