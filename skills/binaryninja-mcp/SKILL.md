---
name: binaryninja-mcp
description: Use whenever analysing a binary (ELF, Mach-O, PE, firmware, app frameworks) in the running Binary Ninja app -- opening/selecting the right binary view, listing symbols/functions/strings, reading disassembly/decompilation, tracing callers, and annotating findings (comments, renames, types) via the Binary Ninja MCP tools (bn_*) or the bnrun script bridge (full Python API inside the app). Trigger for tasks like "load this into Binary Ninja", "look at this binary in BN", "what does function X do", "which API does this app call", "find where this string is used" -- not just when Binary Ninja is named explicitly. Covers tool mechanics, the shared usage lock across sessions, and the Personal-licence workarounds; project-specific binaries, naming conventions and where findings are written live in each project's CLAUDE.md or project-level skill.
---

# Using Binary Ninja (MCP tools + script bridge)

The `bn_*` tools talk to **Binary Ninja's own built-in MCP server**, not a
separate always-on service -- there is no server process to start
yourself. It only answers while the Binary Ninja desktop app is running
with the relevant binary open.

## Connection

The user-scope MCP config (`claude mcp get binaryninja`, scope User) points at the lock-aware proxy
`http://127.0.0.1:24643/mcp` (headersHelper `/Users/patrick/dev/bn-script-bridge/mcp-session-header`),
which forwards to Binary Ninja's own MCP server (moved to a private random port and token
while the bridge runs; 24642 without a token otherwise). The proxy runs
inside Binary Ninja with the script bridge. **If the tools error as unreachable, Binary Ninja
isn't running or the script bridge is stopped** (Plugins > Script Bridge > Start); don't assume
the tool is broken. See "Concurrent sessions" for the lock tools it adds.

## Core gotcha: open item vs. binary view vs. *active* view

Three distinct concepts, easy to conflate:

- **Open item** (`bn_open_item_list`) -- a file/database Binary Ninja has
  loaded.
- **Binary view** (`bn_binary_view_list`) -- one *interpretation* of an
  open item's bytes. A single ELF open item typically produces **two**
  views: a parsed view (`ELF`, `Mach-O`, `PE` -- sections/symbols/relocations, the one
  you almost always want) and a `Raw` view (flat bytes, no parsing).
- **Active** view -- whichever view the *rest* of the `bn_*` tools
  (symbols, functions, disassembly, comments, ...) actually operate on.

**Files opened directly in the Binary Ninja UI are not automatically
active for MCP.** Before touching anything else, always:

```
bn_binary_view_list          # see what's loaded, which view is active
bn_binary_view_set_active    # point MCP at the parsed (ELF/Mach-O/PE) view, not Raw
```

`bn_binary_view_list`'s table has `active`/`available`/`recommended`
columns -- `recommended: true` marks the parsed view as the sane default over
Raw.

## Discover what's already loaded before assuming you need to open anything

Someone (a person at the Binary Ninja UI, or a prior session) may already
have a binary open and partially analysed. Always check first:

```
bn_open_item_list
bn_binary_view_info     # metadata for whichever view is currently active
bn_analysis_status      # IdleState = auto-analysis finished, safe to query
```

`bn_open_item_list`'s `databaseBacked: false` means **the open item has no
`.bndb` on disk yet** -- any renames/comments/type work only live in the
running Binary Ninja instance's memory. `modified`/`analysisModified` can
be `true` with `databaseBacked: false` at the same time: that's real,
unsaved work sitting in RAM. Call `bn_open_item_save` to persist it before
assuming it'll still be there next session (BN closing or crashing loses
anything not saved).

`bn_open_item_save` writes the `.bndb` **right next to the binary** --
e.g. `<dir>/<binary>.bndb` -- not to
some separate project/cache directory. That `.bndb` *is* the durable
record of everything renamed/commented/typed for that binary; the
`bin-*.md` wiki page should stay the human-readable synthesis, but the
`.bndb` is the actual working database to reopen next session rather than
re-analysing from scratch. Check with whoever's mid-analysis before
committing one, though -- multiple sessions can have the same file open
concurrently (see the cross-session coordination note above), so grabbing
it mid-edit captures a moving target.

## Listing symbols/functions

