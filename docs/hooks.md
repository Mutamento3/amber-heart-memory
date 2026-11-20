# 用 Hook 接入 Amber Heart

把这份说明交给实现接入的 AI 即可。你的程序照常管理人设、对话历史和模型调用。Amber Heart 在会话开始时提供固定记忆，每轮生成前召回卡片，收到完整回复后记录对话。已记录的轮次进入日记、抽卡、及时摘要和夜间回执补判流程。

在 Amber Heart 的管理页为目标空间生成访问钥匙，并在自己的后端保管。三条接口都发往同一个 Amber Heart 服务，使用 `POST`、`Content-Type: application/json` 和 `Authorization: Bearer <空间访问钥匙>`。浏览器前端通过自己的后端请求，Claude Code 可以直接请求。单次请求最多 16 MiB。

## 自建前后端

### 1. 会话开始

`POST /hooks/session-start`

请求体示例

```json
{"conversation_id": "chat-42"}
```

`conversation_id` 是可选的字符串，最长 128 字符。省略或传 `null` 时，通用接口把这些轮次视作同一段默认对话。固定记忆按钥匙所属空间读取。没有对话编号时可以发送 `{}`。

以下响应示例缩短了各段正文。

```json
{
  "core_memory": "<core_memory>…</core_memory>",
  "recent_notes": "整理好的书签上下文…",
  "diary": "<recent_diaries>…</recent_diaries>",
  "summary": "<instant_summaries>…</instant_summaries>",
  "context": "<core_memory>…</core_memory>\n\n<recent_diaries>…</recent_diaries>\n\n整理好的书签上下文…\n\n<instant_summaries>…</instant_summaries>"
}
```

四个字段各自是可直接注入的字符串，空段为 `""`。`context` 用空行拼接非空段，顺序是核心记忆、近日日记、已整理的记忆书签、当前业务日的及时摘要。按需取分段或整段，任选一种送给模型。人设由你的程序管理。

### 2. 发送用户消息后、调模型前

`POST /hooks/user-prompt-submit`

```json
{
  "conversation_id": "chat-42",
  "turn_id": "turn-7",
  "message": "还记得我们约好读哪本书吗？",
  "images": [{"url": "https://example.com/book.jpg", "detail": "auto"}]
}
```

`turn_id` 在同一对话内唯一，接受 1–128 字符的字符串或 0 到 JavaScript 最大安全整数的整数。整数与对应的十进制字符串视作同一个编号。重试同一条消息时沿用原编号和正文。两次调用里 `conversation_id` 的传法须一致。

`images` 可省略。图片用 HTTP(S) URL 或 `data:image/…;base64,…` 数据地址，`detail` 可省略，也可填 `auto`、`low` 或 `high`。纯图片消息可省略 `message`，并跳过文字召回。已配置图片描述岗位时，后台将描述写入该轮，原图和地址不入库。

```json
{
  "context": "<retrieved_memories>\n…\n[1] 标题：周末读书\n日期：2026-06-01\n约好一起读科普书。\n</retrieved_memories>",
  "cards": [{
    "memory_id": 123,
    "number": 1,
    "title": "周末读书",
    "content": "约好一起读科普书。",
    "source_date": "2026-06-01",
    "item_kind": "atomic",
    "experience_mode": null
  }]
}
```

把 `context` 放进本轮模型上下文，`cards` 可用来展示召回详情。日期和标题可能是 `null`。召回沿用聊天代理的规则、截止时间（默认 8 秒）和降级。没有可用卡片时返回 `{"context":"","cards":[]}`，可以继续调用模型。Amber Heart 暂存用户消息和返回的卡片。

### 3. AI 完整回复后

`POST /hooks/stop`

```json
{"conversation_id": "chat-42", "turn_id": "turn-7", "reply": "我们约好周末一起读科普书。"}
```

```json
{"turn_id": 208, "replaced": false}
```

