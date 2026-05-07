import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time
import logging
import json
import os
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

# =====================
# CONFIG LOADING
# =====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    with open(os.path.join(BASE_DIR, "config.json")) as f:
        CONFIG = json.load(f)
except FileNotFoundError:
    print("❌ config.json missing. Please create it from template.")
    exit(1)

# Extract configuration
SYSTEM_ENABLED = CONFIG["system"].get("enabled", True)
HA_ENABLED = CONFIG["ha"].get("enabled", True)

HA_URL = CONFIG["ha"]["url"]
HA_TOKEN = CONFIG["ha"]["token"]

MAX_TOTAL_POWER = CONFIG["system"]["max_total_power"]
MAX_DEVICE_POWER = CONFIG["system"]["max_device_power"]
DEADBAND = CONFIG["system"]["deadband"]
LOOP_INTERVAL = CONFIG["system"]["loop_interval"]

ZENDURE_CONFIG = CONFIG["devices"]
SHELLY_IP = CONFIG["shelly"]["ip"]

# =====================
# LOGGING
# =====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

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
            self.session.post(
                f"{self.base_url}/api/states/{entity_id}",
                headers=self.headers,
                json=payload,
                timeout=2
            )
        except Exception as e:
            logging.warning(f"HA write {entity_id}: {e}")

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

    def get_float(self, entity_id, default):
        """Return HA state as float or fallback."""

        val = self.get_state(entity_id)

        try:
            return float(val)
        except:
            return default

# =====================
# DATA MODEL
# =====================

@dataclass
class DeviceState:
    soc: float

    solar: float
    output: float

    pack_in: float
    pack_out: float

    temp: float
    voltage: float
    rssi: int

    remain_minutes: float

    solar1: float
    solar2: float
    solar3: float
    solar4: float

    output_limit: float

    fault_level: int

    ac_status: int
    dc_status: int
    grid_state: int

# =====================
# DEVICE PARSING
# =====================

def parse_device(data):
    """Extract relevant values from Zendure API response."""

    props = data.get("properties", {})

    return DeviceState(
        soc=props.get("electricLevel", 0),

        solar=props.get("solarInputPower", 0),
        output=props.get("outputHomePower", 0),

        pack_in=props.get("packInputPower", 0),
        pack_out=props.get("outputPackPower", 0),

        temp=round((props.get("hyperTmp", 0) / 100), 2),
        voltage=round((props.get("BatVolt", 0) / 100), 2),
        rssi=props.get("rssi", 0),

        remain_minutes=round((props.get("remainOutTime", 0) / 60), 1),

        solar1=props.get("solarPower1", 0),
        solar2=props.get("solarPower2", 0),
        solar3=props.get("solarPower3", 0),
        solar4=props.get("solarPower4", 0),

        output_limit=props.get("outputLimit", 0),

        fault_level=props.get("faultLevel", 0),

        ac_status=props.get("acStatus", 0),
        dc_status=props.get("dcStatus", 0),
        grid_state=props.get("gridState", 0),
    )

# =====================
# DEVICE CLIENTS
# =====================

class ZendureClient:
    """Client for a single Zendure device."""

    def __init__(self, name, ip, sn, session):
        self.name = name
        self.ip = ip
        self.sn = sn
        self.session = session

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

    def __init__(self, ip, session):
        self.ip = ip
        self.session = session
        self.last_value = 0

    def get_power(self):
        """Return current household power usage."""

        try:
            r = self.session.get(
                f"http://{self.ip}/rpc/Shelly.GetStatus",
                timeout=3
            )

            self.last_value = round(
                r.json()["em:0"]["total_act_power"],
                1
            )

        except:
            pass

        return self.last_value

# =====================
# PARALLEL FETCH
# =====================

def fetch_all_devices(devices):
    """Fetch all device states in parallel."""

    results = [None] * len(devices)

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

def calculate_targets(load, devices, max_power):
    """
    Calculate power targets per device.

    Strategy:
    1. Use solar first
    2. Distribute remaining load by SOC
    """

    current_total = sum(d.output for d in devices)
    solar_total = sum(d.solar for d in devices)

    new_total = max(
        0,
        min(max_power, current_total + load)
    )

    if solar_total >= new_total:

        targets = [
            new_total * (
                d.solar / solar_total
                if solar_total else
                1 / len(devices)
            )
            for d in devices
        ]

    else:

        targets = [d.solar for d in devices]

        remaining = new_total - solar_total

        soc_total = sum(d.soc for d in devices)

        for i, d in enumerate(devices):

            share = (
                d.soc / soc_total
                if soc_total else
                1 / len(devices)
            )

            targets[i] += remaining * share

    return [round(t) for t in targets], current_total, new_total

# =====================
# EMS CONTROLLER
# =====================

