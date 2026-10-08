const $ = (selector) => document.querySelector(selector);
const state = { notes: [], current: null, saveTimer: null, pollTimer: null, transcriptView: "edited", limits: null, modelRuntime: null, modelOptionsKey: null, mergeSelection: new Set(), mergeTarget: "", speakerBusy: false };
const colors = ["#17ad6d", "#e49a3d", "#6878df", "#db667a", "#2ca5b7", "#9c67c5"];

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `요청 실패 (${response.status})`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function uploadNote(form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const started = performance.now();
    let uploadedAt = null;
    xhr.open("POST", "/api/notes");
    xhr.responseType = "json";
    xhr.upload.onprogress = onProgress;
    xhr.upload.onload = () => { uploadedAt = performance.now(); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300 && xhr.response) {
        const finished = performance.now();
        const bytes = form.querySelector('input[type="file"]').files[0]?.size;
        if (bytes && uploadedAt !== null) fetch("/api/diagnostics/upload-metrics", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note_id: xhr.response.id, bytes,
            transfer_seconds: (uploadedAt - started) / 1000,
            response_wait_seconds: (finished - uploadedAt) / 1000,
            cf_ray: xhr.getResponseHeader("cf-ray") })
        }).catch(() => {});
        resolve(xhr.response);
      }
      else reject(new Error(xhr.response?.detail || `요청 실패 (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("파일 전송에 실패했습니다. 연결 상태를 확인해 주세요."));
    xhr.onabort = () => reject(new Error("파일 전송이 취소되었습니다."));
    xhr.send(new FormData(form));
  });
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(element.timer);
  element.timer = setTimeout(() => element.className = "toast", 2800);
}

function formatDate(value) {
  return new Intl.DateTimeFormat("ko-KR", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function clock(seconds = 0) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}` : `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}

function statusText(status) {
  return { queued: "대기 중", processing: "전사 중", done: "완료", error: "오류" }[status] || status;
}

function escapeHtml(value = "") {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML.replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function renderList() {
  const query = $("#note-search").value.trim().toLowerCase();
  const notes = state.notes.filter(note => note.title.toLowerCase().includes(query));
  $("#note-count").textContent = state.notes.length;
  $("#mobile-note-picker").replaceChildren(new Option("내 노트 선택", ""), ...state.notes.map(note => new Option(note.title, note.id)));
  $("#mobile-note-picker").value = state.current?.id || "";
  $("#note-list").innerHTML = notes.length ? notes.map(note => `
    <button class="note-item ${state.current?.id === note.id ? "active" : ""}" data-id="${note.id}">
      <b>${escapeHtml(note.title)}</b>
      <small><span>${formatDate(note.created_at)}</span><span class="note-status ${note.status}">${statusText(note.status)}</span></small>
    </button>`).join("") : `<div class="empty-list">${query ? "검색 결과가 없습니다" : "첫 노트를 만들어 보세요"}</div>`;
  document.querySelectorAll(".note-item").forEach(button => button.addEventListener("click", () => openNote(button.dataset.id)));
}

async function loadNotes() {
  try {
    state.notes = await api("/api/notes");
    renderList();
  } catch (error) { toast(error.message, true); }
}

async function openNote(id, quiet = false) {
  try {
    const previousId = state.current?.id;
    if (previousId !== id) {
      clearTimeout(state.saveTimer);
      if (await saveChanges() === false) return;
    }
    const previousCorrection = state.current?.correction_status;
    state.current = await api(`/api/notes/${id}`);
    if (previousId !== id) {
      state.mergeSelection.clear();
      state.mergeTarget = "";
      state.transcriptView = state.current.correction_status === "done" ? "corrected" : "edited";
    } else if (["queued", "processing"].includes(previousCorrection) && state.current.correction_status === "done") {
      state.transcriptView = "corrected";
    }
    $("#welcome").classList.add("hidden");
    $("#workspace").classList.remove("hidden");
    renderWorkspace();
    renderList();
    if (["queued", "processing"].includes(state.current.status) || ["queued", "processing"].includes(state.current.correction_status)) schedulePoll();
    else clearTimeout(state.pollTimer);
  } catch (error) { if (!quiet) toast(error.message, true); }
}

function renderWorkspace() {
  const note = state.current;
  $("#note-title").value = note.title;
  const chunkMeta = note.chunk_count > 1 ? ` · ${note.chunk_count}개 청크 자동 분할` : "";
  const model = state.modelRuntime?.options.find(option => option.id === note.model_variant)?.label || ({ "bf16": "BF16", "rtn-w8": "W8A16 (RTN)", "rtn-w4": "W4A16 (RTN)" }[note.model_variant] || "BF16");
  $("#note-meta").textContent = `${formatDate(note.created_at)} · ${note.original_filename} · ${model}${chunkMeta}`;
  const pending = ["queued", "processing"].includes(note.status);
  $("#processing-card").classList.toggle("hidden", !pending && note.status !== "error");
  $("#editor").classList.toggle("hidden", note.status !== "done");
  $("#export-toggle").disabled = note.status !== "done";
  $("#delete-note").disabled = pending || state.speakerBusy || ["queued", "processing"].includes(note.correction_status);

  if (pending) {
    const loading = note.status === "processing" && state.modelRuntime?.target === note.model_variant;
    $("#processing-title").textContent = loading ? `${model} 준비 중` : note.status === "queued" ? "전사 순서를 기다리고 있어요" : "음성을 읽고 있어요";
    const mapping = note.chunk_count > 1 && note.processed_chunks === note.chunk_count;
    const progress = note.chunk_count > 1
      ? mapping ? " 기본 청크 전사 완료, 경계 화자를 연결하는 중입니다." : ` 현재 ${note.processed_chunks}/${note.chunk_count}개 기본 청크 완료.`
      : "";
    $("#processing-copy").textContent = loading ? "모델을 불러오고 있습니다." : note.status === "queued" ? "앞선 작업이 끝나면 자동으로 시작됩니다." : `화자와 문장을 나누는 중입니다.${progress} 이 페이지를 닫아도 작업은 계속됩니다.`;
    renderTranscriptionProgress(note);
  } else if (note.status === "error") {
    $("#transcription-progress").classList.add("hidden");
    $("#processing-title").textContent = "전사하지 못했습니다";
    $("#processing-copy").textContent = note.error || "vLLM 서버와 파일을 확인해 주세요.";
  } else if (note.status === "done") {
    renderEditor();
  }
}

function renderTranscriptionProgress(note) {
  const progress = note.progress || {};
  $("#transcription-progress").classList.remove("hidden");
  const meter = $("#transcription-meter");
  const percent = progress.percent;
  if (typeof percent === "number") meter.value = percent;
  else meter.removeAttribute("value");
  const labels = { preparing: "모델 준비 중", normalizing: "음성 변환 중", merging: "화자 연결 중", queued: "순서 대기 중" };
  const remaining = progress.remaining_seconds;
  const estimate = typeof remaining === "number" ? `예상 ${clock(remaining)} 남음` : (labels[progress.phase] || "처리 시간 계산 중");
  $("#transcription-eta").textContent = `${typeof percent === "number" ? `약 ${Math.floor(percent)}% · ` : ""}${estimate}`;
}

function displayedSegments() {
  const note = state.current;
  if (["corrected", "diff"].includes(state.transcriptView) && note?.corrected_segments?.length) return note.corrected_segments;
  return note?.segments || [];
}

function renderCorrection() {
  const note = state.current;
  const status = note.correction_status || "idle";
  const running = ["queued", "processing"].includes(status);
  const hasResult = note.corrected_segments?.length > 0;
  const button = $("#start-correction");
  const copy = $("#correction-copy");
  const progress = $("#correction-progress");
  button.disabled = running || state.speakerBusy;
  progress.classList.toggle("hidden", !running);
  progress.removeAttribute("value");

  if (status === "queued") {
    button.textContent = "대기 중";
    copy.textContent = "앞선 AI 교정 작업이 끝나면 자동으로 시작됩니다.";
  } else if (status === "processing") {
    button.textContent = "교정 중…";
    copy.textContent = "gpt-6.1-sol · medium · Fast · 전체 전사 파일 교정·요약 중";
  } else if (status === "done") {
    button.textContent = "다시 교정";
    const usage = note.correction_usage;
    const tokens = usage?.input_tokens !== undefined ? ` · 입력 ${usage.input_tokens.toLocaleString()} / 출력 ${(usage.output_tokens || 0).toLocaleString()} 토큰` : "";
    const speed = note.correction_service_tier === "priority" ? " · Fast" : "";
    copy.textContent = `${note.correction_model || "gpt-6.1-sol"} · medium${speed} · ${note.correction_changes || 0}개 교정 · ${note.summary_stale ? "요약 갱신 필요" : "요약 완료"}${tokens}`;
  } else if (status === "error") {
    button.textContent = "다시 시도";
    copy.textContent = `처리 실패: ${note.correction_error || "Codex 로그인과 사용량 한도를 확인해 주세요."}`;
  } else {
    button.textContent = "AI 교정 시작";
    copy.textContent = "Codex · gpt-6.1-sol · medium · Fast · 전체 전사 파일";
  }

  document.querySelectorAll("[data-transcript-view]").forEach(tab => {
    const requiresResult = tab.dataset.transcriptView !== "edited";
    tab.disabled = tab.dataset.transcriptView === "summary" ? !note.summary?.title || status !== "done" : requiresResult && !hasResult;
    tab.classList.toggle("active", tab.dataset.transcriptView === state.transcriptView);
  });
  const complete = status === "done" && hasResult && Boolean(note.summary?.title);
  $("#result-downloads").classList.toggle("hidden", !complete);
  $("#corrected-download").href = `/api/notes/${note.id}/export/txt?version=corrected`;
  $("#summary-download").href = `/api/notes/${note.id}/summary/md`;
  document.querySelectorAll("[data-summary-format]").forEach(button => { button.disabled = !complete; });
}

function renderSummary(note) {
  const summary = note.summary;
  const sections = { key_points: "핵심 내용", decisions: "결정 사항", action_items: "후속 작업", open_questions: "미해결 질문" };
  $("#summary-note").innerHTML = `${note.summary_stale ? '<p class="summary-stale" role="status">화자 또는 교정본 변경 이후 요약 갱신 필요</p>' : ""}<h2>${escapeHtml(summary.title)}</h2><p class="summary-overview">${escapeHtml(summary.overview)}</p>` +
    Object.entries(sections).filter(([key]) => summary[key]?.length).map(([key, label]) =>
      `<section><h3>${label}</h3><ul>${summary[key].map(item => `<li><p>${escapeHtml(item.text)}</p></li>`).join("")}</ul></section>`
    ).join("");
}

function renderEditor() {
  const note = state.current;
  if (["corrected", "diff"].includes(state.transcriptView) && !note.corrected_segments?.length) state.transcriptView = "edited";
  if (state.transcriptView === "summary" && (!note.summary?.title || note.correction_status !== "done")) state.transcriptView = "edited";
  const segments = displayedSegments();
  const source = note.correction_source_segments?.length ? note.correction_source_segments : note.segments;
  const sourceById = Object.fromEntries(source.map(segment => [segment.id, segment]));
  const speakers = [...new Set(note.segments.map(segment => segment.speaker))];
  const speakerIndex = Object.fromEntries(speakers.map((speaker, index) => [speaker, index]));
  const isDiff = state.transcriptView === "diff";
  const speakerEditable = !state.speakerBusy && !["queued", "processing"].includes(note.correction_status);
  const editable = !isDiff && speakerEditable;
  renderCorrection();
  const mediaPath = `/api/notes/${note.id}/media`;
  if ($("#media-player").getAttribute("src") !== mediaPath) $("#media-player").src = mediaPath;
  $("#file-pill").textContent = note.original_filename;
  $("#duration-label").textContent = note.duration ? `· ${clock(note.duration)}` : "";
  const changed = segments.filter(segment => sourceById[segment.id]?.text !== segment.text).length;
  $("#segment-count").textContent = `${segments.length}개 구간 · ${speakers.length}명${isDiff ? ` · ${changed}개 변경` : ""}`;
  renderChunkNotice(note);
  const isSummary = state.transcriptView === "summary";
  $("#summary-note").classList.toggle("hidden", !isSummary);
  ["#editor-toolbar", "#speaker-panel", "#speaker-tools", "#segments"].forEach(selector => $(selector).classList.toggle("hidden", isSummary));
  if (isSummary) {
    $("#chunk-notice").classList.add("hidden");
    renderSummary(note);
    return;
  }
  $("#summary-note").innerHTML = "";
  state.mergeSelection = new Set([...state.mergeSelection].filter(speaker => speakers.includes(speaker)));
  if (!speakers.includes(state.mergeTarget)) state.mergeTarget = "";
  $("#speaker-panel").innerHTML = speakers.map(speaker => `
    <div class="speaker-chip" style="--speaker-color:${colors[speakerIndex[speaker] % colors.length]}">
      <input class="speaker-select" type="checkbox" data-merge-speaker="${escapeHtml(speaker)}" aria-label="${escapeHtml(speaker)} 병합 선택" ${state.mergeSelection.has(speaker) ? "checked" : ""} ${!speakerEditable ? "disabled" : ""} />
      <i></i><small>${escapeHtml(speaker)}</small><input data-speaker="${escapeHtml(speaker)}" aria-label="${escapeHtml(speaker)} 화자 이름" value="${escapeHtml(note.speaker_names[speaker] || speaker)}" maxlength="30" ${!speakerEditable ? "disabled" : ""} />
    </div>`).join("");
  $("#merge-speaker-target").replaceChildren(new Option("병합 대상 화자", ""), ...speakers.map(speaker => new Option(`${note.speaker_names[speaker] || speaker} (${speaker})`, speaker)));
  $("#merge-speaker-target").value = state.mergeTarget;
  renderSpeakerControls();
  $("#segments").innerHTML = segments.map((segment, index) => {
    const original = sourceById[segment.id]?.text || "";
    const changed = original !== segment.text;
    return `
    <article class="segment${isDiff && changed ? " corrected-change" : ""}${isDiff && !changed ? " unchanged" : ""}" data-index="${index}" data-id="${segment.id}" data-start="${segment.start}" data-end="${segment.end}">
      <button class="time-button" data-seek="${segment.start}">${clock(segment.start)}</button>
      <div class="speaker-label"><i class="speaker-dot" style="--speaker-color:${colors[speakerIndex[segment.speaker] % colors.length]}"></i><span>${escapeHtml(note.speaker_names[segment.speaker] || segment.speaker)}</span></div>
      <div class="segment-text-wrap">
        <div class="segment-text" contenteditable="${editable}" spellcheck="true">${escapeHtml(segment.text)}</div>
        ${isDiff && changed ? `<div class="original-text"><b>교정 전</b>${escapeHtml(original)}</div>` : ""}
      </div>
    </article>`;
  }).join("");
  bindEditorEvents();
}

function bindSeekButtons(container = document) {
  container.querySelectorAll("[data-seek]").forEach(button => button.addEventListener("click", () => {
    $("#media-player").currentTime = Number(button.dataset.seek);
    $("#media-player").play().catch(() => {});
  }));
}

function bindEditorEvents() {
  bindSeekButtons();
  document.querySelectorAll(".segment-text").forEach(element => element.addEventListener("input", () => {
    if (state.transcriptView === "diff" || element.contentEditable !== "true") return;
    const id = Number(element.closest(".segment").dataset.id);
    const field = state.transcriptView === "corrected" ? "corrected_segments" : "segments";
    const segment = state.current[field].find(item => Number(item.id) === id);
    if (!segment) return;
    segment.text = element.textContent.trim();
    queueSave({ [field]: state.current[field] });
  }));
  document.querySelectorAll("[data-speaker]").forEach(input => input.addEventListener("input", () => {
    state.current.speaker_names[input.dataset.speaker] = input.value.trim() || input.dataset.speaker;
    document.querySelectorAll(".segment").forEach(row => {
      const speaker = displayedSegments().find(segment => segment.id === Number(row.dataset.id)).speaker;
      row.querySelector(".speaker-label span").textContent = state.current.speaker_names[speaker] || speaker;
    });
    queueSave({ speaker_names: state.current.speaker_names });
  }));
  document.querySelectorAll("[data-merge-speaker]").forEach(input => input.addEventListener("change", () => {
    if (input.checked) state.mergeSelection.add(input.dataset.mergeSpeaker);
    else state.mergeSelection.delete(input.dataset.mergeSpeaker);
    renderSpeakerControls();
  }));
}

function renderSpeakerControls() {
  const disabled = state.speakerBusy || ["queued", "processing"].includes(state.current?.correction_status);
  $("#merge-speaker-target").disabled = disabled;
  $("#merge-speakers").disabled = disabled || !state.mergeTarget || ![...state.mergeSelection].some(speaker => speaker !== state.mergeTarget);
  $("#undo-speaker-merge").disabled = disabled || !state.current?.speaker_merge_undo_count;
  $("#speaker-selection-status").textContent = state.speakerBusy ? "화자 변경 중…" : state.mergeSelection.size ? `${state.mergeSelection.size}명 선택` : "";
}

async function changeSpeakers(action) {
  if (!state.current || state.speakerBusy) return;
  const noteId = state.current.id;
  const selection = { sources: [...state.mergeSelection], target: state.mergeTarget };
  state.speakerBusy = true;
  renderEditor();
  try {
    clearTimeout(state.saveTimer);
    if (await saveChanges() === false) return;
    if (state.current?.id !== noteId) return;
    const options = { method: "POST" };
    if (action === "merge") {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(selection);
    }
    const updated = await api(`/api/notes/${noteId}/speakers/${action}`, options);
    if (state.current?.id === noteId) {
      state.current = updated;
      state.mergeSelection.clear();
      state.mergeTarget = "";
      renderEditor();
    }
    await loadNotes();
    toast(action === "merge" ? "화자를 병합했습니다." : "화자 병합을 취소했습니다.");
  } catch (error) { toast(error.message, true); }
  finally {
    state.speakerBusy = false;
    if (state.current) renderEditor();
  }
}

function queueSave(changes) {
  state.pendingNoteId = state.current.id;
  state.pendingChanges = { ...(state.pendingChanges || {}), ...changes };
  $("#save-state").textContent = "저장 중…";
  $("#save-state").classList.add("saving");
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveChanges, 650);
}

async function saveChanges() {
  if (state.savePromise && await state.savePromise === false) return false;
  if (!state.current || !state.pendingChanges) return true;
  const saving = persistChanges();
  state.savePromise = saving;
  try { return await saving; }
  finally { if (state.savePromise === saving) state.savePromise = null; }
}

async function persistChanges() {
  const changes = state.pendingChanges;
  const noteId = state.pendingNoteId || state.current.id;
  state.pendingChanges = null;
  state.pendingNoteId = null;
  if ("title" in changes && !changes.title.trim()) {
    $("#note-title").value = state.current.title;
    delete changes.title;
  }
  if (!Object.keys(changes).length) {
    $("#save-state").textContent = "저장됨";
    $("#save-state").classList.remove("saving");
    return;
  }
  try {
    const updated = await api(`/api/notes/${noteId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes) });
    if (state.current?.id === noteId) state.current = { ...updated, ...(state.pendingChanges || {}) };
    $("#save-state").textContent = "저장됨";
    $("#save-state").classList.remove("saving");
    await loadNotes();
    return true;
  } catch (error) {
    state.pendingChanges = { ...changes, ...(state.pendingChanges || {}) };
    state.pendingNoteId = noteId;
    toast(error.message, true);
    return false;
  }
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    if (!state.current) return;
    const previous = state.current.status;
    const previousCorrection = state.current.correction_status;
    await openNote(state.current.id, true);
    await loadNotes();
    if (previous !== "done" && state.current?.status === "done") toast("전사가 완료되었습니다.");
    if (["queued", "processing"].includes(previousCorrection) && state.current?.correction_status === "done") toast("AI 교정과 핵심 요약이 완료되었습니다.");
  }, 2500);
}

