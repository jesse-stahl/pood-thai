// Pure push-to-talk logic, kept free of DOM access so it can be unit tested with node --test.

/**
 * @param {{type: string, code: string, shiftKey: boolean, repeat: boolean}} event
 * @param {{recording: boolean, busy: boolean, inSession: boolean}} state
 * @returns {"start_th"|"start_en"|"stop"|"replay"|"undo"|"toggle_transcript"|null}
 */
export function keyAction(event, state) {
  if (!state.inSession) return null;
  if (event.type === "keyup") {
    return event.code === "Space" && state.recording ? "stop" : null;
  }
  if (event.type !== "keydown" || event.repeat) return null;
  if (event.code === "KeyT") return "toggle_transcript";
  if (state.recording || state.busy) return null;
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
