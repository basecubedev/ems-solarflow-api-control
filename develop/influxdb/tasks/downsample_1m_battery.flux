// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
option task = {name: "zendure-downsample-1m:battery", every: 1m}

from(bucket: "zendure_raw")
  |> range(start: -task.every)
  |> filter(fn: (r) => (r._measurement == "battery_pack" and (r._field == "temp_c" or r._field == "soc")) or (r._measurement == "zendure_device" and (r._field == "charge_avail_w" or r._field == "discharge_avail_w" or r._field == "input_limit_w")))
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> to(bucket: "zendure_1m", org: "zendure-dev")

from(bucket: "zendure_raw")
  |> range(start: -task.every)
  |> filter(fn: (r) => (r._measurement == "zendure_device" and (r._field == "heat_state" or r._field == "low_temp" or r._field == "error_flag" or r._field == "derate" or r._field == "winter_hold" or r._field == "ac_input")))
  |> aggregateWindow(every: 1m, fn: max, createEmpty: false)
  |> to(bucket: "zendure_1m", org: "zendure-dev")

from(bucket: "zendure_raw")
  |> range(start: -task.every)
  |> filter(fn: (r) => (r._measurement == "zendure_device" and (r._field == "winter_target")))
  |> aggregateWindow(every: 1m, fn: last, createEmpty: false)
  |> to(bucket: "zendure_1m", org: "zendure-dev")
