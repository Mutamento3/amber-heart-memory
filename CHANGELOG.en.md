# Changelog

English | [中文](CHANGELOG.md)

What changed in each version. To upgrade, see [Upgrading](README.en.md#upgrading) in the README: run `git pull` in the install directory, then `./install.sh`; configuration and data are kept.

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
