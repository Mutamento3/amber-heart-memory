# Changelog

English | [中文](CHANGELOG.md)

What changed in each version. To upgrade, see [Upgrading](README.en.md#upgrading) in the README: run `git pull` in the install directory, then `./install.sh`; configuration and data are kept.

## v0.4.1 · 2026-11-20

- Chat apps resend the whole conversation each time. Amber Heart never used those older messages, and now no longer checks their content types either, so a video or file in an old message no longer makes the whole request fail. If the current turn contains an unsupported content type, the error names that type.
- The import description in Settings no longer lists every source.

## v0.4.0 · 2026-11-20

- New: hook integration. If you run your own frontend and backend, you can keep your own context assembly and use Amber Heart only for memory: fetch core memory, memory bookmarks, recent diaries and instant summaries at session start, fetch recalled cards before each model call, and send the finished turn back to be recorded, after which card extraction, diaries and summaries run as usual. Regenerating a turn replaces the previous reply; your own frontend can also attach images. See the [hook guide](docs/hooks.en.md).
- Claude Code can connect directly by pasting a snippet into its settings, whether you use it on your computer or behind your own frontend with `claude -p`.
- Hook integration requires the nightly receipt judge role; without it, automatic archiving pauses.

## v0.3.3 · 2026-11-20

- Fix: a single piece of text that looked like a password, key, ID or card number (for example a pasted `sk-` API key, or a long order number) made the whole import fail with `credential_content_blocked`, and could make a whole day's card extraction fail. Imports and extraction no longer run this check, so chat history imports as usual.

## v0.3.2 · 2026-11-19

- When importing memories you organized yourself, the date no longer has to be an exact day: write `## 2025-03` or `## 2025` if you only remember the month or the year, or `## 未注明` ("not given") if you don't remember. Cards keep the date as written, never filled in to a day or replaced by the import date; when filtering by date, a month or year counts as the whole period. See the [memory file import guide](docs/import/memory-file.en.md).

## v0.3.1 · 2026-11-19

- The "Period" (月经周期) section of memory bookmarks can be turned off (Settings → Memory bookmarks). When off, the section is hidden, left out of chats and secretary tidying, and removed from the AI's bookmark tool; anything already saved is kept and comes back when you turn it on again.
- Added this changelog.

## v0.3.0 · 2026-11-18

- Import chat history from DeepSeek's official export (format version 3). Only the selected branch of each conversation is imported; account data, reasoning, search steps and attachments are skipped.
- Every import source accepts the whole export ZIP. The browser picks out the chat data and uploads only that, so images and account files stay on the device.
- Completed imports fold into "Import history". Unconfirmed imports can be discarded, and paused imports can be finished while keeping the cards already made.
- "Daddy" is no longer a reserved address term in the lover template, so new spaces can use it as an alias.
- The README's import section now only lists the sources with a link to each import guide; the steps moved to `docs/import/`.

## v0.2.0 · 2026-11-18

- Import chats and memory entries from Kelivo backups, grouped by assistant so only the chosen assistants are imported.
- Card extraction no longer fails when a quoted line differs slightly from the source text (e.g. a dropped line break). This used to affect the nightly extraction as well as all imports.
- Cards from the same day are structured concurrently, and database connections are released while waiting for the model.
- Import progress shows how many cards have been structured.
- Admin page: aliases are optional; the getting-started card returns after a reinstall.

## v0.1.1 · 2026-11-17

- The code in `memory_core` and `memory_web` is organized into subpackages by function. No behavior changes.
- The README now describes the repository layout.

## v0.1.0 · 2026-11-17

- First public release.
