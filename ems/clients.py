# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ems import config as cfg
from ems.logging_utils import log_event
from ems.models import DeviceState


def zendure_write_succeeded(error_event, dev, response, **fields):
    """Log failed Zendure write responses and return success state."""

    status_code = getattr(response, "status_code", 0)

    if status_code < 300:
        return True

    response_text = getattr(response, "text", "") or ""
    fields.setdefault("device", dev.name)

    log_event(
        logging.WARNING,
        error_event,
        status_code=status_code,
        response_text=response_text[:200],
        **fields
    )

    return False

# =====================
# HTTP SESSION
# =====================


def create_session():
    """Create a requests session with retry logic."""

    session = requests.Session()

    retry = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )

    adapter = HTTPAdapter(max_retries=retry)

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session

# =====================
# HOME ASSISTANT CLIENT
# =====================


class HAClient:
    """Simple REST client for Home Assistant."""

    def __init__(self, base_url, token, session):
        self.base_url = base_url.rstrip("/")
        self.session = session

        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    def set_state(
        self,
        entity_id,
        state,
        unit=None,
        device_class=None,
        state_class=None,
        icon=None,
        extra_attributes=None
    ):
        """Write a sensor state to HA."""

        attributes = {}

        if unit:
            attributes["unit_of_measurement"] = unit

        if device_class:
            attributes["device_class"] = device_class

        if state_class:
            attributes["state_class"] = state_class

        if icon:
            attributes["icon"] = icon

        if extra_attributes:
            attributes.update(extra_attributes)

        payload = {
            "state": state,
            "attributes": attributes
        }

        try:
            r = self.session.post(
                f"{self.base_url}/api/states/{entity_id}",
                headers=self.headers,
                json=payload,
                timeout=2
            )

            if r.status_code >= 300:
                log_event(
                    logging.WARNING,
                    "ha_write_error",
                    entity=entity_id,
                    status_code=r.status_code
                )

        except Exception as e:
            log_event(
                logging.WARNING,
                "ha_write_error",
                entity=entity_id,
                error=e
            )

    def get_state(self, entity_id):
        """Read a state from HA."""

        try:
            r = self.session.get(
                f"{self.base_url}/api/states/{entity_id}",
                headers=self.headers,
                timeout=2
            )

            if r.status_code != 200:
                return None

            data = r.json()

            if not isinstance(data, dict):
                return None

            return data.get("state")

        except Exception:
            return None

    def ping(self):
        """Return True when Home Assistant API is reachable."""

        try:
            r = self.session.get(
                f"{self.base_url}/api/",
                headers=self.headers,
                timeout=2
            )

            return r.status_code == 200

        except Exception:
            return False

    def get_float(self, entity_id, default):
        """Return HA state as float or fallback."""

        val = self.get_state(entity_id)

        try:
            return float(val)
        except:
            return default

def parse_device(data):
    """Extract relevant values from Zendure API response."""

    props = data.get("properties", {})

    return DeviceState(
        soc=props.get("electricLevel") or 0,

        min_soc=(props.get("minSoc") or 0) / 10,
        max_soc=(props.get("socSet") or 0) / 10,

        solar=props.get("solarInputPower") or 0,
        output=props.get("outputHomePower") or 0,

        pack_in=props.get("packInputPower") or 0,
        pack_out=props.get("outputPackPower") or 0,

        temp=round(((props.get("hyperTmp") or 0) / 100), 2),
        voltage=round(((props.get("BatVolt") or 0) / 100), 2),
        rssi=props.get("rssi") or 0,

        remain_minutes=round((props.get("remainOutTime") or 0), 1),

        solar1=props.get("solarPower1") or 0,
        solar2=props.get("solarPower2") or 0,
        solar3=props.get("solarPower3") or 0,
        solar4=props.get("solarPower4") or 0,

        output_limit=props.get("outputLimit") or 0,
        soc_limit=props.get("socLimit") or 0,
        pack_state=props.get("packState") or 0,

        fault_level=props.get("faultLevel") or 0,

        smart_mode=props.get("smartMode") or 0,
        grid_off_mode=props.get("gridOffMode") or 0,
        ac_mode=props.get("acMode") or 0,
        ac_status=props.get("acStatus") or 0,
        dc_status=props.get("dcStatus") or 0,
        grid_state=props.get("gridState") or 0,
        input_limit_w=props.get("inputLimit") or 0,
        pack_num=props.get("packNum") or 0,
        soc_status=props.get("socStatus") or 0,
        battery_calibration_time=props.get("batCalTime"),
    )


