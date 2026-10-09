# Changelog

## Unreleased (usability + gate hardening)

- Fix: repeating `--depends-on` / `--blast-radius` on `skein node add|edit`
  silently kept only the last value (dropping dependencies). Both flags
  are now repeatable and comma-separated.
- Fix: change-policy checks saw a newly created directory as `dir/`
  (`git status` collapses untracked dirs), so any node creating a new
  directory was flagged out of blast radius and rejected under
  `--change-policy strict`. Untracked files are now listed individually.
- Fix: Python completion checks no longer leave `__pycache__/` in the
  worktree (it was committed into result commits and shipped).
- New: `@nonce` completion directive. The gate exports a random
  `SKEIN_GATE_NONCE` and requires it on stdout, so solutions that exit 0
  before any assertion runs are rejected.
- Tests: loopback HTTP in the serve tests now bypasses `HTTP(S)_PROXY`
  (19 tests failed on hosts with a proxy configured).

## Unreleased (Phase 8: planner and automatic DAG generation)

- `skein plan` turns a goal description into a reviewable DAG draft
  (`src/skein/planner.py`). Planning is read-only: nothing touches the
  graph until `skein plan --apply`. Drafts are versioned JSON
  (`{"version": 1, ...}`); unknown versions are refused.
- Heuristic brain (default, no network, no LLM): parses markdown task
  lists deterministically. Numbered lists become sequential chains
  (including `1. a 2. b 3. c` written inline on one line); bulleted
  lists under a heading become parallel nodes with headings ordered
  sequentially (each stage depends on all nodes of the previous
  stage); explicit "after X" / "depends on X" / "once X is done" /
  "following X" hints become edges (parenthesized hints included).
  Unmatched hints warn instead of failing. Dependency cycles are
  refused with the cycle path. A prose-only goal yields a single node.
- LLM brain (`--llm`): pipes a JSON spec (goal, source text, existing
  node titles/statuses, tracked files) to the executable named by
  `SKEIN_PLANNER_CMD` and validates the returned draft (schema
  version, node ids, dep targets, no cycles, no id reuse). A missing,
  failing, or misbehaving command is a hard error: it never silently
  falls back to the heuristic, so the operator always knows which
  brain produced the plan.
