# Network and hostname

Open **Network**.

## Read-only overview

The overview shows the active interface, the Ethernet and WLAN state, IP
addresses, hostname, mDNS name and connectivity. Expert mode adds the gateway
and DNS servers per interface. The WLAN card shows the SSID and signal quality.

## Change the WLAN

A WLAN change can disconnect the browser you are using, so it is handled as a
plan with an automatic revert:

```text
01 Scan networks
02 Select the SSID (or mark the network hidden)
03 Enter the passphrase
04 Validate the input
05 Save the previously working connection
06 Apply the new connection
07 Wait for connectivity
08 Confirm success
09 Revert automatically when the connection fails
```

Details that matter:

- The **previous WLAN profile is never deleted**. When the WLAN device does not
  join the requested SSID and hold an address within the configured timeout
  (`wifi_revert_timeout_seconds`, 90 s by default), the appliance reactivates
  it and reports `wifi_connection_failed` together with `reverted: true`. The
  verdict is read off the WLAN device itself, deliberately never off
  NetworkManager's host-wide connectivity value: with a cable plugged in that
  value reads `full` whatever the radio did, and on a LAN without internet it
  never reads `full` even when the join was perfect.
- The passphrase is handed to `nmcli` on **stdin**, so it never appears in the
  host process table, in an operation record, in the audit log or in any log
  file.
- A stored WLAN passphrase is never shown again after saving.
- A passphrase shorter than 8 or longer than 63 characters is refused before
  anything on the host is touched.

## Change the hostname

The hostname change validates an RFC 1123 label, updates the host
configuration, refreshes mDNS, warns that the URL changes, requires an explicit
confirmation and then shows the new URLs:

```text
http://<new-hostname>.local:8088   Appliance Manager
http://<new-hostname>.local:8090   EMS Admin Console
```

Update your bookmarks after the change; the old `.local` name stops resolving.

## Recovering access after a WLAN change

If you can no longer reach the appliance:

1. **Wait for the automatic revert.** When the new network fails, the previous
   profile is reactivated within the revert timeout and the old address works
   again.
2. **Try the mDNS name** `http://ems-solarflow.local:8088` — the IP address may
   have changed while the name did not. When the name does not resolve, find
   the appliance in your router's list of connected devices and open
   `http://<address>:8088`.
3. **Connect Ethernet.** Ethernet keeps working independently of the WLAN
   profile; the appliance is reachable on its wired address.
4. **From a shell, if you have one:**

   ```bash
   nmcli connection show
   nmcli connection up <previous-profile>
   sudo ems-appliance status
   ```

### Whether you have a shell at all

This matters more than the commands above, and the three installation shapes do
not all answer the same way:

| Shape | Console or SSH login |
|---|---|
| Manager package on your own Raspberry Pi OS | Yes — your own account, the one you set up when you installed the OS |
| **Either appliance image** | **Console only.** `ems-rescue` with a documented password, for the case where nothing else answers — see [console-recovery.md](console-recovery.md). No SSH password login and no shipped authorized key: a shipped *key* is a credential every device shares, and unlike a console password it is reachable over the network |

Both image shapes answer this identically. The rescue account comes from the
package, not from the layout, and neither image accepts an SSH password.

On an appliance image the recovery paths are therefore, in order:

1. Reach the manager over **Ethernet**, which keeps working independently of
   the WLAN profile, and fix it there.
2. Wait out the WLAN revert. A change that loses connectivity returns to the
   previous profile on its own, so a wrong passphrase is not a lockout.
3. Add your own SSH key through the manager **while it is still reachable**.
   That does not buy you a shell — the only key-eligible account is
   `ems-backup`, which is chroot-confined, read-only and SFTP-only. What it buys
   is the ability to retrieve your configuration, data and backups from a box
   you can otherwise no longer reach, so step 4 costs you nothing.
4. Re-flash. This erases the card: writing the image back replaces the
   operator's configuration, data and on-box backups with a fresh installation.
   The backup in step 3 is not a convenience — it is the only copy.

Step 3 is the one worth doing early. There is no way to add a key to a box you
can no longer reach.

## First-boot provisioning portal

A later phase adds a temporary setup access point for a Pi with no usable
network: it starts only when no network is available, serves a separate
provisioning page (not the authenticated management UI), applies the WLAN and
the appliance password, and then shuts itself down. An unauthenticated
configuration access point is never left running. This is not part of the
current release.
