---
name: consolidate-architecture
description: Merge the base architecture document with subsequent sprint update documents into a new consolidated architecture
---

# Consolidate Architecture Skill

Merges the last consolidated architecture document with all subsequent
architecture-update documents to produce a new, up-to-date consolidated
architecture.

## When to Use

Run on demand when:
- Outstanding update documents make it hard to understand current architecture
- A new team member needs to onboard
- You want a clean baseline before a major refactoring sprint

This is NOT run automatically on every sprint close.

## Process

1. **Identify the base**: Find the latest consolidated architecture in
   `.clasi/architecture/architecture-NNN.md` (highest NNN).

2. **Collect updates**: Find all `architecture-update-MMM.md` where
   MMM > NNN. Read them in order.

3. **Read actual code**: Verify current system structure against source
   code. The consolidated document must reflect reality.

4. **Write the new consolidated document**: Incorporate all changes,
   reflect actual codebase state, include updated Mermaid diagrams.

5. **Save**: Write as `.clasi/architecture/architecture-MMM.md`
   (where MMM is the latest update sprint number).

6. **Archive**: Move previous consolidated document and incorporated
   updates to `.clasi/architecture/done/`.

## Output

- New `.clasi/architecture/architecture-MMM.md`
- Old files moved to `.clasi/architecture/done/`
