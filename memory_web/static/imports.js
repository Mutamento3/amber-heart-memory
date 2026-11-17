const names = {chatgpt: "ChatGPT", claude: "Claude", gemini: "Gemini", memory: "自己整理的记忆"};
const statuses = {preview: "待确认", queued: "排队中", running: "处理中", completed: "已完成", failed: "已暂停"};
const MEMORY_SAMPLE = `## 2025-03-14
小雨和阿言第一次一起去看海。出发前阿言说要在海边看日出，结果两人都睡过了头，赶到时太阳已经老高了。那天风很大，小雨的帽子被吹进海里，阿言笑了她一路，说以后每年都要来，补上那次日出。

## 2025-05-02
小雨和阿言吵了一架。那阵子小雨连着熬夜赶稿，阿言催她睡觉，她嫌烦，说了句气话就关了手机。第二天两人把话说开，约定生气不过夜；小雨说“只是想吐槽”的时候，阿言先听她说完，不急着讲道理。

## 2025-06-20
小雨不吃香菜和葱，点外卖要备注。咖啡只喝燕麦拿铁，不加糖；空腹喝会胃疼，上次早上空腹喝完疼了一上午。`;
const errors = {
  import_configuration_changed: "导入期间设置有变化，恢复原设置后可继续；已有草稿仍在待审核中。",
  import_material_expired: "原材料已到保留期限，请重新上传。",
  import_stopped: "已手动停止，可以随时继续",
  import_selection_failed: "挑选没有完成，重试会从这一步继续。",
  import_structure_failed: "部分卡片整理失败，重试只补未完成的部分。",
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
  main.innerHTML = `<form id="import-upload" class="import-upload"><fieldset>
    <label>从哪里搬来<select id="import-source" name="source">${Object.entries(names).map(([key, name]) => `<option value="${key}">${name}</option>`).join("")}</select></label>
    <div class="import-file-row"><button type="button" id="pick-import-file">选择文件</button><span id="import-file-name" class="muted">未选择文件</span><input id="import-file" type="file" accept=".json,.zip" hidden></div>
    <p id="import-help" class="meta"></p><div id="import-sample" class="import-sample" hidden><p class="meta">示例（把小雨和阿言换成你们的名字）：</p><pre></pre></div><button class="primary" id="upload-preview" type="submit">上传并预览</button>
    <p class="meta">上传上限 ${Math.round(config.max_upload_bytes / 1024 / 1024)} MiB，解压后的 JSON 总量上限 ${config.max_json_bytes / 1024 / 1024 / 1024} GiB。确认后才会调用模型。</p>
    </fieldset></form><p id="import-error" class="error" role="alert"></p>
    ${config.configured ? "" : '<p class="meta">尚未配置抽卡模型，可以先上传预览。</p>'}
    <section id="import-preview" class="import-preview" hidden></section>
    <section class="panel"><div class="panelhead"><h2>导入进度</h2><button type="button" class="quiet" id="refresh-imports">刷新</button></div><div id="import-jobs" aria-live="polite"></div></section>`;
  const showError = (error) => { if (live()) $("#import-error").textContent = error.message; };
  const help = () => {
    const source = $("#import-source").value;
    $("#import-file").accept = source === "memory" ? ".md,.txt" : ".json,.zip";
    $("#import-help").textContent = ({
      chatgpt: "只上传 conversations.json 或 conversations-000.json 等分片，不要上传带图片的整个导出包。JSON 超过 90 MiB 时，把聊天 JSON 单独压成 ZIP 再传。只读取最终选定的聊天分支。",
      claude: "只上传官方导出的 conversations.json；超过 90 MiB 时把这个 JSON 单独压成 ZIP，不要上传带附件的整个导出包。",
      gemini: "在 Google Takeout 的 My Activity → Gemini Apps 选择 JSON 格式。只上传 MyActivity.json，超过 90 MiB 时单独压成 ZIP。不要上传整个导出包；日期范围内的普通聊天和 Gem 聊天都会导入。",
      memory: "每条以 ## 日期 开头，下面写正文，可以写多行；支持 .md 和 .txt 文件。用名字来写，不要用“我”“他”，设置里填的名字或又名都可以。写得越具体，整理出来的卡片越有用。也可以把下面的示例发给你的 AI，让它照这个格式帮你整理。",
    })[source];
    $("#import-sample").hidden = source !== "memory";
  };
  $("#import-sample pre").textContent = MEMORY_SAMPLE;
  help();
  const showFileName = () => {
    const file = $("#import-file").files[0];
    $("#import-file-name").textContent = file ? file.name : "未选择文件";
    $("#import-file-name").classList.toggle("muted", !file);
  };
  $("#pick-import-file").onclick = () => $("#import-file").click();
  $("#import-file").onchange = showFileName;
  $("#import-source").onchange = () => { $("#import-file").value = ""; showFileName(); help(); };

  function selection() {
    if (plan.source === "gemini") return {
      date_start: $("#import-start").value || null,
      date_end: $("#import-end").value || null,
    };
    if (plan.source === "memory") return {};
    return {selected_conversations: $$("[data-import-conversation]:checked").map(input => input.value)};
  }
  function counts() {
    const calls = plan.model_calls_min === plan.model_calls_max ? `${plan.model_calls_max}` : `${plan.model_calls_min}～${plan.model_calls_max}`;
    $("#import-counts").innerHTML = `<span><strong>${plan.day_count}</strong> 天</span><span><strong>${plan.source === "memory" ? plan.memory_count : plan.message_count}</strong> ${plan.source === "memory" ? "条记忆" : "条消息"}</span><span><strong>${calls}</strong> 次模型调用</span>`;
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
    if (!live() || request !== revision) return;
    current = identifier;
    plan = next;
    state.dirty = true;
    const choices = plan.source === "gemini"
      ? `<div class="import-range"><label>从哪天<input type="date" id="import-start" value="${esc(plan.first_date)}" min="${esc(plan.first_date)}" max="${esc(plan.last_date)}"></label><label>到哪天<input type="date" id="import-end" value="${esc(plan.last_date)}" min="${esc(plan.first_date)}" max="${esc(plan.last_date)}"></label></div>`
      : plan.source === "memory" ? "" : `<div class="import-select-actions"><button type="button" class="quiet" id="select-all-imports">全选</button><button type="button" class="quiet" id="select-no-imports">取消全选</button></div><div class="import-conversations">${plan.conversations.map(item => `<label class="import-conversation"><input type="checkbox" data-import-conversation value="${esc(item.key)}" checked><span><strong>${esc(item.title)}</strong><span class="meta">${esc(item.first_date)}${item.first_date === item.last_date ? "" : ` 至 ${esc(item.last_date)}`} · ${item.message_count} 条消息${item.imported_day_count ? ` · 已导过 ${item.imported_day_count} 天` : ""}</span></span></label>`).join("")}</div>`;
    const panel = $("#import-preview");
    panel.hidden = false;
    panel.innerHTML = `<div class="panelhead"><h2>${names[plan.source]} · 导入预览</h2></div>${choices}<div id="import-counts" class="import-counts"></div><p id="import-skip" class="meta"></p><p class="meta">${plan.source === "memory" ? "每条记忆调用一次整理。" : "先挑选，再按结果整理成卡，调用次数显示预计范围。"}格式错误等重试可能增加调用，最多按上述上限的两倍计算。不补写日记。</p>${config.auto_approve_supported ? '<label class="check"><input type="checkbox" id="import-auto-approve" checked>整理好后直接收录，不进待审核</label>' : ""}<div class="actions"><button type="button" class="primary" id="confirm-import">确认导入</button><button type="button" class="quiet" id="close-import-preview">收起预览</button></div><p class="meta">确认后会在后台处理。收录或整理失败的草稿留在待审核。原聊天不会混入你的当天聊天历史或摘要。</p>`;
    counts();
    $$("[data-import-conversation], #import-start, #import-end").forEach(input => input.onchange = recalculate);
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
    $("#import-jobs").innerHTML = config.jobs.length ? config.jobs.map(job => {
      const p = job.progress;
      const incomplete = job.status === "completed" && p.failed > 0;
      const label = incomplete ? `完成，${p.failed} 条失败` : statuses[job.status];
      const handled = p.processed + p.failed;
      const seconds = p.estimated_remaining_seconds;
      let estimate = "";
      if (job.status === "running" && seconds !== undefined) {
        if (seconds === null) estimate = "正在估算…";
        else if (seconds < 60) estimate = "预计还需不到 1 分钟";
        else estimate = `预计还需约 ${Math.round(seconds / 60)} 分钟`;
      }
      const currentItem = job.current ? `<p class="meta">${job.current.status === "failed" ? "失败条目 · " : ""}${esc(job.current.business_date)} · ${esc(job.current.title)}</p>` : "";
      const paused = job.status === "failed";
      const stopped = job.error_code === "import_stopped";
      const action = active(job) ? "stop" : (paused || incomplete) ? "retry" : null;
      const actionLabel = active(job) ? "停止" : incomplete ? `重试失败的 ${p.failed} 条` : stopped ? "继续" : "继续处理";
      return `<article class="import-job" data-import-job="${esc(job.id)}">
        <div class="import-job-head"><strong>${names[job.source]}</strong><span class="meta">${esc(job.created_at.slice(0, 16).replace("T", " "))} · ${label}</span></div>
        ${job.status === "preview" ? '<p class="meta">等待选择范围并确认。</p>' : `<progress max="${Math.max(1, p.total)}" value="${handled}" aria-label="导入进度"></progress><p class="meta">已处理 ${handled} / ${p.total} 份原材料 · 失败 ${p.failed} 条 · 跳过 ${p.skipped} 份 · 已调用模型 ${job.model_calls} 次</p>`}
        ${estimate ? `<p class="meta import-estimate">${estimate}</p>` : ""}
        ${currentItem}
        ${paused ? `<p class="${stopped ? "meta" : "error"}">${errors[job.error_code] || "这份原材料未处理完，可以重试。已完成的部分会保留。"}</p>` : ""}
        ${action ? `<button class="quiet" type="button" data-import-action="${action}" ${actionPending.has(job.id) ? "disabled" : ""}>${actionLabel}</button>` : ""}
        ${job.status === "preview" ? `<button type="button" class="quiet" data-import-preview="${esc(job.id)}">继续预览</button>` : ""}
      </article>`;
    }).join("") : '<p class="empty">还没有导入记录。</p>';
    $$("[data-import-preview]").forEach(button => button.onclick = () => openPreview(button.dataset.importPreview).catch(showError));
    $$("[data-import-action]").forEach(button => button.onclick = async () => {
      const identifier = button.closest("[data-import-job]").dataset.importJob;
      if (actionPending.has(identifier)) return;
      actionPending.add(identifier);
      button.disabled = true;
      try { await api(url(`/imports/${identifier}/${button.dataset.importAction}`), {method: "POST", body: {}}); }
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
    const file = $("#import-file").files[0];
    if (!file) return showError(new Error("请先选择文件。"));
    if (file.size > config.max_upload_bytes) return showError(new Error("文件超过 90 MiB，请只把聊天 JSON 文件压成 ZIP 后再上传，不要加入图片或其他附件。"));
    $("#import-error").textContent = "";
    $("#import-upload fieldset").disabled = true;
    $("#upload-preview").textContent = "正在读取文件…";
    try {
      const query = new URLSearchParams({source: $("#import-source").value, filename: file.name});
      const job = await api(url(`/imports/upload?${query}`), {method: "POST", file});
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
