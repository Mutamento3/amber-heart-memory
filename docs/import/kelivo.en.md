# Import chats and memories from Kelivo

[简体中文](kelivo.md)

Kelivo's menu labels may vary by version.

1. In Kelivo, open **Settings → Data Backup** to reach the **Backup & Restore** page.
2. Open **Backup and import content**, choose **Deselect all**, then enable **Chats** and **Assistants**. Enable **Memories** too if you want to import them. Leave **Files** off and choose **Save**.
3. If you enabled backup encryption, turn it off before exporting. Under **Local Backup**, choose the option to export to a file and save the `.zip` archive.
4. In the Amber Heart admin page, open **设置 → 导入记忆 → 去导入** (Settings → Import memories → Go to import). Select **Kelivo**, select the entire backup ZIP, and click **上传并预览** (Upload and preview).
5. In the preview, choose the assistants or conversations for the current memory space and set the chat date range. You can select several groups for the same character. To import memories too, expand the groups in **Kelivo 记忆** (Kelivo memories) and check individual entries before you click **确认导入** (Confirm import). Global memories may mix entries from different assistants, so review them individually.

The **Backup and import content** setting applies to local, WebDAV, and S3 backups. After exporting, return to it, choose **Select all**, and save so future backups include every category.

Chats after you connected to Amber Heart have already been recorded. Set the import end date to before you connected.

If the backup has no name for an assistant, the preview shows a numbered label such as **助手 1** (Assistant 1) and a few conversation titles to help you identify it. Conversations without an assigned assistant appear under **未归属** (Unassigned).

If your chats are still in Kelivo, importing the chats is enough. Use memories to recover details from chats you deleted. Legacy memories with no date stay undated.

Amber Heart imports text messages from you and the AI, using the selected version when available. To import chats from an older `chats.json` backup, update Kelivo and create a new backup.

The page selects the needed files on your device before uploading. If it cannot filter the archive, it uploads the original ZIP. The upload limit of 90 MiB applies to the data actually sent.
