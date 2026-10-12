# bnrun ask — the Haiku interpreter layer for bn-script-bridge

_Design notes from the 2026-10-12 session; implementation target is this repo (bn-script-bridge)._

## Motivation

The BN skill (`binaryninja-bridge`) keeps fighting the same trade-off: the session's model
needs Binary Ninja API knowledge (recipes, tracing technique) to use `bnrun` well, but that
knowledge costs context tokens on every invocation — and cutting it broke sessions because
the model is **not** free-speech capable against the BN API.

Solution: stop teaching the *caller*. Give bnrun its own model — a fresh Haiku instance that
compiles natural-language instructions into BN Python — and move the API knowledge into
bnrun's own prompt, paid per call, never in session context. The session speaks a small,
stable **vocabulary**; bnrun's interpreter maps it onto the (drifting) API.

```
session ──"bnrun ask: who calls parse_header and what does it do?"──▶ bnrun ask
                                                                      │ 1. gather env (lock status, open views)
                                                                      │ 2. compile: instruction + env + BN-API
                                                                      │    cheat-sheet → Python     (Haiku via `claude -p`)
                                                                      │ 3. run through the existing bridge runner
                                                                      │    (lock/cancel/timeout machinery unchanged)
                                                                      │ 4. traceback? → feed back, regenerate (≤3 tries)
                                                                      └──▶ plain result (script shown on --show-script / error)
```

## Core decisions (settled)

1. **Direct script mode stays first-class.** `-e`/file/stdin are the primary interface —
   the calling session writes scripts itself (that's Claude's strength); `ask` is a
   convenience layer for quick one-shots where writing a script is ceremony. Nothing is
   demoted, nothing removed.
2. **`bnrun ask "<instruction>"` as an explicit subcommand** — sibling of `-e`, sharing the
   existing runner/lock/cancel/timeout machinery untouched.
3. **Compiler = `claude -p --model haiku`** — already installed and authenticated locally;
   no API key plumbing. The BN-API cheat-sheet (namespace, recipes, lock rules, exit codes)
   becomes a **prompt file in the bridge repo** — the new, context-free home for all the
   recipe material we kept shuffling between SKILL.md and reference.md.
4. **Repair loop is local to bnrun**: script error → traceback back to the compiler → fixed
   script → rerun, ≤3 attempts; then dump last script + traceback so the session can take
   over with `-e`. Intermediate failures never enter session context.
5. **Lock handling**: if the compiled script needs `bv`/`bvs`, `ask` auto-takes the lock
   with `--purpose "<instruction summary>"`; the compiler is told it may use `bv`/`bvs`;
   bnrun's existing UI-view rules enforce the rest (refusal messages already teach).
6. **Vocabulary (stable) lives in the skill; mapping (drifting) lives in the compiler
   prompt.** The vocabulary is both the *contract* (the caller can only ask for things that
   exist, phrased in terms that map) and the *capability manifest* (what exists:
   callers/callees, data refs, struct decode, naming-style rule). API churn touches the
   prompt file, never the skill.

## Vocabulary draft (skill side, ~120 tokens)

```
bnrun ask understands (combine freely):
  list / find    functions, symbols, strings, imports
  decompile      HLIL, MLIL, disassembly (one function: by name or address)
  trace          callers / callees of a function, refs to an address or string
  read           bytes at an address, as int/struct (decode it, no raw blobs)
  annotate       rename a function (match the DB's naming style),
                 comment at function start or call site (read existing first),
                 define a type
  save           persist edits into the .bndb (once per batch)
  batch          several of the above in one ask
Context it knows: the open views, which is active, analysis state.
```

Plus interface verbs: `ask "<instruction>"` / `-e '<python>'` (direct) / `--help`, and the
lock policy one-liner (`ask` auto-locks; `--force` only after asking the holder).

## CLI surface (old → new)

```
old (unchanged, stays bit-for-bit)
  bnrun [-e CODE | SCRIPT | stdin] [--view SUBSTR] [--main-thread] [--parallel]
        [--json] [--timeout N] [--session ID] [--name TEXT]
  bnrun --status | --lock [--purpose --ttl] | --unlock [--force] | --cancel [--force]
  (plus the 2026-10-12 mackup drop: --bndb PATH/--bndb-save/--bndb-close, bundled
   scripts annotate/info/decompile, bnannotate, -- ARGS/--env, --max-out)

new — purely additive
  bnrun ask "<instruction>"        natural-language layer (Haiku compiles → runs → repairs ≤3)
        --dry-run                  emit the compiled script, don't run
        --show-script              print the script that ran (line-of-sight)
        (lock auto-taken with the instruction as --purpose when bv/bvs needed)
```

## Why this beats the --help/--examples variant

