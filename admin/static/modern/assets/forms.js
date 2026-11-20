/* SPDX-FileCopyrightText: 2026 basecubedev */
/* SPDX-License-Identifier: AGPL-3.0-or-later */
function e(e){return[...e?.querySelectorAll?.(`input[type="number"]`)||[]].find(e=>e.validity?.badInput)||null}export{e as t};