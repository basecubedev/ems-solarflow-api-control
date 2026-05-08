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
SOC_RECONCILE_INTERVAL = CONFIG["system"].get(
    "soc_reconcile_interval",
    10
)

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

    min_soc: float
    max_soc: float

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
    
    smart_mode: int
    grid_off_mode: int
    ac_mode: int
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

        remain_minutes=round(((props.get("remainOutTime") or 0) / 60), 1),

        solar1=props.get("solarPower1") or 0,
        solar2=props.get("solarPower2") or 0,
        solar3=props.get("solarPower3") or 0,
        solar4=props.get("solarPower4") or 0,

        output_limit=props.get("outputLimit") or 0,

        fault_level=props.get("faultLevel") or 0,

        smart_mode=props.get("smartMode") or 0,
        grid_off_mode=props.get("gridOffMode") or 2,
        ac_mode=props.get("acMode") or 0,
        ac_status=props.get("acStatus") or 0,
        dc_status=props.get("dcStatus") or 0,
        grid_state=props.get("gridState") or 0,
    )

# =====================
# DEVICE CLIENTS
# =====================


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
        grid_off_mode
    ):
        self.name = name
        self.ip = ip
        self.sn = sn
        self.session = session
        self.min_soc = min_soc
        self.max_soc = max_soc
        self.smart_mode = smart_mode
        self.grid_off_mode = grid_off_mode

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
    Intelligent EMS target calculation.

    Strategy:

    Solar surplus:
    - prioritize batteries with remaining charge capacity

    Battery discharge:
    - prioritize batteries with more usable SOC

    This avoids:
    - PV curtailment on full batteries
    - overusing nearly empty batteries
    """

    current_total = sum(d.output for d in devices)
    solar_total = sum(d.solar for d in devices)

    new_total = max(
        0,
        min(max_power, current_total + load)
    )

    targets = [0] * len(devices)

    # =====================
    # CASE 1:
    # Enough solar available
    # =====================

    if solar_total >= new_total:

        weights = []

        for d in devices:

            if d.max_soc <= 0:

                #
                # No battery:
                # direct solar priority
                #

                charge_headroom = 1

            else:

                charge_headroom = max(
                    1,
                    d.max_soc - d.soc
                )

            weight = d.solar / charge_headroom

            weights.append(weight)

        weight_total = sum(weights)

        for i, d in enumerate(devices):

            share = (
                weights[i] / weight_total
                if weight_total else
                1 / len(devices)
            )

            targets[i] = new_total * share

    # =====================
    # CASE 2:
    # Battery discharge required
    # =====================

    else:

        targets = [d.solar for d in devices]

        remaining = new_total - solar_total

        weights = []

        for d in devices:

            if d.max_soc <= 0:

                #
                # No battery available
                #

                usable_soc = 0

            else:

                usable_soc = max(
                    0,
                    d.soc - d.min_soc
                )

            weights.append(usable_soc)

        weight_total = sum(weights)

        for i, d in enumerate(devices):

            share = (
                weights[i] / weight_total
                if weight_total else
                1 / len(devices)
            )

            targets[i] += remaining * share

    targets = [
        max(0, min(MAX_DEVICE_POWER, round(t)))
        for t in targets
    ]

    return targets, current_total, new_total

# =====================
# EMS CONTROLLER
# =====================


class EMSController:
    """Main EMS control loop."""

    def __init__(self, devices, shelly, ha=None):
        self.devices = devices
        self.shelly = shelly
        self.ha = ha
        self.soc_reconcile_counter = SOC_RECONCILE_INTERVAL

        self.last_states = {}
        self.last_seen = {}
        self.device_online = {}

    def set_output_limit(self, dev, value):
        """Write output limit to device."""

        try:
            dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "outputLimit": int(value)
                    }
                },
                timeout=2
            )

            logging.info(f"WRITE {dev.name}: {value}W")

        except Exception as e:
            logging.warning(f"Write error {dev.name}: {e}")

    def apply_soc_limits(self, dev, state):
        """Apply configured SOC limits if required."""

        #
        # 0 = unmanaged
        #

        if dev.min_soc <= 0 and dev.max_soc <= 0:
            return

        #
        # Already configured
        #

        if (
            int(state.min_soc) == int(dev.min_soc)
            and
            int(state.max_soc) == int(dev.max_soc)
        ):

            logging.info(
                f"SOC LIMITS {dev.name}: already configured"
            )

            return

        try:

            dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "minSoc": int(dev.min_soc * 10),
                        "maxSoc": int(dev.max_soc * 10)
                    }
                },
                timeout=2
            )

            logging.info(
                f"SOC LIMITS {dev.name}: "
                f"min={dev.min_soc}% "
                f"max={dev.max_soc}%"
            )

        except Exception as e:

            logging.warning(
                f"SOC write error {dev.name}: {e}"
            )

    def apply_device_modes(self, dev, state):
        """Apply device operating modes if required."""

        #
        # unmanaged
        #

        if dev.smart_mode is None:
            return

        #
        # already configured
        #

        if (
            int(state.smart_mode) == int(dev.smart_mode)
            and
            int(state.ac_mode) == 2
            and
            int(state.grid_off_mode) == int(dev.grid_off_mode)
        ):

            logging.info(
                f"DEVICE MODES {dev.name}: already configured"
            )

            return

        try:

            dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "smartMode": int(dev.smart_mode),
                        "acMode": 2,
                        "gridOffMode": int(dev.grid_off_mode)
                    }
                },
                timeout=2
            )

            logging.info(
                f"DEVICE MODES {dev.name}: "
                f"smartMode={dev.smart_mode} "
                f"acMode=2 "
                f"gridOffMode={dev.grid_off_mode}"
            )
            
        except Exception as e:

            logging.warning(
                f"Device mode write error {dev.name}: {e}"
            )


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
                base + "min_soc",
                d.min_soc,
                "%",
                "battery"
            )

            self.publish_sensor(
                base + "max_soc",
                d.max_soc,
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
            # outputPackPower = charging
            # packInputPower  = discharging
            #
            # EMS convention:
            # Positive = charging
            # Negative = discharging

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

        states = []

        for dev, state in zip(self.devices, raw_states):

            #
            # Fresh state available
            #

            if state:

                self.last_states[dev.name] = state
                self.last_seen[dev.name] = time.time()
                self.device_online[dev.name] = True

                states.append(state)

                continue

            #
            # Fallback to last known state
            #

            if dev.name in self.last_states:

                self.device_online[dev.name] = False

                cached = self.last_states[dev.name]

                age = round(
                    time.time() - self.last_seen.get(dev.name, 0),
                    1
                )

                logging.warning(
                    f"{dev.name}: using cached state "
                    f"{age}s old "
                    f"(output={cached.output}W "
                    f"solar={cached.solar}W "
                    f"soc={cached.soc}%)"
                )

                states.append(cached)

                continue

            #
            # No valid state available
            #

            logging.error(
                f"{dev.name}: no valid state available"
            )

            self.device_online[dev.name] = False

            states.append(
                DeviceState(
                    0,  # soc
                    0,  # min_soc
                    0,  # max_soc
                    0,  # solar
                    0,  # output
                    0,  # pack_in
                    0,  # pack_out
                    0,  # temp
                    0,  # voltage
                    0,  # rssi
                    0,  # remain_minutes
                    0,  # solar1
                    0,  # solar2
                    0,  # solar3
                    0,  # solar4
                    0,  # output_limit
                    0,  # fault_level
                    0,  # smart_mode
                    0,  # grid_off_mode
                    0,  # ac_mode
                    0,  # ac_status
                    0,  # dc_status
                    0,  # grid_state
                )
            )

        #
        # Reconcile SOC limits
        #

        if SOC_RECONCILE_INTERVAL > 0:

            self.soc_reconcile_counter += 1

            if (
                self.soc_reconcile_counter
                >= SOC_RECONCILE_INTERVAL
            ):

                self.soc_reconcile_counter = 0

                for dev, state in zip(
                    self.devices,
                    raw_states
                ):

                    if state:

                        self.apply_soc_limits(
                            dev,
                            state
                        )

                        self.apply_device_modes(
                            dev,
                            state
                        )

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

        for i, dev in enumerate(self.devices):

            if not self.device_online.get(dev.name, True):

                logging.warning(
                    f"{dev.name}: offline -> skip write"
                )

                continue

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
            session,
            d.get("min_soc", 0),
            d.get("max_soc", 0),
            d.get("smart_mode", 1),
            d.get("grid_off_mode", 2)
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
