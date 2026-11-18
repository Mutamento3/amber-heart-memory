# 导入 Gemini 聊天记录

[English](gemini.en.md)

Gemini 的聊天记录不在 Gemini 里导出，而在 Google Takeout。

1. 打开 https://takeout.google.com，点**取消全选**。
2. 勾选**我的活动**，点它下面的**包括所有活动记录数据**，在弹窗里先取消全选，再只勾 **Gemini Apps**。
3. 还是这一行，点**多种格式**，把"活动记录"从 HTML 改成 **JSON**。
4. 创建导出，等邮件，下载压缩包。
5. 打开 Amber Heart 管理网页，进入**设置 → 导入记忆 → 去导入**，选择 **Gemini**，上传压缩包里的 `MyActivity.json`。
6. Gemini 的导出不分对话，在预览里按日期范围选，再点**确认导入**。

**不要上传整个导出包**，只传 `MyActivity.json`。上传的文件不超过 90 MB；超过时把它单独压缩成 zip 再上传（文字压缩率很高）。
