---
paths:
  - clasi/**
  - tests/**
---

You are modifying source code or tests. Before writing code:

1. If `.clasi/oop` exists, the stakeholder has opted out of CLASI
   for this session. Skip these gates entirely and proceed.
2. You must have a ticket in `in-progress` status, or the stakeholder
   said "out of process".
3. If you have a ticket, follow the execute-ticket skill — call
   `get_skill_definition("execute-ticket")` if unsure of the steps.
4. Run the project's test suite after changes.
