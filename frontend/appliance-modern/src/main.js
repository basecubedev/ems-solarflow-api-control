// SPDX-License-Identifier: AGPL-3.0-or-later
import "../../shared/styles/tokens.css";
import "../../shared/styles/base.css";
import "./styles.css";
import { mount } from "svelte";
import App from "./App.svelte";
import { applyDocumentPrefs, watchScreenWidth, watchSystemMotion } from "./state/prefs.svelte.js";
import { loadSession } from "./state/session.svelte.js";
import { watchHash } from "./app/router.svelte.js";

applyDocumentPrefs();
watchSystemMotion();
watchScreenWidth();
watchHash();
loadSession();

mount(App, { target: document.getElementById("app") });
