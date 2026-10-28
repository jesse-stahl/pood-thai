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
// On the review screen only the player keys do anything.
const REVIEW_ACTIONS = new Set(["prev_word", "next_word", "speed_up", "speed_down", "play_pause", "replay"]);
const KEYS_WITH_DEFAULTS = new Set(["Space", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);

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
  reviewing: false,
  transcriptVisible: false,
  hideEnglish: loadPref("hideEnglish", false),
  rate: clampRate(Number(loadPref("rate", 1))),
  voice: loadPref("voice", null),
  voices: [],
  defaultVoice: "premwadee",
  currentKey: null, // clip owner loaded in the player
  currentVoiceKey: null, // voice of the clip actually loaded (the picker may have moved on)
  activeWord: -1,
  scrubbing: false,
};
const messages = new Map(); // message id -> message JSON (current session)
// Everything playable: Ploy's messages ("m:<id>") and review fixes ("r:<review id>:<index>").
const owners = new Map(); // key -> { row, texts: { thai, note }, speechPath }
const clips = new Map(); // owner key -> Map(voice key -> { url, words })
const pendingClips = new Map(); // `${owner key}|${voice key}` -> in-flight ensureClip promise
let mediaStream = null;
let recorder = null;
let noticeTimer = null;
let rafId = 0;
// Bumped whenever the player is pointed at something; a clip request that resolves late compares against it.
let selection = 0;
// Bumped on every review navigation; a past review that loads late compares against it.
let reviewNav = 0;
const player = new Audio();
player.preservesPitch = true;

const $ = (id) => document.getElementById(id);
const messageKey = (id) => `m:${id}`;

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
    case "review_not_found": return "That review is no longer available.";
    default: return err.message;
  }
}

function showScreen(name) {
  for (const screen of ["topics", "chat", "review"]) $(`screen-${screen}`).hidden = name !== screen;
  state.reviewing = name === "review";
  // One player, moved to whichever screen needs it.
  const slot = $(`${name}-player-slot`);
  if (slot) slot.appendChild($("player"));
}

function formatDate(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
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
  for (const key of owners.keys()) renderWords(key);
  if (state.currentKey !== null) {
    stopPlayback();
    await openClip(state.currentKey, 0);
  }
}

// --- clip owners, clips and the player ---

function registerOwner(key, row, texts, speechPath) {
  owners.set(key, { row, texts, speechPath });
  row.dataset.key = key;
}

// `vkey` is the voice the clip was requested with; the picker may have changed while the request was in flight.
function storeClip(key, payload, vkey = voiceKey()) {
  if (!payload.audio_b64) return;
  if (payload.fallback_voice && !state.fallbackNoticeShown) {
    state.fallbackNoticeShown = true;
    showNotice("Azure voice unavailable, so I'm using the Mac's built-in Thai voice.");
  }
  const bytes = Uint8Array.from(atob(payload.audio_b64), (c) => c.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type: payload.audio_mime }));
  if (!clips.has(key)) clips.set(key, new Map());
  const byVoice = clips.get(key);
  const old = byVoice.get(vkey);
  if (old) URL.revokeObjectURL(old.url);
  // Mac-fallback clips are kept so they can play now, but marked so the next open retries the real voice.
  byVoice.set(vkey, { url, words: payload.words || [], fallback: Boolean(payload.fallback_voice) });
}

function dropClips(key) {
  for (const clip of clips.get(key)?.values() ?? []) URL.revokeObjectURL(clip.url);
  clips.delete(key);
}

const clipFor = (key) => clips.get(key)?.get(voiceKey()) ?? null;
const currentWords = () => clips.get(state.currentKey)?.get(state.currentVoiceKey)?.words ?? [];

function clearActiveWord() {
  document.querySelectorAll(".word.active").forEach((el) => el.classList.remove("active"));
  state.activeWord = -1;
}

function loadClip(key, { autoplay = true, at = 0 } = {}) {
  const clip = clipFor(key);
  if (!clip) return false;
  state.currentKey = key;
  state.currentVoiceKey = voiceKey();
  selection += 1;
  clearActiveWord();
  if (player.src !== clip.url) player.src = clip.url;
  // Chrome resets playbackRate to defaultPlaybackRate when a new source loads.
  player.defaultPlaybackRate = state.rate;
  player.playbackRate = state.rate;
  player.currentTime = at;
  $("player").hidden = false;
  document.querySelectorAll(".current").forEach((el) => el.classList.remove("current"));
  owners.get(key)?.row.classList.add("current");
  if (autoplay) play();
  refreshPlayer();
  return true;
}

