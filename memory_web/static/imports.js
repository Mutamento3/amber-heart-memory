import {packOperitFiles, prepareImportUpload} from "./import-zip.js?v=2";

const names = {chatgpt: "ChatGPT", claude: "Claude", gemini: "Gemini", deepseek: "DeepSeek", kelivo: "Kelivo", operit: "Operit", memory: "自己整理的记忆"};
const usesGroups = source => source === "kelivo" || source === "operit";
const usesDateRange = source => source === "gemini" || usesGroups(source);
const usesConversationSelection = source => usesGroups(source) || ["chatgpt", "claude", "deepseek"].includes(source);
const statuses = {preview: "待确认", queued: "排队中", running: "处理中", completed: "已完成", failed: "已暂停"};
const MEMORY_SAMPLE = `## 2025-03-14
小雨和阿言第一次一起去看海。出发前阿言说要在海边看日出，结果两人都睡过了头，赶到时太阳已经老高了。那天风很大，小雨的帽子被吹进海里，阿言笑了她一路，说以后每年都要来，补上那次日出。

## 2025-05-02
小雨和阿言吵了一架。那阵子小雨连着熬夜赶稿，阿言催她睡觉，她嫌烦，说了句气话就关了手机。第二天两人把话说开，约定生气不过夜；小雨说“只是想吐槽”的时候，阿言先听她说完，不急着讲道理。

## 未注明
小雨不吃香菜和葱，点外卖要备注。咖啡只喝燕麦拿铁，不加糖；空腹喝会胃疼，上次早上空腹喝完疼了一上午。`;
const errors = {
  import_configuration_changed: "导入期间设置有变化，恢复原设置后可继续；已有草稿仍在待审核中。",
  import_material_expired: "原材料已到保留期限，请重新上传。",
  import_stopped: "已手动停止，可以随时继续",
  import_selection_failed: "挑选没有完成，重试会从这一步继续。",
  import_structure_failed: "部分卡片整理失败，重试只补未完成的部分。",
};
const actionConfirmations = {
  discard: "放弃这次导入？待确认的批次和暂存材料会被删除。",
  end: "结束这次导入？已整理和已收录的卡片会保留，剩下的部分不会再导入。以后可以重新上传同一份文件补导，已导过的部分会自动跳过。",
};

