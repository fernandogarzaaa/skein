# Changelog

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