- Draft review loop: `skein plan` prints the draft DAG and saves it to
  `.skein/plan-<ts>.json` (goal text redacted at the write boundary,
  like all stored content); `--dry-run` prints without saving;
  `--list` lists saved drafts; `skein plan --apply [draft]` (default:
  latest) creates nodes through the validated `add_node` path.
  Applying is idempotent: the draft's semantic hash (excluding
  `created_at`) is recorded in a `plan_applied` event anchored at the
  reserved `skein-plan` id (informational, like shipping's `release`);
  a second apply errors unless `--force`, which skips already-existing
  nodes so partially applied plans can be resumed.
- `skein plan --from-git-log [--commits N]` builds a sequential draft
  from recent commit subjects (oldest first).
- Each draft node gets a generated completion prompt template from its
  task text and the goal; node ids are slugified from titles with
  `-2`/`-3` disambiguation and always pass id validation.

## Unreleased (Phase 7: UI and observability)

- Lifecycle rejections are now visible: fenced mutations that raise
  (`claim`, `heartbeat`, `release`, `complete`, `fail`) record an
  informational `rejected` event (op + reason, never secrets) via a
  decorator in `claim.py`. `apply_event` derives only a per-node
  `rejected_count`, so the fencer and state machine are untouched.
  Surfaces in `skein log --rejected`, the new `REJ` column in
  `skein status`, and the timeline UI.
- Web observability in `serve.py` (server-rendered HTML, no new
  dependencies): `/timeline` (reverse-chronological event timeline
  with `?node=` and `?type=` filters), `/node/<id>` (status, claim,
  attempt history, retry policy, worktree, result, shipped state,
  handoff, evidence), `/api/metrics` (JSON: nodes by status, events,
  attempts by outcome, pending retries, shipped, uptime, per-backend
  done/failed), `/metrics` (Prometheus text with `skein_*` gauges),
  `/api/health` (git/log/worktree writability, version).
- CLI observability: `skein status --watch` (2s refresh until Ctrl-C),
  `skein log --tail N --follow --rejected`, and `skein doctor`
  (read-only: git, log parseability, worktree root, orphaned
  worktrees via the extracted `find_orphaned_worktrees()`, expired
  leases, config validity; exit 0/1 with one-line fix hints).
- `gc_worktrees()` was refactored to share its scan with doctor
  through `find_orphaned_worktrees()`; removal behavior unchanged.

## Unreleased (Phase 6: security and sandboxing)

- Secret redaction at write boundaries (`src/skein/redact.py`):
  `redact_secrets()` masks AWS keys (`AKIA...`), GitHub tokens
  (`ghp_`/`gho_`), `sk-`/`sk-ant-` API keys, `xoxb-`/`xoxp-` Slack
  tokens, PEM private-key blocks, and `password=`/`api_key=`
  assignments. Applied at three boundaries so nothing unredacted
  reaches `.skein/log.ndjson` or `.skein/evidence/`: the event-log
  payload writer (`append_event` redacts every payload), evidence file
  writers (verification evidence and supervisor adapter evidence), and
  therefore handoff notes (they flow through the payload writer).
  Redaction is idempotent, leaves ordinary prose mentioning
  "password"/"secret" untouched, and already-stored data is never
  mutated.
- Backend environment scrubbing: `runtime.execute()` takes an `env`
  parameter; backend runs (supervisor and `ProfileAdapter.run()`) now
  start from `minimal_environ()` (PATH, HOME, LANG, `SKEIN_*`, plus
  SystemRoot on Windows) instead of inheriting the ambient environment.
- `skein run --sandbox` (also `default_sandbox` in config.json):
  scrubs the backend env and, on Linux, wraps the backend in
  `prlimit(1)` caps (600s CPU, 8 GiB address space). Best-effort: when
  prlimit is missing or the platform is not Linux, the run continues
  unsandboxed with a note, never fails. Missing binaries still report
  exit 127 with the real binary name.
- `skein serve` hardening: `--auth-token` (or `SKEIN_AUTH_TOKEN`); when
  set, mutating requests (POST/PUT/DELETE) require
  `Authorization: Bearer <token>` (401 otherwise, constant-time
  comparison) while GETs stay open. 1 MB request body cap (413),
  best-effort per-IP rate limit of 60 requests/minute (429). Binding
  0.0.0.0 without a token is refused with a clear error. Failed auth
  attempts are audited as `security` events without logging the
  presented credential.
- `security` event type: informational (no node state derived, bypasses
  the lifecycle fencer like `shipped`/`release`). Recorded for
  redaction hits (count per event, never the secret), sandbox
  fallbacks, and serve auth failures; visible in `skein log`.
- Known tradeoff (documented): the env allowlist means backends that
  relied on ambient credentials (e.g. `ANTHROPIC_API_KEY`) must receive
  them via `SKEIN_`-prefixed variables mapped by their wrapper; and a
  literal secret baked into `backend_config` or a completion command is
  redacted at the write boundary, so backend auth belongs in the
  environment, not in node intent.

## Unreleased (Phase 5: integration and shipping lifecycle)

- `skein ship <node-id> [--to <branch>] [--ff-only] [--force]`: merges a
  done node's recorded result commit into the target branch (default:
  the current branch). Refuses nodes that are not done, have no result
  record, or whose result commit object is gone (`BaseCommitUnavailable`,
  never a dead ref). Default is a `--no-ff` merge commit
  `skein: ship <id> (<short-sha>)` with skein-local commit identity;
  `--ff-only` fails unless the target fast-forwards. The merge runs in
  the live checkout when the target is checked out, otherwise in a
  throwaway worktree, and is aborted on conflict (conflicting files are
  reported, no half-merged state). A `shipped` event records node,
  result commit, target branch, merge commit, and divergence info.
  Idempotent: an already-ancestor result reports "already shipped" with
  no new commit and no event.
- Divergence guard: the target is compared against the result's
  recorded base by tree diff outside `.skein` (control-plane commits
  are ignored, otherwise every ship would warn). Real movement requires
  `--force`, recorded as `diverged`/`forced` on the event.