`bn_symbol_list` / `bn_function_list` default to `limit: 100` (cap 1000)
and their `query` param is a **case-insensitive substring match, not a
regex** -- `query: "msg"` matches `msgget`/`msgrcv`/etc., not a pattern.
Use `start`/`end`/`length` to scope to an address range instead of paging
through everything when you already know roughly where to look.

Don't assume "stripped" (per a ticket's `file`-based description) means
no names at all: the **dynamic symbol table survives stripping** (dynamic
linking needs it), so every imported libc/library function still shows up
in `bn_symbol_list` with its real name and `autoDefined: true`. Only the
binary's *own* internal functions are anonymous (`sub_XXXXXXXX`) until
someone renames them -- check `bn_function_list` first, don't manually
grep imports that Binary Ninja already resolved for you.

## Reading raw memory

`bn_memory_read` returns bytes as base64 -- fine to decode by hand for a
handful of bytes, but **don't hand-transcribe a large base64 blob for a
whole struct-array dump**: a single copy/offset slip produces garbled,
plausible-looking-but-wrong values (e.g. a struct field that decodes to a
nonsensical shifted hex number) with no error raised anywhere, since the
tool did its job correctly and the mistake is purely in the manual
decode step afterward. Prefer several small, precisely-addressed reads
(one struct entry, or a few, at a time) over one large read you then
decode yourself -- easier to sanity-check each result and cheaper to
redo if a field looks wrong.

## Reading and annotating

- `bn_function_decompile` / `bn_function_disassembly` / `bn_function_il`
  -- read a function once you've located it (by address or by name via
  `bn_function_search`).
- `bn_function_xrefs_to` / `bn_function_callers` / `bn_function_callees`
  -- trace call graphs, e.g. from a UART-write import back to whoever
  builds the message being sent.
- `bn_symbol_rename`, `bn_comment_set`, `bn_type_define` /
  `bn_data_variable_define` -- record findings *in the database* as you
  go, not just in your own notes. Match the naming style already established
  in that database so renamed functions read consistently across binaries.
- **`bn_symbol_rename` fails with `symbol_not_found` against a still
  purely-auto-defined symbol** (i.e. anything still showing as
  `sub_XXXXXXXX`/an auto data name) -- it seems to need an existing
  matching user symbol to "find" rather than creating one from an
  address alone. Use **`bn_symbol_define`** instead for the *first*
  rename of any given address (pass `type: FunctionSymbol` or
  `DataSymbol` and `binding: GlobalBinding` explicitly) -- it cleanly
  supersedes the auto symbol with a single user-defined one.
  Renaming-by-address with `bn_symbol_rename` after that first
  `bn_symbol_define` (e.g. a later tidy-up) can otherwise leave two
  symbols at the same address (one `autoDefined: true`, one now
  `false`, same name) -- an ambiguous-duplicate-symbol state. If a
  rename attempt "succeeds" but later lookups on that address complain
  about ambiguity, that's what happened; `bn_symbol_define` again is the
  fix, not a second `bn_symbol_rename`.
- Mutation tools request async analysis updates during bulk/initial work;
  call `bn_analysis_update_and_wait` after a batch that skipped waiting,
  per the MCP server's own tool instructions.
- Remember to `bn_open_item_save` once you've made annotations worth
  keeping (see the gotcha above).

## Tracing technique (lessons from earlier projects)

1. **Tools are deferred in this harness**: load them with `ToolSearch` (`select:mcp__binaryninja__bn_function_decompile,...`)
   before first use, then `bn_open_item_list`. `bn_analysis_status` should read `IdleState` before trusting results.
2. **Start from BN, not from your own instruction scanner**: `bn_function_decompile` the function of interest, then
   `bn_function_callers` up the chain (call-site address + caller name, level by level).
3. **The vendor's own strings name things**: log/error strings with file and function names, config keys, URL/API
   path strings. Find a string, follow its code xrefs to the function that uses it, name the function after it.
4. **`bn_data_xrefs_to` is empty for globals reached through literal pools / ADRP+ADD pairs**: find the loader or
   setter function instead, or scan with `bnrun` (`bv.get_code_refs(addr)` on the string start often works).
5. **Comment both places**: function start (summary) and the specific call site; comments show as `//` lines in
   the decompile output, the quickest check that a rename took.
6. **Independent `bn_symbol_define` / `bn_comment_set` calls can go out in one parallel batch**; a transient
   "auto mode classifier gave no verdict" is retry-once, not a rejection.
