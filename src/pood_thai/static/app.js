import { keyAction, pickMimeType, suffixForMime } from "./ptt.js";

const MIN_RECORDING_MS = 400;
const STATUS = {
  idle: "Hold Space to speak Thai · Shift+Space for English",
  recording_th: "Listening… (Thai)",
  recording_en: "Listening… (English)",
  thinking: "Thinking…",
  speaking: "Speaking…",
};

const state = { sessionId: null, recording: false, busy: false, transcriptVisible: false, lastAudio: null };
let mediaStream = null;
let recorder = null;
let noticeTimer = null;
const player = new Audio();

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.detail || body.error || `HTTP ${res.status}`);
    err.code = body.error;
    throw err;
  }
  return body;
}

const postJson = (path, data) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

function setStatus(mode, text = STATUS[mode]) {
  $("orb").dataset.mode = mode;
  $("status").textContent = text;
}

function showNotice(text, kind = "info") {
  const el = $("notice");
  el.textContent = text;
  el.className = `notice ${kind}`;
  el.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { el.hidden = true; }, kind === "error" ? 10000 : 5000);
}

function errorText(err) {
  switch (err.code) {
    case "no_speech": return "I didn't catch that. Hold Space a little longer and try again.";
    case "brain_unavailable": return `Claude isn't responding (${err.message}). Run "claude" in a terminal to check you're logged in, or wait if you've hit your usage limit.`;
    case "speech_unavailable": return `The voice failed: ${err.message}`;
    case "transcription_failed": return `Speech recognition failed (${err.message}). Check that ffmpeg is installed, then try again.`;
    case "session_not_found": return "This session has ended. Pick a topic to start again.";
    default: return err.message;
  }
}

function showScreen(name) {
  $("screen-topics").hidden = name !== "topics";
  $("screen-chat").hidden = name !== "chat";
}

// --- audio playback ---

function playBlob(blob) {
  if (player.src) URL.revokeObjectURL(player.src);
  player.src = URL.createObjectURL(blob);
  setStatus("speaking");
  // play() rejects with AbortError when a new recording pauses it before it starts; don't clobber that status.
  player.play().catch(() => { if (!state.recording && !state.busy) setStatus("idle"); });
}

function playPayload(payload) {
  if (payload.fallback_voice) showNotice("Azure voice unavailable, so I'm using the Mac's built-in Thai voice.");
  const bytes = Uint8Array.from(atob(payload.audio_b64), (c) => c.charCodeAt(0));
  state.lastAudio = new Blob([bytes], { type: payload.audio_mime });
  playBlob(state.lastAudio);
}

function stopPlayback() {
  player.pause();
}

player.addEventListener("ended", () => {
  if (!state.recording && !state.busy) setStatus("idle");
});

// --- transcript ---

function addLine(row, cls, text) {
  const p = document.createElement("p");
  p.className = cls;
  p.textContent = text;
  row.appendChild(p);
}

function addMessage(message) {
  if (!message) return;
  const row = document.createElement("div");
  row.className = `msg ${message.role}`;
  row.dataset.id = String(message.id);
  if (message.english_note) addLine(row, "note", message.english_note);
  if (message.thai) addLine(row, "thai", message.thai);
  if (message.english) addLine(row, "english", message.english);
  $("transcript").appendChild(row);
  row.scrollIntoView({ block: "end" });
}

function toggleTranscript() {
  state.transcriptVisible = !state.transcriptVisible;
  $("transcript").hidden = !state.transcriptVisible;
  $("btn-transcript").textContent = state.transcriptVisible ? "Hide transcript" : "Show transcript";
}

// --- topics and memory ---

async function loadTopics() {
  const { topics } = await api("/api/topics");
  const container = $("topics");
  container.replaceChildren();
  for (const topic of topics) {
    const btn = document.createElement("button");
    btn.type = "button";
    const th = document.createElement("span");
    th.className = "th";
    th.textContent = topic.thai;
    const en = document.createElement("span");
    en.className = "en";
    en.textContent = topic.label;
    btn.append(th, en);
    btn.addEventListener("click", () => startSession(topic.id));
    container.appendChild(btn);
  }
}

async function loadMemory() {
  const { facts } = await api("/api/memory");
  const list = $("memory-list");
  list.replaceChildren();
  $("memory-empty").hidden = facts.length > 0;
  for (const fact of facts) {
    const li = document.createElement("li");
    const text = document.createElement("span");
    text.textContent = fact.fact;
    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "Forget";
    del.addEventListener("click", async () => {
      try {
        await api(`/api/memory/${fact.id}`, { method: "DELETE" });
        await loadMemory();
      } catch (err) {
        showNotice(errorText(err), "error");
      }
    });
    li.append(text, del);
    list.appendChild(li);
  }
}

// --- session lifecycle ---

function stopMic() {
  mediaStream?.getTracks().forEach((track) => track.stop());
  mediaStream = null;
}

