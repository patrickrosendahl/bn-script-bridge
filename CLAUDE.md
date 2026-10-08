# bn-script-bridge

Binary Ninja plugin plus the `bnrun` / `bnrestart` CLIs; see `README.md` for setup and usage.

## The `binaryninja-mcp` skill

`skills/binaryninja-mcp/SKILL.md` is the source of truth for the skill. Install it by
symlinking the directory into Claude Code's user-level skills folder, so edits here take
effect without copying:

```sh
ln -s /Users/patrick/dev/bn-script-bridge/skills/binaryninja-mcp ~/.claude/skills/binaryninja-mcp
```

- Link the **directory** (not `SKILL.md`); Claude Code expects `~/.claude/skills/<name>/SKILL.md`,
  and `<name>` must match the `name:` in the frontmatter.
- For a single project only, link into that project's `.claude/skills/` instead.
- Check with `ls -l ~/.claude/skills/`; new sessions pick it up.
- The skill hard-codes absolute paths to `bnrun`, `bnrestart` and `mcp-session-header` in this
  checkout -- update them in `SKILL.md` if the repo moves.
