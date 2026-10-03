---
allowed-tools: Bash(gh:*), Bash(git:*)
argument-hint: [issue-number]
---

# Automated Issue Breakdown

## Issue Context
- Original Issue: !`gh issue view $ARGUMENTS --json title,body,assignees,labels`
- Repository info: !`gh repo view --json name,description`

## Breakdown Process
1. Extract the requirements from github issue $ARGUMENTS
2. Use `/speckit.specify` command to create the user stories for the issue requirements
3. **IMPORTANT** Ensure the spec files where correctly created
