# Setup guide: from the vendor cloud to Local Control

The short path, in the order that works. Each step says where the details are.
[Deutsch](setup-guide.de.md)

**You need:** Home Assistant with the MQTT integration connected to a broker (the
Mosquitto add-on is the easy case), an EZHI inverter, and — for Local Control —
an APsystems SEM smart meter on the **same network segment** as the inverter.

## 1. Prepare the broker

The inverter dials the vendor's MQTT hostname on **port 9005 with TLS 1.2** and
presents a login. Give the broker you already run a listener for it: a
self-signed certificate (the device validates nothing), the inverter's login, and
port 9005 mapped to that listener. Home Assistant's MQTT integration stays on
1883 and is not changed.

→ [The broker](local-control.md#the-broker-use-the-one-you-already-have) —
including how to read the inverter's password, which is printed nowhere. With the
add-on of step 2 that is one toggle, so do steps 1 and 2 together.

## 2. Send the inverter (and the meter) to your broker

The devices have no setting for a broker address, so the redirect happens in your
network: either a **DNS rewrite** of the vendor's MQTT hostname, or a **static
route** plus a DNAT rule. For the routing variant use the Home Assistant add-on
**[APSystems Reroute](https://github.com/phiten/apsystems-reroute)**:

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add
   `https://github.com/phiten/apsystems-reroute`.
2. Install **APSystems Reroute**, start it, and read its log: it prints the exact
   static route to add in your router.
3. Add that route in your router, then turn on the add-on's **Watchdog**.
4. Turn on its `capture_credentials` option to read the inverter's password off
   the wire (needed for step 1; no need to stop the broker).

The add-on needs Home Assistant OS on aarch64 and a router with static routes.
Read its *"When this is the wrong mechanism"* section first, and
[choosing a way](local-control.md#choosing-a-transport) here for which variants
you can undo while away from home.

The **meter** has to end up on the same broker. In our test one DNS rewrite
covered both devices; with the routing variant, check after the meter
reconnects that its traffic arrives at your broker (see the add-on's log).

## 3. Install the integration

1. HACS → ⋮ → **Custom repositories** → `https://github.com/phiten/EZHI`,
   category *Integration* → install → restart Home Assistant.
2. Settings → Devices & services → **Add integration** → *APsystems EZHI Local
   API*: IP address of the inverter, a name, **Control transport = Local MQTT
   broker**. The cloud fields stay empty.

On saving, the integration asks the inverter one question *through your broker*.
If the redirect does not work you get an error here (*"The broker is there, but
the inverter did not answer over it"*), and nothing is saved.

## 4. Add the smart meter

Enter the meter's id (a letter and digits, printed on the device and shown in the
vendor app) under **Smart meter ID (SEM) for Local Control** — in the setup form,
or later under **Configure**. The meter is asked one question as well; a wrong id
or a meter that is not on your broker is refused.

A *Smart Meter* device appears with the grid power and the import / export energy
(kWh, usable in the energy dashboard).

## 5. Switch Local Control on

1. Set **Local Control Offset** (default 30 W, 0–120 W: how much grid draw the
   inverter leaves standing).
2. Turn on the **Local Control** switch. After about 30 s the inverter
   regulates by itself, with or without Home Assistant.
3. **Local Control Status** shows *Regulating*. **Local Control Problem** must
   stay off. If something is wrong, the status names the reason (for example
   *Smart meter not answering*), and the Problem sensor's attributes (`cause`,
   `reason`, `differences`) give the details.

→ [Smart meter and Local Control](smart-meter.md): what goes over the wire and
what has not been verified on hardware.

## Optional

*Answer the devices' time requests (NTP) on the local broker*: only if the
devices' clocks stay at 1970 after the redirect. Local Control starts without it.
Leave it off if something else on your broker already answers.

## When something does not work

| Symptom | Likely cause |
|---|---|
| Error *"the inverter did not answer over it"* when saving | The redirect or the broker listener (port 9005, TLS, login) is not working yet — steps 1–2 |
| Error *"…but the smart meter did not"* when saving the id | The meter is not on your broker, or the id is wrong |
| Problem sensor `no_data` | Inverter and meter are not on the same segment, or mDNS / TCP 3333 is blocked between them |
| Status *Inverter / Smart meter / Neither device not answering* | That device is not on your broker (or off). Right after a start it is retried for a few seconds before anything is reported |
| Problem sensor `inverter_only` / `meter_only` / `mismatch` | Only half of the group is set: switch Local Control off and on |
| *On-Grid Power* is refused | It only acts in the *Local* system mode; with Local Control use the Offset instead |
