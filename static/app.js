const $ = (selector) => document.querySelector(selector);
const state = { notes: [], current: null, saveTimer: null, pollTimer: null, transcriptView: "edited" };
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
  return div.innerHTML;
}

function renderList() {
  const query = $("#note-search").value.trim().toLowerCase();
  const notes = state.notes.filter(note => note.title.toLowerCase().includes(query));
  $("#note-count").textContent = state.notes.length;
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
    const previousCorrection = state.current?.correction_status;
    state.current = await api(`/api/notes/${id}`);
    if (previousId !== id) {
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
  $("#note-meta").textContent = `${formatDate(note.created_at)} · ${note.original_filename}${chunkMeta}`;
  const pending = ["queued", "processing"].includes(note.status);
  $("#processing-card").classList.toggle("hidden", !pending && note.status !== "error");
  $("#editor").classList.toggle("hidden", note.status !== "done");
  $("#export-toggle").disabled = note.status !== "done";
  $("#delete-note").disabled = pending;

  if (pending) {
    $("#processing-title").textContent = note.status === "queued" ? "전사 순서를 기다리고 있어요" : "음성을 읽고 있어요";
    const mapping = note.chunk_count > 1 && note.processed_chunks === note.chunk_count;
    const progress = note.chunk_count > 1
      ? mapping ? " 기본 청크 전사 완료, 경계 화자를 연결하는 중입니다." : ` 현재 ${note.processed_chunks}/${note.chunk_count}개 기본 청크 완료.`
      : "";
    $("#processing-copy").textContent = note.status === "queued" ? "앞선 작업이 끝나면 자동으로 시작됩니다." : `화자와 문장을 나누는 중입니다.${progress} 이 페이지를 닫아도 작업은 계속됩니다.`;
  } else if (note.status === "error") {
    $("#processing-title").textContent = "전사하지 못했습니다";
    $("#processing-copy").textContent = note.error || "vLLM 서버와 파일을 확인해 주세요.";
  } else if (note.status === "done") {
    renderEditor();
  }
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
  const chunks = $("#correction-chunks");
  button.disabled = running;
  progress.classList.toggle("hidden", !running);
  const totalChunks = note.correction_total_windows || 0;
  const completedChunks = note.correction_processed_windows || 0;
  chunks.classList.toggle("hidden", !totalChunks || (!["processing", "error"].includes(status) && status !== "done"));
  chunks.innerHTML = totalChunks ? Array.from({ length: totalChunks }, (_, index) => {
    const number = index + 1;
    const chunkStatus = number <= completedChunks ? "completed" : (status === "processing" && number === completedChunks + 1 ? "current" : "pending");
    const label = chunkStatus === "completed" ? "완료" : chunkStatus === "current" ? "처리 중" : "대기";
    return `<span class="${chunkStatus}" title="교정 청크 ${number}: ${label}">${number}</span>`;
  }).join("") : "";

  if (status === "queued") {
    button.textContent = "대기 중";
    copy.textContent = "앞선 AI 교정 작업이 끝나면 자동으로 시작됩니다.";
  } else if (status === "processing") {
    const done = note.correction_processed_windows || 0;
    const total = note.correction_total_windows || 0;
    button.textContent = "교정 중…";
    copy.textContent = `슬라이딩 윈도우 ${done}/${total}개 처리 중 · 원본과 타임스탬프는 그대로 보존됩니다.`;
    progress.querySelector("i").style.width = total ? `${Math.max(3, done / total * 100)}%` : "3%";
  } else if (status === "done") {
    button.textContent = "다시 교정";
    copy.textContent = `${note.correction_model || "Qwen3.8-27B"}가 ${note.correction_changes || 0}개 발화를 수정했습니다. 변경 비교에서 원문과 확인할 수 있습니다.`;
  } else if (status === "error") {
    button.textContent = "다시 시도";
    copy.textContent = `교정 실패: ${note.correction_error || "Qwen 서버를 확인해 주세요."}`;
  } else {
    button.textContent = "AI 교정 시작";
    copy.textContent = "Qwen3.8-27B가 문맥을 겹쳐 읽고 기술 용어와 오탈자를 교정합니다.";
  }

  document.querySelectorAll("[data-transcript-view]").forEach(tab => {
    const requiresResult = tab.dataset.transcriptView !== "edited";
    tab.disabled = requiresResult && !hasResult;
    tab.classList.toggle("active", tab.dataset.transcriptView === state.transcriptView);
  });
}

function renderEditor() {
  const note = state.current;
  if (["corrected", "diff"].includes(state.transcriptView) && !note.corrected_segments?.length) state.transcriptView = "edited";
  const segments = displayedSegments();
  const source = note.correction_source_segments?.length ? note.correction_source_segments : note.segments;
  const sourceById = Object.fromEntries(source.map(segment => [segment.id, segment]));
  const speakers = [...new Set(note.segments.map(segment => segment.speaker))];
  const speakerIndex = Object.fromEntries(speakers.map((speaker, index) => [speaker, index]));
  const isDiff = state.transcriptView === "diff";
  const editable = !isDiff;
  renderCorrection();
  $("#media-player").src = `/api/notes/${note.id}/media`;
  $("#file-pill").textContent = note.original_filename;
  $("#duration-label").textContent = note.duration ? `· ${clock(note.duration)}` : "";
  const changed = segments.filter(segment => sourceById[segment.id]?.text !== segment.text).length;
  $("#segment-count").textContent = `${segments.length}개 구간 · ${speakers.length}명${isDiff ? ` · ${changed}개 변경` : ""}`;
  $("#chunk-notice").classList.toggle("hidden", note.chunk_count <= 1);
  $("#chunk-notice").textContent = note.chunk_count > 1
    ? `이 파일은 ${note.chunk_count}개로 자동 분할되었습니다. 각 경계의 앞뒤 5분을 다시 전사해 양쪽 화자 ID를 연결했습니다. 브리지 구간에서 말하지 않은 화자는 별도 ID로 남을 수 있습니다.`
    : "";
  $("#speaker-panel").innerHTML = speakers.map(speaker => `
    <label class="speaker-chip" style="--speaker-color:${colors[speakerIndex[speaker] % colors.length]}">
      <i></i><small>${speaker}</small><input data-speaker="${speaker}" value="${escapeHtml(note.speaker_names[speaker] || speaker)}" maxlength="30" />
    </label>`).join("");
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

function bindEditorEvents() {
  document.querySelectorAll("[data-seek]").forEach(button => button.addEventListener("click", () => {
    $("#media-player").currentTime = Number(button.dataset.seek);
    $("#media-player").play();
  }));
  document.querySelectorAll(".segment-text").forEach(element => element.addEventListener("input", () => {
    if (state.transcriptView === "diff") return;
    const id = Number(element.closest(".segment").dataset.id);
    const field = state.transcriptView === "corrected" ? "corrected_segments" : "segments";
    const segment = state.current[field].find(item => Number(item.id) === id);
    if (!segment) return;
    segment.text = element.textContent.trim();
    queueSave({ [field]: state.current[field] });
  }));
  document.querySelectorAll("[data-speaker]").forEach(input => input.addEventListener("input", () => {
    state.current.speaker_names[input.dataset.speaker] = input.value.trim() || input.dataset.speaker;
    document.querySelectorAll(".segment").forEach((row, index) => {
      const speaker = state.current.segments[index].speaker;
      row.querySelector(".speaker-label span").textContent = state.current.speaker_names[speaker] || speaker;
    });
    queueSave({ speaker_names: state.current.speaker_names });
  }));
}

function queueSave(changes) {
  state.pendingChanges = { ...(state.pendingChanges || {}), ...changes };
  $("#save-state").textContent = "저장 중…";
  $("#save-state").classList.add("saving");
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveChanges, 650);
}

async function saveChanges() {
  if (!state.current || !state.pendingChanges) return;
  const changes = state.pendingChanges;
  state.pendingChanges = null;
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
    state.current = await api(`/api/notes/${state.current.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes) });
    $("#save-state").textContent = "저장됨";
    $("#save-state").classList.remove("saving");
    await loadNotes();
  } catch (error) { toast(error.message, true); }
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
    if (["queued", "processing"].includes(previousCorrection) && state.current?.correction_status === "done") toast("AI 기술 용어 교정이 완료되었습니다.");
  }, 2500);
}

function openUpload() {
  $("#upload-dialog").showModal();
}

$("#open-upload").addEventListener("click", openUpload);
$("#welcome-upload").addEventListener("click", openUpload);
$("#close-upload").addEventListener("click", () => $("#upload-dialog").close());
$("#note-search").addEventListener("input", renderList);
$("#note-title").addEventListener("input", event => queueSave({ title: event.target.value }));
document.querySelectorAll("[data-transcript-view]").forEach(button => button.addEventListener("click", () => {
  if (button.disabled || !state.current) return;
  state.transcriptView = button.dataset.transcriptView;
  renderEditor();
}));
$("#start-correction").addEventListener("click", async () => {
  if (!state.current) return;
  try {
    state.current = await api(`/api/notes/${state.current.id}/postprocess`, { method: "POST" });
    state.transcriptView = "edited";
    renderWorkspace();
    schedulePoll();
    toast("AI 교정 대기열에 추가했습니다.");
  } catch (error) { toast(error.message, true); }
});

$("#upload-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true;
  button.firstChild.textContent = "업로드 중… ";
  try {
    const note = await api("/api/notes", { method: "POST", body: new FormData(event.target) });
    $("#upload-dialog").close();
    event.target.reset();
    resetDropZone();
    await loadNotes();
    await openNote(note.id);
    toast("전사 대기열에 추가했습니다.");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.firstChild.textContent = "전사 시작하기 "; }
});

const dropZone = $("#drop-zone");
const fileInput = $("#audio-file");
function resetDropZone() {
  $("#drop-title").textContent = "음성 또는 영상 파일을 놓으세요";
  $("#drop-copy").textContent = "MP3, WAV, M4A, MP4 등 · 90분 초과 시 자동 분할 · 최대 5GB";
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
    const version = ["corrected", "diff"].includes(state.transcriptView) ? "corrected" : "edited";
    window.location.href = `/api/notes/${state.current.id}/export/${button.dataset.format}?version=${version}`;
  }
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
    const online = health.vllm.ok;
    const qwen = health.qwen?.ok;
    element.className = `server-state ${online ? "online" : "offline"}`;
    element.querySelector("b").textContent = online ? "MOSS 준비됨" : "vLLM 연결 안 됨";
    element.querySelector("small").textContent = online ? `MOSS GPU 4 · Qwen ${qwen ? "GPU 5" : "대기"}` : "scripts/start.sh 실행 필요";
  } catch (_) {
    element.className = "server-state offline";
  }
}

loadNotes();
checkHealth();
setInterval(checkHealth, 10000);