def zero_device_state():
    """Create an empty telemetry state for unavailable devices."""

    return DeviceState(
        soc=0,
        min_soc=0,
        max_soc=0,
        solar=0,
        output=0,
        pack_in=0,
        pack_out=0,
        temp=0,
        voltage=0,
        rssi=0,
        remain_minutes=0,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
        output_limit=0,
        soc_limit=0,
        pack_state=0,
        fault_level=0,
        smart_mode=0,
        grid_off_mode=0,
        ac_mode=0,
        ac_status=0,
        dc_status=0,
        grid_state=0,
        input_limit_w=0,
        pack_num=0,
        soc_status=0,
        battery_calibration_time=None,
    )

class ZendureClient:
    """Client for a single Zendure device."""

    def __init__(
        self,
        name,
        ip,
        sn,
        session,
        min_soc,
        max_soc,
        smart_mode,
        grid_off_mode,
        max_power=None,
        pv_kwp=1.0,
        battery_kwh=1.0,
        pv_priority_factor=1.0
    ):
        self.name = name
        self.ip = ip
        self.sn = sn
        self.session = session
        self.min_soc = min_soc
        self.max_soc = max_soc
        self.smart_mode = smart_mode
        self.grid_off_mode = grid_off_mode
        self.max_power = max_power or cfg.MAX_DEVICE_POWER
        self.pv_kwp = pv_kwp or 1.0
        self.battery_kwh = battery_kwh or 1.0
        self.pv_priority_factor = pv_priority_factor or 1.0

    def fetch(self):
        """Fetch current device state."""

        try:
            r = self.session.get(
                f"http://{self.ip}/properties/report",
                timeout=2
            )

            return parse_device(r.json())

        except Exception as e:
            logging.warning(f"{self.name} fetch failed: {e}")
            return None


class ShellyClient:
    """Client for Shelly power meter."""

    def __init__(self, ip, session, channels=None):
        self.ip = ip
        self.session = session
        self._channels = _normalize_shelly_channels(channels)
        self.last_value = 0

    @property
    def channels(self):
        return self._channels

    def get_power(self):
        """Return current household power usage."""

        try:
            r = self.session.get(
                f"http://{self.ip}/rpc/Shelly.GetStatus",
                timeout=3
            )

            self.last_value = round(
                _parse_shelly_power(
                    r.json(),
                    channels=self._channels,
                ),
                1
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "shelly_read_error",
                ip=self.ip,
                channels=self._channels,
                error=e,
                stale_value=self.last_value
            )

        return self.last_value


class Shelly3EMGen1Client:
    """Client for Shelly 3EM Gen1 power meters."""

    def __init__(self, ip, session, channels=None):
        self.ip = ip
        self.session = session
        self._channels = _normalize_shelly_channels(channels)
        self.last_value = 0

    @property
    def channels(self):
        return self._channels

    def get_power(self):
        """Return current household/grid power usage."""

        try:
            r = self.session.get(
                f"http://{self.ip}/status",
                timeout=3
            )

            self.last_value = round(
                _parse_shelly_3em_gen1_power(
                    r.json(),
                    channels=self._channels,
                ),
                1
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "shelly_3em_gen1_read_error",
                ip=self.ip,
                channels=self._channels,
                error=e,
                stale_value=self.last_value
            )

        return self.last_value


class EcoTrackerClient:
    """Client for everHome EcoTracker local REST API."""

    def __init__(self, ip, session):
        self.ip = ip
        self.session = session
        self.last_value = 0

    def get_power(self):
        """Return current household/grid power usage."""

        try:
            r = self.session.get(
                f"http://{self.ip}/v1/json",
                timeout=3
            )

            self.last_value = round(
                _parse_ecotracker_power(r.json()),
                1
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "ecotracker_read_error",
                ip=self.ip,
                error=e,
                stale_value=self.last_value
            )

        return self.last_value