function openUpload() {
  restoreModelPreference();
  $("#upload-dialog").showModal();
}

$("#open-upload").addEventListener("click", openUpload);
$("#welcome-upload").addEventListener("click", openUpload);
$("#close-upload").addEventListener("click", () => $("#upload-dialog").close());
$("#note-search").addEventListener("input", renderList);
$("#merge-speaker-target").addEventListener("change", event => { state.mergeTarget = event.target.value; renderSpeakerControls(); });
$("#merge-speakers").addEventListener("click", () => changeSpeakers("merge"));
$("#undo-speaker-merge").addEventListener("click", () => changeSpeakers("undo"));
$("#mobile-note-picker").addEventListener("change", event => { if (event.target.value) openNote(event.target.value); });
$("#note-title").addEventListener("input", event => queueSave({ title: event.target.value }));
document.querySelectorAll("[data-transcript-view]").forEach(button => button.addEventListener("click", () => {
  if (button.disabled || !state.current) return;
  state.transcriptView = button.dataset.transcriptView;
  renderEditor();
}));
$("#start-correction").addEventListener("click", async event => {
  if (!state.current) return;
  const noteId = state.current.id;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    clearTimeout(state.saveTimer);
    if (await saveChanges() === false) return;
    if (state.current?.id !== noteId) return;
    const queued = await api(`/api/notes/${noteId}/postprocess`, { method: "POST" });
    if (state.current?.id === noteId) {
      state.current = queued;
      state.transcriptView = "edited";
      renderWorkspace();
      schedulePoll();
    }
    toast("AI 교정 대기열에 추가했습니다.");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = ["queued", "processing"].includes(state.current?.correction_status); }
});

