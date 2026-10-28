import { keyAction, pickMimeType, suffixForMime } from "./ptt.js";
import { RATE_KEY_STEP, clampRate, formatTime, splitByWords, stepWord, wordIndexAt } from "./player.js";

const MIN_RECORDING_MS = 400;
const NO_TIMING_SEEK_S = 1; // ←/→ step when a voice gives no word timings (the Mac fallback voice)
const STATUS = {
  idle: "Hold Space to speak Thai · Shift+Space for English",
  recording_th: "Listening… (Thai)",
  recording_en: "Listening… (English)",
  thinking: "Thinking…",
  speaking: "Speaking…",
};

function loadPref(key, fallback) {
  try {
    const value = localStorage.getItem(`pood.${key}`);
    return value === null ? fallback : JSON.parse(value);
  } catch {
    return fallback;
  }
}

function savePref(key, value) {
  try {
    localStorage.setItem(`pood.${key}`, JSON.stringify(value));
  } catch {
    // Storage can be unavailable (private window); the preference just won't stick.
  }
}

const state = {
  sessionId: null,
  fallbackNoticeShown: false,
  starting: false,
  recording: false,
  busy: false,
  transcriptVisible: false,
  hideEnglish: loadPref("hideEnglish", false),
  rate: clampRate(Number(loadPref("rate", 1))),
  voice: loadPref("voice", null),
  voices: [],
  defaultVoice: "premwadee",
  currentId: null, // Ploy's message loaded in the player
  currentVoiceKey: null, // voice of the clip actually loaded (the picker may have moved on)
  activeWord: -1,
  scrubbing: false,
};
const messages = new Map(); // message id -> message JSON
const clips = new Map(); // message id -> Map(voice key -> { url, words })
let mediaStream = null;
let recorder = null;
let noticeTimer = null;
let rafId = 0;
// Bumped whenever the player is pointed at something; a clip request that resolves late compares against it.
let selection = 0;
const player = new Audio();
player.preservesPitch = true;

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
    case "message_not_found": return "That reply is no longer in this session.";
    default: return err.message;
  }
}

function showScreen(name) {
  $("screen-topics").hidden = name !== "topics";
  $("screen-chat").hidden = name !== "chat";
}

// --- voices ---

// Without Azure there is one (Mac) voice, so every clip shares one cache key.
const voiceKey = () => state.voice ?? "default";
const requestVoice = () => state.voice ?? state.defaultVoice;

async function loadVoices() {
  const { voices, default: defaultVoice } = await api("/api/voices");
  state.voices = voices;
  state.defaultVoice = defaultVoice;
  if (!voices.some((v) => v.id === state.voice)) state.voice = voices.length ? defaultVoice : null;
  const select = $("voice");
  select.replaceChildren(...voices.map((v) => {
    const option = document.createElement("option");
    option.value = v.id;
    option.textContent = v.label;
    return option;
  }));
  if (state.voice) select.value = state.voice;
  $("voice-picker").hidden = voices.length === 0;
}

async function setVoice(voice) {
  state.voice = voice;
  savePref("voice", voice);
  $("voice").value = voice;
  for (const id of messages.keys()) renderWords(id);
  if (state.currentId !== null) {
    stopPlayback();
    await openMessage(state.currentId, 0);
  }
}

// --- clips and the player ---

// `key` is the voice the clip was requested with; the picker may have changed while the request was in flight.
function storeClip(messageId, payload, key = voiceKey()) {
  if (!payload.audio_b64) return;
  if (payload.fallback_voice && !state.fallbackNoticeShown) {
    state.fallbackNoticeShown = true;
    showNotice("Azure voice unavailable, so I'm using the Mac's built-in Thai voice.");
  }
  const bytes = Uint8Array.from(atob(payload.audio_b64), (c) => c.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type: payload.audio_mime }));
  if (!clips.has(messageId)) clips.set(messageId, new Map());
  const byVoice = clips.get(messageId);
  const old = byVoice.get(key);
  if (old) URL.revokeObjectURL(old.url);
  // Mac-fallback clips are kept so they can play now, but marked so the next open retries the real voice.
  byVoice.set(key, { url, words: payload.words || [], fallback: Boolean(payload.fallback_voice) });
}

