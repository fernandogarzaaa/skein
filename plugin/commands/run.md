---
description: Run the skein graph with N parallel agents (default 3) and the live dashboard
argument-hint: "[number of agents]"
---

Run the graph with `$ARGUMENTS` agents.

1. Check the agent count. If `$ARGUMENTS` is empty, use 3. If it is not a whole number of at least 1, stop and ask the user for one. If it is above 8, ask for confirmation first.
2. Run `skein doctor`, then `skein status` (which also refreshes `.skein/graph.json`). Stop and report if doctor finds problems.
3. Run `git branch --show-current`. Refuse if the output is empty (detached HEAD), `main` or `master` in any letter case; ask the user to switch to a feature branch.
4. List the backend of every node that is not `done`:

   ```bash
   python3 -I -c 'import json; g = json.load(open(".skein/graph.json")); [print(i, n.get("backend") or "claude_code (default)") for i, n in g.items() if n["status"] != "done" and not n.get("removed")]'
   ```

   Compare with `skein backends list`. If any node uses `claude_code` or no backend, warn that this profile skips the coding agent's permission prompts, and ask the user to confirm before starting.
5. Check for a listener on port 8765 with `lsof -nP -iTCP:8765 -sTCP:LISTEN`. If none, start `skein serve --port 8765` in the background. If something else holds the port, do not start a second dashboard; say what holds it.
6. Start N agents in the background, one process each, for i in 1..N. Logs go inside the git directory, so skein never commits them:

   ```bash
   LOGS="$(git rev-parse --git-dir)/skein-logs"; mkdir -p "$LOGS"
   PYTHONUNBUFFERED=1 SKEIN_ACTOR=agent-$i skein run --all --agent-id agent-$i > "$LOGS/run-$i.log" 2>&1
   ```

   Keep `SKEIN_ACTOR` equal to `--agent-id`: the event log then credits each claim to its agent, and a later `skein release <id>` from that agent is accepted. A single `skein run --all` is sequential, so one process per agent is what gives parallelism. A `skipped: node '<id>' status is 'claimed', expected 'unclaimed'` line means that agent lost a claim race, which is harmless.
7. Report the dashboard URL `http://127.0.0.1:8765/`, the log directory, and how to watch: `skein status --watch` or `skein log --follow`.
