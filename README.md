# Skein v0.1 — parallel task-graph orchestration for coding agents

Skein lets multiple coding agents claim and work nodes of a shared task
graph in parallel without stepping on each other, using git worktrees for
isolation. A human can inspect and edit the graph while agents run against
it.

How it works:

- The graph's true state is an **append-only event log**
  (`.skein/log.ndjson`), committed to git on every append. The "current
  graph" (`.skein/graph.json`) is derived from that log — never hand-edited.
- Agents **claim** a node before working it. A claim is a lease with a TTL,
  renewed by heartbeat; if the agent dies, a reaper releases the node back
  to `unclaimed` with a note saying the previous attempt did not complete.
- Each claimed node gets its **own git worktree**, branched from its
  dependency's branch (not always main). Multi-dependency nodes get an
  auto-created **integration node** that deterministically merges the parent
  branches first.
- Before a node flips to `done`, the supervisor **actually executes** the
  node's `intent.completion` command(s) in the worktree and records exit
  code + output as `evidence`. An agent's self-report is never sufficient.
- On completion the node produces a **handoff note** — a short structured
  summary dependents read instead of the parent's full transcript. This
  **bounds** context growth across a chain of nodes; it does not eliminate
  context degradation.

v0.1 ships exactly one backend adapter: **Claude Code**, invoked headlessly
(`claude --print`) against the node's worktree.

## Install

```bash
pip install -e .
```

This installs the `skein` entrypoint (requires Python ≥ 3.9 and git).

## Usage walkthrough

Run inside a git repo (or an empty directory — `skein init` runs
`git init` for you):

```bash
skein init

skein node add auth-3 --title "Add session login" \
  --goal "Users can log in with email+password and get a session." \
  --context "Python project, follow existing patterns in src/." \
  --constraints "No new dependencies." \
  --completion "python -c \"print('login check ok')\"" \
  --blast-radius "src/auth/**"

skein node add profile-1 --title "Profile page" \
  --goal "Logged-in users see their profile." \
  --context "Python project." \
  --constraints "No new dependencies." \
  --completion "python -c \"print('profile check ok')\"" \
  --depends-on auth-3 --blast-radius "src/profile/**"

skein graph
skein status

# claim a node (fails cleanly if dependencies aren't done or a
# blast-radius overlap exists with another claimed node)
skein claim auth-3 --agent-id agent-1
skein release auth-3   # release a claim (only claimed/in-progress nodes)

# full supervised path: claim (if needed) + worktree + backend adapter +
# verification gate + report. Backend defaults to the node's recorded
# backend (`--backend` on node add, `claude_code` if omitted). Requires
# the backend's CLI on PATH; for claude_code that means an authenticated
# `claude` CLI (subscription with Claude Code access, or
# ANTHROPIC_API_KEY). The adapter invokes it headlessly as
# `claude -p "<intent prompt>" --dangerously-skip-permissions
# --output-format text` inside the node's worktree
# (override the binary with SKEIN_CLAUDE_BIN for testing).
skein run auth-3 --agent-id agent-1

