Please analyze and fix the GitHub issue: $1.

Follow these steps:

1. Use `gh issue view` to get the issue details
2. Understand the problem described in the issue and create context file
3. Pull the current branch and create a git issue branch `gh-issue-{number}`
4. Search the codebase for relevant files by subagents
5. Implement the necessary changes to fix the issue by subagents
6. Write and run tests to verify the fix by subagents
7. Ensure code passes linting and type checking
8. Create a descriptive commit message
9. Push and create a PR

Remember to use the GitHub CLI (`gh`) for all GitHub-related tasks.