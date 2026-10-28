# พูดไทย · pood-thai

A voice-only Thai conversation partner that runs on your Mac. Hold Space and speak Thai, and Ploy answers out loud in simple Thai.

## Setup (once)

1. Make sure Claude Code is logged in: run `claude` once in a terminal.
   Whisper also needs ffmpeg: `brew install ffmpeg`.
2. Optional, for the best voice: create a free Azure Speech resource (pricing tier **F0**) in the Azure portal, then copy `.env.example` to `.env` and fill in `AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION`. Without it, the app uses the Mac's built-in Thai voice (Kanya).
3. `./start.sh`. The first run downloads the Whisper model (~1.5GB).

## Using it

| Key | Action |
|---|---|
| Hold **Space** | Speak Thai |
| Hold **Shift+Space** | Ask in English ("how do I say tired?") |
| **← / →** | Jump to the previous / next word and keep playing |
| **↑ / ↓** | Speed up / slow down (0.1× to 2×) |
| **Enter** | Play / pause |
| **R** | Replay the current reply |
| **Backspace** | Undo your last turn (if it misheard you) |
| **T** | Show or hide the transcript |
| **E** | Hide or show the English translations (tap "Show English" on one line to peek) |

The player under Ploy's face has a scrubber, a speed slider and a voice picker (Premwadee, Achara, Niwat or Ava; needs the Azure key). Click any word in Ploy's lines of the transcript to hear it from there. Say **ช้าๆ หน่อย** or **อีกครั้ง** to hear the reply again. Click **End** when you're done, and Ploy will remember the important things for next time ("What I remember" on the start screen lets you delete facts).

Replies take a few seconds because Claude runs through your Claude Code subscription.

## Development

- Unit tests: `uv run pytest -q` and `node --test tests/js/*.test.mjs`
- Live checks (real Whisper/Claude/say; Azure if `AZURE_SPEECH_KEY` is exported): `env -u ANTHROPIC_API_KEY uv run pytest -m live -s`
- Data lives in `./data/pood-thai.sqlite3`.