function ensureClip(key) {
  const cached = clipFor(key);
  if (cached && !cached.fallback) return Promise.resolve(true);
  // A second request for the same clip (e.g. R pressed while a voice switch loads) shares the first one,
  // so it doesn't revoke the URL the player is already using.
  const pendingKey = `${key}|${voiceKey()}`;
  if (!pendingClips.has(pendingKey)) {
    // Only clear our own entry: resetPlayer may have replaced it with a newer request for the same key.
    const pending = fetchClip(key).finally(() => {
      if (pendingClips.get(pendingKey) === pending) pendingClips.delete(pendingKey);
    });
    pendingClips.set(pendingKey, pending);
  }
  return pendingClips.get(pendingKey);
}

async function fetchClip(key) {
  const owner = owners.get(key);
  if (!owner) return false;
  const vkey = voiceKey();
  try {
    const res = await postJson(owner.speechPath, { voice: requestVoice() });
    if (owners.get(key) !== owner) return false; // the session or review closed while this was in flight
    storeClip(key, res, vkey);
    renderWords(key);
    return Boolean(clipFor(key));
  } catch (err) {
    showNotice(errorText(err), "error");
    return Boolean(clipFor(key)); // a fallback clip can still play
  }
}

async function openClip(key, at = 0) {
  if (state.recording) return;
  const mine = ++selection;
  const ready = await ensureClip(key);
  // Re-check after the fetch: a recording may have started (playing would leak into the mic), the owner may be gone
  // (undo, End, Done), or the user may have picked something else meanwhile.
  if (ready && !state.recording && mine === selection && owners.has(key)) loadClip(key, { autoplay: true, at });
}

function play() {
  // play() rejects with AbortError when a new recording pauses it before it starts; don't clobber that status.
  player.play().catch(() => { if (!state.recording && !state.busy) setStatus("idle"); });
}

function stopPlayback() {
  player.pause();
}

function togglePlay() {
  if (state.currentKey === null || state.recording) return;
  if (player.paused) play();
  else stopPlayback();
}

function lastAssistantKey() {
  let last = null;
  for (const message of messages.values()) if (message.role === "assistant") last = messageKey(message.id);
  return last;
}

function replay() {
  if (state.recording || (state.sessionId === null && !state.reviewing)) return;
  const key = state.currentKey ?? (state.reviewing ? null : lastAssistantKey());
  if (key !== null) openClip(key, 0);
}