7. **Prove naming hypotheses against runtime data when you can** (captured traffic, files, traces).
8. **`.bndb` files are big** (tens of MB) and saved next to the binary: save once after a batch of renames; whether
   it belongs in git is the project's call (check its CLAUDE.md/.gitignore) -- never commit one by default.

## Beyond the MCP tools: scripts via the bridge (`bnrun`)

The `bn_*` tools are a fixed set of point operations. Anything else -- loops over many functions,
bulk typing/annotation, IL queries, operand display, whatever the Python API offers -- goes through
**`/Users/patrick/dev/bn-script-bridge/bnrun`**, which runs a Python script *inside the running Binary Ninja app*
and returns its output. Details and security notes: `/Users/patrick/dev/bn-script-bridge/README.md`.

- **Why a bridge:** the licence is Personal. `import binaryninja` works in a plain `python3` (append
  `/Applications/Binary Ninja.app/Contents/Resources/python` to `sys.path`), but `binaryninja.load()`
  fails with `License is not valid` -- no headless API. Nothing needs building for Python; the API
  ships in the app, and `Vector35/binaryninja-api` is only needed for C++ plugins.
- **Is it up?** It autostarts (setting `scriptbridge.autostart` = true). Quick check:
  `/Users/patrick/dev/bn-script-bridge/bnrun --status` (`free` / `locked by ...` plus running scripts). Exit 2 with "not
  found" / "unreachable" means it isn't running: ask the user to use **Plugins > Script Bridge >
  Start** (Start is greyed out while running, Stop while stopped). Plugin code changes load only on
  an app restart, which hits every session sharing the instance -- coordinate it.
- **Restarting Binary Ninja:** `/Users/patrick/dev/bn-script-bridge/bnrestart`. It refuses (exit 3)
  if another session holds the usage lock or a bridge script is running or queued (either lane); it
  takes the lock itself while it lists and saves views. Modified views (analysis
  alone sets the flag) that have a `.bndb` are saved into it; modified views without one make it
  refuse unless `--save-new` (create `<file>.bndb` next to the binary) or `--discard` (quit without
  saving via SIGTERM — the Qt UI's own save prompt would otherwise block the quit; use it for
  throwaway read-only tabs). Then it quits, relaunches with the previously open files
  (the `.bndb` where one exists; `--no-reopen` to skip) and waits until the bridge answers.
  `--force` overrides the lock/script checks -- ask first. Restarting kills the GUI state for
  everyone sharing the instance, so get the user's OK; auto mode may block it as interfering with
  a running workload -- then ask the user to run it (`! /Users/patrick/dev/bn-script-bridge/bnrestart`).
- **UI views need the usage lock** (bridge 2026-10-07): `bv`, `bvs`, `--view` and `--main-thread` only
  work while your session holds the lock -- `bnrun --lock --purpose "..."` first, `bnrun --unlock` when
  done. Without it `--view`/`--main-thread` are refused (exit 2, "take the usage lock first") and
  `bv`/`bvs` raise `UIViewsUnavailable` on any use. Enforced at the namespace level only (a script
  could still reach UI views via `binaryninjaui`) -- don't.
- **Two lanes.** Default scripts run serialized (one at a time, later ones queue): use it for UI
  views and shared state (registering types/architectures, the MCP active view, saving databases).
  Scripts that only create their own views (`BinaryViewType[...].create(BinaryView.open(path))`,
  `binaryninja.load(...)`, then `close()`) need no lock and should use **`bnrun --parallel`**: up to
  `scriptbridge.maxParallel` (default 4) run at once, alongside the serialized lane. `--parallel`
  can't be combined with `--view` / `--main-thread`; parallel scripts must not touch UI views, the MCP
  active view or databases.