$("#upload-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true;
  button.firstChild.textContent = "업로드 중… ";
  const progress = $("#upload-progress");
  const meter = $("#upload-meter");
  const status = $("#upload-status");
  const started = performance.now();
  progress.classList.remove("hidden");
  meter.value = 0;
  status.textContent = "연결 중…";
  try {
    const note = await uploadNote(event.target, event => {
      const mb = event.loaded / 1024 / 1024;
      const elapsed = Math.max((performance.now() - started) / 1000, 0.001);
      if (event.lengthComputable) {
        meter.value = event.loaded / event.total * 100;
        status.textContent = event.loaded === event.total ? "서버 응답 대기 중…" :
          `${Math.floor(meter.value)}% · ${mb.toFixed(1)} / ${(event.total / 1024 / 1024).toFixed(1)} MB · ${(mb / elapsed).toFixed(1)} MB/s`;
      } else {
        meter.removeAttribute("value");
        status.textContent = `${mb.toFixed(1)} MB · ${(mb / elapsed).toFixed(1)} MB/s`;
      }
    });
    $("#upload-dialog").close();
    event.target.reset();
    restoreModelPreference();
    resetDropZone();
    await loadNotes();
    await openNote(note.id);
    toast("전사 대기열에 추가했습니다.");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.firstChild.textContent = "전사 시작하기 "; progress.classList.add("hidden"); }
});