function dropClips(messageId) {
  for (const clip of clips.get(messageId)?.values() ?? []) URL.revokeObjectURL(clip.url);
  clips.delete(messageId);
}

const clipFor = (messageId) => clips.get(messageId)?.get(voiceKey()) ?? null;
const currentWords = () => clips.get(state.currentId)?.get(state.currentVoiceKey)?.words ?? [];

function clearActiveWord() {
  document.querySelectorAll(".word.active").forEach((el) => el.classList.remove("active"));
  state.activeWord = -1;
}

function loadClip(messageId, { autoplay = true, at = 0 } = {}) {
  const clip = clipFor(messageId);
  if (!clip) return false;
  state.currentId = messageId;
  state.currentVoiceKey = voiceKey();
  selection += 1;
  clearActiveWord();
  if (player.src !== clip.url) player.src = clip.url;
  // Chrome resets playbackRate to defaultPlaybackRate when a new source loads.
  player.defaultPlaybackRate = state.rate;
  player.playbackRate = state.rate;
  player.currentTime = at;
  $("player").hidden = false;
  document.querySelectorAll(".msg.current").forEach((el) => el.classList.remove("current"));
  document.querySelector(`.msg[data-id="${messageId}"]`)?.classList.add("current");
  if (autoplay) play();
  refreshPlayer();
  return true;
}

const pendingClips = new Map(); // `${messageId}|${voice key}` -> in-flight ensureClip promise

function ensureClip(messageId) {
  const cached = clipFor(messageId);
  if (cached && !cached.fallback) return Promise.resolve(true);
  // A second request for the same clip (e.g. R pressed while a voice switch loads) shares the first one,
  // so it doesn't revoke the URL the player is already using.
  const pendingKey = `${messageId}|${voiceKey()}`;
  if (!pendingClips.has(pendingKey)) {
    pendingClips.set(pendingKey, fetchClip(messageId).finally(() => pendingClips.delete(pendingKey)));
  }
  return pendingClips.get(pendingKey);
}

async function fetchClip(messageId) {
  if (state.sessionId === null) return false;
  const session = state.sessionId;
  const key = voiceKey();
  try {
    const res = await postJson(`/api/sessions/${session}/messages/${messageId}/speech`, { voice: requestVoice() });
    // The session ended, or undo removed this reply, while the request was in flight.
    if (state.sessionId !== session || !messages.has(messageId)) return false;
    storeClip(messageId, res, key);
    renderWords(messageId);
    return Boolean(clipFor(messageId));
  } catch (err) {
    showNotice(errorText(err), "error");
    return Boolean(clipFor(messageId)); // a fallback clip can still play
  }
}

async function openMessage(messageId, at = 0) {
  if (state.recording) return;
  const mine = ++selection;
  const ready = await ensureClip(messageId);
  // Re-check after the fetch: a recording may have started (playing would leak into the mic), the reply may have
  // been undone, or the user may have picked something else meanwhile.
  if (ready && !state.recording && mine === selection && messages.has(messageId)) loadClip(messageId, { autoplay: true, at });
}

function play() {
  // play() rejects with AbortError when a new recording pauses it before it starts; don't clobber that status.
  player.play().catch(() => { if (!state.recording && !state.busy) setStatus("idle"); });
}

function stopPlayback() {
  player.pause();
}

function togglePlay() {
  if (state.currentId === null || state.recording) return;
  if (player.paused) play();
  else stopPlayback();
}

function lastAssistantId() {
  let last = null;
  for (const message of messages.values()) if (message.role === "assistant") last = message.id;
  return last;
}

function replay() {
  if (state.sessionId === null || state.recording) return;
  const id = state.currentId ?? lastAssistantId();
  if (id !== null) openMessage(id, 0);
}

function stepWords(dir) {
  if (state.currentId === null || state.recording) return;
  const words = currentWords();
  if (words.length) {
    const i = stepWord(words, player.currentTime, dir);
    if (i < 0) return;
    player.currentTime = words[i].t_start;
  } else {
    const duration = Number.isFinite(player.duration) ? player.duration : 0;
    player.currentTime = Math.max(0, Math.min(duration, player.currentTime + dir * NO_TIMING_SEEK_S));
  }
  if (player.paused) play();
  refreshPlayer();
}

