# Smart meter and Local Control: the inverter regulates by itself

The short version is in the [README](../README.md#local-control-smart-meter).
This is the long one: what the vendor app calls *Local Control*, what this
integration does for it, what it needs from your network, and — stated plainly —
what has been measured and what has not.

## What it is

A smart meter (the APsystems **SEM**, SEM3-WL-2) and the inverter are put into a
**group** by two commands. From then on the inverter reads the meter by itself
and holds the grid draw at an **offset** — a few tens of watts it deliberately
leaves standing, so that it never feeds in. Home Assistant is the configurator
and the monitor, not the controller: **when it is down, the group keeps
regulating.** An MQTT disconnect was measured to leave it running, and unloading
or reloading this integration does not touch the group either.

It is not the other things called "local" in this repository:

| | What it is |
|---|---|
| **Local mode** (`systemMode` 4) | A system mode in which the local HTTP `setPower` is obeyed. |
| **Local MQTT** transport | The inverter is redirected to your broker; see [local-control.md](local-control.md). |
| **Local Control** (this page) | The meter/inverter group. It runs in *Balcony Storage* mode, not in Local mode. |

Local Control **needs** the local MQTT transport, because both devices are
configured over the broker they were redirected to.

## What the integration needs from your network

1. **Both devices are redirected to the same broker.** The inverter as described
   in [local-control.md](local-control.md); the meter the same way. On the
   development install one DNS override covered both devices, and no add-on was
   needed.
2. **The meter and the inverter are on the same network segment.** The group
   configuration contains **no IP address**. The inverter finds the meter itself:
   it asks the network (mDNS, an `_http._tcp` service) for the meter and then
   polls it on **TCP port 3333** about every two seconds. So mDNS and port 3333
   must work between the two — which is what "same segment" means in practice.
   Routed networks and broadcast filtering break exactly this. Both devices
   reaching the *broker* across a routed network is fine; reaching *each other*
   is the requirement.
3. The meter's **id** (a letter and digits, printed on the device and shown in
   the vendor app). Not its IP: nothing here needs, or accepts, one.

## Setting it up

1. Redirect the inverter and the meter to your broker and select the **Local
   MQTT** control transport (see [local-control.md](local-control.md)).
2. Enter the meter's id under *Smart meter ID (SEM) for Local Control* -- in the
   setup form when you first add the integration, or later under **Configure**
   (the transport has to be *Local MQTT broker*). On saving, the integration asks
   both devices one question over the broker; a wrong id, or a meter that was not
   redirected, is refused there rather than saved.
3. Turn on the **Local Control** switch.

The switch returns as soon as both devices accepted their command. About 11 s
later the inverter reconnects, and after about 28 s it regulates. The switch
shows the state you asked for until the devices confirm it (or 90 s have passed).

Switching on, or calling `local_control_enable`, when a group already stands
with the same offset **sends nothing**: forming the group again draws a new
version, and the inverter would reconnect and stop regulating for about 40 s.
If the inverter does not take the group, the meter is put back the way it was --
out of the group, or, if a group stood before (an offset change), back in the
old one -- judged by asking the inverter what it holds, not by guessing.

To remove the meter id from the options, switch Local Control off first: the
group regulates without this integration, and nothing here could dissolve it once
the id is gone. The form refuses until then.

## Entities

| Entity | Type | Notes |
|---|---|---|
| Local Control | `switch` | Forms or dissolves the group. State is read back from both devices (every 30 s; every 5 s for 90 s after a change), so a group made in the vendor app shows up too. Stays available with its last known state while a read fails, so the group can still be dissolved from the UI. |
| Local Control Offset | `number` | 0–120 W: how much grid draw the inverter leaves standing. 120 W is the app's own cap, 10 % of the group's 1200 W. Changing it while the group stands re-applies the group (see below). |
| Local Control Problem | `binary_sensor` | On when a group was asked for but does not work -- see below for what it says. Off while there is no group. |
| Grid Power, Grid Power L1–L3 | `sensor` | The meter's live readings, pushed by the meter (positive = draw from the grid). On a separate *Smart Meter* device attached to the inverter. Unavailable when the meter has been silent for a minute. |
| Grid Import Energy, Grid Export Energy (and L1–L3 each) | `sensor` | The meter's cumulative energy, `iE` (imported from the grid) and `eE` (exported to it), in kWh -- for the energy dashboard as *grid consumption* and *return to grid*. The vendor app labels the two "imported" and "exported" and formats both as kWh. Under Local Control the export counter stays small, since the inverter holds the draw above zero. |

### What the Problem sensor says

It is on when a group was asked for but does not work, and it also stays on (it
never goes *unavailable*) when the devices cannot be read, so an alert on `on`
fires. Its attributes say why:

| `cause` | Meaning |
|---|---|
| `inverter_only` | The inverter is in the group (`thirdLink` 4), the meter is not (`localLink` status 0). |
| `meter_only` | The meter is in the group, the inverter is not. Something took the inverter out. If it followed a change of System Mode, Backup Power, ECO or an SOC limit, that is the lead -- whether such a write drops the inverter out of the group is not established. |
| `mismatch` | Both claim a group, but not the same one. `differences` lists every field that differs, with both values (the group version, the meter id, the inverter missing from the list). |
| `no_data` | The group stands on both devices, but the inverter reports no readings from the meter (`seconds_without_meter_data`). Not judged for ~90 s after a command, while the group comes up. Usually the network: same segment, mDNS, TCP 3333. |
| `unreadable` | The inverter or the meter did not answer. Reported after the third failed read in a row (about a minute and a half): one lost read is a hiccup and changes nothing. A standing group keeps regulating without Home Assistant. |

