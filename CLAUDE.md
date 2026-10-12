# bn-script-bridge

Binary Ninja plugin plus the `bnrun` / `bnrestart` CLIs; see `README.md` for setup and usage.

## The `binaryninja-bridge` skill

`skills/binaryninja-bridge/SKILL.md` is the source of truth for the skill. Install it by
symlinking the directory into Claude Code's user-level skills folder, so edits here take
effect without copying:

```sh
ln -s /Users/patrick/dev/bn-script-bridge/skills/binaryninja-bridge ~/.claude/skills/binaryninja-bridge
```

- Link the **directory** (not `SKILL.md`); Claude Code expects `~/.claude/skills/<name>/SKILL.md`,
  and `<name>` must match the `name:` in the frontmatter.
- For a single project only, link into that project's `.claude/skills/` instead
  (a `skills/<name>/SKILL.md` file symlink inside the project also works). This repo does
  that itself: `.claude/skills/binaryninja-bridge -> ../../skills/binaryninja-bridge`
  (relative, so it works in any checkout).
- Check with `ls -l ~/.claude/skills/`; new sessions pick it up.
- The skill hard-codes the absolute paths to `bnrun`, `bnannotate`, `bnrestart` and `scripts/`
  in this checkout -- update them in `SKILL.md` and `reference.md` if the repo moves.
