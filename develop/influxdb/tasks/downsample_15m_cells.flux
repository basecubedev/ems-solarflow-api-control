// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
option task = {name: "zendure-downsample-15m:cells", every: 15m}

from(bucket: "zendure_raw")
  |> range(start: -task.every)
  |> filter(fn: (r) => r._measurement == "battery_pack" and r._field == "cell_min_v")
  |> aggregateWindow(every: 15m, fn: min, createEmpty: false)
  |> to(bucket: "zendure_15m", org: "zendure-dev")

from(bucket: "zendure_raw")
  |> range(start: -task.every)
  |> filter(fn: (r) => r._measurement == "battery_pack" and r._field == "cell_max_v")
  |> aggregateWindow(every: 15m, fn: max, createEmpty: false)
  |> to(bucket: "zendure_15m", org: "zendure-dev")