- **Usage:** `bnrun -e CODE`, `bnrun script.py`, or stdin. `--view SUBSTRING` (with the lock) picks the open view by
  filename (use it: the UI's active view is separate from the MCP "active" view, and can be empty);
  without it `bv` is the UI-active view, else the only open view. Files opened with
  `bn_open_item_open` show up as UI tabs, so the bridge sees them; if `bvs` is empty, nothing is
  open -- open the file via MCP first. `bvs` = all open views,
  `bn`/`binaryninja` = the API. `print()` output comes back; set `result = ...` for structured
  output (JSON, else `repr`). `--main-thread` for UI objects (`binaryninjaui`); it blocks the UI.
  Exit 0 ok / 1 script error (traceback on stderr) / 2 bridge refused or unreachable.
- **Status / cancel:** `bnrun --status` lists every running script (`, parallel` marks the parallel
  lane) and the queue lengths; `--timeout N` has the bridge cancel the script after N s;
  Ctrl-C / SIGTERM on `bnrun` cancels its own script in BN (exit 130); `bnrun --cancel` cancels
  your own running scripts (both lanes); with none, the lock holder cancels all; `--cancel --force`
  anyone's (ask first).
  Cancellation lands between Python bytecodes only: a script stuck in one long native call
  (`bv.update_analysis_and_wait()`, `time.sleep`) stops when that call returns -- so still bound
  long jobs as loops over small steps, pass `--timeout` for anything open-ended, and print
  progress. Mutations are the same as MCP ones: same shared databases and the same
  check-in rule before switching or saving another session's `.bndb` (`bv.file.save_auto_snapshot()`
  / MCP `bn_open_item_save` to persist).
- **Probe unknown UI/API calls** with a throwaway name and undo them in the same script (the
  `binaryninjaui` module has no docstrings), e.g. `UIAction.registerAction("Probe\\X")` ...
  `unregisterAction`.
- Example -- names of all callers of a function (verified 2026-09-25 on a kernel module):
  ```sh
  /Users/patrick/dev/bn-script-bridge/bnrun --lock --purpose "callers of <function>"
  /Users/patrick/dev/bn-script-bridge/bnrun --view <file> -e '
  f = bv.get_functions_by_name("<function>")[0]
  result = sorted({c.function.name for c in bv.get_code_refs(f.start)})'
  /Users/patrick/dev/bn-script-bridge/bnrun --unlock
  ```

## Project specifics

Which binaries a project analyses, its naming conventions, and where findings are written (wiki pages, tickets)
belong in that project's CLAUDE.md or a project-level skill, not here. Write reverse-engineering *findings* there;
this skill is only tool mechanics. Mach-O apps (iOS `.app` bundles): the main executable and every
`Frameworks/*.framework/<name>` binary are separate open items; Swift/ObjC class names survive in symbols and
strings (`_TtC...`), so `bn_symbol_list`/string search is a better start than anonymous `sub_` functions.

**Java (`.class` / `.jar`):** the `binary-jvm` plugin (`/Users/patrick/dev/binary-jvm`, project skill
`.claude/skills/jvm/SKILL.md`) is **installed** (resumed 2026-10-06 after a short pause). BN can't open a
`.jar`, so **unpack it next to the JAR into a dir named like it** (`foo.jar` → `foo/`:
`unzip -o -q foo.jar -d foo`) and open the `.class` files; `0xCAFEBABE` is shared with fat Mach-O, so
`files.container.excludedTransforms = ["Universal"]` must stay set; switch the active view to
"JVM Class" after opening.

## Concurrent sessions

The "active" binary view is **shared state across every MCP client
talking to that Binary Ninja instance** -- not per-session. If another
Claude session (or a person at the UI) might be working a different
binary right now, switching the active view out from under them breaks
their in-flight `bn_*` calls. Before a session of work: check
`bn_open_item_list`/`bn_binary_view_list` for what's currently loaded and
active, and if it's unclear whether someone else is using it, ask over
`SendMessage`/`ListAgents` rather than just switching views. Mirror that
same courtesy toward whoever asks you.

**Use the usage lock instead of guessing whether Binary Ninja is in use.** No lock = free; a lock
= one session is busy. The MCP connection goes through the lock-aware proxy on port 24643 (see
`/Users/patrick/dev/bn-script-bridge/README.md`, "MCP proxy"), which adds two tools:

