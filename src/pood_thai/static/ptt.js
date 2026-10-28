// Pure push-to-talk logic, kept free of DOM access so it can be unit tested with node --test.

/**
 * @param {{type: string, code: string, shiftKey: boolean, repeat: boolean, metaKey?: boolean, ctrlKey?: boolean, altKey?: boolean}} event
 * @param {{recording: boolean, busy: boolean, inSession: boolean}} state
 * @returns {"start_th"|"start_en"|"stop"|"replay"|"undo"|"toggle_transcript"|"toggle_english"|"prev_word"|"next_word"|"speed_up"|"speed_down"|"play_pause"|null}
 */
export function keyAction(event, state) {
  if (!state.inSession) return null;
  if (event.type === "keyup") {
    return event.code === "Space" && state.recording ? "stop" : null;
  }
  if (event.type !== "keydown" || event.repeat) return null;
  if (event.metaKey || event.ctrlKey || event.altKey) return null; // leave browser shortcuts alone
  if (event.code === "KeyT") return "toggle_transcript";
  if (event.code === "KeyE") return "toggle_english";
  if (state.recording) return null; // playing audio now would leak into the microphone
  const playerKeys = { ArrowLeft: "prev_word", ArrowRight: "next_word", ArrowUp: "speed_up", ArrowDown: "speed_down", Enter: "play_pause" };
  if (event.code in playerKeys) return playerKeys[event.code];
  if (state.busy) return null;
  if (event.code === "Space") return event.shiftKey ? "start_en" : "start_th";
  if (event.code === "KeyR") return "replay";
  if (event.code === "Backspace") return "undo";
  return null;
}

const MIME_PREFERENCE = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];

export function pickMimeType(isSupported) {
  return MIME_PREFERENCE.find((type) => isSupported(type)) ?? "";
}

export function suffixForMime(mime) {
  if (mime.startsWith("audio/mp4")) return ".mp4";
  if (mime.startsWith("audio/ogg")) return ".ogg";
  return ".webm";
}
