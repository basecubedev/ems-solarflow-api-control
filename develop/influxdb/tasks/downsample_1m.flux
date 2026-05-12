option task = {name: "zendure-downsample-1m", every: 1m}

numeric_fields = [
  "soc",
  "min_soc",
  "max_soc",
  "solar",
  "solar1",
  "solar2",
  "solar3",
  "solar4",
  "output",
  "output_limit",
  "pack_in",
  "pack_out",
  "voltage",
  "temp",
  "remain_minutes",
  "house_load",
]

state_fields = [
  "soc_limit",
  "pack_state",
  "fault_level",
  "smart_mode",
  "grid_off_mode",
  "ac_mode",
  "ac_status",
  "dc_status",
  "grid_state",
  "available",
  "pv_present",
  "output_active",
  "fault_active",
]

from(bucket: "zendure_raw")
  |> range(start: -task.every)
  |> filter(fn: (r) => r._measurement == "zendure_device" or r._measurement == "shelly_meter" or r._measurement == "ems_runtime")
  |> filter(fn: (r) => contains(value: r._field, set: numeric_fields))
  |> aggregateWindow(every: 1m, fn: last, createEmpty: false)
  |> to(bucket: "zendure_1m", org: "zendure-dev")

from(bucket: "zendure_raw")
  |> range(start: -task.every)
  |> filter(fn: (r) => r._measurement == "zendure_device" or r._measurement == "shelly_meter" or r._measurement == "ems_runtime")
  |> filter(fn: (r) => contains(value: r._field, set: state_fields))
  |> aggregateWindow(every: 1m, fn: last, createEmpty: false)
  |> to(bucket: "zendure_1m", org: "zendure-dev")
