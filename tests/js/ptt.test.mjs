import { test } from "node:test";
import assert from "node:assert/strict";
import { keyAction, pickMimeType, suffixForMime } from "../../src/pood_thai/static/ptt.js";

const idle = { recording: false, busy: false, inSession: true };
const down = (code, extra = {}) => ({ type: "keydown", code, shiftKey: false, repeat: false, ...extra });
const up = (code) => ({ type: "keyup", code, shiftKey: false, repeat: false });

test("space starts Thai, shift+space starts English", () => {
  assert.equal(keyAction(down("Space"), idle), "start_th");
  assert.equal(keyAction(down("Space", { shiftKey: true }), idle), "start_en");
});

test("space release stops only while recording", () => {
  assert.equal(keyAction(up("Space"), { ...idle, recording: true }), "stop");
  assert.equal(keyAction(up("Space"), idle), null);
});

test("auto-repeat keydown and busy state are ignored", () => {
  assert.equal(keyAction(down("Space", { repeat: true }), idle), null);
  assert.equal(keyAction(down("Space"), { ...idle, busy: true }), null);
  assert.equal(keyAction(down("Space"), { ...idle, recording: true }), null);
});

test("shortcut keys", () => {
  assert.equal(keyAction(down("KeyR"), idle), "replay");
  assert.equal(keyAction(down("Backspace"), idle), "undo");
  assert.equal(keyAction(down("KeyT"), idle), "toggle_transcript");
  assert.equal(keyAction(down("KeyT"), { ...idle, busy: true }), "toggle_transcript");
  assert.equal(keyAction(down("KeyR"), { ...idle, busy: true }), null);
  assert.equal(keyAction(down("Backspace"), { ...idle, recording: true }), null);
  assert.equal(keyAction(down("KeyX"), idle), null);
});

test("nothing happens outside a session", () => {
  assert.equal(keyAction(down("Space"), { ...idle, inSession: false }), null);
});

test("pickMimeType prefers webm/opus and falls back", () => {
  assert.equal(pickMimeType(() => true), "audio/webm;codecs=opus");
  assert.equal(pickMimeType((t) => t === "audio/mp4"), "audio/mp4");
  assert.equal(pickMimeType(() => false), "");
});

test("suffixForMime", () => {
  assert.equal(suffixForMime("audio/webm;codecs=opus"), ".webm");
  assert.equal(suffixForMime("audio/mp4"), ".mp4");
  assert.equal(suffixForMime("audio/ogg;codecs=opus"), ".ogg");
  assert.equal(suffixForMime(""), ".webm");
});

test("browser shortcuts with Cmd/Ctrl/Alt are left alone", () => {
  assert.equal(keyAction(down("KeyR", { metaKey: true }), idle), null);
  assert.equal(keyAction(down("KeyT", { ctrlKey: true }), idle), null);
  assert.equal(keyAction(down("Backspace", { altKey: true }), idle), null);
  assert.equal(keyAction(down("Space", { metaKey: true }), idle), null);
  assert.equal(keyAction(up("Space"), { ...idle, recording: true }), "stop");
});

test("player keys work while thinking but not while recording", () => {
  assert.equal(keyAction(down("ArrowLeft"), idle), "prev_word");
  assert.equal(keyAction(down("ArrowRight"), idle), "next_word");
  assert.equal(keyAction(down("ArrowUp"), idle), "speed_up");
  assert.equal(keyAction(down("ArrowDown"), idle), "speed_down");
  assert.equal(keyAction(down("Enter"), idle), "play_pause");
  assert.equal(keyAction(down("KeyE"), idle), "toggle_english");
  assert.equal(keyAction(down("ArrowRight"), { ...idle, busy: true }), "next_word");
  assert.equal(keyAction(down("Enter"), { ...idle, recording: true }), null);
  assert.equal(keyAction(down("KeyE"), { ...idle, recording: true }), "toggle_english");
  assert.equal(keyAction(down("ArrowLeft", { metaKey: true }), idle), null);
});
