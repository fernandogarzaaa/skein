---
description: Run the skein graph with N parallel agents (default 3) and the live dashboard
argument-hint: "[number of agents]"
---

Run the graph with `$ARGUMENTS` agents. If no number is given, use 3.

1. Run `skein doctor`. Stop and report if it finds problems.
2. Run `git branch --show-current`. Refuse if it is `main` or `master`; ask the user to switch to a feature branch.
3. Check whether anything listens on port 8765 (`lsof -i :8765`). If not, start `skein serve --port 8765` in the background.
4. Start N agents in the background, one process each, for i in 1..N:

   ```bash
   SKEIN_ACTOR=agent-$i skein run --all --agent-id agent-$i > .skein/run-$i.log 2>&1
   ```

   `SKEIN_ACTOR` must equal `--agent-id`, or release, run and ship reject the agent as not the holder. A single `skein run --all` is sequential, so one process per agent is what gives parallelism. A `skipped: ... held by agent-N` line in a log means that agent lost a claim race, which is harmless.
5. Report the dashboard URL `http://127.0.0.1:8765/`, and how to watch: `skein status --watch`, `tail -f .skein/run-*.log`, or `skein log --follow`.