- `skein ship --all [--to <branch>]`: ships every done node with a
  result record in dependency order (integration nodes last), with a
  per-node table (shipped / already-shipped / skipped with reason). A
  node whose dependency did not ship is skipped with
  "dependency <id> not shipped". Within one run the divergence guard is
  relaxed (each ship legitimately moves the target for the next); merge
  conflicts still abort per node and are reported as skips.
- Integration conflict surfacing: when the `__integrate` auto-merge
  conflicts, the node is parked at `needs_human` via `human_interrupt`
  (claim cleared atomically) with the conflicting file list in the
  event payload and the handoff note, instead of silently releasing
  back to unclaimed. `skein status` shows the note for `needs_human`
  nodes, and the child stays blocked naming the parked node.
- `skein release <tag> [--message] [--allow-unshipped]`: creates an
  annotated tag on the target branch HEAD and appends a `release` event
  (repo-level, anchored at the reserved `skein-release` id) with the
  tag, head, and nodes shipped since the previous release. Refuses a
  dirty working tree (outside `.skein`), duplicate or invalid tag names
  (must match `^[A-Za-z0-9._-]+$`), and unshipped done nodes unless
  `--allow-unshipped`. A done node whose result commit is already an
  ancestor of the target (e.g. landed via a manual conflict resolution
  after `ship` aborted) does not block the release. Only merge commits
  and tags are created; history is never rewritten.
- The existing `skein release <id>` (claim release) is unchanged when
  the target names a live node; anything else is treated as a tag name.
  A tag colliding with a live node id always takes the claim path.
- Ship/release state rides on the event log: `shipped` events derive
  `node["shipped"]` per target branch via `reduce_events` (shown in the
  new SHIPPED column of `skein status`); `release` events derive no
  node state and are listed by scanning the log. The web API exposes
  the same functions: `POST /api/nodes/<id>/ship`,
  `POST /api/release`, and read-only shipped state in `/api/graph`.

## Unreleased (Phase 4: scheduler and retries)

- Per-node retry policy: `skein node add --max-retries N
  --retry-backoff-seconds S` stores the policy on the node
  (`default_max_retries` / `default_retry_backoff_seconds` from
  config.json when omitted; 0 means no retries). Editable via
  `skein node edit` and through the web API (same validation as the
  CLI). The previously dead `claim_node(max_retries=...)` parameter is
  removed; the policy lives on the node record, not the claim call.
- Retryable failure: `fail_node` parks the node at `unclaimed` with
  `retry_at` set to now + backoff while failures used are below
  `max_retries`; only exhausted retries park at `failed`. Backoff is
  exponential (base * 2^failures, +/-25% jitter), computed once at fail
  time and recorded on the failed event so reduction stays
  deterministic. Timeouts (exit 124) and verification failures retry;
  human interrupts and stale-token violations never reach the fail
  path. `eligibility()` reports "backoff until <ts>" while the deadline
  is in the future.
- First-class attempt history: every claim -> outcome cycle appends an
  entry (attempt_id, holder, started_at, ended_at, outcome, error) to
  `node["attempts"]`, derived entirely in `reduce_events` and bounded
  to the last 20 entries. Outcomes recorded: done, failed, timeout,
  released, reaped, interrupted.
- `skein run --all`: multi-node scheduler loop. Each pass claims and
  runs up to `--max-parallel` eligible nodes sequentially in one
  process (default 2), re-scans, and stops when no eligible nodes
  remain or `--max-nodes` is hit. When nodes are only waiting out a
  retry backoff, the scheduler waits for the earliest deadline instead
  of dropping the retries. Ends with a node/outcome/attempts summary
  table plus a not-run list with reasons. Exit codes mirror single
  runs: 0 ok, 2 failed, 3 interrupted.
- `skein status` shows retry state: a TRIES column with the attempt
  count, and `backoff Ns` / `retry due` in the lease column for nodes
  waiting out a backoff. The web `/api/graph` payload carries the same
  fields (`max_retries`, `retry_backoff_seconds`, `retry_at`,
  `attempts_used`, `attempts`).