function stepWords(dir) {
  if (state.currentKey === null || state.recording) return;
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

async function seekToWord(key, word) {
  if (state.recording) return;
  // The line may have been rendered from another voice's clip, so fetch this voice's and match by position.
  const mine = ++selection;
  if (!(await ensureClip(key)) || state.recording || mine !== selection || !owners.has(key)) return;
  const words = clipFor(key).words;
  const match = words.find((w) => w.part === word.part && w.end > word.start);
  const at = match?.t_start ?? 0;
  // Seek in place only if the player still holds this exact clip (a voice switch or a re-fetched fallback replaces it).
  if (key === state.currentKey && player.src === clipFor(key).url) {
    player.currentTime = at;
    if (player.paused) play();
    refreshPlayer();
  } else {
    loadClip(key, { autoplay: true, at });
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
  const i = state.currentKey === null ? -1 : wordIndexAt(currentWords(), player.currentTime);
  if (i === state.activeWord) return;
  clearActiveWord();
  state.activeWord = i;
  if (i >= 0) {
    owners.get(state.currentKey)?.row
      .querySelectorAll(`.word[data-word="${i}"]`)
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
  if (!state.reviewing && !state.busy && !state.recording) setStatus("speaking");
  cancelAnimationFrame(rafId);
  rafId = requestAnimationFrame(tick);
});
player.addEventListener("pause", () => {
  cancelAnimationFrame(rafId);
  refreshPlayer();
  if (!state.recording && !state.busy && !state.reviewing) setStatus("idle");
});
player.addEventListener("loadedmetadata", refreshPlayer);
player.addEventListener("seeked", refreshPlayer);

/** Rebuild an owner's lines so each spoken word (for the current voice) is a clickable span. */
function renderWords(key) {
  const owner = owners.get(key);
  if (!owner) return;
  // Fall back to any voice's timings so lines stay clickable after a voice switch; a click fetches the new voice.
  const byVoice = clips.get(key);
  const clip = [clipFor(key), ...(byVoice?.values() ?? [])].find((c) => c?.words.length);
  const words = clip?.words ?? [];
  for (const line of owner.row.querySelectorAll("p[data-part]")) {
    const part = line.dataset.part;
    const text = owner.texts[part] ?? "";
    line.replaceChildren(...splitByWords(text, words, part).map((piece) => {
      if (piece.word === null) return document.createTextNode(piece.text);
      const span = document.createElement("span");
      span.className = "word";
      span.dataset.word = String(piece.word);
      span.textContent = piece.text;
      span.addEventListener("click", () => seekToWord(key, words[piece.word]));
      return span;
    }));
  }
  if (key === state.currentKey) {
    state.activeWord = -1;
    highlightWord();
  }
}

function resetPlayer() {
  stopPlayback();
  for (const key of [...clips.keys()]) dropClips(key);
  owners.clear();
  pendingClips.clear(); // in-flight fetches belong to owners that are gone
  messages.clear();
  state.currentKey = null;
  state.activeWord = -1;
  player.removeAttribute("src");
  $("player").hidden = true;
}

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
  if (ploy) {
    const key = messageKey(message.id);
    registerOwner(
      key, row, { thai: message.thai, note: message.english_note },
      `/api/sessions/${state.sessionId}/messages/${message.id}/speech`,
    );
    renderWords(key);
  }
  row.scrollIntoView({ block: "end" });
}

function forgetMessage(messageId) {
  document.querySelector(`.msg[data-id="${messageId}"]`)?.remove();
  messages.delete(messageId);
  owners.delete(messageKey(messageId));
  dropClips(messageKey(messageId));
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

// --- review ---

function showReview(review) {
  resetPlayer();
  $("review-title").textContent = `Review · ${review.topic_label} · ${formatDate(review.created_at)}`;
  const fixes = review.items.filter((item) => !item.ok).length;
  $("review-summary").textContent = review.items.length
    ? `${review.items.length - fixes} of ${review.items.length} sentence${review.items.length === 1 ? "" : "s"} sounded natural.`
    : "No Thai sentences to review.";
  const list = $("review-items");
  list.replaceChildren();
  review.items.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = `review-item ${item.ok ? "ok" : "fix"}`;
    const said = document.createElement("p");
    said.className = "said";
    said.textContent = item.ok ? `✓ ${item.said}` : item.said;
    row.appendChild(said);
    if (!item.ok) {
      const key = `r:${review.id}:${index}`;
      const line = document.createElement("div");
      line.className = "better-line";
      const playBtn = document.createElement("button");
      playBtn.type = "button";
      playBtn.className = "play-better";
      playBtn.textContent = "▶";
      playBtn.setAttribute("aria-label", "Hear the better version");
      playBtn.addEventListener("click", (event) => {
        event.currentTarget.blur();
        openClip(key, 0);
      });
      const better = document.createElement("p");
      better.className = "thai better";
      better.dataset.part = "thai";
      better.textContent = item.better;
      line.append(playBtn, better);
      row.appendChild(line);
      if (item.why) {
        const why = document.createElement("p");
        why.className = "why";
        why.textContent = item.why;
        row.appendChild(why);
      }
      registerOwner(key, row, { thai: item.better }, `/api/reviews/${review.id}/items/${index}/speech`);
    }
    list.appendChild(row);
  });
  $("review-tips").replaceChildren(...review.tips.map((tip) => {
    const li = document.createElement("li");
    li.textContent = tip;
    return li;
  }));
  $("review-tips-block").hidden = review.tips.length === 0;
  showScreen("review");
}

function closeReview() {
  reviewNav += 1;
  resetPlayer();
  showScreen("topics"); // the list was refreshed when the session ended
}

async function openReview(reviewId) {
  try {
    const mine = ++reviewNav;
    const review = await api(`/api/reviews/${reviewId}`);
    // A newer review click, Done, or a topic started meanwhile owns the screen now.
    if (mine !== reviewNav || state.starting || state.sessionId !== null) return;
    showReview(review);
  } catch (err) {
    showNotice(errorText(err), "error");
  }
}

