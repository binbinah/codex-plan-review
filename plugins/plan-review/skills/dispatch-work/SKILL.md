---
name: dispatch-work
description: Delegate explicitly requested, already-specified mechanical implementation or verification tasks to Codex workers, then verify returned artifacts and command results.
---

Use only when the user or applicable project/skill instructions request delegation.
Do not force simple tasks into a multi-agent workflow. Complete any required plan
review before dispatching implementation work.

The main session owns requirements, architectural choices, unresolved debugging
judgment, task decomposition, and final acceptance. A worker executes an assignment
whose material decisions are already made.

Before spawning a worker, provide:

- The approved behavior and relevant facts, with source paths.
- Exact files or an isolated worktree it owns; identify other concurrent writers.
- Dependencies and which independent assignments may run concurrently.
- Concrete acceptance commands or artifact checks.
- A direction to stop and return missing information or a new design decision.

Use a built-in or project-defined worker when the host exposes role selection.
If the host exposes only task_name/message, provide the same explicit mechanical
assignment through that supported interface. Inherit the
configured model unless an applicable instruction explicitly selects another one.
Pass the assignment through the host's spawn tool, using its actual supported
arguments. Do not copy Claude's `subagent_type` parameters into Codex calls.

Wait for the result, inspect changed files and validation output, and evaluate the
acceptance conditions. A worker's assertion of success is insufficient. If another
worker depends on the result, verify it before dispatching that dependent task.
Preserve file ownership and do not overwrite other workers' changes.

Report implementation, mock validation, real execution, and unverified behavior
separately. The plugin records worker returns as evidence leads; it does not certify
correctness, enforce a task DAG, or change worker permissions.
