// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRouter } from "../../../shared/shell/router.svelte.js";
import { legacyPage, pageSet } from "./pages.js";

export const nav = createRouter(pageSet, { alias: legacyPage });
export const { router, href, watchHash } = nav;
