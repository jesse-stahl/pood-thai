// Pure audio-player helpers (no DOM), unit tested with node --test.

export const MIN_RATE = 0.1;
export const MAX_RATE = 2;
export const RATE_STEP = 0.05;
export const RATE_KEY_STEP = 0.1;
const RESTART_WORD_AFTER_S = 0.25; // going back this far into a word restarts it instead of jumping back

export function clampRate(rate) {
  if (!Number.isFinite(rate)) return 1;
  const snapped = Math.round(rate / RATE_STEP) * RATE_STEP;
  return Number(Math.min(MAX_RATE, Math.max(MIN_RATE, snapped)).toFixed(2));
}

/** Index of the last word starting at or before t (seconds), or -1 before the first word. */
export function wordIndexAt(words, t) {
  let lo = 0;
  let hi = words.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (words[mid].t_start <= t) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

/** The word to jump to for ← (dir -1) or → (dir 1); -1 when there are no words. */
export function stepWord(words, t, dir) {
  if (!words.length) return -1;
  const i = wordIndexAt(words, t);
  if (dir > 0) return i + 1 < words.length ? i + 1 : -1; // nothing after the last word
  if (i < 0) return 0;
  return t - words[i].t_start > RESTART_WORD_AFTER_S ? i : Math.max(0, i - 1);
}

/** Split text into pieces; pieces covering one of this part's words carry its index in `words`. */
export function splitByWords(text, words, part) {
  // Offsets count Unicode code points (Python string indices), not UTF-16 units, so slice by code point.
  const chars = Array.from(text);
  const slice = (start, end) => chars.slice(start, end).join("");
  const pieces = [];
  let pos = 0;
  words.forEach((w, i) => {
    if (w.part !== part || w.start < pos || w.end > chars.length || w.end <= w.start) return;
    if (w.start > pos) pieces.push({ text: slice(pos, w.start), word: null });
    pieces.push({ text: slice(w.start, w.end), word: i });
    pos = w.end;
  });
  if (pos < chars.length) pieces.push({ text: slice(pos), word: null });
  return pieces;
}

export function formatTime(seconds) {
  const s = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}
