<!--
Drop the sections that do not earn their place. A one-file fix needs Summary and
Test plan and nothing else; the tables are for changes a reviewer would otherwise
have to map by hand.
-->

## Summary

<!--
Prose, not bullets. What this implements, which ADR or issue it satisfies, and
what it deliberately does NOT touch. Name the design choices inline — where the
surface lives, what it authenticates with, what is out of scope — each with its
one-line reason.
-->

---

## Test plan

<!-- [x] for what you actually ran, [ ] for what still needs a human. Real counts. -->

- [ ] All tests green
- [ ] N new tests: <what they cover, split by area>
- [ ] Existing <suite> unchanged and passing
- [ ] Manual: <exact command, what to observe>

---

## New files — what each implements

| File | Lines | Role |
|------|------:|------|
| `path/Thing` | 000 | One dense sentence naming the actual types and mechanisms, not a category |

---

## Modified files — line-by-line

| File | Change | Why |
|------|--------|-----|
| **`path/File`** | what changed | what it buys — this column is why the section is worth reading |
| | second change to the same file | |

---

## `BiggestNewFile` — section map

<!-- For the one or two largest new files. This is what lets a reviewer jump
     straight to the part they care about. -->

| Lines | What |
|------:|------|
| 00–00 | |

---

## Deliberately unchanged

<!-- What already existed and why this PR does not touch it. -->

---

## Follow-ups (out of scope)

-