class TasmotaHttpClient:
    """Client for Tasmota HTTP JSON smart meter payloads."""

    def __init__(self, url, power_path, session):
        self.url = url
        self.power_path = power_path
        self.session = session
        self.last_value = 0

    def get_power(self):
        """Return current household/grid power usage."""

        try:
            r = self.session.get(
                self.url,
                timeout=3
            )

            self.last_value = round(
                _parse_tasmota_http_power(r.json(), self.power_path),
                1
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "tasmota_http_read_error",
                url=self.url,
                power_path=self.power_path,
                error=e,
                stale_value=self.last_value
            )

        return self.last_value


def create_grid_meter_client(config, session):
    """Create the configured household/grid power meter client."""

    config = config if isinstance(config, dict) else {}
    meter_type = str(config.get("type", "shelly")).strip().lower()
    ip = config.get("ip", "")

    if meter_type == "shelly":
        return ShellyClient(
            ip,
            session,
            channels=config.get("channels"),
        )

    if meter_type == "shelly_3em_gen1":
        return Shelly3EMGen1Client(
            ip,
            session,
            channels=config.get("channels"),
        )

    if meter_type == "ecotracker":
        return EcoTrackerClient(ip, session)

    if meter_type == "tasmota_http":
        power_path = config.get("power_path")
        if not isinstance(power_path, str) or not power_path.strip():
            raise ValueError("Tasmota HTTP grid meter requires power_path")

        url = str(config.get("url") or "").strip()
        if not url:
            if not ip:
                raise ValueError("Tasmota HTTP grid meter requires url or ip")
            url = f"http://{ip}/cm?cmnd=Status%2010"

        return TasmotaHttpClient(url, power_path.strip(), session)

    raise ValueError(f"Unsupported grid meter type: {meter_type}")


