# FAQ

## Do I Need Home Assistant?

No. Home Assistant is optional.

## Do I Have To Use Docker?

No, but Docker is recommended for normal users.

## Which Grid Meters Are Supported?

Shelly, Shelly 3EM Gen1, EcoTracker, and Tasmota HTTP setups are documented in
[supported-setups.md](supported-setups.md).

## Can A Shelly Meter Hang Or Become Slow?

Yes. If you repeatedly see Shelly `ReadTimeoutError` messages, the meter may be
reachable but slow or temporarily stuck. First run `diagnose --hardware` and
`grid-meter test`. If the meter keeps timing out, also try checking the network
path and rebooting the Shelly before changing EMS settings.

See [troubleshooting.md](troubleshooting.md#grid-meter-not-reachable) for
details.

## Can I Use Multiple Zendure Inverters?

Yes, if each configured device has a real IP address, serial number, and
suitable limits.

## Can I Keep Using The Zendure App?

Read-only use is usually fine. Avoid running another controller that writes
Zendure `outputLimit`.

## Does EMS Need Internet Or Cloud Access?

Control is local-first. Docker image pulls, updates, and optional support
workflows need internet access; normal EMS control uses local devices and your
configured local meter.

## Is Native Python Still Supported?

Yes. It is documented as an advanced/manual setup in
[native-python.md](native-python.md).

## Do I Need InfluxDB?

No. The dashboard and local history work without InfluxDB. InfluxDB is optional
for long-range analytics.

## Do I Have To Use `config init`?

No. It is optional.

## What Happens On First Docker Start?

The container creates `config/config.json` from the template if it does not
exist.

## Does Docker Overwrite My Config?

No. Existing `config/config.json` is not overwritten.

## Is The Generated Template Config Safe?

Yes. Template placeholder configs force safe mode and disable hardware writes
until placeholders are replaced.

## What Is Safe Mode?

Required template placeholders force EMS safe mode: control disabled, dry-run
enabled, and hardware writes blocked.

## What Is Dry-Run?

EMS calculates and logs intended values but does not write Zendure hardware
output.

## Where Is My Config?

`config/config.json`

## Where Is My Data?

`data/`

## Where Are Backups?

`data/backups/` by default. Docker users see that folder on the host via the
existing `./data:/app/data` mount; no separate backup volume is needed.

## Where Is The Dashboard?

`http://<host-ip>:8080`

Do not expose the dashboard publicly without a reverse proxy/auth design.

## How Do I Update?

Backup, pull, restart, run `config upgrade`, then run `diagnose`.

## How Do I Stop EMS?

```bash
docker compose down
```

Use `docker compose up -d` to start it again.

## What Should I Do After Editing Config?

Restart the container and run diagnose:

```bash
docker compose restart
docker compose exec ems python3 emsctl.py diagnose
```

The [first-run checklist](first-run-checklist.md) is a good next step after a
larger config edit.

## Why Does EMS Say My Config Contains Placeholders?

Replace template values such as example IP addresses and `YOUR_SN` in
`config/config.json`.

## What If The Dashboard Is Not Reachable?

Run `docker compose ps`, check `docker compose logs -f`, and verify that port
`8080` is reachable on the host.

## How Do I Create A Support Bundle?

```bash
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

## Where Do I Find Logs?

```bash
docker compose logs -f
```

## What Should I Do Before Opening An Issue?

Run diagnose, create a support bundle, and include what hardware and grid meter
type you use.