## Unreleased (Phase 3: worktree and result-commit model)

- Node deletion owns the worktree lifecycle: `skein node delete <id>`
  removes the node's registered worktree through the same `edit_node`
  rule the web canvas uses (the previously dead `remove_worktree()` is
  now wired in). The branch is kept by default; `--delete-branch`
  removes it too. Deleting a claimed or in-progress node still records
  `human_interrupt` and parks the claim instead of yanking the worktree.
- `skein worktree gc` reclaims orphaned and stale worktrees: registered
  worktrees with no live node, worktrees whose branch ref is gone, and
  unregistered directories under `.skein/worktrees`. Claimed and
  in-progress worktrees are never touched, and the main repository
  working tree is explicitly refused.
- Base pinning on worktree reuse: `ensure_worktree()` records the
  resolved `base_commit` SHA in the node's worktree record. Reuse is
  refused when the worktree sits on a different base (HEAD is neither
  the pinned base nor a descendant of it), when the recorded base
  object is gone, or when a pinned-SHA base (a dependency result) no
  longer matches because the dependency re-ran. The error directs the
  operator to `skein worktree gc` or a node reset. The existing
  branch-mismatch refusal is unchanged.
- `base_branch_for()` no longer trusts recorded result SHAs blindly: a
  dependency result commit whose object is absent from the store now
  raises `BaseCommitUnavailable` instead of handing downstream nodes a
  dead base. Legacy dependency branches are resolved before use too.
- Result introspection: `skein result show <node-id>` prints the result
  record (base/result commits, attempt, changed files, diff stats) and
  `skein result verify <node-id>` checks it against the object store:
  result and base objects exist, the result descends from the base, and
  the recorded diff stats match a recomputed numstat. A missing result
  record is an error, not an empty pass.

## Unreleased (Phase 2: canonical execution runtime)

- One execution path: `runtime.execute()` is now the single
  spawn/drain/kill implementation used by the supervisor, the verifier,
  and `ProfileAdapter.run()`. Separate stdout/stderr capture feeds the
  backend's exact `(stdout, stderr, exit_code)` triple to its output
  parser, so supervised evidence matches `adapter.run()` byte for byte.
- The supervisor no longer bypasses parsers: stream-json backends
  (gemini, cursor) now record harvested text as evidence instead of raw
  event JSON. Legacy adapters without a profile keep the stdout plus
  stderr-trailer shape.
- Missing backend binary is exit 127 through every path, never an
  uncaught `FileNotFoundError` (the old supervisor `Popen` call could
  crash `run_node` outright).
- Verification is cancellable: `run_completion()` takes `should_abort`
  and the supervisor wires its interrupt check in, so a human interrupt
  kills a running completion command's process tree and stops the rest.
- Windows process-tree kill is real now: `taskkill /PID /T /F` replaces
  the old best-effort direct-child terminate; tree-kill tests use
  forward-slash pidfile paths (the old `r'C:\...'` embedding was a
  `SyntaxError` on Windows via the `\U` escape).

## Unreleased (Phase 1: correctness and state-machine invariants)

- Fenced claims: every claim mints an immutable `attempt_id` and
  `claim_token`; heartbeat, release, complete, and fail must present the
  live token. Stale attempts (reaped, force-released, superseded) can no
  longer complete, heartbeat, or revive a lease. Tokenless legacy
  lifecycle events are rejected at append and during reduction.
- Human interrupts clear ownership atomically and park the node at
  `needs_human`; the supervisor no longer force-releases after an
  interrupt (which used to drag the node back to `unclaimed`).
- Reaper releases are scoped to the exact attempt examined
  (`reaped_attempt_id`) and carry the decision time separately
  (`reaped_as_of`), so a delayed duplicate release cannot wipe a newer
  claim that landed in between.
- Durable result commits: verified work is checkpointed as a git commit
  recording base, result, changed files, diff stats, and attempt id.
  Downstream worktrees branch from the dependency's result commit, never
  from an ambient branch. Missing base refs raise
  `BaseCommitUnavailable` instead of silently substituting the current
  branch.
