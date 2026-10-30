---
description: Merge finished skein nodes onto the current feature branch
argument-hint: "[target branch]"
---

skein itself will ship onto any branch, including `main`, so these checks are the only guard.

1. Run `git branch --show-current`. Refuse if the output is empty (detached HEAD), `main` or `master` in any letter case.
2. If the user gave a target branch in `$ARGUMENTS`:
   - Refuse if it contains `$`, a backtick, a quote or a backslash.
   - Validate it with `git check-ref-format --branch '$ARGUMENTS'`. Refuse if that fails.
   - Refuse if the name, after removing any leading `refs/heads/` or `<remote>/` part, is `main` or `master` in any letter case. On macOS and Windows `MAIN` resolves to `main`.
   - Always pass it in single quotes: `skein ship --all --to '$ARGUMENTS'`.
3. Run `skein status` and show which nodes are `done` and not yet shipped. If none, stop.
4. Run `skein ship --all`, or the single-quoted `--to` form from step 2.
5. Report what merged and any conflicts, with the node ids involved. Never pass `--force`. On a conflict, stop and ask the user how to proceed.