async function seekToWord(messageId, word) {
  if (state.recording) return;
  // The line may have been rendered from another voice's clip, so fetch this voice's and match by position.
  const mine = ++selection;
  if (!(await ensureClip(messageId)) || state.recording || mine !== selection || !messages.has(messageId)) return;
  const words = clipFor(messageId).words;
  const match = words.find((w) => w.part === word.part && w.end > word.start);
  const at = match?.t_start ?? 0;
  if (messageId === state.currentId && state.currentVoiceKey === voiceKey()) {
    player.currentTime = at;
    if (player.paused) play();
    refreshPlayer();
  } else {
    loadClip(messageId, { autoplay: true, at });
  }
}

function setRate(rate) {
  state.rate = clampRate(rate);
  player.defaultPlaybackRate = state.rate;
  player.playbackRate = state.rate;
  $("speed").value = String(state.rate);
  $("speed-label").textContent = `${state.rate.toFixed(2)}×`;
  savePref("rate", state.rate);
}

function highlightWord() {
  const i = state.currentId === null ? -1 : wordIndexAt(currentWords(), player.currentTime);
  if (i === state.activeWord) return;
  clearActiveWord();
  state.activeWord = i;
  if (i >= 0) {
    document
      .querySelectorAll(`.msg[data-id="${state.currentId}"] .word[data-word="${i}"]`)
      .forEach((el) => el.classList.add("active"));
  }
}

function refreshPlayer() {
  const duration = Number.isFinite(player.duration) ? player.duration : 0;
  if (!state.scrubbing) $("scrub").value = duration ? String(Math.round((player.currentTime / duration) * 1000)) : "0";
  $("time").textContent = `${formatTime(player.currentTime)} / ${formatTime(duration)}`;
  $("btn-play").textContent = player.paused ? "▶" : "❚❚";
  highlightWord();
}

function tick() {
  refreshPlayer();
  if (!player.paused) rafId = requestAnimationFrame(tick);
}

player.addEventListener("play", () => {
  if (!state.busy && !state.recording) setStatus("speaking");
  cancelAnimationFrame(rafId);
  rafId = requestAnimationFrame(tick);
});
player.addEventListener("pause", () => {
  cancelAnimationFrame(rafId);
  refreshPlayer();
  if (!state.recording && !state.busy) setStatus("idle");
});
player.addEventListener("loadedmetadata", refreshPlayer);
player.addEventListener("seeked", refreshPlayer);

// --- transcript ---

function addLine(row, cls, text, part = null) {
  const p = document.createElement("p");
  p.className = cls;
  if (part) p.dataset.part = part;
  p.textContent = text;
  row.appendChild(p);
}

function addMessage(message) {
  if (!message) return;
  messages.set(message.id, message);
  const ploy = message.role === "assistant";
  const row = document.createElement("div");
  row.className = `msg ${message.role}`;
  row.dataset.id = String(message.id);
  if (message.english_note) addLine(row, "note", message.english_note, ploy ? "note" : null);
  if (message.thai) addLine(row, "thai", message.thai, ploy ? "thai" : null);
  if (message.english) {
    addLine(row, "english", message.english);
    const peek = document.createElement("button");
    peek.type = "button";
    peek.className = "peek-btn";
    peek.textContent = "Show English";
    peek.addEventListener("click", (event) => {
      event.currentTarget.blur();
      row.classList.toggle("peek");
      peek.textContent = row.classList.contains("peek") ? "Hide English" : "Show English";
    });
    row.appendChild(peek);
  }
  $("transcript").appendChild(row);
  renderWords(message.id);
  row.scrollIntoView({ block: "end" });
}