Phase-1-style "make `--help` carry the knowledge" (namespace/exit codes/recipes in the
docstring) is still worth doing — it's cheap and helps direct mode — but it only moves the
tokens from *always-paid* to *fetched-on-demand by the session*. `ask` additionally:

- removes the need for the session to know the API at all (the knowledge lives in a fresh
  model's system prompt, paid per call),
- repairs its own API mistakes inside the loop where they're cheap (Haiku misreading the
  API is fixed by its own traceback, not by polluting the session),
- keeps the knowledge current: the prompt file is versioned with the bridge, while skill
  content drifts between bridge releases.

## Honest trade-offs

- Each `ask` = 1–3 Haiku calls (~2–5 s latency, cents); direct `-e` is free and instant.
- The session loses line-of-sight into the exact script → `--show-script` always available,
  `--dry-run` to draft-then-refine (use `ask --dry-run` to *generate* a script you then run
  with `-e` — a nice workflow on its own).
- The inner model can misread the API → mitigated by vocabulary-grounded instructions
  (mostly mechanical mapping), the repair loop, and refusal messages that teach.
- Hallucination containment works both ways: the caller can't invent capabilities (no
  "render the flow graph"), and the compiler gets instructions already grounded in real
  concepts — which is why Haiku (not a bigger model) suffices.

## Relationship to the pulled bundled scripts (5645e30)

`annotate`/`info`/`decompile` + `bnannotate` are a **hardcoded slice** of the same idea:
named operations callable without writing Python. `ask` generalizes it (any vocabulary
phrase, Haiku-compiled) while the bundled ones stay the deterministic, zero-cost fast path
for their exact operations. The vocabulary should grow entries for them ("bndb: cached
headless views", "annotate: R:/D:/C: directives").

## Implementation plan

1. `ask` subcommand in `bnrun` (argparse sibling of `-e`); `--dry-run`, `--show-script`.
2. `prompts/ask-system.md` (or constant): BN-API cheat-sheet — namespace, recipes, lock
   rules, exit codes, bundled-script pointers. This is where the cold recipe material
   finally lives.
3. Env gathering: `--status`, open-view list (when lock held), `--bndb` cache list.
4. Compile via `claude -p --model haiku` subprocess; extract fenced/```` ```python ```` block.
5. Run through the existing runner; on exit 1 feed traceback back (≤3 rounds).
6. Auto-lock: compiler declares `needs: ui` → bnrun takes the lock with the instruction
   as purpose; release is left to the session's normal flow (ask doesn't silently unlock).
7. Tests: fake compiler (injected, deterministic) — happy path, repair loop, dry-run,
   auto-lock, refusal paths. No live `claude` call in tests.
8. SKILL.md update: keep the whole hot path for direct mode; add one `ask` line + the
   vocabulary manifest; reference.md slims (recipes absorbed by the prompt file).

## Acceptance test

A **fresh session**, BN task, with only the updated skill (no recipes in context):
- direct-mode tasks still work (hot path unchanged);
- quick exploration tasks via `ask` succeed without the session ever writing BN Python;
- failures: `ask` exhausts 3 repairs < ~10% of calls; otherwise the vocabulary or the
  prompt file needs the missing piece added.

## Paste-prompt for other sessions (collect their knowledge into this doc)

```text
Read /Users/patrick/dev/bn-script-bridge/haiku-layer.md — the design doc for a
planned `bnrun ask` layer: bn-script-bridge's bnrun gets a natural-language subcommand,
compiled to Binary Ninja Python by an internal Haiku (via `claude -p`), with a repair
loop, auto-lock, and a stable vocabulary in the skill vs a drifting API mapping in the
compiler prompt. Direct script mode stays first-class.

You have hands-on knowledge this doc lacks. Add yours to the doc directly (edit the file,
don't just answer in chat):
- gaps/risks you see in the design (from your experience with bnrun, the lock, --bndb,
  the bundled scripts, or Binary Ninja's API where it bit you);
- API facts the compiler prompt must carry — the things you had to discover the hard way
  (fn.instructions tuple shapes, xref/literal-pool behavior, mis-typed ints, save/rollback
  semantics, per-format quirks like Mach-O frameworks or JVM classes);
- vocabulary candidates: operations you perform often that the draft manifest misses
  (see "Vocabulary draft");
- workflow ideas: where `ask` beats `-e` in your flows, and where it must never be used.

Edit surgically — add sections or bullets under the matching heading; don't restructure
or delete existing content. Mark your additions with a `<!-- from <session/project> -->`
comment. Don't commit; leave the working tree dirty.
```

Working design doc: contributions land here; it folds into the compiler prompt +
slimmed skill when `ask` is built.
