// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The preview's synthetic day is the last 24 hours, whatever the clock says, so
// a page opened just after midnight would show an empty day. pinTheDay pins the
// page's clock to 18:30 today, or to the instant it is given, and lays the
// synthetic day on that day and on the day before from midnight, the day
// before at 85 %, so every run sees the same day and the same comparison.
const SHOT_TIME = new Date();
SHOT_TIME.setHours(18, 30, 0, 0);

// The documentation captures go one step further: one fixed day in one zone,
// in the October the preview's energy figures run up to, and with no clock
// change on it or on the day before. playwright.dashboard.config.ts starts the
// preview server on the same instant (--now) and runs every process in
// DOCS_ZONE, so a capture shows the same content on any date and any host.
export const DOCS_ZONE = "Europe/Berlin";
export const DOCS_SHOT_TIME = "2026-10-21T18:30:00+02:00";

export async function pinTheDay(page, at: Date | string = SHOT_TIME) {
  const shot = new Date(at);
  await page.clock.install({ time: shot });
  const now = Math.floor(shot.getTime() / 1000);
  const midnight = Math.floor(new Date(shot.getFullYear(), shot.getMonth(), shot.getDate()).getTime() / 1000);
  const yesterday = Math.floor(new Date(shot.getFullYear(), shot.getMonth(), shot.getDate() - 1).getTime() / 1000);
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
