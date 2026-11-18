# Import DeepSeek chats

[简体中文](deepseek.md)

1. Open [DeepSeek on the web](https://chat.deepseek.com), click your profile icon, and go to **Settings → Data Management → Export all chat history**.
2. Once the export is ready, use the link from DeepSeek to download the ZIP. The download link is valid for seven days.
3. In the Amber Heart admin web app, go to **Settings → Import → Go to import** (设置 → 导入记忆 → 去导入), choose **DeepSeek**, and upload the entire ZIP.
4. The preview lists conversations with all selected by default. Uncheck any conversations you want to leave out, then confirm the import.

Amber Heart supports the official ZIP export format version 3. It imports chat text from the selected branch of each conversation and skips account data, reasoning, search activity, images, and other attachments.

The page selects the needed files on your device before uploading. If it cannot filter the archive, it uploads the original ZIP. The upload limit of 90 MiB applies to the data actually sent.
