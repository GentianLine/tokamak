# Merge Agent Playbook (integrator for the swarm)

You merge branches produced by worker agents onto `main` after they finish.
Worker protocol lives in AGENTS.md; this file is your runbook.

## Inputs
* Branches: all refs matching `agent/*` under `.git/refs/heads/agent/`
  (`git branch --list 'agent/*'`).
* Claims/results: git notes — find the runtime board anchor once:
  `ANCHOR=$(git notes --ref tasks list | head -1 | awk '{print $2}')`, then read
  it fully (`git notes --ref tasks show "$ANCHOR"`) — it carries seed list,
  claims, TASK+/PROPOSED entries (AGENTS.md §7.1). Plus per-agent results on
  `--ref results`. Read before merging so you know each agent's intent, test
  evidence, and self-declared risks.

## Procedure (per branch)
1. Verify provenance: branch contains an agent id matching its RESULTS note;
   it was created from a recorded baseline commit (`git merge-base main <branch>`).
2. Check the worker's own test claim first — you only have branches, so checkout
   the branch in a scratch worktree and run `python manage.py test`. If it fails
   on its own baseline, reject with a note (`--ref merges`) and continue.
3. Prefer `git merge --no-ff <branch>` onto an integration topic branch rather
   than touching main directly for each; this keeps bisectable history.
4. Conflicts: resolve by respecting the conventions in AGENTS.md §1 — service
   layer is authoritative, signal kwarg names immutable, append-only css/tests.
   When two branches touch the same hot file (services/views/board template),
   merge the *semantic* union: keep both features wired through services.py;
   never drop a feature silently. If semantics genuinely conflict, record the
   decision in your merges note explaining which branch wins and why.
5. After each successful integration run the combined test suite; all tests —
   baseline (21) plus every new agent test file must pass together. Also run
   `python manage.py check` and ensure migrations exist for any model change
   (`makemigrations` yourself only if a worker forgot them, message:
   "fix(merge): …").
   If any integrated branch touched `requirements*.txt`, refresh the base
   demo venv from the base repo root afterwards (AGENTS.md §2:
   `PYTHONPATH="" PIP_TARGET="" .venv/bin/python -m pip install --ignore-installed \
   -r requirements.txt -r requirements-dev.txt` plus the clm-mem git URL).
   Workers must never point a workspace worktree at that venv — only the
   base repo uses it.
6. Record per-branch integration notes on `--ref merges`: merged sha, conflicts
   resolved (files + resolution), test-run tail output, rejects.
7. Status markers: after integrating a branch, append its task's done-marker on
   the board anchor (`ANCHOR=$(git notes --ref tasks list | head -1 | awk '{print $2}')`
   then `git notes --ref tasks add -m "done: <slug> (merged)" "$ANCHOR"`, or
   `"done: <slug> (rejected: reason)"`). Use `-m` only; never overwrite others'
   notes.

## Final steps
* Merge the topic branch onto `main` once everything passes; run full suite again
  on main; post final note: commit sha, summary table of all branches
  (merged/rejected), total tests passing.
* Leave worktrees behind untouched until humans confirm (`git worktree list`).

## Rejection criteria
* Branch breaks the service-layer rule or renames signal kwargs → reject with a
  detailed merges note; ask agent to fix via new branch `agent/<id>-<slug>-v2`.
* Missing tests for claimed behavior, failing suite, or committed db.sqlite3.
