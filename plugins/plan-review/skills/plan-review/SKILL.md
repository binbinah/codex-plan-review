---
name: plan-review
description: Before finalizing a Codex Plan, submit its complete body directly to an independent red-team review, revise or rebut findings, and preserve native execution authorization.
---

Use for a completed implementation plan or an explicit plan-review request.
Ordinary questions, investigation, and status updates do not need submission.

1. Resolve material uncertainty with the user. Prepare the full Markdown plan,
   including project constraints, intended changes, and meaningful acceptance
   checks. Include rollback and failure handling when the proposed work needs them.
2. **Before emitting the final proposed_plan**, call the exact `submit --stdin`
   command injected by SessionStart/UserPromptSubmit. Supply the complete Markdown
   body, without proposed_plan tags, using a single-quoted literal heredoc:

   ```sh
   python3 <installed-plugin-root>/scripts/review.py submit --stdin <<'CODEX_PLAN_REVIEW'
   <complete Markdown plan body>
   CODEX_PLAN_REVIEW
   ```

   Replace the root with the injected absolute path. Use a delimiter absent from
   the body. Do not add another shell command, pipe, redirection, or environment
   assignment. The PreToolUse hook receives the body, runs the independent reviewer
   on the host, binds the state to this session, and returns JSON through stdout.
   No plan file or manual session id is required, even in a read-only Plan sandbox.
   From a multi-repository workspace, append one `--project <directory>` per target
   before the heredoc. Add `--evidence <file>` for the key existing source files
   already inspected. Evidence must reside inside the declared projects; a source
   mirror without Git needs at least one evidence file. These paths bind review to
   the actual projects, without changing the submitted Markdown body. For example:

   ```sh
   python3 <installed-plugin-root>/scripts/review.py submit --stdin --project /path/repo --evidence /path/repo/src/entry.py <<'CODEX_PLAN_REVIEW'
   <complete Markdown plan body>
   CODEX_PLAN_REVIEW
   ```

   The reviewer uses supplied evidence first, permits targeted read-only checks,
   and stops after a bounded query budget. On query-budget failure, supply the
   missing source evidence and resubmit; do not just raise the wall-clock timeout.
3. Check **both `approved: true` and `verdict: "approve"`**. For concerns/reject,
   revise or rebut with evidence and resubmit the complete body. The JSON reports
   engine failures and remaining round budget; exit 0 from the output command
   alone is not approval. Stop and report failure or exhausted budget.
4. After approval, emit the **identical submitted body** inside proposed_plan,
   with tags on separate lines and no surrounding prose. Any change requires a
   new submission. The native Plan renderer may not provide that final body to
   Stop; exact final-body reuse is therefore part of this skill's contract.
5. Continue the host's normal Plan-to-execution flow. Preserve already-given
   authorization; do not ask for a hash phrase or an extra approval ritual.
   Technical approval does not grant permission for new external actions.

If `submit` reports that its hook is missing or untrusted, stop and explain that
review did not run. Do not invent approval or parse a session transcript as fallback.

For a standalone CLI review outside the native workflow, use
`review --stdin --session <actual-session-id> --cwd <repo>` or `review --plan <file>`.
An explicit `--data-dir` selects the same state directory as the hook if needed.
Exit 0 means technical approval; exit 3 means the plan remains gated; exit 2 is an error.

For native recovery, call the injected script with only `status`, `plan`, `retry`, or
`reset`; hooks bind them to this session. `status` includes live phase/timing metadata;
`plan` returns the saved body even while review is gated. Use `retry` only for user-requested renewed
review, then resubmit the original complete body. Use `reset` only when the user
cancels this plan or explicitly requests clearing its state. Neither changes
Codex permissions. Standalone recovery requires the actual session and state directory.

When approved work includes explicitly requested mechanical delegation, use the
sibling `dispatch-work` skill. Main owns decisions and final acceptance; workers
receive bounded assignments and return actual validation evidence.