While it is on, the attributes also carry the raw values the verdict rests on
(`inverter_third_link`, `meter_link_status`, `inverter_in_group`, `meter_in_group`,
`configs_match`, `seconds_without_meter_data`). `last_problem` and
`last_problem_at` stay after the problem has gone, so a fault that cleared by
itself can still be told from none. The same sentence is written to the Home
Assistant log as a warning when the problem appears, and an info line when it is
gone.

Actions:

```yaml
action: apsystems_ezhi_local.local_control_enable
data:
  offset: 40        # optional, W; remembered for the switch
  wait: true        # optional; default true: wait until the inverter gets readings (~30 s)
```

```yaml
action: apsystems_ezhi_local.local_control_disable
```

The **On-Grid Power** number and the `set_power` action are a different thing:
they set the setpoint of the inverter's *Local system mode* and are refused with
an error whenever the inverter is in any other mode (see the README). With a
group standing they point at the Local Control Offset instead.

While the group stands, two writes are refused with an explanation instead of
being sent: the **System Mode** select and **Preset Output Power**. The group
lives in Balcony Storage mode and the inverter follows the meter, not a preset;
changing either could do nothing or pull the inverter out of the group, and
which of the two is not established. Nothing else is blocked -- but see *Not yet
verified* for what that means for Backup Power, ECO and the SOC limits.

Switching **off** leaves the inverter in Balcony Storage mode; the previous mode
is not restored.

## What goes over the wire

For reference, and for anyone reading the MQTT traffic. Both commands carry the
same `config`, a **nested JSON object** whose values are all strings:

```json
{"meter": "<SEM id>", "power": "30", "vrn": "<6 digits>",
 "totalPower": "1200", "totalPvPower": "1200",
 "device": {"<inverter id>": "1.00"}}
```

| Step | Device | Topic / identifier | Payload |
|---|---|---|---|
| 1 | meter | `localLink` | `{"status": "1", "config": CFG}` |
| 2 | inverter | `systemMode` | `{"systemMode": "1", "thirdLink": "4", "config": CFG}` |

Dissolving is the mirror image, **in the other order**: the inverter first
(`thirdLink "0"`, `config {}`), then the meter (`status "0"`, `config {}`).
Measured, the inverter's output stops within 2 s of its half being dissolved.
These are the orders that were verified, and the only ones the integration
uses.

`vrn` is a group version the app draws at random; the integration draws a fresh
one each time it sets the group, which is also how an offset change is applied.

`thirdLink` in this field is not a boolean: `"0"` is off, `"1"` and `"2"` were
seen on inverters with linking enabled (a device coupled / nothing coupled), and
**`"4"` is a Local Control group**. The earlier *Smart Linking* switch of this
integration wrote `0`/`1` to it; it has been removed, because it would have shown
a working group as "on" and could have turned it into something else with one
tap.

## Time requests (optional)

When a device is redirected to a local broker nothing answers its *"what time is
it"* (`/ntp/<product>/<id>/get`) — the vendor cloud used to. Without an answer a
device's clock stays at the epoch (its events then say `deviceTime
19700101000000`). **Local Control was measured to start without any answer**, so
this is not needed for it.

*Answer the devices' time requests on the local broker* makes the integration
answer, for the inverter and the meter it knows, by exact topic (never a
wildcard — your broker may have other people's devices on it). The inverter gets
its own time zone back; the meter asks for none and gets Home Assistant's. It is
**off by default**, because something else on your broker may already answer, and
an answer would then come twice.

## Not yet verified on hardware

Measured on the development install: forming and dissolving the group in both
orders, regulation to the offset, the 120 W cap, the ~11 s / ~28 s timings,
survival of an MQTT disconnect, and that it starts without time answers.

**Not** measured, and worth a check before depending on it:

- **Other writes while the group stands.** Backup Power, ECO, the SOC limits and
  Discharge Protection are `systemMode` writes that carry `systemMode` along but
  not `thirdLink` or `config`. Whether the firmware keeps the group through such a
  write is unverified. The *Local Control Problem* sensor shows it if it does not;
  re-enable the group afterwards.
- **Changing the offset live.** Applying a new offset re-forms the group, which
  should cost one reconnect (~11 s) and the ~28 s until it regulates again. The
  gap in the regulation has not been timed.
- **This integration's polling against the regulation.** The group is read every
  30 s (three small reads). Nothing indicates it disturbs the inverter, but that
  has not been watched over a long run.
- **`totalPower`.** The group is sent with `totalPower` and `totalPvPower` of
  1200 W (which also sets the 120 W cap on the offset), whatever the inverter's
  power limit (800/1200 W) is. That is what the vendor app does: its group page
  takes the number from a table of nominal powers by device type (D02 1200 W)
  and never reads the power limit. This comes from the app's code, not from a
  capture -- the app sets the group over Bluetooth, which a broker or proxy does
  not see. Measured on a unit set to 800 W (2026-10-08): `totalPower` 1200
  regulated correctly -- link after 28 s, grid draw 33 W on average (25-42 W) on
  an offset of 30 W, output about 340 W. Not tried: a load above 800 W, where
  the ceiling matters, and what the firmware does with `totalPvPower`.
- **The energy counters.** kWh is what the vendor app shows them as, not a
  comparison with the meter's own display. An early capture showed `iE` 368.8 and
  `eE` 0.0140; compare with the display before feeding them into long-term
  statistics.
- **A meter or inverter power loss, and a broker restart** with the group set.
- **Surplus feed-in.** Local Control holds the grid draw *at* the offset; it is
  not a feed-in strategy.
