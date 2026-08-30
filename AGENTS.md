<!-- project-rules:start -->
# Mandatory Project Rules

Before planning, editing or committing, read
[`docs/developer/agent-rules.md`](docs/developer/agent-rules.md). That file is
the canonical project rule set. Tool-specific sections below supplement it and
never replace it.
<!-- project-rules:end -->

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **ems-solarflow-api-control** (32450 symbols, 77920 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/ems-solarflow-api-control/context` | Codebase overview, check index freshness |
| `gitnexus://repo/ems-solarflow-api-control/clusters` | All functional areas |
| `gitnexus://repo/ems-solarflow-api-control/processes` | All execution flows |
| `gitnexus://repo/ems-solarflow-api-control/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

# Serena — Language-Server Intelligence

Serena runs a Python language server over this repo. Register it in your agent's
own MCP config with `serena start-mcp-server --project-from-cwd`, which activates
the project from the working directory — no `activate_project` call per session,
and no project pinned across repos. Choose the `--context` that matches your agent
(`claude-code`, `codex`, `copilot-cli`, … — `serena start-mcp-server --help` lists
them); the context only shapes the prompt and the exposed tool set, not the
analysis.

Serena's state lives in `.serena/`, git-excluded alongside `.gitnexus/` via
`.git/info/exclude`. The notes under `.serena/memories/` are agent-neutral: they
are read by whichever agent connects, so keep them free of agent-specific detail.
Entry point is `core`; `memory_maintenance` defines their style rules.

Serena and GitNexus overlap on symbol lookup but are not interchangeable:

- **GitNexus answers "what does this touch"** — call graph, execution flows, blast
  radius, risk. It reads a snapshot written by the last `analyze` run, so it lags
  behind edits made in the current session, silently.
- **Serena answers "what is this right now"** — the language server always reflects
  the working tree, including edits made a minute ago.

## Which tool for what

| Question | Tool |
|---|---|
| Blast radius before an edit, risk level | GitNexus `impact` — mandatory, see above |
| Execution flows, call chains, "how does X work" | GitNexus `query` / `context` |
| Scope check before committing | GitNexus `detect_changes` |
| Taint / source→sink findings | GitNexus `explain` |
| Current body or signature of a symbol | Serena `find_symbol` with `include_body` |
| Structure of a file not yet read | Serena `get_symbols_overview` |
| Callers/usages, resolved through the type system | Serena `find_referencing_symbols` |
| Replace a whole function, method or class | Serena `replace_symbol_body` |
| Type/syntax errors after an edit | Serena `get_diagnostics_for_file` |
| Config, docs, JSON/YAML, a few lines at a known path | built-in Read / Grep / Edit |

## Rules that follow from the split

- The GitNexus `impact`-before-edit mandate stands unchanged. Serena does not
  replace it — a language server has no notion of blast radius.
- After editing a symbol in this session, trust Serena over GitNexus for that
  symbol's contents until the index has been re-analyzed. When the two disagree
  about what the code says, the language server is newer.
- Do not read a whole module to find one function. `get_symbols_overview`, then
  `find_symbol`. `ems/controller.py` and `emsctl.py` are large enough that this
  is the difference between a page and a wall of context.
- Never rename by find-and-replace. Serena `rename_symbol` is language-server-exact;
  GitNexus `rename` is call-graph-aware. Run `impact` first either way.
- Both toolsets are read-mostly and safe to use freely. Neither is a substitute for
  the safety rules in `CLAUDE.md` "Safety Model" — no tool output authorizes a
  write-gate change.
