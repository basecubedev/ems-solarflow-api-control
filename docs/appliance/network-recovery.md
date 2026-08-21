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

- The **previous WLAN profile is never deleted**. When the new network does not
  reach full connectivity within the configured timeout
  (`wifi_revert_timeout_seconds`, 90 s by default), the appliance reactivates
  it and reports `wifi_connection_failed` together with `reverted: true`.
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

This matters more than the commands above, and the two installation shapes
differ:

| Shape | Console or SSH login |
|---|---|
| Manager package on your own Raspberry Pi OS | Yes — your own account, the one you set up when you installed the OS |
| **A/B appliance image** | **No.** The image ships no login account, no default password and no authorized key, on purpose: a shipped credential is a credential every device shares |

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
4. Re-flash. Configuration and data live on the shared partition and are not
   erased by writing a new system image, but take a backup first if you can.

Step 3 is the one worth doing early. There is no way to add a key to a box you
can no longer reach.

## First-boot provisioning portal

A later phase adds a temporary setup access point for a Pi with no usable
network: it starts only when no network is available, serves a separate
provisioning page (not the authenticated management UI), applies the WLAN and
the appliance password, and then shuts itself down. An unauthenticated
configuration access point is never left running. This is not part of the
current release.
