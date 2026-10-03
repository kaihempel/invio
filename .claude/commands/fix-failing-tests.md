Please analyze the testcase $2 in $1. The testcase fails with the following error: "$3".

Follow these steps:

1. Check the testcase and the corresponding implementation by the code-reviewer subagent and create a context file in `.claude/contexts` with the name `context-testcase-{$2}.md`.
2. Adjust the test or the implementation using subagents. The subagent should read the context file.
3. Verify the testcase by the swift-test-engineer.
4. If the testcase still fails repeate the steps 1., 2. and 3. and use ultrathinking. Repeat it only once!

Use the following Simulator for testing: 
device: iPhone16
OS: 18.5