/** Rebuild Ploy's lines so each spoken word (for the current voice) is a clickable span. */
function renderWords(messageId) {
  const message = messages.get(messageId);
  const row = document.querySelector(`.msg[data-id="${messageId}"]`);
  if (!message || !row || message.role !== "assistant") return;
  // Fall back to any voice's timings so lines stay clickable after a voice switch; a click fetches the new voice.
  const byVoice = clips.get(messageId);
  const clip = [clipFor(messageId), ...(byVoice?.values() ?? [])].find((c) => c?.words.length);
  const words = clip?.words ?? [];
  for (const line of row.querySelectorAll("p[data-part]")) {
    const part = line.dataset.part;
    const text = part === "note" ? message.english_note : message.thai;
    line.replaceChildren(...splitByWords(text, words, part).map((piece) => {
      if (piece.word === null) return document.createTextNode(piece.text);
      const span = document.createElement("span");
      span.className = "word";
      span.dataset.word = String(piece.word);
      span.textContent = piece.text;
      span.addEventListener("click", () => seekToWord(messageId, words[piece.word]));
      return span;
    }));
  }
  if (messageId === state.currentId) {
    state.activeWord = -1;
    highlightWord();
  }
}

function forgetMessage(messageId) {
  document.querySelector(`.msg[data-id="${messageId}"]`)?.remove();
  messages.delete(messageId);
  dropClips(messageId);
}

function toggleTranscript() {
  state.transcriptVisible = !state.transcriptVisible;
  $("transcript").hidden = !state.transcriptVisible;
  $("btn-transcript").textContent = state.transcriptVisible ? "Hide transcript" : "Show transcript";
}

function applyHideEnglish() {
  $("transcript").classList.toggle("hide-english", state.hideEnglish);
  $("btn-english").textContent = state.hideEnglish ? "Show English" : "Hide English";
}

