# Import Gemini chats

[简体中文](gemini.md)

Gemini chat history isn't exported from Gemini itself but from Google Takeout.

1. Open https://takeout.google.com and click **Deselect all**.
2. Check **My Activity**, click **All activity data included** below it, deselect everything in the dialog, then check only **Gemini Apps**.
3. In the same row, click **Multiple formats** and change "Activity records" from HTML to **JSON**.
4. Create the export, wait for the email, and download the archive.
5. In the Amber Heart admin web app, go to **Settings → Import → Go to import** (设置 → 导入记忆 → 去导入), choose **Gemini**, and upload the `MyActivity.json` from the archive.
6. Gemini exports aren't split into conversations, so choose a date range in the preview, then confirm the import.

**Don't upload the whole export archive**; upload only `MyActivity.json`. An uploaded file can be at most 90 MB; if it is larger, compress it alone into a zip and upload that (text compresses very well).