1. `bn_owner_get` first. `{locked: false}` -> go on. Locked by another session -> don't use
   Binary Ninja; its other tool calls are refused anyway ("Binary Ninja is locked by X:
   purpose"). Message X over `SendMessage` if you need it.
2. Before switching the active view, editing or saving: `bn_owner_set` with `name` (your
   session name) and `purpose`. Your own tool calls renew it; for long pauses renew with
   `bn_owner_set` again or pass `ttl_minutes` (default 5).
3. When done: `bn_owner_set` with `release: true`. A stale lock (holder gone) can be cleared
   with `release: true, force: true`, after asking the holder or the user.

The same lock gates `bnrun` scripts: `bnrun --status`, `--lock --purpose ...`, `--unlock`
(identity = `$CLAUDE_CODE_SESSION_ID`; the proxy's headers helper sends the same id, so MCP and
bnrun count as the same holder). The lock is coordination, not security, but it can't be
bypassed: the plugin moves Binary Ninja's own MCP server to a random port with a random token
that only the proxy knows (restarting it via Plugins > MCP > Stop/Start Server), so never
point an MCP client at Binary Ninja's server directly. MCP settings changed at runtime do
nothing until that server restarts.

## Gotchas recap

- Tools unreachable -> check Binary Ninja is running and the script bridge is started
  (`claude mcp get binaryninja` shows the proxy URL), don't assume the MCP tools are broken.
- Always `bn_binary_view_set_active` the parsed view (not Raw) before other
  calls, especially for files opened via the BN UI rather than MCP.
- `databaseBacked: false` -> unsaved work, `bn_open_item_save` before it's lost.
- `.bndb` files save next to the selected binary --
  check with concurrent sessions before committing one, it may be mid-edit.
- The active view is shared across every connected MCP client -- `bn_owner_get`, then take the
  usage lock with `bn_owner_set` before switching views, editing or saving; release it after.
- A tool call answered with "Binary Ninja is locked by X" never reached Binary Ninja: wait or ask
  X, don't retry in a loop. "Connection refused" on port 24643 = the script bridge (and with it
  the proxy) isn't running: ask the user to start it (Plugins > Script Bridge > Start).
- "Stripped" != nameless -- check `bn_symbol_list`/`bn_function_list`
  before assuming you need to hand-identify imports.
- `query` filters are substrings, not regexes; default page size is 100.
- `bn_symbol_rename` errors `symbol_not_found` on a still-auto-defined
  symbol -- use `bn_symbol_define` (with explicit `type`/`binding`) for
  a fresh rename instead, or you can end up with an ambiguous duplicate
  symbol at that address.
- Don't hand-decode a large `bn_memory_read` base64 dump for a
  struct-array -- do several small, precisely-addressed reads instead;
  manual transcription errors look like real (wrong) data, not a
  failure.
- Trace with `bn_function_decompile` + `bn_function_callers`; vendor strings give real names; `bn_data_xrefs_to`
  finds nothing for literal-pool globals (see "Tracing technique").
- A transient "auto mode classifier gave no verdict" on a `bn_*` call is a retry-once condition, not a denial.
- Need more than the `bn_*` tools offer (batch edits, IL, API calls)? Use `/Users/patrick/dev/bn-script-bridge/bnrun
  --view <file>` with the usage lock held, or `bnrun --parallel` for scripts on their own views (see
  "Beyond the MCP tools"); plain `python3` can't load binaries on this licence.
- `bnrun` hangs = a script (maybe another session's) is running or queued: `bnrun --status`, then
  `bnrun --cancel` (own) or ask its session before `--cancel --force`. A cancel doesn't interrupt a
  native call in progress (e.g. `update_analysis_and_wait`); it fires when the call returns. A
  cancelled script exits 1 with "script cancelled (by ...)"; its partial edits stay applied.
- Typing a small byte buffer as `uint8_t[N]` can make the decompiler fold separate byte stores into a wrong
  `__builtin_memset` (seen once: register `0x1010` shown where the code writes `0x101a`). Use a struct with
  one field per byte instead, and check typed output against `bn_function_disassembly`.
- Save (`bn_open_item_save`) once after a rename batch; commit a `.bndb` only where the project does that.
- A freshly opened `.bndb` already reports `modified: true`, and `bn_open_item_close` with `save: discard` then
  fails (`requires_user_choice`: the UI wants its own close prompt). Leave the item open or ask the user to
  close the tab; don't save it just to be able to close it.
- **`bn_comment_set` REPLACES an existing comment**, it does not append: read the function's decompile first (the
  comment shows as `//` lines at the top) and carry the old text into the new one.
- Binary Ninja can mis-type integer constants (ioctl request codes, flags) as pointers into the string table; they
  show up as nonsense like `"ialogBoxIndirectParamEx"` in `ioctl(...)` calls. Treat them as plain integers and say so
  in a comment on the function.