export async function importsPage(main, {api, read, keepView, url, esc, state, epoch}) {
  const $ = (selector) => main.querySelector(selector);
  const $$ = (selector) => [...main.querySelectorAll(selector)];
  const live = () => main.isConnected && state.epoch === epoch;
  let config = await read(url("/imports"));
  if (!live()) return;
  let current = null, plan = null, revision = 0, listRevision = 0, timer;
  const active = job => job.status === "queued" || job.status === "running";
  const actionPending = new Set();
  const discardedPreviews = new Set();
  main.innerHTML = `<form id="import-upload" class="import-upload"><fieldset>
    <label>从哪里搬来<select id="import-source" name="source">${Object.entries(names).map(([key, name]) => `<option value="${key}">${name}</option>`).join("")}</select></label>
    <div class="import-file-row"><button type="button" id="pick-import-file">选择文件</button><span id="import-file-name" class="muted">未选择文件</span><input id="import-file" type="file" accept=".json,.zip" hidden></div>
    <p id="import-help" class="meta"></p><div id="import-sample" class="import-sample" hidden><p class="meta">示例（把小雨和阿言换成你们的名字）：</p><pre></pre></div><button class="primary" id="upload-preview" type="submit">上传并预览</button>
    <p class="meta">ZIP 会先在本机筛选所需文件，无法筛选时按原包上传。实际上传上限 ${Math.round(config.max_upload_bytes / 1024 / 1024)} MiB，解压后的聊天数据上限 ${config.max_json_bytes / 1024 / 1024 / 1024} GiB。确认后才会调用模型。</p>
    </fieldset></form><p id="import-error" class="error" role="alert"></p>
    ${config.configured ? "" : '<p class="meta">尚未配置抽卡模型，可以先上传预览。</p>'}
    <section id="import-preview" class="import-preview" hidden></section>
    <section class="panel"><div class="panelhead"><h2>导入进度</h2><button type="button" class="quiet" id="refresh-imports">刷新</button></div><div id="import-jobs" aria-live="polite"></div></section>`;
  const showError = (error) => { if (live()) $("#import-error").textContent = error.message; };
  const help = () => {
    const source = $("#import-source").value;
    $("#import-file").accept = ({memory: ".md,.txt", deepseek: ".zip", kelivo: ".zip,.kbackup"})[source] || ".json,.zip";
    $("#import-file").multiple = source === "operit";
    $("#import-help").textContent = ({
      chatgpt: "直接选择官方导出 ZIP。只读取最终选定的聊天分支。",
      claude: "直接选择 Claude 官方导出 ZIP。预览时可以按对话选择。",
      gemini: "在 Google Takeout 的 My Activity → Gemini Apps 选择 JSON 格式，再直接选择导出 ZIP。日期范围内的普通聊天和 Gem 聊天都会导入。",
      deepseek: "上传完整的 DeepSeek 官方导出 ZIP。只导入当前选择分支的聊天文字。",
      kelivo: "在 Kelivo 选择「聊天记录」和「助手」。想导入记忆时再选「记忆」。上传未加密的 ZIP。接入 Amber Heart 之后的聊天已经记录过了，导入时把结束日期选在接入之前。",
      operit: "在 Operit 的「设置 → 数据备份与恢复」里，分别导出「聊天记录」的 JSON 或「记忆库」。可以选一个 JSON、同时选两个 JSON，或上传装有导出文件的 ZIP。接入 Amber Heart 之后的聊天已经记录过了，导入时把结束日期选在接入之前。",
      memory: "每条以 ## 日期 开头，下面写正文，可以写多行。只记得月份或年份就写 ## 2025-03 或 ## 2025，不记得日期写 ## 未注明；支持 .md 和 .txt 文件。用名字来写，不要用“我”“他”，设置里填的名字或又名都可以。写得越具体，整理出来的卡片越有用。也可以把下面的示例发给你的 AI，让它照这个格式帮你整理。",
    })[source];
    $("#import-sample").hidden = source !== "memory";
  };
  $("#import-sample pre").textContent = MEMORY_SAMPLE;
  help();
  const showFileName = () => {
    const names = [...$("#import-file").files].map(file => file.name);
    $("#import-file-name").textContent = names.join("、") || "未选择文件";
    $("#import-file-name").classList.toggle("muted", !names.length);
  };
  $("#pick-import-file").onclick = () => $("#import-file").click();
  $("#import-file").onchange = showFileName;
  $("#import-source").onchange = () => { $("#import-file").value = ""; showFileName(); help(); };

  function selection() {
    const filters = usesDateRange(plan.source) && plan.first_date ? {
      date_start: $("#import-start").value || null,
      date_end: $("#import-end").value || null,
    } : {};
    if (usesConversationSelection(plan.source)) {
      filters.selected_conversations = $$("[data-import-conversation]:checked").map(input => input.value);
    }
    if (usesGroups(plan.source)) {
      filters.selected_memories = $$("[data-import-memory]:checked").map(input => input.value);
    }
    return filters;
  }
  function conversationChoice(item) {
    const dates = item.first_date === item.last_date ? esc(item.first_date)
      : `${esc(item.first_date)} 至 ${esc(item.last_date)}`;
    return `<label class="import-conversation">
      <input type="checkbox" data-import-conversation value="${esc(item.key)}" ${item.selected ? "checked" : ""}>
      <span><strong>${esc(item.title)}</strong><span class="meta">${dates} · ${item.message_count} 条消息${item.imported_day_count ? ` · 已导过 ${item.imported_day_count} 天` : ""}</span></span>
    </label>`;
  }
  function conversationChoices() {
    if (usesGroups(plan.source)) {
      if (!plan.assistant_groups.length) return "";
      const group = plan.source === "operit" ? "角色卡" : "助手";
      return `<p class="meta">勾选要导入的${group}或对话，可以同时选多个${group}。</p>${assistantChoices(plan.assistant_groups, "conversations")}`;
    }
    if (!usesConversationSelection(plan.source)) return "";
    return `<div class="import-select-actions"><button type="button" class="quiet" id="select-all-imports">全选</button><button type="button" class="quiet" id="select-no-imports">取消全选</button></div>
      <div class="import-conversations">${plan.conversations.map(conversationChoice).join("")}</div>`;
  }
  function memoryChoice(item) {
    const date = item.source_date ? esc(item.source_date) : "日期未注明";
    return `<label class="import-conversation">
      <input type="checkbox" data-import-memory value="${esc(item.key)}" ${item.selected ? "checked" : ""}>
      <span><strong>${esc(item.text ?? item.title)}</strong><span class="meta">${date}${item.imported ? " · 已导过" : ""}</span></span>
    </label>`;
  }
  function assistantChoices(groups, kind) {
    const memories = kind === "memories";
    const markup = groups.map((group, index) => {
      const items = group[kind];
      const examples = group.sample_titles.length
        ? `<span class="meta">${group.sample_titles.map(esc).join(" · ")}</span>` : "";
      const label = `import-assistant-${kind}-${index}`;
      const choices = items.map(memories ? memoryChoice : conversationChoice).join("");
      return `<div class="import-assistant-group" role="group" aria-labelledby="${label}" data-import-assistant-group>
        <label class="import-conversation import-assistant-heading">
          <input type="checkbox" data-import-assistant>
          <span><strong id="${label}">${esc(group.name)} · ${items.length} ${memories ? "条记忆" : "个对话"}</strong>${examples}</span>
        </label>
        ${memories ? `<details class="import-memory-entries"><summary>查看条目</summary>${choices}</details>` : choices}
      </div>`;
    });
    return `<div class="import-conversations">${markup.join("")}</div>`;
  }
  function memoryChoices() {
    if (!usesGroups(plan.source) || !plan.memory_groups.length) return "";
    const count = plan.memory_groups.reduce((total, group) => total + group.memories.length, 0);
    return `<section class="import-memory-section"><div class="panelhead"><h3>${names[plan.source]} 记忆 ${count} 条</h3></div>
      <p class="meta">聊天还在的话导聊天就够了，聊天删掉的可以用记忆补回来。</p>
      ${plan.memory_groups.some(group => group.name === "全局记忆") ? '<p class="meta">全局记忆可能涉及多个助手，可以展开后按条挑选。</p>' : ""}
      ${assistantChoices(plan.memory_groups, "memories")}
    </section>`;
  }
  function counts() {
    const calls = plan.model_calls_min === plan.model_calls_max ? `${plan.model_calls_max}` : `${plan.model_calls_min}～${plan.model_calls_max}`;
    const grouped = usesGroups(plan.source);
    const showChat = plan.source !== "memory" && (!grouped || plan.first_date);
    const showMemory = plan.source === "memory" || (grouped && plan.memory_groups.length);
    $("#import-counts").innerHTML = `${!grouped || showChat ? `<span><strong>${plan.day_count}</strong> 天</span>` : ""}
      ${showChat ? `<span><strong>${plan.message_count}</strong> 条消息</span>` : ""}
      ${showMemory ? `<span><strong>${plan.memory_count}</strong> 条记忆</span>` : ""}
      <span><strong>${calls}</strong> 次模型调用</span>`;
    $("#import-skip").textContent = plan.skipped_count ? `${plan.skipped_count} 份原材料已经导过，将跳过。` : "";
    $("#confirm-import").disabled = !config.configured || !plan.material_count;
  }
  async function recalculate() {
    const request = ++revision;
    $("#confirm-import").disabled = true;
    try {
      const next = await api(url(`/imports/${current}/preview`), {method: "POST", body: selection()});
      if (!live() || request !== revision) return;
      plan = next;
      counts();
    } catch (error) { if (request === revision) showError(error); }
  }
  async function openPreview(identifier) {
    const request = ++revision;
    const next = await api(url(`/imports/${identifier}/preview`), {method: "POST", body: {}});
    if (!live() || request !== revision || discardedPreviews.has(identifier)) return;
    current = identifier;
    plan = next;
    state.dirty = true;
    const range = usesDateRange(plan.source) && plan.first_date
      ? `<div class="import-range"><label>从哪天<input type="date" id="import-start" value="${esc(plan.first_date)}" min="${esc(plan.first_date)}" max="${esc(plan.last_date)}"></label><label>到哪天<input type="date" id="import-end" value="${esc(plan.last_date)}" min="${esc(plan.first_date)}" max="${esc(plan.last_date)}"></label></div>`
      : "";
    const choices = range + conversationChoices() + memoryChoices();
    const grouped = usesGroups(plan.source);
    const modelInfo = plan.source === "memory" || (grouped && !plan.first_date)
      ? "每条记忆调用一次整理。"
      : grouped && plan.memory_groups.length
        ? "聊天先挑选再整理成卡，每条记忆直接整理一次。"
        : "先挑选，再按结果整理成卡，调用次数显示预计范围。";
    const panel = $("#import-preview");
    panel.hidden = false;
    panel.innerHTML = `<div class="panelhead"><h2>${names[plan.source]} · 导入预览</h2></div>${choices}<div id="import-counts" class="import-counts"></div><p id="import-skip" class="meta"></p><p class="meta">${modelInfo}格式错误等重试可能增加调用，最多按上述上限的两倍计算。不补写日记。</p>${config.auto_approve_supported ? '<label class="check"><input type="checkbox" id="import-auto-approve" checked>整理好后直接收录，不进待审核</label>' : ""}<div class="actions"><button type="button" class="primary" id="confirm-import">确认导入</button><button type="button" class="quiet" id="close-import-preview">收起预览</button></div><p class="meta">确认后会在后台处理。收录或整理失败的草稿留在待审核。原聊天不会混入你的当天聊天历史或摘要。</p>`;
    counts();
    $$("[data-import-conversation], #import-start, #import-end").forEach(input => input.onchange = recalculate);
    for (const group of $$("[data-import-assistant-group]")) {
      const toggle = group.querySelector("[data-import-assistant]");
      const choices = [...group.querySelectorAll("[data-import-conversation], [data-import-memory]")];
      const syncToggle = () => {
        toggle.checked = choices.every(input => input.checked);
        toggle.indeterminate = !toggle.checked && choices.some(input => input.checked);
      };
      syncToggle();
      toggle.onchange = () => {
        choices.forEach(input => input.checked = toggle.checked);
        syncToggle();
        recalculate();
      };
      choices.forEach(input => input.onchange = () => { syncToggle(); recalculate(); });
    }
    for (const [id, checked] of [["select-all-imports", true], ["select-no-imports", false]]) {
      const button = $("#" + id);
      if (button) button.onclick = () => { $$("[data-import-conversation]").forEach(input => input.checked = checked); recalculate(); };
    }
    $("#close-import-preview").onclick = () => { revision++; panel.hidden = true; state.dirty = false; current = null; };
    $("#confirm-import").onclick = async () => {
      const button = $("#confirm-import");
      button.disabled = true;
      try {
        await api(url(`/imports/${current}/confirm`), {method: "POST",
          body: {...selection(), auto_approve: $("#import-auto-approve")?.checked ?? false}});
        if (!live()) return;
        state.dirty = false;
        current = null;
        panel.hidden = true;
        await refresh();
      } catch (error) { showError(error); if (live()) counts(); }
    };
    panel.scrollIntoView({block: "nearest", behavior: "smooth"});
  }

  function renderJobs() {
    const historyOpen = $("#import-history")?.open;
    const currentJobs = [], history = [];
    for (const job of config.jobs) {
      const finished = job.status === "completed" && job.progress.failed === 0 && job.progress.running === 0;
      (finished ? history : currentJobs).push(job);
    }
    const renderJob = job => {
      const p = job.progress;
      const incomplete = job.status === "completed" && p.failed > 0;
      const label = incomplete ? `完成，${p.failed} 条失败`
        : job.error_code === "import_ended" ? "已结束" : statuses[job.status];
      const handled = p.processed + p.failed;
      const seconds = p.estimated_remaining_seconds;
      let estimate = "";
      if (job.status === "running" && seconds !== undefined) {
        if (seconds === null) estimate = "正在估算…";
        else if (seconds < 60) estimate = "预计还需不到 1 分钟";
        else estimate = `预计还需约 ${Math.round(seconds / 60)} 分钟`;
      }
      const currentItem = job.current ? `<p class="meta">${job.current.status === "failed" ? "失败条目 · " : ""}${esc(job.current.business_date || "日期未注明")} · ${esc(job.current.title)}</p>` : "";
      const paused = job.status === "failed";
      const stopped = job.error_code === "import_stopped";
      const action = active(job) ? "stop" : (paused || incomplete) ? "retry" : null;
      const actionLabel = active(job) ? "停止" : incomplete ? `重试失败的 ${p.failed} 条` : stopped ? "继续" : "继续处理";
      const canDiscard = config.lifecycle_supported && job.status === "preview";
      const canEnd = config.lifecycle_supported && paused;
      const disabled = actionPending.has(job.id) ? "disabled" : "";
      const actionButton = (kind, name) => `<button class="quiet" type="button" data-import-action="${kind}" ${disabled}>${name}</button>`;
      return `<article class="import-job" data-import-job="${esc(job.id)}">
        <div class="import-job-head"><strong>${names[job.source]}</strong><span class="meta">${esc(job.created_at.slice(0, 16).replace("T", " "))} · ${label}</span></div>
        ${job.status === "preview" ? '<p class="meta">等待选择范围并确认。</p>' : `<progress max="${Math.max(1, p.total)}" value="${handled}" aria-label="导入进度"></progress><p class="meta">已处理 ${handled} / ${p.total} 份原材料 · 已整理 ${p.structured_cards ?? 0} 张卡 · 失败 ${p.failed} 条 · 已调用模型 ${job.model_calls} 次</p>`}
        ${estimate ? `<p class="meta import-estimate">${estimate}</p>` : ""}
        ${currentItem}
        ${paused ? `<p class="${stopped ? "meta" : "error"}">${errors[job.error_code] || "这份原材料未处理完，可以重试。已完成的部分会保留。"}</p>` : ""}
        ${action ? actionButton(action, actionLabel) : ""}
        ${job.status === "preview" ? `<button type="button" class="quiet" data-import-preview="${esc(job.id)}" ${disabled}>继续预览</button>` : ""}
        ${canDiscard ? actionButton("discard", "放弃") : ""}
        ${canEnd ? actionButton("end", "结束") : ""}
      </article>`;
    };
    const previous = history.length ? `<details id="import-history"${historyOpen ? " open" : ""}>
      <summary>历史导入（${history.length}）</summary>${history.map(renderJob).join("")}</details>` : "";
    $("#import-jobs").innerHTML = config.jobs.length ? currentJobs.map(renderJob).join("") + previous
      : '<p class="empty">还没有导入记录。</p>';
    $$("[data-import-preview]").forEach(button => button.onclick = () => openPreview(button.dataset.importPreview).catch(showError));
    $$("[data-import-action]").forEach(button => button.onclick = async () => {
      const identifier = button.closest("[data-import-job]").dataset.importJob;
      if (actionPending.has(identifier)) return;
      const action = button.dataset.importAction;
      if (actionConfirmations[action] && !window.confirm(actionConfirmations[action])) return;
      actionPending.add(identifier);
      button.disabled = true;
      try {
        await api(url(`/imports/${identifier}/${action}`), {method: "POST", body: {}});
        if (action === "discard") {
          discardedPreviews.add(identifier);
          if (live() && current === identifier) {
            revision++;
            current = null;
            state.dirty = false;
            $("#import-preview").hidden = true;
          }
        }
      }
      catch (error) { showError(error); }
      finally {
        actionPending.delete(identifier);
        if (live()) { button.disabled = false; await refresh(); }
      }
    });
  }
  function scheduleRefresh() {
    if (config.jobs.some(job => active(job) || job.progress.running > 0)) timer = setTimeout(refresh, 2000);
  }
  async function refresh() {
    clearTimeout(timer);
    const request = ++listRevision;
    try {
      const next = await api(url("/imports"));
      if (!live() || request !== listRevision) return;
      keepView();
      config = next;
      renderJobs();
      scheduleRefresh();
    } catch (error) {
      if (!live() || request !== listRevision) return;
      showError(error);
      timer = setTimeout(refresh, 5000);
    }
  }
  $("#import-upload").onsubmit = async event => {
    event.preventDefault();
    const files = [...$("#import-file").files];
    const [file] = files;
    if (!file) return showError(new Error("请先选择文件。"));
    const source = $("#import-source").value;
    if (files.length > 1 && (files.length !== 2 || files.some(file => !/\.json$/i.test(file.name)))) {
      return showError(new Error("请选择一个 ZIP，或同时选择聊天和记忆的两个 JSON。"));
    }
    $("#import-error").textContent = "";
    $("#import-upload fieldset").disabled = true;
    $("#upload-preview").textContent = "正在准备导入数据…";
    try {
      const max = Math.round(config.max_upload_bytes / 1024 / 1024);
      const checkSize = size => {
        if (size > config.max_upload_bytes) throw new Error(`实际上传的内容超过 ${max} MiB 上限。`);
      };
      if (files.length > 1) checkSize(files[0].size + files[1].size);
      const upload = files.length > 1 ? await packOperitFiles(files) : await prepareImportUpload(source, file);
      if (!live()) return;
      checkSize(upload.size);
      $("#upload-preview").textContent = upload === file && /\.zip$/i.test(file.name) ? "正在上传原包…" : "正在上传…";
      const query = new URLSearchParams({source, filename: upload.name ?? file.name});
      const job = await api(url(`/imports/upload?${query}`), {method: "POST", file: upload});
      if (!live()) return;
      await openPreview(job.id);
      await refresh();
    } catch (error) { showError(error); }
    finally {
      if (live()) {
        $("#import-upload fieldset").disabled = false;
        $("#upload-preview").textContent = "上传并预览";
      }
    }
  };
  $("#refresh-imports").onclick = refresh;
  renderJobs();
  scheduleRefresh();
}
