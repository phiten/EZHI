# Einrichtung: von der Hersteller-Cloud zu Local Control

Der kurze Weg, in der Reihenfolge, in der er funktioniert. Zu jedem Schritt steht,
wo die Einzelheiten stehen. [English](setup-guide.md)

**Du brauchst:** Home Assistant mit der MQTT-Integration an einem Broker (das
Mosquitto-Add-on ist der einfache Fall), einen EZHI-Wechselrichter und — für Local
Control — einen APsystems-**SEM**-Zähler im **selben Netzsegment** wie den
Wechselrichter.

## 1. Broker vorbereiten

Der Wechselrichter wählt den MQTT-Hostnamen des Herstellers auf **Port 9005 mit
TLS 1.2** an und legt ein Login vor. Gib dem Broker, den du ohnehin betreibst, einen
Listener dafür: ein selbstsigniertes Zertifikat (das Gerät prüft nichts), das Login
des Wechselrichters und Port 9005 auf diesen Listener. Die MQTT-Integration von Home
Assistant bleibt auf 1883 und wird nicht verändert.

→ [Der Broker](local-control.md#the-broker-use-the-one-you-already-have)
(englisch) — mit der Frage, wie du das Passwort des Wechselrichters ausliest, das
nirgends aufgedruckt ist. Mit dem Add-on aus Schritt 2 ist das ein Schalter;
mach die Schritte 1 und 2 also zusammen.

## 2. Wechselrichter (und Zähler) auf deinen Broker umleiten

Die Geräte haben keine Einstellung für eine Broker-Adresse, die Umleitung passiert
also in deinem Netzwerk: entweder ein **DNS-Rewrite** des Hersteller-Hostnamens
oder eine **statische Route** plus DNAT-Regel. Für die Routing-Variante gibt es das
Home-Assistant-Add-on **[APSystems Reroute](https://github.com/phiten/apsystems-reroute)**:

1. Einstellungen → Add-ons → Add-on-Store → ⋮ → **Repositories** → hinzufügen:
   `https://github.com/phiten/apsystems-reroute`.
2. **APSystems Reroute** installieren, starten und das Log lesen: Es nennt die
   genaue statische Route für deinen Router.
3. Diese Route im Router eintragen, danach im Add-on den **Watchdog** einschalten.
4. Die Option `capture_credentials` einschalten, um das Passwort des
   Wechselrichters vom Draht zu lesen (nötig für Schritt 1; der Broker muss dafür
   nicht anhalten).

Das Add-on braucht Home Assistant OS auf aarch64 und einen Router mit statischen
Routen. Lies zuerst den Abschnitt *„When this is the wrong mechanism"* im Add-on und
[die Auswahl der Variante](local-control.md#choosing-a-transport) hier: dort steht,
welche Variante du auch von unterwegs zurücknehmen kannst.

Der **Zähler** muss auf demselben Broker landen. Im Test hat ein DNS-Rewrite beide
Geräte erfasst; prüfe bei der Routing-Variante nach dem Neuverbinden des Zählers im
Log des Add-ons, ob sein Verkehr bei deinem Broker ankommt.

## 3. Integration installieren

1. HACS → ⋮ → **Benutzerdefinierte Repositories** → `https://github.com/phiten/EZHI`,
   Kategorie *Integration* → installieren → Home Assistant neu starten.
2. Einstellungen → Geräte & Dienste → **Integration hinzufügen** → *APsystems EZHI
   Local API*: IP-Adresse des Wechselrichters, ein Name, **Steuerweg = Lokaler
   MQTT-Broker**. Die Cloud-Felder bleiben leer.

Beim Speichern stellt die Integration dem Wechselrichter *über deinen Broker* eine
Frage. Funktioniert die Umleitung nicht, bekommst du hier eine Fehlermeldung (*„Der
Broker ist da, aber der Wechselrichter antwortet nicht darüber"*), und es wird nichts
gespeichert.

## 4. Smart Meter ergänzen

Trag die ID des Zählers (ein Buchstabe und Ziffern, auf dem Gerät aufgedruckt und in
der Hersteller-App zu sehen) unter **Smart-Meter-ID (SEM) für Local Control** ein —
im Einrichtungsformular oder später unter **Konfigurieren**. Auch der Zähler wird
einmal gefragt; eine falsche ID oder ein Zähler, der nicht an deinem Broker hängt,
wird abgelehnt.

Es erscheint ein Gerät *Smart Meter* mit der Netzleistung und der Import-/Export-
Energie (kWh, im Energie-Dashboard nutzbar).

## 5. Local Control einschalten

1. **Local Control Offset** einstellen (Standard 30 W, 0–120 W: so viel Netzbezug
   lässt der Wechselrichter stehen).
2. Den Schalter **Local Control** einschalten. Nach etwa 30 s regelt der
   Wechselrichter von selbst — mit oder ohne Home Assistant.
3. **Local Control Problem** muss aus bleiben. Geht er an, sagen seine Attribute
   (`cause`, `reason`, `differences`) warum.

→ [Smart Meter und Local Control](smart-meter.md) (englisch): was über den Draht
geht und was auf Hardware noch nicht geprüft ist.

## Optional

*Zeitanfragen (NTP) am lokalen Broker beantworten*: nur, wenn die Uhren der Geräte
nach der Umleitung auf 1970 stehen bleiben. Local Control startet auch ohne. Aus
lassen, wenn an deinem Broker schon etwas anderes antwortet.

## Wenn etwas nicht klappt

| Symptom | Wahrscheinliche Ursache |
|---|---|
| Fehler *„…der Wechselrichter antwortet nicht darüber"* beim Speichern | Die Umleitung oder der Broker-Listener (Port 9005, TLS, Login) arbeitet noch nicht — Schritte 1–2 |
| Fehler *„…der Zähler aber nicht"* beim Speichern der ID | Der Zähler hängt nicht an deinem Broker, oder die ID stimmt nicht |
| Problem-Sensor `no_data` | Wechselrichter und Zähler liegen nicht im selben Segment, oder mDNS / TCP 3333 ist dazwischen gesperrt |
| Problem-Sensor `inverter_only` / `meter_only` / `mismatch` | Nur die Hälfte der Gruppe ist gesetzt: Local Control aus- und wieder einschalten |
| *On-Grid Power* wird abgelehnt | Es wirkt nur im Systemmodus *Local*; mit Local Control stattdessen den Offset benutzen |