请求用第二步的原始编号。`reply` 传完整可见回复，流式输出先在自己这边拼好。返回的 `turn_id` 是 Amber Heart 内部记录编号。重新生成时带原编号和新回复再发一次，`replaced` 为 `true`。重发相同回复时返回现有记录，`replaced` 为 `false`。

未收到回复的暂存轮次 30 分钟后过期。首个完整回复决定轮次归入哪个业务日，重新生成沿用该业务日。已经跑完的夜间任务不会因替换旧回复自动重跑。

错误响应带 `error.code` 和 `error.message`。常见状态为 `400` 无效请求、`401` 无效或已作废的钥匙、`404` 无法配对或已过期、`409` 同编号对应不同用户消息或召回尚未完成、`413` 请求过大、`503` 服务暂不可用或需升级数据库。

## Claude Code 直连

给启动 Claude Code 的进程设置环境变量 `AMBER_HEART_ACCESS_KEY` 为目标空间的钥匙。把以下配置合并到 `~/.claude/settings.json` 或项目的 `.claude/settings.local.json`，替换示例域名。可在 `CLAUDE.md` 里管理人设。 [Claude Code Hook 官方文档](https://code.claude.com/docs/en/hooks)

```json
{
  "hooks": {
    "SessionStart": [
      {"matcher": "startup|clear|compact", "hooks": [{
        "type": "command",
        "command": "curl -sS -X POST -H \"Authorization: Bearer $AMBER_HEART_ACCESS_KEY\" -H 'Content-Type: application/json' --data-binary @- https://memory.example.com/hooks/session-start",
        "timeout": 30
      }]}
    ],
    "UserPromptSubmit": [
      {"hooks": [{
        "type": "http",
        "url": "https://memory.example.com/hooks/user-prompt-submit",
        "headers": {"Authorization": "Bearer $AMBER_HEART_ACCESS_KEY"},
        "allowedEnvVars": ["AMBER_HEART_ACCESS_KEY"],
        "timeout": 30
      }]}
    ],
    "Stop": [
      {"hooks": [{
        "type": "http",
        "url": "https://memory.example.com/hooks/stop",
        "headers": {"Authorization": "Bearer $AMBER_HEART_ACCESS_KEY"},
        "allowedEnvVars": ["AMBER_HEART_ACCESS_KEY"]
      }]}
    ]
  }
}
```

SessionStart 只支持 `command` 类型的 hook，不支持 `http`，所以会话开始这一步用 `curl` 转发。`matcher` 让它只在新会话、`/clear` 和压缩后触发：用 `--resume` 或 `--continue` 接着聊时不再重复注入固定记忆。

如果你的自建前端在后端调用 `claude -p`，这份配置同样生效（已用 `claude -p` 和 `--resume` 实测）。你也可以不配 Claude Code 的 hook，改由自己的后端在调用前后直接请求上面三条通用接口，这样还能带图片。

Amber Heart 直接读取事件 JSON。它按 `session_id` 和 `prompt_id` 配对 `UserPromptSubmit.prompt` 与 `Stop.last_assistant_message`。旧客户端省略 `prompt_id` 时，使用对应会话里最近一条无编号的提交。响应格式如下，`hookEventName` 跟随实际事件，`additionalContext` 最长 10,000 字符。Stop 的上下文为空，使 Claude Code 正常结束该轮。

```json
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"<retrieved_memories>…</retrieved_memories>"}}
```

Claude Code 只传文字。打断后没有 Stop 的轮次会过期。定时任务或子任务汇报也可能触发 UserPromptSubmit，并作为普通消息记录。有 Stop 但无可配对的消息时，接口返回空上下文。

## 夜间回执补判

Hook 接入要配置 **夜间回执补判** 岗位（`MEMORY_RECEIPT_JUDGE_MODEL`）。Hook 轮次的用卡情况由夜间补判记录。未配置时，Hook 轮次保持未报告，自动归档会因回执不足暂停。