# inspect history; force-release is explicit, logged, and only for leases
# held by someone else
skein log --node auth-3
skein release auth-3 --force
```

While an agent runs, a human editing (or deleting) its node appends a
`human_interrupt` event and the supervisor terminates the agent process
gracefully instead of completing:

```bash
skein node edit auth-3 --title "New title"   # on a claimed node -> human_interrupt
```

Expired leases can be swept manually (`status`, `claim`, and `run` also
reap opportunistically):

```bash
skein reap
```

## Web canvas

`skein serve` starts a live graph view with human editing in the
browser (stdlib only, no extra install). It polls the event log, so
running agents show up as they claim nodes, and editing a claimed
node from the UI appends `human_interrupt` exactly like the CLI —
the supervisor stops that agent gracefully:

```bash
skein serve --host 127.0.0.1 --port 8765
# open http://127.0.0.1:8765/ -- view, add, edit, claim, release
```

The same surface is also a JSON API (`GET /api/graph`,
`GET /api/events?since=N`, `POST /api/nodes`,
`POST /api/nodes/<id>` with `{"delete": true}` supported,
`POST /api/nodes/<id>/claim`, `POST /api/nodes/<id>/release`).

## Blast-radius inference

`skein infer-blast` suggests globs from keywords in the node's
title/goal/context matched against tracked files. It is a heuristic
starting point, printed only — never applied. Apply explicitly:

```bash
skein infer-blast auth-3
skein node edit auth-3 --blast-radius "src/auth/**"
```

## Multi-machine sync

The event log is append-only, so concurrent appends on two machines
merge by line union and the timestamp-ordered reduction makes the
union safe. `skein sync` fetches, merges (auto-resolving log
conflicts by union, rebuilding the derived graph), and pushes.
Conflicts outside `.skein/` abort the merge for you to resolve:

```bash
skein sync
```

## Layout

- `src/skein/graph.py` — event log, reduction, node/edge model
- `src/skein/claim.py` — claim protocol, TTL/heartbeat, reaper
- `src/skein/worktree.py` — branch/base selection, integration node logic
- `src/skein/verify.py` — completion execution, evidence capture
- `src/skein/supervisor.py` — heartbeat + interrupt watch around the backend
- `src/skein/serve.py` + `static/app.html` — web canvas (stdlib HTTP + dependency-free UI)
- `src/skein/infer.py` — blast-radius suggestion heuristics
- `src/skein/sync.py` — multi-machine log sharing over git
- `src/skein/edits.py` — node mutations shared by CLI and web API
- `src/skein/adapters/` — `base.py` interface, `profiles.py` (backend
  profiles + registry), `engine.py` (generic profile-driven adapter),
  per-backend profile entries
- `src/skein/cli.py` — the CLI

## Backend support matrix

Backends are data profiles consumed by one generic engine — adding a
backend means adding a profile entry, not new orchestration code. Each
profile records whether it has been run against the real binary
(`verified`); anything marked `no` below is honest about that. This
table is generated from the registry (`skein backends list`), not
hand-maintained, so it cannot drift from reality:

<!-- skein:backends:begin -->
```
NAME          VERIFIED  INVOCATION                                                              SOURCE
aider         no        aider --message <prompt> --yes                                          Stage 8e proof: added with zero orchestration changes - headless shape (aider --message + --yes) from aider's documented non-interactive usage; not run against the real binary in this repo.
claude_code   yes       claude -p <prompt> --dangerously-skip-permissions --output-format text  Migrated from bespoke adapter; invocation validated against real claude CLI 2.1.248.
codex         no        codex exec --full-auto <prompt>                                         Invocation shape from OpenAI Codex docs (codex exec non-interactive mode); not yet run against the real binary in this repo. Upstream now prefers --sandbox workspace-write over --full-auto (deprecated compat flag); --json event output available via --adapter-args.
cursor_agent  no        cursor-agent -p --output-format stream-json <prompt>                    Invocation shape from the Stage 8 spec (cursor-agent -p --output-format stream-json); stream-json event schema assumed, binary not installed here - not yet run end-to-end in this repo.
gemini_cli    no        gemini -p <prompt> --yolo --output-format stream-json                   Invocation shape from the Stage 8 spec (gemini -p --yolo --output-format stream-json); stream-json event schema assumed, not yet run against the real binary in this repo.
opencode      no        opencode run --model <model> <prompt>                                   opencode run --model <provider/model> <prompt> per opencode 1.x CLI help (verified present locally); model has no default so backend_config['model'] is required. Binary present but zero provider credentials here - not yet run end-to-end in this repo.
```
<!-- skein:backends:end -->

Pick a backend per node (`--backend`, default `claude_code`) and pass
backend-specific parameters via repeatable `--backend-config key=value`
(e.g. `--backend opencode --backend-config model=openai/gpt-5`, since
OpenCode requires a model with no default):

```bash
skein node add task-1 --title "..." --goal "..." --completion "..." \
  --backend opencode --backend-config model=openai/gpt-5
