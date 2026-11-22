# Connecting Operit to Amber Heart

[中文](operit.md)

The admin web app has the same guide under "Settings → Access keys" (设置 → 访问钥匙), with your address and key already filled in for copying. Below, replace `memory.yourdomain` with your own address; generate and copy the access key under "Settings → Access keys". Menu names are given as in Operit's Chinese interface, in parentheses.

## 1. Configure the model

1. Open Settings → Model & parameter configuration (设置 → 模型与参数配置). On first launch, tapping "Configure other models" (配置其他模型) on the start screen also leads here. Keep the "Default configuration" (默认配置).
2. API provider (API提供商): choose "OpenAI Generic" (OpenAI通用), in the "Generic compatible" (通用兼容) group.
3. API endpoint (API端点): `https://memory.yourdomain/v1`.
4. API key (API密钥): Amber Heart's access key. The API keys you got for models such as DeepSeek or Gemini were already entered when you installed Amber Heart; don't put them into Operit.
5. Model name (模型名称): tap the list icon on the right to fetch the model list and choose a chat model.
6. "Model supports image recognition" (模型支持识图): turn on if your model can read images; "Model supports ToolCall" (模型支持ToolCall): keep on.
7. Scroll down to "Summary settings" (总结设置) and turn off "Enable conversation summary" (启用对话总结). Leave context settings at their defaults; with summaries off they no longer trigger compression.

Leave other settings at their defaults.

## 2. Turn off Operit's own memory

1. Back in a chat, tap the tune icon (three lines, each with a short vertical tick) under the input box, left of the plus; expand "Memory" (记忆) and turn off "Auto-save memory" (自动保存记忆). If left on, Operit calls the model again after every turn to organize its own memory, and those requests are recorded as chat.
2. In the same panel, expand "Tools" (工具), tap "Tool management" (工具管理), and turn off `query_memory` and `get_memory_by_title`. These are tools of Operit's own memory; with them off, the companion uses Amber Heart to search memories.

## 3. Connect MCP (so the companion can search memories and write bookmarks itself)

1. Tap the menu at the top left → Package management (包管理), switch to MCP at the top, and tap + at the bottom right.
2. In the dialog, swipe left and choose "Remote" (远程).
3. Host address (主机地址): `https://memory.yourdomain/mcp`.
4. Connection type (连接类型): keep httpStream.
5. Bearer Token: the access key only, without "Bearer" — Operit adds it.
6. Plugin name (插件名称): `amber_heart`.
7. Tap Connect. The companion then activates Amber Heart by itself when needed; nothing needs turning on in the chat.
8. Open Settings → Tool permission settings (工具权限设置), tap + next to "Allow" (允许), tick "琥珀之心:memory" and "琥珀之心:mark_moment", and tap Done, so searching memories and writing bookmarks don't wait for your approval each time.

## 4. Small features

- Operit's translation uses the "Default configuration" — through Amber Heart — so translated text is recorded as chat. If you use translation, give it another configuration under Settings → Function model configuration (设置 → 功能模型配置).

## Notes

- In Operit, enter only Amber Heart's access key. Model API keys are entered only when installing Amber Heart; don't put them directly into Operit, or chats bypass Amber Heart and are neither recorded nor recalled.
- Turn off Operit's own memory and conversation summaries, and leave memory to Amber Heart.
- If Amber Heart has a persona, it replaces Operit's system prompt and character card.
- Starting a new chat in Operit does not clear the chats Amber Heart has stored; your companion still remembers what you talked about recently. To make it forget some rounds, open the Recall page in the Amber Heart admin site, tap Delete in the top right and tick the rounds to remove.

## Can't connect?

- 401: the access key wasn't copied completely or has been revoked; copy it again from the admin web app.
- 404: the API endpoint is wrong; re-enter it in the format above.
- Can't fetch models: open `https://memory.yourdomain` in a browser and check whether the login page appears. If it doesn't, the service or certificate has a problem; see the installation checks in the README.
- Chatting works but the "Recall" page has no records: this chat is most likely not using the Amber Heart configuration.
