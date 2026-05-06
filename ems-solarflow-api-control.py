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

    def set_state(self, entity_id, state):
        """Write a sensor state to HA."""
        try:
            self.session.post(
                f"{self.base_url}/api/states/{entity_id}",
                headers=self.headers,
                json={"state": state},
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
                logging.warning(f"HA read {entity_id}: HTTP {r.status_code}")
                return None

            try:
                data = r.json()
            except Exception:
                logging.warning(f"HA read {entity_id}: invalid JSON")
                return None

            state = data.get("state") if isinstance(data, dict) else None
            logging.info(f"HA STATE {entity_id} -> {state}")

            return state

        except Exception as e:
            logging.warning(f"HA read {entity_id}: {e}")
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
    """Normalized device data."""
    soc: float
    solar: float
    output: float
    pack_in: float
    pack_out: float

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
            r = self.session.get(f"http://{self.ip}/properties/report", timeout=2)
            return parse_device(r.json())
        except:
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
            r = self.session.get(f"http://{self.ip}/rpc/Shelly.GetStatus", timeout=3)
            self.last_value = round(r.json()["em:0"]["total_act_power"], 1)
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
        futures = {executor.submit(dev.fetch): i for i, dev in enumerate(devices)}

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

    new_total = max(0, min(max_power, current_total + load))

    if solar_total >= new_total:
        # Pure solar distribution
        targets = [
            new_total * (d.solar / solar_total if solar_total else 1 / len(devices))
            for d in devices
        ]
    else:
        # Solar + battery contribution
        targets = [d.solar for d in devices]
        remaining = new_total - solar_total

        soc_total = sum(d.soc for d in devices)

        for i, d in enumerate(devices):
            share = d.soc / soc_total if soc_total else 1 / len(devices)
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

    def publish_to_ha(self, load, states, targets, current, new):
        """Publish calculated values to Home Assistant."""
        p = "sensor.ems_solarflow_"

        solar_total = sum(d.solar for d in states)
        pack_in_total = sum(d.pack_in for d in states)
        pack_out_total = sum(d.pack_out for d in states)

        battery_power = pack_out_total - pack_in_total
        home = current + max(load, 0)

        # Global values
        self.ha.set_state(p + "load", round(load, 1))
        self.ha.set_state(p + "target_total", round(new, 1))
        self.ha.set_state(p + "solar_total", round(solar_total, 1))
        self.ha.set_state(p + "battery_power", round(battery_power, 1))
        self.ha.set_state(p + "home", round(home, 1))
        self.ha.set_state(p + "battery_charge", round(pack_in_total, 1))
        self.ha.set_state(p + "battery_discharge", round(pack_out_total, 1))

        # Per-device values
        for i, dev in enumerate(self.devices):
            d = states[i]
            self.ha.set_state(p + f"{dev.name.lower()}_target", targets[i])
            self.ha.set_state(p + f"{dev.name.lower()}_solar", d.solar)
            self.ha.set_state(p + f"{dev.name.lower()}_output", d.output)

    def run_once(self):
        """Execute one EMS cycle."""
        start = time.time()

        load = self.shelly.get_power()

        # Read HA config values
        if self.ha:
            max_power = self.ha.get_float(
                "input_number.ems_solarflow_max_power",
                MAX_TOTAL_POWER
            )

            val = self.ha.get_state("input_boolean.ems_solarflow_enable")
            enabled = str(val).strip().lower() == "on"

            interval = self.ha.get_float(
                "input_number.ems_solarflow_interval",
                LOOP_INTERVAL
            )

            logging.info(f"HA enable raw='{val}' -> enabled={enabled}")
        else:
            max_power = MAX_TOTAL_POWER
            enabled = True
            interval = LOOP_INTERVAL

        # Fetch device data
        raw_states = fetch_all_devices(self.devices)
        states = [s if s else DeviceState(0, 0, 0, 0, 0) for s in raw_states]

        # Calculate targets
        targets, current, new = calculate_targets(load, states, max_power)

        logging.info(f"Load={load}W Target={new}W Enabled={enabled}")

        # Publish to HA
        if self.ha:
            self.publish_to_ha(load, states, targets, current, new)

        # Apply control
        if enabled:
            for i, dev in enumerate(self.devices):
                target = targets[i]
                current_output = states[i].output

                if abs(target - current_output) < DEADBAND:
                    continue

                target = max(0, min(MAX_DEVICE_POWER, target))
                self.set_output_limit(dev, target)

        # Maintain fixed loop timing
        elapsed = time.time() - start
        time.sleep(max(0, interval - elapsed))

# =====================
# MAIN
# =====================

if __name__ == "__main__":
    session = create_session()

    ha = HAClient(HA_URL, HA_TOKEN, session)

    devices = [
        ZendureClient(d["name"], d["ip"], d["sn"], session)
        for d in ZENDURE_CONFIG
    ]

    shelly = ShellyClient(SHELLY_IP, session)

    ems = EMSController(devices, shelly, ha)

    logging.info("EMS started")

    while True:
        ems.run_once()