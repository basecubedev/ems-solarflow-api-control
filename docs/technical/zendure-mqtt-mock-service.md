# Zendure MQTT mock telemetry publisher

`tools/zendure_mqtt_mock_service.py` publishes known-good Zendure telemetry to
an MQTT broker so MQTT discovery and the EMS MQTT clients have reproducible
data without real hardware.

> **Test/dev only.** This tool publishes **telemetry only**. It never publishes
> write/control topics, so nothing it emits can drive a device.

## Supported schemas

`--schema` selects a topic family (default `all`). These mirror the Admin
discovery classifier in `admin/mqtt_topic_discovery.py`.

| `--schema` value   | Topic(s)                                          |
| ------------------ | ------------------------------------------------- |
| `zensdk-scalar`    | `Zendure/sensor/<device_sn>/<metric>`             |
| `legacy-iot-json`  | `iot/<product_key>/<device_id>/properties/report` |
| `legacy-slash-json`| `/<product_key>/<device_id>/properties/report`    |
| `cloud-scalar`     | `<app_key>/sensor/<device_sn>/<metric>`           |

The legacy JSON schemas share one payload shape (`properties` + `packData`).

## Dry run

`--dry-run` prints one batch as a JSONL publish plan (`topic`, `payload`,
`qos`, `retain`) and never connects. The plan never contains credentials.

```bash
python tools/zendure_mqtt_mock_service.py --schema all --dry-run
```

## Publish to an existing broker

```bash
# one batch and exit
python tools/zendure_mqtt_mock_service.py --host 192.0.2.10 --once

# long-running mock, new batch every 3s
python tools/zendure_mqtt_mock_service.py --host 192.0.2.10
```

### Username / password

The password is never printed — not in logs, dry-run output, or connection
errors.

```bash
python tools/zendure_mqtt_mock_service.py \
  --host 192.0.2.10 --username mock --password "$MQTT_PASSWORD" --once
```

### TLS

```bash
# TLS with system CA verification
python tools/zendure_mqtt_mock_service.py --host broker.local --port 8883 --tls --once

# TLS accepting a self-signed local test cert (only valid together with --tls)
python tools/zendure_mqtt_mock_service.py --host broker.local --port 8883 --tls --tls-insecure --once
```

## Optional dev broker

`deploy/docker-compose.mqtt-dev.yml` starts a throwaway Mosquitto broker on
`1883` with no authentication. It is **not** wired into any EMS/Admin compose
file and must not be used in production — run your own secured broker there.

```bash
docker compose -f deploy/docker-compose.mqtt-dev.yml up -d
python tools/zendure_mqtt_mock_service.py --host localhost --once
docker compose -f deploy/docker-compose.mqtt-dev.yml down
```