- Integration is now a real fenced attempt: `__integrate` nodes are
  claimed by `skein-auto-merge`, merge in a temporary worktree, verify,
  and record a durable result commit. Integration nodes are exempt from
  recursively requiring their own `__integrate` node.
- Event log hardening: UUID event ids, schema version, per-repo
  sequences, fsync on append, malformed/truncated NDJSON recovery,
  atomic snapshot replacement, deterministic reduction ordering, and a
  snapshot digest that binds both the log and the generated graph (a
  corrupted graph.json is rebuilt, not trusted).
- Repository-wide lock moved to `.git/skein.lock` so it can never be
  staged through `.skein`; heartbeat git commits are off by default;
  Skein git commits use command-local identity flags and never touch
  the user's git config.
- Execution runtime hardening (`src/skein/runtime.py`): backend stdout
  is drained continuously (the old read-after-exit design deadlocked
  past ~64KB), output is capped at 1MB with truncation noted, and
  aborts/timeouts kill the whole process tree via process groups on
  POSIX. Verification commands run through the same bounded runner.
- New regression suites: `tests/test_fencing.py` (fencing, reaping,
  lifecycle, graph integrity) and `tests/test_runtime.py` (deadlock,
  output caps, tree kill). 99 tests green.

## Unreleased (Stage 8: multi-backend adapter engine)

- New profile-driven backend engine: `BackendProfile` data schema +
  generic `ProfileAdapter`; adding a backend is now a profile entry,
  not new orchestration code. No router dependency, no claim/event/
  worktree behavior changes.
- Profiles: `claude_code` (migrated byte-identical, verified),
  `codex` (stub-verified e2e, real binary absent), `gemini_cli`,
  `opencode`, `cursor_agent`, `aider` (mock-verified, honestly
  unverified). Per-node `--backend` + repeatable `--backend-config
  key=value` (e.g. opencode's required `model`); `--backend` run
  override;   `skein backends list` support matrix generated from the
  registry and lockstep-tested against the README.
- Bring-your-own-backend: `skein backends add/remove` registers any
  CLI as a JSON data profile (repo or user scope), so new backends
  need no code changes; broken files warn-and-skip, built-ins are
  unremovable.
- Tests only (no behavior change): ASCII guard over shipped sources
  (cp1252 pattern), pin that log commits contain only `.skein` paths.

## 0.2.0 — completion pass

- Web canvas: `skein serve` (stdlib HTTP + embedded SVG UI, live
  polling, add/edit/claim/release, same human_interrupt rules as CLI).
- Blast-radius inference: `skein infer-blast` heuristic suggestions,
  print-only, never auto-applied.
- Multi-machine sync: `skein sync` (fetch/merge/push with log-union
  auto-merge; non-.skein conflicts abort for manual resolve).
- Shared `edits.py` mutation ruleset for CLI and web API.

- Claude Code adapter now invokes `claude -p <prompt>
  --dangerously-skip-permissions --output-format text` (headless
  file-editing requires the permission-skip flag; output format pinned
  for the stdout contract).
- Supervisor `run_node` gained `adapter_timeout` (default 1800s,
  `--adapter-timeout` in `skein run`): a stalled backend process is
  killed and recorded as `failed` instead of hanging.
- Event-log git commits scoped to `.skein` so unrelated user-staged
  files can't ride a log commit.
- Proven against the real `claude` CLI binary end-to-end (see README
  "Backend note"); CI remains stub-only with no live credentials.

## 0.1.0 — 2026-09-09

- Initial release: event-sourced task graph (`.skein/log.ndjson` + derived `graph.json`).
- CAS claim protocol with dependency + blast-radius eligibility, TTL/heartbeat, reaper, explicit force-release.
- Git worktree isolation per node; base branch inherited from dependency; auto-created integration nodes with deterministic merge attempt.
- Verification gate: completion commands executed for real, evidence recorded, done/failed gated on exit codes.
- One backend adapter: Claude Code (headless `claude --print` against the worktree).
- `skein run` full path: claim → worktree → adapter → verify → report, supervised with heartbeat + `human_interrupt` abort.
- CLI: `init`, `node add/edit`, `graph`, `claim`, `run`, `release [--force]`, `status`, `log`, `reap`.
