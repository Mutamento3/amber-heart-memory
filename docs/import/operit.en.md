# Import chats and memories from Operit

[简体中文](operit.md)

1. In Operit, open **Settings → Data Backup & Restore** (设置 → 数据备份与恢复).
2. For chats, open **Chat history** (聊天记录) and choose **Export JSON** (导出 JSON). For memories, open **Memory library** (记忆库) on the same page and choose **Export** (导出). Each export produces a separate file.
3. In Amber Heart, open **设置 → 导入记忆 → 去导入** (Settings → Import memories → Go to import) and select **Operit**. Choose one JSON file, select the two JSON files together, or choose a ZIP containing the exports. Click **上传并预览** (Upload and preview).
4. Choose chats by character card or by conversation, then set the chat date range. To import memories, expand **Operit 记忆** (Operit memories), check the entries you want, and click **确认导入** (Confirm import). Memory entries start unchecked, and the date range filters chats only.

Chats after you connected to Amber Heart have already been recorded. Set the import end date to before you connected.

Amber Heart imports the reply version selected in Operit and keeps the text from you and the AI. It skips summaries, reasoning, and tool content. Images and other files appear as text placeholders. Branches and group chats are treated as ordinary conversations.

The memory import accepts plain-text entries and sends the title and body together for processing. Entries produced by memory analysis use their creation date in the installation time zone. All other entries are undated.

The browser selects JSON files from a ZIP before upload. If it cannot filter the archive, it uploads the original ZIP. The upload limit of 90 MiB applies to the data actually sent.
