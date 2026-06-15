Create a git worktree for the current repository and switch this root session's operational working
directory to it.

Use this when you need to isolate a risky or parallel implementation attempt from the original
checkout. The switch is process-local: it affects this running session's tools, but it is not a
durable session migration.

The tool never deletes worktrees. Use `ExitWorktree` to return to the original working directory;
remove the worktree manually after preserving or merging any work you want to keep.
