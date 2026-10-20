// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The preview's synthetic day is the last 24 hours, whatever the clock says, so
// a page opened just after midnight would show an empty day. pinTheDay pins the
// page's clock to 18:30 today and lays the synthetic day on today and on
// yesterday from midnight, yesterday at 85 %, so every run sees the same day
// and the same comparison.
const SHOT_TIME = new Date();
SHOT_TIME.setHours(18, 30, 0, 0);

export async function pinTheDay(page) {
  await page.clock.install({ time: SHOT_TIME });
  const now = Math.floor(SHOT_TIME.getTime() / 1000);
  const midnight = Math.floor(new Date(SHOT_TIME.getFullYear(), SHOT_TIME.getMonth(), SHOT_TIME.getDate()).getTime() / 1000);
  const yesterday = Math.floor(new Date(SHOT_TIME.getFullYear(), SHOT_TIME.getMonth(), SHOT_TIME.getDate() - 1).getTime() / 1000);
  await page.route("**/api/history/series**", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const first = body.time[0];
    const time = [];
    const series = Object.fromEntries(Object.keys(body.series).map((id) => [id, []]));
    for (const [start, share] of [[yesterday, 0.85], [midnight, 1]]) {
      body.time.forEach((stamp, index) => {
        const at = start + (stamp - first);
        if (at > now) return;
        time.push(at);
        for (const id of Object.keys(series)) {
          const value = body.series[id][index];
          series[id].push(value === null ? null : value * share);
        }
      });
    }
    await route.fulfill({ response, json: { ...body, time, series } });
  });
}
