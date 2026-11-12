# Tibber Pulse IR as grid meter (experimental)

**Short version: if you can, use a different grid meter.**

The Pulse IR is a fine companion for the Tibber app. As a grid meter for EMS it
is the colleague who always turns up a little late: its bridge takes about
1.5 s to answer, has new values only every 2–3 s, and, when asked too often,
can hang until it is unplugged. EMS works around all of that and does support
the Pulse IR, but only as an **experimental grid meter without guaranteed
support**. If it is what you have, it will get you started. If you are still
choosing, a reader with a stable local interface regulates better and spares
you the unplugging: a Tasmota IR reader (Hichi, bitShake), an everHome
EcoTracker or a powerfox poweropti on the same optical port of your meter, or a
Shelly Pro 3EM in the distribution board.

EMS reads the Pulse IR locally over the bridge's own web server, without the
Tibber cloud.

## Before you choose it

What other projects report about reading the bridge locally, and what EMS
does about it:

| Limit | What EMS does |
| --- | --- |
| The bridge often takes about 1.5 s to answer, and too many requests can make it hang until it is unplugged; the Tibber app then gets no data either. | One background reader asks the bridge at most every 3 s. The control loop never waits on it. Do not let a second program (Home Assistant, ioBroker, …) read the same bridge at the same time. |
| New values arrive only every 2–3 s, not every second. | A reading older than 10 s counts as a failed meter read. |
| The bridge now and then serves a damaged telegram. | Every telegram's checksum is verified; a damaged one is dropped, never used. |
| Tibber updates the bridge over the air and has changed its unofficial local interface before (September 2026). | EMS tries the current and the older data path. A future change can still break the reading until EMS is updated. |
| An empty battery in the reading head stops the data; the bridge keeps answering. | A reading that does not change ages out instead of looking fresh. |

Still keen? Then read on. Otherwise the [supported setups](supported-setups.md)
list the meters that answer faster.

## 1. Switch on the bridge's web server permanently

In normal operation the bridge opens no port at all. Switch its web server on
once:

1. **Put the bridge into setup mode.** Unplug it; plug it in after three
   seconds; unplug it again after three seconds; plug it in again after three
   seconds. The LED now lights green.
2. **Join its Wi-Fi.** The network is called `Tibber Bridge`. The password is
   the nine characters printed on the bridge, including the dash (for example
   `AD56-54BA`).
3. **Set the switch.** Open `http://10.133.70.1/console/` and log in as user
   `admin` with the same nine characters. Type `param_set 39 TRUE` and send it,
   then `param_store`. On older firmware the same setting is
   `webserver_force_enable` on the *params* page, set to `true` and stored to
   flash.
4. **Back to normal operation.** Unplug the bridge, wait ten seconds, and plug
   it in again. The LED lights light blue, and the Tibber app works as before.

Give the bridge a fixed address in your router. Its *Pulse* page (old
firmware: *Nodes*) lists the reading heads. The *Last data* value there should
stay below about 5 seconds; turning the reading head a few degrees
anticlockwise often helps.

## 2. Save the bridge password in the Admin Console

Admin Console › Settings › Devices › *What the search may use* › **Tibber
bridge passwords**: enter a name and the nine characters from the bridge. The
password is stored encrypted with the other saved credentials. It never
appears in `config.json`, which only names it. A password saved or corrected
later takes effect within half a minute, without restarting the EMS.

## 3. Add the bridge as grid meter

- **Network scan.** The scan finds a bridge whose web server refuses a login
  the way a Tibber bridge does. It tries your saved bridge passwords only on
  such a device, over plain HTTP as the bridge requires, and offers the
  bridge as grid meter once one of them opens it.
- **By hand.** Choose *Tibber Pulse IR — local bridge (experimental)* as grid
  meter type, enter its IP address, and pick the saved password under
  *Bridge password*. Set the *Pulse node* (advanced) only when the bridge lists
  more than one reading head.

A password the saved grid meter uses cannot be removed; choose another
password or meter first.

`emsctl grid-meter test` and `emsctl diagnose --hardware` read the bridge the
same way the EMS does. While the EMS runs they are a second reader on a bridge
that can hang under load, so keep such tests short. Configuration details:
[configuration reference](../technical/configuration.md#tibber-pulse-ir--local-bridge-tibber_pulse_bridge-experimental).