async function loadReviews() {
  const { reviews } = await api("/api/reviews");
  const list = $("reviews-list");
  list.replaceChildren();
  $("reviews-empty").hidden = reviews.length > 0;
  for (const review of reviews) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    const verdict = review.fixes ? `${review.fixes} to improve` : "all natural";
    btn.textContent = `${formatDate(review.created_at)} · ${review.topic_label} · ${verdict} (${review.total} sentence${review.total === 1 ? "" : "s"})`;
    btn.addEventListener("click", (event) => {
      event.currentTarget.blur();
      openReview(review.id);
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
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
  // A double-click must not open two mic streams and two sessions.
  if (state.starting || state.sessionId !== null) return;
  state.starting = true;
  reviewNav += 1;
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    state.starting = false;
    showNotice("Microphone access is blocked. Click the icon left of the address bar → Microphone → Allow, then reload the page.", "error");
    return;
  }
  await voicesReady;
  resetPlayer();
  showScreen("chat");
  $("transcript").replaceChildren();
  state.busy = true;
  setStatus("thinking");
  const vkey = voiceKey();
  try {
    const res = await postJson("/api/sessions", { topic: topicId, voice: requestVoice() });
    state.sessionId = res.session_id;
    state.fallbackNoticeShown = false;
    const key = messageKey(res.message.id);
    storeClip(key, res, vkey);
    addMessage(res.message);
    state.busy = false;
    if (!loadClip(key)) openClip(key, 0);
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
  setStatus("thinking", "Saving, updating what I remember and reviewing your Thai…");
  let review = null;
  try {
    const result = await api(`/api/sessions/${state.sessionId}/end`, { method: "POST" });
    review = result.review;
    const facts = result.facts.length;
    const reviewNote = result.review_failed ? " The review couldn't be made this time." : "";
    showNotice(`Saved. I now remember ${facts} thing${facts === 1 ? "" : "s"} about you.${reviewNote}`, result.review_failed ? "error" : "info");
  } catch (err) {
    showNotice(errorText(err), "error");
  } finally {
    state.busy = false;
    state.sessionId = null;
    resetPlayer();
    stopMic();
    if (review) showReview(review);
    else showScreen("topics");
    loadMemory().catch(() => {});
    loadReviews().catch(() => {});
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
  const vkey = voiceKey();
  setStatus("thinking");
  try {
    const res = await api(`/api/sessions/${state.sessionId}/turns`, { method: "POST", body: form });
    state.busy = false;
    if (res.kind === "reply") {
      const key = messageKey(res.assistant_message.id);
      storeClip(key, res, vkey);
      addMessage(res.user_message);
      addMessage(res.assistant_message);
      if (!loadClip(key)) await openClip(key, 0);
    } else if (res.replay_message_id !== null) {
      await openClip(messageKey(res.replay_message_id), 0);
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
    const currentRemoved = removed.some((id) => messageKey(id) === state.currentKey);
    removed.forEach(forgetMessage);
    if (currentRemoved) {
      state.currentKey = null;
      const last = lastAssistantKey();
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

function onKey(event) {
  const tag = event.target?.tagName;
  // A focused slider or select owns the arrow keys and Enter; push-to-talk still works.
  if ((tag === "INPUT" || tag === "SELECT") && event.code !== "Space") return;
  const active = state.sessionId !== null || state.reviewing;
  const modified = event.metaKey || event.ctrlKey || event.altKey;
  const action = keyAction(
    {
      type: event.type, code: event.code, shiftKey: event.shiftKey, repeat: event.repeat,
      metaKey: event.metaKey, ctrlKey: event.ctrlKey, altKey: event.altKey,
    },
    { recording: state.recording, busy: state.busy, inSession: active },
  );
  // In a session every push-to-talk key is ours; on the review screen only the player keys are, so the rest still scroll.
  // Until a clip is loaded the review player is hidden, so let arrows scroll instead of silently changing speed.
  const handled = state.reviewing ? REVIEW_ACTIONS.has(action) && state.currentKey !== null : state.sessionId !== null;
  if (handled && !modified && KEYS_WITH_DEFAULTS.has(event.code)) event.preventDefault();
  if (state.reviewing && !handled) return;
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
bindButton("btn-review-done", closeReview);

setRate(state.rate);
applyHideEnglish();
showScreen("topics");
// A stored voice may no longer exist (Azure key removed, catalog changed); never send it unchecked.
const voicesReady = loadVoices().catch(() => { state.voice = null; });
loadTopics().catch((err) => showNotice(errorText(err), "error"));
loadMemory().catch(() => {});
loadReviews().catch(() => {});
