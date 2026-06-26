---
status: pending
---

# Admin panel: fully delete a user (stop servers, delete repos, delete user)

## Summary

Add an admin-only action in the admin panel to **fully delete a user**. A
delete must perform the following, in order:

1. **Stop and remove all of the user's code servers** — every `CodeHost`
   belonging to the user, and the backing swarm service for each.
2. **Delete the user's GitHub repositories** — the repos created for that
   user under the `League-Students` GitHub org (`GITHUB_ORG`).
3. **Delete the user record** from the database.

This is a destructive, admin-only operation. It should be exposed in the
existing admin user-management UI, require explicit confirmation, and report
what was cleaned up.

## Motivation

Today the only "delete user" path is [auth/routes.py:227](cspawn/auth/routes.py#L227)
(`admin_user` POST → `db.session.delete(user)`), which deletes only the DB row.
It leaves orphaned code-server services running on the swarm and orphaned
GitHub repos under the org. Admins need a single action that tears down
everything associated with a user.

## Scope / acceptance criteria

- [ ] Admin-only route (guarded by `admin_required`) to delete a user by id.
- [ ] Confirmation step before the destructive action (no one-click delete).
- [ ] Stops + removes all of the user's code servers. Anchors:
  `CodeHost` model ([models.py](cspawn/models.py)),
  `CodeServerManager.stop_cs(username)` / per-host `.stop()` / `.remove()`
  ([csmanager.py:643](cspawn/cs_docker/csmanager.py#L643),
  [csmanager.py:38](cspawn/cs_docker/csmanager.py#L38)).
- [ ] Deletes the user's GitHub repos under `GITHUB_ORG`. Anchors:
  [cs_github/repo.py](cspawn/cs_github/repo.py) (repo `.delete()` at
  [repo.py:445](cspawn/cs_github/repo.py#L445); org/token resolution at
  [repo.py:335](cspawn/cs_github/repo.py#L335)). May need a "list/delete all
  repos for a user" helper — currently repo lookup is keyed per-upstream.
- [ ] Deletes the user DB record last (after servers + repos are gone).
- [ ] **Idempotent / partial-state safe**: deleting a user with no servers and
  no repos succeeds cleanly; deleting one with only some of those succeeds.
- [ ] **Partial-failure handling**: if a GitHub repo delete (or server stop)
  fails, surface the failure to the admin rather than silently orphaning
  resources or half-deleting. Decide whether to abort-and-report or
  continue-and-report; the user must see what succeeded and what didn't.
- [ ] UI reports a summary: servers stopped, repos deleted (and any failures).

## Open questions (for sprint planning)

- **Ordering on failure**: if step 1 or 2 partially fails, do we still delete
  the DB row? Recommendation: do NOT delete the user record unless servers and
  repos are confirmed gone (or the admin explicitly forces it), so the user
  remains discoverable for retry/cleanup.
- **Root/self protection**: must refuse to delete the `root` user (id=0) and
  probably the currently-logged-in admin.
- **GitHub repo enumeration**: confirm how to enumerate all repos belonging to
  a user (by naming convention under the org, or tracked via `CodeHost` /
  a student-repo record). This drives whether a new helper is needed in
  `cs_github/repo.py`.
- **Long-running operation**: stopping services + deleting repos hits the
  remote swarm and the GitHub API; consider timeout/UX (synchronous with
  progress vs. background task).

## Notes

Related existing destructive admin actions for consistency of pattern:
`delete_host` ([admin/routes.py:70](cspawn/admin/routes.py#L70)),
`delete_class` ([admin/routes.py:326](cspawn/admin/routes.py#L326)).

A sprint (003) was planned for this and then abandoned on 2026-06-17 before any
implementation. The planner's resolved decisions, if useful next time: run the
teardown synchronously; continue-and-collect on partial failure and retain the
user record; enumerate repos by the `-{username}` suffix under the org; isolate
the orchestration in a new `admin/teardown.py`.
