Return this root session from a previously entered session worktree to the original working
directory.

This tool only restores the process-local working directory override. It intentionally leaves the
worktree directory intact so user work is never deleted silently.
