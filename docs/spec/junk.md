# manga_pipeline spec — Junk decisions

Part of the spec; the index is [`SPEC.md`](../../SPEC.md). Update this file in the same change as the behaviour it describes.

## Junk decisions

Candidate pages are matched against the stored decisions first. A chapter with a candidate no stored decision covers waits on `junk_decision` and is held out of the library. Decisions are per page signature: the same page in 20 chapters is 1 candidate and 1 decision.

Claude makes the decisions. `junk review` runs `claude -p` on the same machine when there are undecided candidates and exits at once when there are none. It hands `claude -p` the Claude token from the config (a secret, like the Kavita key) and no other credential. A systemd timer starts it; no part starts or waits for Claude. Claude lists the candidates with `junk list`, looks at the page images, and records each verdict with `junk decide`: junk, keep or unsure. An unsure verdict makes the chapter `needs_human`, and so does a candidate still undecided after the review limit; only a review in which Claude was shown the candidate counts toward it. Claude may read files and run the `junk` commands, nothing else. The pipeline reads decisions only from the store.