function toggleEnglish() {
  state.hideEnglish = !state.hideEnglish;
  savePref("hideEnglish", state.hideEnglish);
  document.querySelectorAll(".msg.peek").forEach((row) => row.classList.remove("peek"));
  document.querySelectorAll(".peek-btn").forEach((btn) => { btn.textContent = "Show English"; });
  applyHideEnglish();
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

function resetPlayer() {
  stopPlayback();
  for (const id of [...clips.keys()]) dropClips(id);
  messages.clear();
  state.currentId = null;
  state.activeWord = -1;
  player.removeAttribute("src");
  $("player").hidden = true;
}

async function startSession(topicId) {
  // A double-click must not open two mic streams and two sessions.
  if (state.starting || state.sessionId !== null) return;
  state.starting = true;
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    state.starting = false;
    showNotice("Microphone access is blocked. Click the icon left of the address bar → Microphone → Allow, then reload the page.", "error");
    return;
  }
  await voicesReady;
  showScreen("chat");
  resetPlayer();
  $("transcript").replaceChildren();
  state.busy = true;
  setStatus("thinking");
  const key = voiceKey();
  try {
    const res = await postJson("/api/sessions", { topic: topicId, voice: requestVoice() });
    state.sessionId = res.session_id;
    state.fallbackNoticeShown = false;
    storeClip(res.message.id, res, key);
    addMessage(res.message);
    state.busy = false;
    if (!loadClip(res.message.id)) openMessage(res.message.id, 0);
  } catch (err) {
    state.busy = false;
    stopMic();
    showScreen("topics");
    showNotice(errorText(err), "error");
  } finally {
    state.starting = false;
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
    resetPlayer();
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
  rec.onstop = () => {
    // The recorder can stop by itself (e.g. a headset disconnects); never leave the page stuck "recording".
    if (recorder === rec) state.recording = false;
    sendRecording(lang, rec.mimeType || mime, chunks, performance.now() - startedAt);
  };
  recorder = rec;
  rec.start();
  state.recording = true;
  setStatus("recording", STATUS[`recording_${lang}`]);
}

function stopRecording() {
  if (!recorder || recorder.state === "inactive") {
    state.recording = false;
    return;
  }
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
  form.append("voice", requestVoice());
  const key = voiceKey();
  setStatus("thinking");
  try {
    const res = await api(`/api/sessions/${state.sessionId}/turns`, { method: "POST", body: form });
    state.busy = false;
    if (res.kind === "reply") {
      storeClip(res.assistant_message.id, res, key);
      addMessage(res.user_message);
      addMessage(res.assistant_message);
      if (!loadClip(res.assistant_message.id)) await openMessage(res.assistant_message.id, 0);
    } else if (res.replay_message_id !== null) {
      await openMessage(res.replay_message_id, 0);
    } else {
      setStatus("idle");
    }
  } catch (err) {
    state.busy = false;
    setStatus("idle");
    showNotice(errorText(err), err.code === "no_speech" ? "info" : "error");
  }
}

// --- controls ---

async function undo() {
  if (state.sessionId === null || state.busy || state.recording) return;
  stopPlayback();
  state.busy = true;
  try {
    const { removed_ids: removed } = await api(`/api/sessions/${state.sessionId}/undo`, { method: "POST" });
    removed.forEach(forgetMessage);
    if (removed.includes(state.currentId)) {
      state.currentId = null;
      const last = lastAssistantId();
      if (last === null || !loadClip(last, { autoplay: false })) $("player").hidden = true;
    }
    showNotice(removed.length ? "Last turn removed. Say it again." : "Nothing to undo.");
  } catch (err) {
    showNotice(errorText(err), "error");
  } finally {
    state.busy = false;
    setStatus("idle");
  }
}

const KEYS_WITH_DEFAULTS = new Set(["Space", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);

function onKey(event) {
  const tag = event.target?.tagName;
  // A focused slider or select owns the arrow keys and Enter; push-to-talk still works.
  if ((tag === "INPUT" || tag === "SELECT") && event.code !== "Space") return;
  const inSession = state.sessionId !== null;
  const modified = event.metaKey || event.ctrlKey || event.altKey;
  if (inSession && !modified && KEYS_WITH_DEFAULTS.has(event.code)) event.preventDefault();
  const action = keyAction(
    {
      type: event.type, code: event.code, shiftKey: event.shiftKey, repeat: event.repeat,
      metaKey: event.metaKey, ctrlKey: event.ctrlKey, altKey: event.altKey,
    },
    { recording: state.recording, busy: state.busy, inSession },
  );
  switch (action) {
    case "start_th": startRecording("th"); break;
    case "start_en": startRecording("en"); break;
    case "stop": stopRecording(); break;
    case "replay": replay(); break;
    case "undo": undo(); break;
    case "toggle_transcript": toggleTranscript(); break;
    case "toggle_english": toggleEnglish(); break;
    case "prev_word": stepWords(-1); break;
    case "next_word": stepWords(1); break;
    case "speed_up": setRate(state.rate + RATE_KEY_STEP); break;
    case "speed_down": setRate(state.rate - RATE_KEY_STEP); break;
    case "play_pause": togglePlay(); break;
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
// Switching apps while holding Space swallows the keyup, so stop the recording here instead.
window.addEventListener("blur", () => { if (state.recording) stopRecording(); });

const scrub = $("scrub");
scrub.addEventListener("pointerdown", () => { state.scrubbing = true; });
scrub.addEventListener("input", () => {
  const duration = Number.isFinite(player.duration) ? player.duration : 0;
  if (duration) player.currentTime = (Number(scrub.value) / 1000) * duration;
  refreshPlayer();
});
scrub.addEventListener("change", () => { state.scrubbing = false; scrub.blur(); });
// Clicking the thumb without moving it fires no change event, so end the drag on release too.
window.addEventListener("pointerup", () => { state.scrubbing = false; });
window.addEventListener("pointercancel", () => { state.scrubbing = false; });
$("speed").addEventListener("input", (event) => setRate(Number(event.target.value)));
$("speed").addEventListener("change", (event) => event.target.blur());
$("voice").addEventListener("change", (event) => {
  event.target.blur();
  setVoice(event.target.value);
});

bindButton("btn-play", togglePlay);
bindButton("btn-prev", () => stepWords(-1));
bindButton("btn-next", () => stepWords(1));
bindButton("btn-replay", replay);
bindButton("btn-undo", undo);
bindButton("btn-transcript", toggleTranscript);
bindButton("btn-english", toggleEnglish);
bindButton("btn-end", endSession);

setRate(state.rate);
applyHideEnglish();
showScreen("topics");
// A stored voice may no longer exist (Azure key removed, catalog changed); never send it unchecked.
const voicesReady = loadVoices().catch(() => { state.voice = null; });
loadTopics().catch((err) => showNotice(errorText(err), "error"));
loadMemory().catch(() => {});
