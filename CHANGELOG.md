# Changelog

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
