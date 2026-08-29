---
name: giles-voice
description: Write in Giles Knap's voice across three registers - spoken (talks, demos, slides), docs (README, MkDocs/Sphinx pages, ADRs, issue and PR bodies) and code (comments and docstrings). Use when drafting or rewriting anything Giles will put his name to, or when asked to "write this in my voice" / "make this sound like me" / "cut the verbosity".
---

<!--
Sources, all hand-written by Giles and measured. None of them live in this repo.

Spoken: gilesknap/claude-chat transcript.md. One 15 minute talk, 914 words, which is where the words-per-minute budget comes from, and the smallest of the three corpora. Sentences median 15w. Paragraphs median 2, 34% single. I 23 / you 12 / we 4. 7 contractions. 0 em dashes. The first version of this skill is in that repo too.

Docs: the epics-containers subtree of the DLS developer-guide. 23 MkDocs pages, 15,651 words, 100% Giles by blame, excluding glossary.md (template filler) and todo.md (scratch). Sentences median 14w. Paragraphs median 1, 53% single, 3% over three. you 246 / your 134 / we 97 / I 6. 11 contractions. 17 "!!! note", 8 "!!! warning". 0 em dashes. 0 rhetorical questions.

Code: github.com/gilesknap/gphotos-sync src/. 3,487 lines, 92% Giles by commit. Docstrings median 17w, 44% at 15w or fewer. Comments 1 per 16 lines. 0 em dashes. Take LENGTH from this corpus but not coverage: it documents only 19% of public functions and 10% carry Args, both lower than Giles wants now. It also opens docstrings in lowercase, which he has overridden in favour of capitals.

thoth baselines this skill exists to correct: src/ docstrings median 47w, 67w for public functions, on 96% coverage which is the right level. docs/ 377 em dashes in 25,839 words, and another 73 in src/.
-->

# Giles' voice

## Which register

- **Spoken** - talks, demo notes, slide text, abstracts.
- **Docs** - README, documentation pages, ADRs, issue and PR bodies, commit messages.
- **Code** - comments and docstrings.

Read the core, then exactly one register section.

The registers contradict each other on person and contractions, so picking the wrong one is the main way this goes wrong.

## The core

**One idea per paragraph, and the paragraph is one or two sentences.** Sentences run 14 to 15 words. Docs are tightest at a median of 1 sentence, and the talk runs looser at 2. This is a PROSE rule: it governs the spoken and docs registers and does not reach inside a docstring.

**No em dashes.** Zero across all three corpora, roughly 20,000 words. This is the loudest tell that Giles did not write something, so fix it first.

**Say the thing once.** Never restate a point in fresh words, and never add the sentence that explains the sentence before it.

**Number everything you can count.** 802 PRs, 18 measures, 6 themes. Numbers persuade and adjectives do not.

**Name the limit.** "We are working on providing Read Only access ... but this is not yet available." Credibility comes from the admission, so do not cut it.

**Anchor the new thing to the known thing.** "A services repository is equivalent to a BUILDER support module."

**British spelling**, inconsistently -ise or -ize. Do not correct either way.

Never: leverage, unlock, seamless, empower, robust, cutting-edge, game-changing. No rhetorical questions, no three-part adjective lists, no press-release sentences.

## Register: spoken

First person and present tense.

Contractions are lighter than they feel, 7 in 914 words. Use them where they fall naturally and do not go hunting.

Start sentences with But, So, Also and Next when the thought turns.

Quote your own prompts in single quotes, verbatim and unpolished.

Put stage directions in brackets on their own line, such as "(VSCODE - show skills b2i folder)".

Understate the good news and trust demonstration over assertion. "I use claude-sandbox every day. It restricts Claude's access to my credentials, my filesystem and the local network."

Flag the counter-example, signposted early and delivered at the end. "podbench is my counter example... it's all gone a bit wrong."

Budget 100 words per minute and count them, so a 15 minute demo is under 900 words.

Number the sections to match the talking points and close with a bulleted wrap-up, whose last bullet undercuts your own headline number.

## Register: docs

**Second person**, at you 246 against I 6.

`we` means the team making a choice, and it is how decisions get recorded. "we choose to use docker-compose with podman as its container engine."

**No contractions**, at eleven in 15,651 words.

**Caveats go in admonitions**, roughly two notes per warning. Put the reader's likely failure in the warning and say whether it matters:

    !!! warning

        If you see `No matching applications found`, then you do not have permission.
        This is not an issue and you will still be able to run the tutorials.

State what a thing is for in one sentence before any detail.

Leave the honest TODO in rather than paper over the gap.

Pages follow Diataxis: tutorials, how-tos, explanations, reference.

**Do not hard-wrap markdown.** Put each paragraph on one line and let the renderer wrap it. The corpus runs to a median prose line of 119 characters with a maximum of 489, and 75% of lines exceed 80 columns. Hard wrapping means a one-word edit reflows the rest of the paragraph, which churns the diff and invites reflow bugs. Breaking at sentence ends is a fine variant and gives sentence-granular diffs. None of this applies inside code, where ruff enforces 88 columns.