async function startSession(topicId) {
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    showNotice("Microphone access is blocked. Click the icon left of the address bar → Microphone → Allow, then reload the page.", "error");
    return;
  }
  showScreen("chat");
  $("transcript").replaceChildren();
  state.busy = true;
  setStatus("thinking");
  try {
    const res = await postJson("/api/sessions", { topic: topicId });
    state.sessionId = res.session_id;
    addMessage(res.message);
    state.busy = false;
    playPayload(res);
  } catch (err) {
    state.busy = false;
    stopMic();
    showScreen("topics");
    showNotice(errorText(err), "error");
  }
}

async function endSession() {
  if (state.sessionId === null || state.busy || state.recording) return;
  stopPlayback();
  state.busy = true;
  setStatus("thinking", "Saving and updating what I remember…");
  try {
    const { facts } = await api(`/api/sessions/${state.sessionId}/end`, { method: "POST" });
    showNotice(`Saved. I now remember ${facts.length} thing${facts.length === 1 ? "" : "s"} about you.`);
  } catch (err) {
    showNotice(errorText(err), "error");
  } finally {
    state.busy = false;
    state.sessionId = null;
    stopMic();
    showScreen("topics");
    loadMemory().catch(() => {});
  }
}

// --- recording ---

function startRecording(lang) {
  if (!mediaStream) return;
  stopPlayback();
  const mime = pickMimeType((type) => MediaRecorder.isTypeSupported(type));
  // Per-recording locals: onstop fires asynchronously, so a quick re-press must not reuse this one's chunks.
  const rec = new MediaRecorder(mediaStream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  const startedAt = performance.now();
  rec.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data); };
  rec.onstop = () => sendRecording(lang, rec.mimeType || mime, chunks, performance.now() - startedAt);
  recorder = rec;
  rec.start();
  state.recording = true;
  setStatus("recording", STATUS[`recording_${lang}`]);
}

function stopRecording() {
  if (!recorder || recorder.state === "inactive") return;
  state.recording = false;
  // Stay busy until onstop hands off to sendRecording, so no new recording or End can slip in between.
  state.busy = true;
  recorder.stop();
}

async function sendRecording(lang, mime, chunks, durationMs) {
  if (durationMs < MIN_RECORDING_MS || chunks.length === 0) {
    state.busy = false;
    setStatus("idle");
    return;
  }
  const form = new FormData();
  form.append("audio", new Blob(chunks, { type: mime }), `recording${suffixForMime(mime)}`);
  form.append("lang", lang);
  setStatus("thinking");
  try {
    const res = await api(`/api/sessions/${state.sessionId}/turns`, { method: "POST", body: form });
    state.busy = false;
    if (res.kind === "reply") {
      addMessage(res.user_message);
      addMessage(res.assistant_message);
    }
    playPayload(res);
  } catch (err) {
    state.busy = false;
    setStatus("idle");
    showNotice(errorText(err), err.code === "no_speech" ? "info" : "error");
  }
}

// --- controls ---

async function requestReplay(slow) {
  if (state.sessionId === null || state.busy || state.recording) return;
  state.busy = true;
  setStatus("thinking");
  try {
    const res = await postJson(`/api/sessions/${state.sessionId}/replay`, { slow });
    state.busy = false;
    playPayload(res);
  } catch (err) {
    state.busy = false;
    setStatus("idle");
    showNotice(errorText(err), "error");
  }
}

function replay() {
  // After an undo the cached audio is the withdrawn reply, so ask the server for the current one.
  if (state.lastAudio) playBlob(state.lastAudio);
  else requestReplay(false);
}

const slower = () => requestReplay(true);

async function undo() {
  if (state.sessionId === null || state.busy || state.recording) return;
  stopPlayback();
  state.busy = true;
  try {
    const { removed_ids: removed } = await api(`/api/sessions/${state.sessionId}/undo`, { method: "POST" });
    for (const id of removed) document.querySelector(`.msg[data-id="${id}"]`)?.remove();
    if (removed.length) state.lastAudio = null;
    showNotice(removed.length ? "Last turn removed. Say it again." : "Nothing to undo.");
  } catch (err) {
    showNotice(errorText(err), "error");
  } finally {
    state.busy = false;
    setStatus("idle");
  }
}

function onKey(event) {
  const inSession = state.sessionId !== null;
  if (inSession && (event.code === "Space" || event.code === "Backspace")) event.preventDefault();
  const action = keyAction(
    { type: event.type, code: event.code, shiftKey: event.shiftKey, repeat: event.repeat },
    { recording: state.recording, busy: state.busy, inSession },
  );
  switch (action) {
    case "start_th": startRecording("th"); break;
    case "start_en": startRecording("en"); break;
    case "stop": stopRecording(); break;
    case "replay": replay(); break;
    case "undo": undo(); break;
    case "toggle_transcript": toggleTranscript(); break;
    default: break;
  }
}

function bindButton(id, handler) {
  $(id).addEventListener("click", (event) => {
    event.currentTarget.blur(); // keep Space from re-clicking a focused button
    handler();
  });
}

document.addEventListener("keydown", onKey);
document.addEventListener("keyup", onKey);
bindButton("btn-replay", replay);
bindButton("btn-slower", slower);
bindButton("btn-undo", undo);
bindButton("btn-transcript", toggleTranscript);
bindButton("btn-end", endSession);

showScreen("topics");
loadTopics().catch((err) => showNotice(errorText(err), "error"));
loadMemory().catch(() => {});
