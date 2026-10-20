"""Generate the voice-over with ElevenLabs, one clip per caption.

The lines come from cues.json, so the spoken words and the burned-in captions can never
drift apart — there is one source of truth for both, and re-cutting the film re-cuts the
script with it.

The key is read from the environment or backend/.env and is never written anywhere. It
is a credential for an account that bills, so it does not belong in this repository.

    ELEVEN_API_KEY=... ./planning/video/voice.py --out ~/Desktop/potluck-voice
    ELEVEN_API_KEY=... ./planning/video/voice.py --only 10,16   # regenerate two lines
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
API = "https://api.elevenlabs.io/v1"


def load_key() -> str:
    key = os.environ.get("ELEVEN_API_KEY") or os.environ.get("ELEVENLABS_API_KEY")
    if key:
        return key.strip()
    env = HERE.parent.parent / "backend" / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(("ELEVEN_API_KEY=", "ELEVENLABS_API_KEY=")):
                return line.split("=", 1)[1].strip()
    raise SystemExit(
        "No ElevenLabs key. Put it in backend/.env as ELEVEN_API_KEY=... (that file is\n"
        "gitignored) or pass it as an environment variable. Get it from\n"
        "elevenlabs.io -> your avatar -> API Keys.")


def lines_from_cues() -> list[str]:
    """The captions, spoken. Leading '!' marks a lead-in caption, it is not read aloud."""
    cues = json.loads((HERE / "cues.json").read_text())
    out = []
    for c in cues:
        t = c["text"].lstrip("!").replace("\\N", " ")
        out.append(t.replace("“", "").replace("”", "").strip())
    return out


def find_voice(client: httpx.Client, key: str, name: str) -> str:
    r = client.get(f"{API}/voices", headers={"xi-api-key": key}, timeout=30)
    r.raise_for_status()
    voices = r.json().get("voices", [])
    # Names in the UI carry a descriptor — "Eric - Smooth, Trustworthy" — so match on
    # the part before the dash, and fall back to an exact match for a custom voice whose
    # own name contains one.
    want = name.strip().lower()
    for v in voices:
        full = v.get("name", "").strip().lower()
        if full == want or full.split(" - ")[0].strip() == want:
            return v["voice_id"]
    names = ", ".join(sorted(v.get("name", "?") for v in voices))
    raise SystemExit(f"No voice called {name!r} on this account. Available: {names}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Voice the Potluck film with ElevenLabs.")
    ap.add_argument("--voice", default="Eric", help="voice name, as it appears in the UI")
    ap.add_argument("--out", default=str(Path.home() / "Desktop" / "potluck-voice"))
    ap.add_argument("--model", default="eleven_multilingual_v2")
    ap.add_argument("--stability", type=float, default=0.5)
    ap.add_argument("--similarity", type=float, default=0.75)
    ap.add_argument("--only", default="", help="comma list of 1-based line numbers")
    ap.add_argument("--dry-run", action="store_true", help="print the lines and stop")
    args = ap.parse_args()

    lines = lines_from_cues()
    wanted = ({int(x) for x in args.only.split(",") if x.strip()}
              if args.only else set(range(1, len(lines) + 1)))
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    print(f"{len(lines)} lines; generating {len(wanted)} into {out}\n")
    if args.dry_run:
        for i, t in enumerate(lines, 1):
            print(f"  line-{i:02d}  {t}")
        return 0

    key = load_key()
    with httpx.Client(timeout=180) as c:
        vid = find_voice(c, key, args.voice)
        print(f"voice {args.voice} -> {vid}\n")
        total = 0
        for i, text in enumerate(lines, 1):
            if i not in wanted:
                continue
            r = c.post(
                f"{API}/text-to-speech/{vid}",
                headers={"xi-api-key": key, "accept": "audio/mpeg",
                         "content-type": "application/json"},
                json={"text": text, "model_id": args.model,
                      "voice_settings": {"stability": args.stability,
                                         "similarity_boost": args.similarity}},
            )
            if r.status_code != 200:
                print(f"  line-{i:02d}  FAILED {r.status_code}: {r.text[:220]}")
                if r.status_code in (401, 402):
                    return 1               # bad key or out of quota: stop, do not hammer
                continue
            path = out / f"line-{i:02d}.mp3"
            path.write_bytes(r.content)
            total += len(text)
            print(f"  line-{i:02d}  {len(r.content) // 1024:>4} KB  {text[:58]}")
    print(f"\n{total} characters spent. Next: assemble.py --voice {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
