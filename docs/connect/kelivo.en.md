# Connecting Kelivo to Amber Heart

[中文](kelivo.md)

The admin web app has the same guide under "Settings → Access keys" (设置 → 访问钥匙), with your address and key already filled in for copying. Below, replace `memory.yourdomain` with your own address; generate and copy the access key under "Settings → Access keys". Menu names are given as in Kelivo's Chinese interface, in parentheses.

## 1. Add a provider

1. Open Settings → Providers (设置 → 供应商), tap "Add provider" at the top right, and choose the OpenAI type.
2. Name: Amber Heart / 琥珀之心 (or any name you like).
3. API Key: Amber Heart's access key. The API keys you got for models such as DeepSeek or Gemini were already entered when you installed Amber Heart; don't put them into Kelivo.
4. API address: `https://memory.yourdomain/v1`. Keep the API path at the default `/chat/completions` and leave Response API off.
5. Save, fetch the model list, and add the chat models you want to use.

## 2. Create an assistant

1. Create a new assistant and open its page.
2. Basic settings (基础设置): set the context message count (上下文消息数量) to 4. Don't turn it off — off means unlimited, and requests grow over a long chat until they may fail.
3. Prompt (提示词): if Amber Heart has a persona, this system prompt has no effect and can stay empty. "Append current time" (追加当前时间) isn't needed; Amber Heart adds the current time every turn.
4. Memory (记忆): turn off both "Use long-term memory" (使用长期记忆) and "Allow recalling past conversations" (允许回忆过去对话). With these off, Kelivo's own memory and conversation summaries don't run, so the "Memory" settings and the "Summary model" under default models can be left alone.
5. In a chat, tap the model name under the title at the top and choose a model under Amber Heart.

Leave other settings at their defaults.

## 3. Connect MCP (so the companion can search memories and write bookmarks itself)

1. Open Settings → MCP and tap + at the top right.
2. Enabled (是否启用): on; Name: Amber Heart; Transport type (传输类型): HTTP.
3. Server address (服务器地址): `https://memory.yourdomain/mcp`.
4. Custom headers (自定义请求头): tap "Add header"; header name `Authorization`, header value `Bearer <access key>` (one space after Bearer).
5. Save; Kelivo connects automatically.
6. Back in a chat, tap the fourth icon from the left under the input box (a toolbox) and turn on Amber Heart under "Tools" (工具).

## 4. Small features (Settings → Default models / 设置 → 默认模型)

- Chat suggestion model (聊天建议模型): keep it disabled. If enabled, the suggestion requests are recorded in memory as chat.
- Translation model (翻译模型): don't choose a model under Amber Heart. If it isn't set, translation uses the current chat model — through Amber Heart — and the translated text is recorded as chat.

## Notes

- In Kelivo, enter only Amber Heart's access key. Model API keys are entered only when installing Amber Heart; don't put them directly into Kelivo, or chats bypass Amber Heart and are neither recorded nor recalled.
- Turn off Kelivo's own memory and conversation summaries, and leave memory to Amber Heart.
- If Amber Heart has a persona, it replaces Kelivo's system prompt, character cards and lorebooks.

## Can't connect?

- 401: the access key wasn't copied completely or has been revoked; copy it again from the admin web app.
- 404: the API address is wrong; re-enter it in the format above.
- Can't fetch models: open `https://memory.yourdomain` in a browser and check whether the login page appears. If it doesn't, the service or certificate has a problem; see the installation checks in the README.
- Chatting works but the "Recall" page has no records: this chat is most likely not using a model under Amber Heart.
