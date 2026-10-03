---
allowed-tools: Bash(git:*), Bash(ls:*)
description: Implement spec using Claude Code subagents
---

# Implementation mit Subagents

### Phase 1: Implementation  
- **architect**: Execute implementation with: `/speckit.implement`
- **tdd-guide**: Build tests and implements the solution to solve the architect design
- **python-reviewer**: Review structural decisions in parallel

### Phase 2: Tests & Quality
- **qa-architect**: Create and execute tests
- **qa-reviewer**: Final review of the issue and implementation

### Finalize ###
Create a descriptive commit message, push and create a PR
