---
name: close-sprint
description: Validates and closes a completed sprint — verifies tickets, merges branch, archives sprint
---

# Close Sprint Skill

This skill closes a completed sprint using the `close_sprint` MCP tool,
which handles the full lifecycle.

## Inputs

- Active sprint directory in `docs/clasi/sprints/NNN-slug/`
- All tickets for this sprint should be `done`

## Process

1. **Gather sprint context**: Call `list_sprints()` to identify the
   active sprint. Record the `id` and `branch` values — you will need
   them as `sprint_id` and `branch_name` in step 4. Do not proceed
   without these values in hand.

2. **Confirm with stakeholder**: Present a summary of the sprint —
   list the completed tickets and key changes. Ask whether to proceed:
   - "Close sprint and merge to main" (recommended)
   - "Review completed work first"

   If the stakeholder chooses to review, invoke the `sprint-review`
   skill first.

3. **Load the tool schema**: Call `ToolSearch` with query
   `select:mcp__clasi__close_sprint` to load the tool's parameter schema.
   This is required because CLASI MCP tools are deferred — calling them
   without first loading their schema causes all parameters to be silently
   dropped.

4. **Call close_sprint**: Invoke the `close_sprint` MCP tool using the
   `sprint_id` and `branch` values collected in step 1:
   ```
   close_sprint(
       sprint_id="NNN",        ← from list_sprints() in step 1
       branch_name="sprint/NNN-slug",  ← from list_sprints() in step 1
       main_branch="master",
       push_tags=True,
       delete_branch=True,
       test_command="uv run pytest",  # or "" to skip tests
   )
   ```

   The `test_command` parameter controls how tests are run:
   - Omit or `None`: runs `uv run pytest` (default)
   - Custom string (e.g., `"npm test"`): runs that command
   - Empty string `""`: skips tests entirely (non-Python projects)

   The tool handles internally:
   - Pre-condition verification with self-repair
   - Run tests (if test_command is provided)
   - Archive sprint directory to `sprints/done/`
   - Update state DB, release execution lock
   - Version bump and git tag
   - Merge to master, push tags, delete branch

5. **Report result**: On success, report the version tag and merged
   branch. On error, report the blocker and recovery steps.

## Issue Sweep at Close

When `close_sprint` runs, it automatically calls `_sweep_done_issues`, which
moves any resolved sprint issues from `<sprint>/issues/` to
`<sprint>/issues/done/`. No manual `move_issue_to_done` call is needed for
issues whose tickets are all done.

If any sprint issues remain unresolved at close, the close still succeeds.
The result JSON will contain an `unresolved_issues` list with the filenames.
Read this list and surface it to the team-lead for mop-up — these issues were
not resolved in the sprint and need follow-up.

## Issue Preconditions

Issues that are intentionally deferred (ticket carries `completes_issue: false`)
pass cleanly through close without appearing in `unresolved_issues`. For all
other in-progress issues, close collects their filenames in `unresolved_issues`
and continues — the close is non-blocking on unresolved issues.

**Resolution paths for issues that should have been resolved:**
- **Tickets are done but issue not swept**: check that all tickets referencing
  the issue carry `issue:` back-refs. If a back-ref is missing, call
  `add_issue_ref` and re-run close.
- **Issue has work remaining**: call `split_issue` to split the remaining
  work into a new issue, then either defer it (it stays in the pool for
  the next sprint) or call `create_ticket` to bring it into the current
  sprint before closing.
- **Issue is intentionally deferred**: set `completes_issue: false` on
  the ticket(s) referencing this issue. Close-sprint will then exclude
  that issue from `unresolved_issues`.

## Output

- Sprint branch merged to main and deleted
- Sprint document moved to `docs/clasi/sprints/done/`
- Sprint completion summary
