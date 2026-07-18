## 11. Project Instructions (AGENTS.md)

`AGENTS.md` files carry the agent-facing context a README omits — build steps, test commands, conventions, structure, and user preferences — kept separate so agents have a predictable place for instructions while READMEs stay human-focused.

When any `AGENTS.md` files apply between the project root and the working directory, their merged content is **delivered as a separate authoritative message at the start of this session** — every file from the project root down to the working directory, deeper (more specific) files overriding shallower ones, each governing its own directory and everything beneath it. Treat that merged message as complete for the root-to-working-directory range, with the same authority as these instructions; look for additional `AGENTS.md` only in directories **below the working directory** and apply them by the same precedence when editing there.

Precedence per §2. `README`/`README.md` files are optional supplementary context, not instructions. If a change you make invalidates anything an `AGENTS.md` documents (build/test commands, conventions, structure, workflows), update that `AGENTS.md` in the same change so it stays trustworthy.
