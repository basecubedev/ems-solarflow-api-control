// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRouter } from "../../../shared/shell/router.svelte.js";
import { pageSet } from "./pages.js";

export const nav = createRouter(pageSet);
export const { router, href, watchHash } = nav;