const dropZone = $("#drop-zone");
const fileInput = $("#audio-file");
function restoreModelPreference() {
  let preferred = "bf16";
  try { preferred = localStorage.getItem("moss-model-variant") || preferred; } catch (_) {}
  const select = $("#model-variant");
  select.value = [...select.options].some(option => option.value === preferred && !option.disabled) ? preferred : "bf16";
}
$("#model-variant").addEventListener("change", event => {
  try { localStorage.setItem("moss-model-variant", event.target.value); } catch (_) {}
});

function renderModelOptions(runtime) {
  state.modelRuntime = runtime;
  const key = JSON.stringify(runtime.options);
  if (key === state.modelOptionsKey) return;
  const select = $("#model-variant");
  const previous = select.value;
  select.innerHTML = runtime.options.map(option => `<option value="${escapeHtml(option.id)}"${option.available ? "" : " disabled"}>${escapeHtml(option.label)}</option>`).join("");
  state.modelOptionsKey = key;
  restoreModelPreference();
  if ($("#upload-dialog").open && runtime.options.some(option => option.id === previous && option.available)) select.value = previous;
}
function resetDropZone() {
  $("#drop-title").textContent = "음성 또는 영상 파일을 놓으세요";
  $("#drop-copy").textContent = state.limits
    ? `MP3, WAV, M4A, MP4 · ${state.limits.chunk_seconds / 60}분 단위 · 최대 ${(state.limits.max_upload_bytes / 1000000).toFixed(1)} MB`
    : "MP3, WAV, M4A, MP4";
}