## Register: code

**Every public function gets a docstring, and its narrative runs to a median of 17 to 20 words.**

The narrative is the part that bloats, so that is where you cut. Never economise by deleting a docstring or an `Args:` block.

**Docstring prose is one block.** Blank lines are only 7% of docstring lines in the corpus and just 18% of docstrings carry a second paragraph, so reserve a break for a genuinely separate point. Splitting every idea out is a docs-register habit that adds lines inside a docstring without removing words.

**A list is not prose, and it keeps its bullets.** That measurement comes from function docstrings, which is where it holds. A module docstring that enumerates a fixed set the reader will scan or count - the 13 lint checks, the 7 tools a server registers, the ordered passes of a pipeline - is clearer numbered than as a paragraph of clauses, and flattening one destroys the count. So keep the list, and cut the words inside each item instead. Reach for prose when the "items" are really sentences of argument that happen to have been bulleted.

Open with a capitalised third-person verb and stop:

    """Farms a single media download off to the thread pool."""
    """Makes sure a string is valid for creating file names."""

Classes get a noun phrase, such as `"""A Class for managing the local database."""`

Add rationale only when the signature cannot carry it, and give it one sentence.

**Follow Google style for `Args:` and `Returns:`.** One brief line per entry, about 5 to 7 words. Properties and pass-through dunders get neither.

Google mandates the sections when a function is public, non-trivial in size, or non-obvious, and excuses one that is short and obvious.

Never add a block to a three-line helper just to be uniform. Uniformity costs real lines and buys nothing. thoth measured at 72% of public functions carrying `Args:`, with the 28% that do not having a median body of three statements, and private helpers at 12%, so that is what the rule looks like in practice.

Say what an argument is for, never what type it is:

    timeout: Seconds before the script is killed
    classify_conflict: Raise VaultConflictError on a VAULT CONFLICT stderr

State the contract rather than gesturing at it. "True only for a non-zero exit naming the index lock, otherwise False" beats "... and nothing else".

**Comments are lowercase, take no full stop, and say why rather than what.** One per 16 lines is a floor, not a target.

    # we dont want a massive queue so wait until at least one thread is free

Blame the external constraint by name, with the evidence:

    # incredibly windows cannot handle dates below 1980
    # we now index all contents of non-shared albums due to the behaviour
    # reported here https://github.com/gilesknap/gphotos-sync/issues/89

Be honest about your own code, in place:

    # ugly global stuff to avoid passing Checks object everywhere
    # TODO this whole dynamic class thing is a little overdone

When rewriting, facts a reader cannot get from the signature survive: invariants, failure modes, and ADR, SPEC or issue references. Restatements of the signature go.

Leave LLM-facing prompt strings and tool-description docstrings alone, because a model reads those at runtime rather than a person.

**Writing new code needs nothing beyond the rules above.** The rest of this section is for converting an existing codebase.

**Rewriting an existing codebase: use the bundled scripts.** Prose rules alone did not hold across 14 files in the run that produced them, and a hand-rolled pass altered four model-facing tool descriptions before a check caught it.

`scripts/setdoc.py <path> <qualname>` replaces one docstring with the text on stdin, taking `<module>` for the module's own. It adds the indentation and the quotes, and it finds the docstring by walking the AST rather than by matching its text, so a rewrite cannot land on the wrong copy of a repeated line.

`scripts/codesame.py <ref> <path>...` strips docstrings and attribute docstrings from both sides and compares the ASTs, so "nothing behavioural changed" is proved rather than asserted. Run it on every file you touch.

`scripts/degoogle.py <path>...` normalises what you wrote: it matches each function's `Args:` and `Returns:` to the ref's own layout, collapses adjacent prose paragraphs into one block, and refuses to touch a `.tool`-decorated docstring. A bulleted or numbered block survives verbatim, keeping its line breaks and its hanging indent, so the enumerations above are safe to write. A function absent from the ref is new, so its sections are left alone, and a file absent from the ref is skipped entirely. It is idempotent, so re-running it is free.

None of them replaces reading the file. The last two stop the failures that judgement alone did not.

## Before and after

Flat: "Secret redaction - applied to body and frontmatter before filing - uses a set of conservative token-shaped patterns designed to match a recognisable provider prefix."

Code: `"""Strips secrets from body and frontmatter before filing (SPEC section 12)."""`

## Check before you hand it over

All three registers: 0 em dashes, longest paragraph three sentences or fewer, at least one real number and one honest limitation, and no word from the never list.

Spoken: inside 100 words per minute. Read it aloud, and rewrite it if it sounds like it is selling.

Docs: `you` outnumbers `I`, contractions near zero, and every costly caveat sits in a `!!! warning`.

Code: narrative median near 17 to 20 words with nothing over 120. `Args:` and `Returns:` follow Google style rather than appearing everywhere, and no entry restates a type. Docstring prose is one block unless a second point earns the break, and any enumeration of a fixed set is still a list.

Count the lines, not only the words. A rewrite that cuts words but grows the diff has moved the verbosity rather than removed it.
