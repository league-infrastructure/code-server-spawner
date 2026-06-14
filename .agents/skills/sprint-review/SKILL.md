---
name: sprint-review
description: Post-sprint validation — verifies all tickets are done, tests pass, and process was followed correctly
---

# Sprint Review Skill

Perform post-sprint validation before closing. This is a read-only
review that checks all process requirements were met.

## Inputs

- Sprint ID and path to the sprint directory
- All tickets should be in `done` status

## Validation Checklist

### Ticket Completion

- [ ] All tickets in the sprint have `status: done` in frontmatter
- [ ] All ticket files are in `tickets/done/`
- [ ] All acceptance criteria in each ticket are checked (`- [x]`)
- [ ] No tickets remain in `tickets/` (only in `tickets/done/`)

### Tests

- [ ] Full test suite passes (`uv run pytest`)
- [ ] No test files are missing or skipped unexpectedly

### Architecture

- [ ] Architecture document reflects the actual end-of-sprint state
- [ ] Sprint Changes section is filled in
- [ ] Architecture version matches the sprint

### Git State

- [ ] All changes are committed on the sprint branch
- [ ] No uncommitted modifications related to the sprint
- [ ] Commit messages reference ticket IDs

## Output

- **Verdict**: pass or fail
- **Checklist results**: each item with pass/fail and details
- **Blocking issues**: anything that must be fixed before close
- **Advisory notes**: non-blocking observations for future improvement

## Rules

- This is read-only. Do not modify any files.
- Report all findings, not just failures.
- Be specific about failures: which ticket, which criterion, what is wrong.
- Distinguish blocking issues from advisory notes.