def _is_numeric(value):
    """Return True for power values Shelly reports as JSON numbers."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


_SHELLY_PHASE_KEYS = ("em1:0", "em1:1", "em1:2")
_SHELLY_CHANNEL_KEYS = {
    "a": "em1:0",
    "b": "em1:1",
    "c": "em1:2",
    "em1:0": "em1:0",
    "em1:1": "em1:1",
    "em1:2": "em1:2",
}


def _normalize_shelly_channels(channels):
    if channels is None:
        return None

    if not isinstance(channels, list):
        raise ValueError("Shelly channels must be a list")

    normalized = []
    for item in channels:
        value = str(item).strip().lower()
        if not value:
            raise ValueError("Shelly channels must not contain empty values")
        normalized.append(value)

    if not normalized:
        return None

    return normalized


def _parse_shelly_phase_sum(data):
    total = 0.0
    found = False
    for key in _SHELLY_PHASE_KEYS:
        meter = data.get(key)
        if not isinstance(meter, dict):
            continue

        value = meter.get("act_power")
        if _is_numeric(value):
            total += float(value)
            found = True

    if found:
        return total

    raise ValueError(
        "Unsupported Shelly status payload: missing numeric em1:* act_power values"
    )


def _parse_shelly_channel(data, channel, meter_key):
    meter = data.get(meter_key)
    if isinstance(meter, dict):
        value = meter.get("act_power")
        if _is_numeric(value):
            return float(value)

    raise ValueError(
        "Unsupported Shelly status payload: missing numeric "
        f"{meter_key}.act_power for channel {channel}"
    )


def _parse_shelly_power(data, channels=None):
    """Extract grid power from Shelly Pro 3EM status payloads."""

    if not isinstance(data, dict):
        raise ValueError("Unsupported Shelly status payload: expected object")

    channels = _normalize_shelly_channels(channels)
    if channels:
        for item in channels:
            if item not in _SHELLY_CHANNEL_KEYS:
                raise ValueError(f"Unsupported Shelly channel in channels: {item}")

        return sum(
            _parse_shelly_channel(data, item, _SHELLY_CHANNEL_KEYS[item])
            for item in channels
        )

    em = data.get("em:0")
    if isinstance(em, dict):
        value = em.get("total_act_power")
        if _is_numeric(value):
            return float(value)

    try:
        return _parse_shelly_phase_sum(data)
    except ValueError as e:
        raise ValueError(
            "Unsupported Shelly status payload: missing "
            "em:0.total_act_power or em1:* act_power values"
        ) from e


_SHELLY_3EM_GEN1_CHANNEL_INDEX = {
    "a": 0,
    "b": 1,
    "c": 2,
    "emeter:0": 0,
    "emeter:1": 1,
    "emeter:2": 2,
    "0": 0,
    "1": 1,
    "2": 2,
}


def _parse_shelly_3em_gen1_emeter_power(data, index, channel):
    emeters = data.get("emeters")
    if isinstance(emeters, list) and 0 <= index < len(emeters):
        meter = emeters[index]
        if isinstance(meter, dict):
            value = meter.get("power")
            if _is_numeric(value):
                return float(value)

    raise ValueError(
        "Unsupported Shelly 3EM Gen1 status payload: missing numeric "
        f"emeters[{index}].power for channel {channel}"
    )


def _parse_shelly_3em_gen1_power(data, channels=None):
    """Extract grid power from Shelly 3EM Gen1 /status payloads."""

    if not isinstance(data, dict):
        raise ValueError("Unsupported Shelly 3EM Gen1 status payload: expected object")

    channels = _normalize_shelly_channels(channels)
    if channels:
        for item in channels:
            if item not in _SHELLY_3EM_GEN1_CHANNEL_INDEX:
                raise ValueError(
                    f"Unsupported Shelly 3EM Gen1 channel in channels: {item}"
                )

        return sum(
            _parse_shelly_3em_gen1_emeter_power(
                data, _SHELLY_3EM_GEN1_CHANNEL_INDEX[item], item
            )
            for item in channels
        )

    total_power = data.get("total_power")
    if _is_numeric(total_power):
        return float(total_power)

    emeters = data.get("emeters")
    if isinstance(emeters, list):
        total = 0.0
        found = False
        for meter in emeters:
            if not isinstance(meter, dict):
                continue
            value = meter.get("power")
            if _is_numeric(value):
                total += float(value)
                found = True

        if found:
            return total

    raise ValueError(
        "Unsupported Shelly 3EM Gen1 status payload: missing numeric "
        "total_power or emeters[].power values"
    )


def _parse_ecotracker_power(data):
    """Extract grid power from everHome EcoTracker /v1/json payload."""

    if not isinstance(data, dict):
        raise ValueError("Unsupported EcoTracker payload: expected object")

    value = data.get("power")
    if _is_numeric(value):
        return float(value)

    raise ValueError("Unsupported EcoTracker payload: missing numeric power")


def _get_json_path(data, path):
    """Return nested JSON value from a dot-separated object path."""

    if not isinstance(path, str) or not path.strip():
        raise ValueError("JSON path must be a non-empty string")

    value = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"Missing JSON path: {path}")
        value = value[part]

    return value


def _parse_tasmota_http_power(data, power_path):
    """Extract grid power from a Tasmota HTTP JSON payload."""

    if not isinstance(data, dict):
        raise ValueError("Unsupported Tasmota payload: expected object")

    value = _get_json_path(data, power_path)
    if _is_numeric(value):
        return float(value)

    raise ValueError(f"Tasmota power path is not numeric: {power_path}")


def fetch_all_devices(devices):
    """Fetch all device states in parallel."""

    results = [None] * len(devices)

    if not devices:
        return results

    with ThreadPoolExecutor(max_workers=len(devices)) as executor:

        futures = {
            executor.submit(dev.fetch): i
            for i, dev in enumerate(devices)
        }

        for future in as_completed(futures):
            i = futures[future]

            try:
                results[i] = future.result()
            except:
                pass

    return results

# =====================
# CONTROL LOGIC
# =====================
