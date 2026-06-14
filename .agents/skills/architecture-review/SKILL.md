---
name: architecture-review
description: Review sprint architecture updates for consistency, quality, and risk
---

# Architecture Review Skill

Evaluate a sprint's architecture update for consistency with the existing
system, design quality, risks, and completeness.

## Process

1. **Read the current architecture** in `docs/clasi/architecture/`.
2. **Read the sprint's architecture update** (`architecture-update.md`).
3. **Explore the codebase** to check alignment between documented and
   actual architecture.
4. **Review against the criteria below.**

## Review Criteria

### Version Consistency
- Changes in Sprint Changes section reflected in document body?
- Updated architecture internally consistent?
- Design rationale updated for changed decisions?

### Codebase Alignment
- Does current code match documented architecture?
- If drift exists, does the sprint plan account for it?
- Are proposed changes feasible given actual code state?

### Design Quality
- **Cohesion**: Each component responsible for one concern?
- **Coupling**: Minimal, intentional, no circular dependencies?
- **Boundaries**: Clear, enforceable, narrow interfaces?
- **Dependency health**: No cycles, consistent direction, reasonable fan-out?

### Anti-Pattern Detection
- God component, shotgun surgery, feature envy
- Shared mutable state, circular dependencies
- Leaky abstractions, speculative generality

### Risks
- Data migration issues, breaking changes
- Performance or security implications
- Deployment sequencing concerns

## Verdict

- **APPROVE**: No significant issues.
- **APPROVE WITH CHANGES**: Minor issues addressable during implementation.
- **REVISE**: Significant structural issues that need resolution first.

Guidelines:
- Circular deps, god components, broken interfaces → REVISE
- Single contained anti-pattern → APPROVE WITH CHANGES
- Inconsistencies between Sprint Changes and document body → REVISE
- Missing rationale for significant decisions → APPROVE WITH CHANGES
