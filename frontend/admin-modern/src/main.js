// SPDX-License-Identifier: AGPL-3.0-or-later
import "../../shared/styles/tokens.css";
import "../../shared/styles/base.css";
import { mount } from "svelte";
import App from "./App.svelte";
import { applyDocumentPrefs, rememberModern, watchScreenWidth, watchSystemMotion } from "./state/prefs.svelte.js";
import { auth, loadAuth, watchActivity, watchSession } from "./state/auth.svelte.js";
import { startPolling } from "./state/system.svelte.js";
import { guardLeaving } from "./state/backupJobs.svelte.js";
import { serviceAction } from "./state/services.svelte.js";
import { watchHash } from "./app/router.svelte.js";

applyDocumentPrefs();
watchSystemMotion();
watchScreenWidth();
rememberModern();
watchHash();
loadAuth();
watchSession();
watchActivity();
startPolling(() => auth.authenticated);
guardLeaving(() => Boolean(serviceAction.busy));

mount(App, { target: document.getElementById("app") });
