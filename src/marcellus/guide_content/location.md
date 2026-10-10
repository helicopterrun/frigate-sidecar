---
title: Location
section: tuning
order: 4
routes: []
config: ["location"]
---

Marcellus can be told where the property is. Today that has one job:
deciding when it is **night** for [Encounters](/guide/encounters) -- an
encounter that begins after dark is tagged notable rather than a passer-by.
It is a general setting, so later features can use it too.

It is optional. Without a location, night falls back to 22:00 to 06:00 on
the **server's clock**. That is wrong if the server runs in UTC (as many do):
"night" would then be 22:00 to 06:00 UTC, which is the middle of the
afternoon in some time zones. With a location, night is when the sun is
below the horizon at that spot, wherever the server's clock says it is, and
it follows the seasons.

## Setting it

Open [Settings](/settings#location) and find the **Location** block in the
Tuning section.

- Type the **Latitude** and **Longitude** (decimal degrees; north and east
  are positive) and press **Save tuning**. Both or neither -- one alone is
  refused. Clear both fields and save to remove the location.
- Or type an address in **Address** and press **Look up**. Up to five
  matches appear; pick one and the latitude, longitude and a label are filled
  in. Nothing is saved until you press **Save tuning**.

The change takes effect straight away, with no restart, and applies to
encounters recorded afterwards (see the note under
"Background or notable" in [Encounters](/guide/encounters)).

Once a location is saved the block shows **Sun is up** or **Sun is down**
right now, worked out by the server from the saved coordinates. If that
disagrees with the window, the coordinates are wrong (a swapped sign is the
usual cause).

## What the address lookup sends

The lookup runs in your **browser**, not on the Marcellus server: the address
you type is sent from your browser to OpenStreetMap's Nominatim search
service (`nominatim.openstreetmap.org`) and the matches come straight back.
Marcellus never sees or forwards the address and needs no API key. Marcellus
stores only the coordinates and the label you pick. If you would rather not
send an address anywhere, type the coordinates yourself.

## Configuration

The same values live under `location:` in `sidecar.yml`. Editing them on the
Settings page stores a runtime override (in the tuning file, `config/tuning.json`),
which wins over the YAML and survives restarts and deploys.

| Field | Default | Live | Effect |
|---|---|---|---|
| `latitude` | unset | yes | Property latitude, -90 to 90 (north positive). Set together with `longitude`, or leave both unset. |
| `longitude` | unset | yes | Property longitude, -180 to 180 (east positive). Set together with `latitude`. |
| `label` | unset | yes | Free text shown beside the coordinates, normally the address that was looked up. Display only; nothing reads it. |
