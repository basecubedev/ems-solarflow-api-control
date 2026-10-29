// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const compiler = await import("svelte/compiler").catch(() => null);
const SKIP = !compiler && "node_modules not installed";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "../..");
const ROOTS = ["admin-modern/src", "appliance-modern/src", "dashboard-modern/src", "shared"];
const KEEPS_SPACES = new Set(["pre", "textarea"]);
const SET_ASIDE = new Set(["Comment", "SnippetBlock", "ConstTag", "DeclarationTag", "DebugTag", "SvelteHead", "SvelteWindow", "SvelteBody", "SvelteDocument", "SvelteOptions", "TitleElement"]);

function sources(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (name === "node_modules") return [];
    if (statSync(path).isDirectory()) return sources(path);
    return name.endsWith(".svelte") ? [path] : [];
  });
}

/**
 * Svelte 5 drops the whitespace at the start and end of every fragment (an
 * element's, a component's or a block's content, with comments, snippets,
 * tags such as {@const} and <svelte:head> set aside first), so a space
 * written there to set two words apart never reaches the page ("4 Jul,
 * 20:20·Rolling"). Such a space is written as {" "}; this lists the line of
 * every one still written as plain text. A run that holds a line break is
 * layout and is meant to go; <pre>, <textarea> and <title> keep their spaces.
 */
function trimmedSpaces(text) {
  const found = [];
  const lineOf = (offset) => text.slice(0, offset).split("\n").length;
  const edge = (nodes) => {
    const run = [];
    for (const node of nodes) {
      if (node.type !== "Text") break;
      run.push(node);
      if (/[^ \t\r\n]/.test(node.data)) break;
    }
    return run;
  };
  const check = (fragment) => {
    const nodes = fragment.nodes.filter((node) => !SET_ASIDE.has(node.type));
    const head = edge(nodes);
    const tail = edge([...nodes].reverse()).reverse();
    const lead = /^[ \t\r\n]*/.exec(head.map((node) => node.data).join(""))[0];
    const trail = /[ \t\r\n]*$/.exec(tail.map((node) => node.data).join(""))[0];
    if (lead && !lead.includes("\n")) found.push(lineOf(head[0].start));
    if (trail && !trail.includes("\n")) found.push(lineOf(tail[tail.length - 1].end));
  };
  const visit = (node, kept) => {
    const keeps = kept || KEEPS_SPACES.has(node.name) || node.type === "TitleElement";
    for (const value of Object.values(node)) {
      if (value?.type !== "Fragment") continue;
      if (!keeps) check(value);
      value.nodes.forEach((child) => visit(child, keeps));
    }
  };
  visit({ name: "", fragment: compiler.parse(text, { modern: true }).fragment }, false);
  return found;
}

test("the check finds a space at the edge of a fragment, and accepts an explicit one or a line break", { skip: SKIP }, () => {
  assert.deepEqual(trimmedSpaces("<span> · {a}</span>"), [1]);
  assert.deepEqual(trimmedSpaces("{a}{#if b} · {c}{/if}"), [1]);
  assert.deepEqual(trimmedSpaces("<span>{a} </span>"), [1]);
  assert.deepEqual(trimmedSpaces("<span><b>x</b> </span>next"), [1]);
  assert.deepEqual(trimmedSpaces("<span><!-- c --> {a}</span>"), [1]);
  assert.deepEqual(trimmedSpaces("<C>\t{a}</C>{#each xs as x}{x}  {/each}"), [1, 1]);
  assert.deepEqual(trimmedSpaces('<span>{" "}· {a}</span>{#if b}{" "}{c}{/if}'), []);
  assert.deepEqual(trimmedSpaces("<p>\n  {a} {b}\n</p>{#if b}{c} ·\n{/if}"), []);
  assert.deepEqual(trimmedSpaces("<C>{#snippet s()}x{/snippet} {a}</C>{#each xs as x}{@const y = x} {y}{/each}"), [1, 1]);
  assert.deepEqual(trimmedSpaces("<pre> x<span> y</span></pre><textarea> z</textarea><svelte:head><title> t</title></svelte:head>"), []);
  assert.deepEqual(trimmedSpaces("<span>\u00a0kWh</span>"), []);
});

test("no Modern page relies on a space Svelte drops at the edge of a fragment", { skip: SKIP }, () => {
  const offenders = ROOTS.flatMap((root) => sources(join(FRONTEND, root))).flatMap((path) => trimmedSpaces(readFileSync(path, "utf8")).map((line) => `${relative(FRONTEND, path)}:${line}`));
  assert.deepEqual(offenders, []);
});
