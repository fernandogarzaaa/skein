# skein plugin for Claude Code

Adds slash commands and an orchestration skill for [skein](../README.md), the task-DAG runner for parallel coding agents. The commands are thin wrappers over the `skein` CLI, which must already be on your PATH.

## Install

```
/plugin marketplace add fernandogarzaaa/skein
/plugin install skein@skein
```

## Commands

| Command | What it does |
|---|---|
| `/skein:status` | Summarise the graph per node, most actionable first |
| `/skein:run [N]` | Preflight, then run N agents (default 3) in parallel with the dashboard |
| `/skein:ship [branch]` | Merge done nodes onto the current feature branch, never `main` |
| `/skein:dashboard` | Start the live dashboard at http://127.0.0.1:8765/ |

## Skill

`orchestrate` is model-invoked. Claude loads it when you fan out code-writing agents or hand it a work-units table, and it turns the table into `skein node add` calls.