skein backends list   # same matrix as above, from the live registry
```

### Bring your own backend: any CLI, no code changes

Skein is not exclusive to any vendor. Any headless-capable executable
— another agent CLI, a multi-agent router, a wrapper script, anything
that takes a prompt and works files in its cwd — becomes a backend
through a data profile. Model providers and orchestration platforms
plug in the same way, through whatever CLI fronts them (model choice
stays with the backend: `--backend-config model=...`, `--model ...`
via `--adapter-args`, etc.). A backend that cannot act on files
(a pure HTTP model endpoint with no agentic loop, for example) does
not fit the adapter contract — the backend does the work, Skein does
the worktree, lease, and evidence around it.

```bash
# minimal: `mycli <prompt>` (use --opt=value form for dash-leading tokens)
skein backends add --name mycli --binary mycli

# headless flags, approval bypass, required model key, JSON output
skein backends add --name myrouter --binary myrouter \
  --prompt-mode flag --headless=exec --headless=--full-auto \
  --required-config model --config-flag model=--model \
  --parser stream_json --source-note "team router v2"

skein backends list    # custom rows are labeled custom profile (repo|user:file)
skein backends remove myrouter   # built-ins cannot be removed
```

Profiles live as JSON in `.skein/backends/` (repo scope, shared with
the team through git) or `~/.config/skein/backends/` (user scope);
repo scope wins on name clash. Broken files are skipped with a
warning, never a crash. Custom profiles are unverified by default —
marking one verified is the owner's claim about their own binary,
stated in its source note.

## Backend note (proven against the real CLI)

- Proven with the real `claude` CLI binary (2.1.248) driving a real
  model: the headless invocation above is accepted, stdout/exit-code
  parsing works, heartbeats renew the lease mid-run (observed on
  23–40s invocations), and `skein run` completes end-to-end —
  claim → worktree → real file edit → verification gate → `done`
  with handoff note and evidence (completion + adapter logs).
  Note on method: this machine's Claude login is org-blocked for
  Claude Code, so proof ran the real binary against a throwaway
  localhost proxy translating to a free OpenRouter model. The proxy
  is harness-only (not shipped); the adapter and supervisor it
  exercised are the real code.
- Real failure modes observed: org-disabled 403 auth error and
  unrecognized-model error both record as `failed` with the CLI
  message in evidence — no hang, no crash. A dead backend endpoint
  makes the CLI retry for minutes (observed 201s), which is what the
  `--adapter-timeout` ceiling (default 1800s) bounds. Twice a weak
  model claimed "file created" without writing anything; the
  verification gate marked both `failed` — the gate working as
  designed, not a bug.
- What stub testing missed (all fixed): the real CLI needs
  `--dangerously-skip-permissions` for headless file work,
  `--output-format text` must be pinned for the stdout contract, the
  CLI calls `/v1/messages?beta=true`, sandboxed paths (e.g. Temp
  dirs) are denied with `FileSystem.access` — keep repos/worktrees
  in normal locations — and a stalled agent process needed the
  supervisor-side timeout ceiling.
- CI stays stub-only (`SKEIN_CLAUDE_BIN` override + in-repo fake
  adapter in tests) and needs no live credentials.

## Scope honesty

v0.1 was the CLI core with one backend. Stage 8 added the
profile-driven backend engine above: five profiles plus one
Stage-8e proof profile, of which only `claude_code` (and the codex
stub path) have run end-to-end — the rest are honestly marked
unverified, and any CLI is addable without code changes. The web
canvas (`skein serve`) covers live visualization and browser editing.
Still no static blast-radius proof — `infer-blast` suggests, humans
decide — and `blast_radius` overlap itself is checked heuristically.

## Development

```bash
pip install -e ".[test]"
python -m pytest
```
