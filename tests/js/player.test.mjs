import { test } from "node:test";
import assert from "node:assert/strict";
import { MAX_RATE, MIN_RATE, clampRate, formatTime, splitByWords, stepWord, wordIndexAt } from "../../src/pood_thai/static/player.js";

const W = [
  { part: "thai", start: 0, end: 3, t_start: 0.05, t_end: 0.25 },
  { part: "thai", start: 4, end: 7, t_start: 0.25, t_end: 0.475 },
  { part: "thai", start: 7, end: 9, t_start: 0.475, t_end: 0.613 },
];

test("clampRate keeps 0.1–2, snaps to 0.05 and defaults to 1", () => {
  assert.equal(MIN_RATE, 0.1);
  assert.equal(MAX_RATE, 2);
  assert.equal(clampRate(0.01), 0.1);
  assert.equal(clampRate(5), 2);
  assert.equal(clampRate(0.93), 0.95);
  assert.equal(clampRate(1.1 + 0.1), 1.2);
  assert.equal(clampRate(Number.NaN), 1);
});

test("wordIndexAt finds the word being heard", () => {
  assert.equal(wordIndexAt(W, 0), -1);
  assert.equal(wordIndexAt(W, 0.3), 1);
  assert.equal(wordIndexAt(W, 10), 2);
  assert.equal(wordIndexAt([], 1), -1);
});

test("stepWord moves forward and back like a music player", () => {
  assert.equal(stepWord(W, 0, 1), 0);
  assert.equal(stepWord(W, 0.3, 1), 2);
  assert.equal(stepWord(W, 0.5, 1), -1); // already on the last word: nothing to jump to
  assert.equal(stepWord(W, 1.8, 1), -1); // trailing silence after the last word
  assert.equal(stepWord(W, 0.3, -1), 0); // just started word 1 -> previous word
  assert.equal(stepWord(W, 0.75, -1), 2); // well into word 2 -> restart it
  assert.equal(stepWord(W, 0, -1), 0);
  assert.equal(stepWord([], 1, 1), -1);
});

test("splitByWords wraps one part's words and keeps the text between them", () => {
  const note = { part: "note", start: 0, end: 5, t_start: 0, t_end: 0.2 };
  assert.deepEqual(splitByWords("โอ้ ฟังดู", [note, ...W], "thai"), [
    { text: "โอ้", word: 1 },
    { text: " ", word: null },
    { text: "ฟัง", word: 2 },
    { text: "ดู", word: 3 },
  ]);
  assert.deepEqual(splitByWords("abc", [], "thai"), [{ text: "abc", word: null }]);
  const bad = [{ part: "thai", start: 2, end: 99, t_start: 0, t_end: 1 }];
  assert.deepEqual(splitByWords("abc", bad, "thai"), [{ text: "abc", word: null }]);
});

test("formatTime shows tenths under a minute", () => {
  assert.equal(formatTime(1.234), "1.2s");
  assert.equal(formatTime(75), "1:15");
  assert.equal(formatTime(Number.NaN), "0.0s");
});

test("splitByWords counts code points like the server, so emoji don't shift words", () => {
  const word = { part: "thai", start: 2, end: 4, t_start: 0, t_end: 0.3 };
  assert.deepEqual(splitByWords("😊 ดีค่ะ", [word], "thai"), [
    { text: "😊 ", word: null },
    { text: "ดี", word: 0 },
    { text: "ค่ะ", word: null },
  ]);
});