class EMSController:
    """Main EMS control loop."""

    def __init__(self, devices, shelly, ha=None):
        self.devices = devices
        self.shelly = shelly
        self.ha = ha

    def set_output_limit(self, dev, value):
        """Write output limit to device."""

        try:
            dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "acMode": 2,
                        "outputLimit": int(value),
                        "smartMode": 0
                    }
                },
                timeout=2
            )

            logging.info(f"WRITE {dev.name}: {value}W")

        except Exception as e:
            logging.warning(f"Write error {dev.name}: {e}")

    def publish_sensor(
        self,
        entity,
        value,
        unit=None,
        device_class=None,
        state_class="measurement",
        icon=None,
        extra=None
    ):
        self.ha.set_state(
            entity,
            value,
            unit=unit,
            device_class=device_class,
            state_class=state_class,
            icon=icon,
            extra_attributes=extra
        )

    def publish_to_ha(
        self,
        load,
        states,
        targets,
        current,
        new
    ):
        """Publish values to Home Assistant."""

        p = "sensor.ems_solarflow_"

        solar_total = sum(d.solar for d in states)
        pack_in_total = sum(d.pack_in for d in states)
        pack_out_total = sum(d.pack_out for d in states)

        battery_power = pack_out_total - pack_in_total
        home = current + max(load, 0)

        soc_avg = round(
            sum(d.soc for d in states) / len(states),
            1
        )

        # =====================
        # GLOBAL
        # =====================

        self.publish_sensor(
            p + "load",
            round(load, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "target_total",
            round(new, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "solar_total",
            round(solar_total, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "battery_power",
            round(battery_power, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "battery_charge",
            round(pack_in_total, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "battery_discharge",
            round(pack_out_total, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "home",
            round(home, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "soc_avg",
            soc_avg,
            "%",
            "battery"
        )

        # =====================
        # PER DEVICE
        # =====================

        for i, dev in enumerate(self.devices):

            d = states[i]

            base = p + dev.name.lower() + "_"

            # Core
            self.publish_sensor(
                base + "soc",
                d.soc,
                "%",
                "battery"
            )

            self.publish_sensor(
                base + "solar",
                d.solar,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "output",
                d.output,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "target",
                targets[i],
                "W",
                "power"
            )

            self.publish_sensor(
                base + "output_limit",
                d.output_limit,
                "W",
                "power"
            )

            # Zendure API uses controller/inverter perspective:
            # pack_out behaves like charging power
            # pack_in behaves like discharge power

            # Positive  = charging
            # Negative  = discharging
            
            device_battery_power = d.pack_out - d.pack_in

            self.publish_sensor(
                base + "battery_power",
                round(device_battery_power, 1),
                "W",
                "power"
            )

            self.publish_sensor(
                base + "voltage",
                d.voltage,
                "V",
                "voltage"
            )

            self.publish_sensor(
                base + "remaining_minutes",
                d.remain_minutes,
                "min",
                state_class=None,
                icon="mdi:timer-outline"
            )

            # Thermal / Signal
            self.publish_sensor(
                base + "temp",
                d.temp,
                "°C",
                "temperature"
            )

            self.publish_sensor(
                base + "rssi",
                d.rssi,
                "dBm",
                "signal_strength"
            )

            # Panels
            self.publish_sensor(
                base + "panel1",
                d.solar1,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "panel2",
                d.solar2,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "panel3",
                d.solar3,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "panel4",
                d.solar4,
                "W",
                "power"
            )

            # Status
            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_fault",
                "on" if d.fault_level > 0 else "off",
                device_class="problem",
                extra_attributes={
                    "fault_level": d.fault_level
                }
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_ac_active",
                "on" if d.ac_status else "off",
                device_class="power"
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_dc_active",
                "on" if d.dc_status else "off",
                device_class="power"
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_grid_online",
                "on" if d.grid_state else "off",
                device_class="connectivity"
            )

    def run_once(self):
        """Execute one EMS cycle."""

        start = time.time()

        load = self.shelly.get_power()

        # =====================
        # HA CONFIG
        # =====================

        if self.ha:

            max_power = self.ha.get_float(
                "input_number.ems_solarflow_max_power",
                MAX_TOTAL_POWER
            )

            val = self.ha.get_state(
                "input_boolean.ems_solarflow_enable"
            )

            enabled = (
                str(val).strip().lower() == "on"
            )

            interval = self.ha.get_float(
                "input_number.ems_solarflow_interval",
                LOOP_INTERVAL
            )

        else:

            max_power = MAX_TOTAL_POWER
            enabled = SYSTEM_ENABLED
            interval = LOOP_INTERVAL

        # =====================
        # FETCH STATES
        # =====================

        raw_states = fetch_all_devices(self.devices)

        states = [
            s if s else DeviceState(
                0, 0, 0, 0, 0,
                0, 0, 0,
                0,
                0, 0, 0, 0,
                0,
                0,
                0, 0, 0
            )
            for s in raw_states
        ]

        # =====================
        # CALCULATE TARGETS
        # =====================

        targets, current, new = calculate_targets(
            load,
            states,
            max_power
        )

        logging.info(
            f"Load={load}W "
            f"Target={new}W "
            f"Enabled={enabled}"
        )

        # =====================
        # PUBLISH TO HA
        # =====================

        if self.ha:
            self.publish_to_ha(
                load,
                states,
                targets,
                current,
                new
            )

        # =====================
        # APPLY CONTROL
        # =====================

        if enabled:

            for i, dev in enumerate(self.devices):

                target = targets[i]

                current_output = states[i].output

                if abs(target - current_output) < DEADBAND:
                    continue

                target = max(
                    0,
                    min(MAX_DEVICE_POWER, target)
                )

                self.set_output_limit(dev, target)

        # =====================
        # LOOP TIMING
        # =====================

        elapsed = time.time() - start

        time.sleep(max(0, interval - elapsed))

# =====================
# MAIN
# =====================

if __name__ == "__main__":

    session = create_session()

    ha = None

    if HA_ENABLED:
        ha = HAClient(
            HA_URL,
            HA_TOKEN,
            session
        )

    devices = [
        ZendureClient(
            d["name"],
            d["ip"],
            d["sn"],
            session
        )
        for d in ZENDURE_CONFIG
    ]

    shelly = ShellyClient(
        SHELLY_IP,
        session
    )

    ems = EMSController(
        devices,
        shelly,
        ha
    )

    logging.info("EMS started")

    while True:
        ems.run_once()