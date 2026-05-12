option task = {name: "zendure-downsample-15m", every: 15m}

tracked_fields = [
  "soc",
  "solar",
  "output",
  "output_limit",
  "pack_in",
  "pack_out",
  "house_load",
  "soc_limit",
  "pack_state",
  "fault_level",
  "ac_status",
  "dc_status",
  "grid_state",
]

from(bucket: "zendure_raw")
  |> range(start: -task.every)
  |> filter(fn: (r) => r._measurement == "zendure_device" or r._measurement == "shelly_meter")
  |> filter(fn: (r) => contains(value: r._field, set: tracked_fields))
  |> aggregateWindow(every: 15m, fn: last, createEmpty: false)
  |> to(bucket: "zendure_15m", org: "zendure-dev")