function renderChunkNotice(note) {
  $("#chunk-notice").classList.toggle("hidden", note.chunk_count <= 1);
  const overlap = note.chunk_overlap_seconds ? `${note.chunk_overlap_seconds / 60}분 겹침` : "";
  $("#chunk-notice").textContent = note.chunk_count > 1
    ? `${note.chunk_count}개 청크${overlap ? ` · ${overlap}` : ""}`
    : "";
}
function showFile(file) {
  if (!file) return resetDropZone();
  $("#drop-title").textContent = file.name;
  $("#drop-copy").textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB · 준비됨`;
}
fileInput.addEventListener("change", () => showFile(fileInput.files[0]));
["dragenter", "dragover"].forEach(name => dropZone.addEventListener(name, event => { event.preventDefault(); dropZone.classList.add("dragging"); }));
["dragleave", "drop"].forEach(name => dropZone.addEventListener(name, event => { event.preventDefault(); dropZone.classList.remove("dragging"); }));
dropZone.addEventListener("drop", event => {
  if (!event.dataTransfer.files.length) return;
  fileInput.files = event.dataTransfer.files;
  showFile(fileInput.files[0]);
});

$("#transcript-search").addEventListener("input", event => {
  const query = event.target.value.trim().toLowerCase();
  const segments = displayedSegments();
  document.querySelectorAll(".segment").forEach((row, index) => {
    const segment = segments[index];
    const speaker = state.current.speaker_names[segment.speaker] || segment.speaker;
    row.classList.toggle("filtered", query && !`${speaker} ${segment.text}`.toLowerCase().includes(query));
  });
});

$("#media-player").addEventListener("timeupdate", event => {
  const time = event.target.currentTime;
  document.querySelectorAll(".segment").forEach(row => row.classList.toggle("active", time >= Number(row.dataset.start) && time < Number(row.dataset.end)));
});

$("#export-toggle").addEventListener("click", () => $("#export-options").classList.toggle("open"));
document.querySelectorAll("[data-format]").forEach(button => button.addEventListener("click", () => {
  if (state.current) {
    const version = ["corrected", "diff", "summary"].includes(state.transcriptView) ? "corrected" : "edited";
    window.location.href = `/api/notes/${state.current.id}/export/${button.dataset.format}?version=${version}`;
  }
  $("#export-options").classList.remove("open");
}));
document.querySelectorAll("[data-summary-format]").forEach(button => button.addEventListener("click", () => {
  if (state.current && !button.disabled) window.location.href = `/api/notes/${state.current.id}/summary/${button.dataset.summaryFormat}`;
  $("#export-options").classList.remove("open");
}));

$("#delete-note").addEventListener("click", async () => {
  if (!state.current || !confirm(`'${state.current.title}' 노트와 원본 파일을 삭제할까요?`)) return;
  try {
    await api(`/api/notes/${state.current.id}`, { method: "DELETE" });
    state.current = null;
    $("#workspace").classList.add("hidden");
    $("#welcome").classList.remove("hidden");
    await loadNotes();
    toast("노트를 삭제했습니다.");
  } catch (error) { toast(error.message, true); }
});

async function checkHealth() {
  const element = $("#server-state");
  try {
    const health = await api("/api/health");
    if (health.model_runtime) {
      renderModelOptions(health.model_runtime);
      if (state.current && ["queued", "processing"].includes(state.current.status)) renderWorkspace();
    }
    if (health.limits) {
      state.limits = health.limits;
      $("#chunk-feature").textContent = `${health.limits.chunk_seconds / 60}분 단위 분할`;
      if (!fileInput.files.length) resetDropZone();
      if (state.current) renderChunkNotice(state.current);
    }
    const online = health.vllm.ok;
    element.className = `server-state ${online ? "online" : "offline"}`;
    const runtime = health.model_runtime;
    const active = runtime?.options.find(option => option.id === runtime.active)?.label;
    const target = runtime?.options.find(option => option.id === runtime.target)?.label;
    element.querySelector("b").textContent = target ? `${target} 준비 중` : online ? "MOSS 준비됨" : "vLLM 연결 안 됨";
    element.querySelector("small").textContent = online ? `MOSS · ${active || "BF16"} · Codex ${health.codex?.available ? "CLI" : "확인 필요"}` : "모델 서버 시작 중";
  } catch (_) {
    element.className = "server-state offline";
  }
}

loadNotes();
checkHealth();
setInterval(checkHealth, 10000);
