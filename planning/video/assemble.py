"""Cut a recorded take into the demo film, with captions timed to what really happened.

Captions are written against POOL TIME — "two seconds after the decision event" — not
against a stopwatch. The recorder stamps the wall clock it started at, every event in the
pool carries its own timestamp, and the frame rate is fixed, so a caption can be anchored
to the frame where the thing it describes is actually on screen. Re-record the take and
the captions land in the right place again without being re-timed by hand.

    cd backend && ../planning/video/assemble.py --frames /tmp/pf-main --out /tmp/potluck.mp4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
FFMPEG = HERE / "ffbin" / "ffmpeg"
if not FFMPEG.exists():
    FFMPEG = Path("/Users/mac/Desktop/Archive/projects/resonance/planning/video/ffbin/ffmpeg")

# A caption is placed by the EVENT it belongs to, plus an offset, and holds for a duration.
# `at` is matched against the pool's event log: "type" or "type:field=value".
@dataclass
class Cue:
    anchor: str          # event selector, or "start+N" / "end-N"
    offset: float        # seconds after that event
    hold: float          # seconds on screen
    text: str            # one or two short lines; \\N is a line break in ASS


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def build_ass(cues: list[tuple[float, float, str]], width: int, height: int) -> str:
    """Captions only — the scrim behind them is a filter, not a subtitle, so it can be a
    soft gradient rather than a hard box."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Helvetica Neue,46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0.4,0,1,2,1,2,320,320,54,1
Style: Lead,Helvetica Neue,58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0.6,0,1,2,1,2,300,300,54,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for start, end, text in cues:
        style = "Lead" if text.startswith("!") else "Cap"
        body = text[1:] if text.startswith("!") else text
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{body}")
    return head + "\n".join(lines) + "\n"


def resolve_anchor(sel: str, events: list[dict], t0: float, total: float) -> float | None:
    """Turn an event selector into a time in seconds from the first frame."""
    if sel.startswith("start"):
        return float(sel[6:]) if len(sel) > 5 else 0.0
    if sel.startswith("end"):
        return total + float(sel[3:])
    etype, _, cond = sel.partition(":")
    key, _, val = cond.partition("=")
    for ev in events:
        if ev.get("type") != etype:
            continue
        if cond and str(ev.get(key)) != val:
            continue
        return float(ev["ts"]) - t0
    return None


def run(cmd: list[str]) -> None:
    print("  $", " ".join(str(c) for c in cmd[:6]), "...", flush=True)
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stderr[-2500:])
        raise SystemExit(f"ffmpeg failed ({p.returncode})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble the Potluck demo film.")
    ap.add_argument("--frames", required=True)
    ap.add_argument("--coda", default="", help="a second frames dir, appended as the twin")
    ap.add_argument("--coda-tail", type=float, default=16.0,
                    help="seconds to keep from the END of the coda take")
    ap.add_argument("--cues", default="", help="JSON file of cues; omit to use the built-in script")
    ap.add_argument("--out", default="/tmp/potluck-demo.mp4")
    ap.add_argument("--api", default="https://potluck.overpro.fit")
    ap.add_argument("--scrim", type=int, default=230, help="height of the caption scrim")
    ap.add_argument("--music", default="")
    ap.add_argument("--keep", default="", help="comma list of start-end seconds to keep")
    args = ap.parse_args()

    frames = Path(args.frames)
    meta = json.loads((frames / "meta.json").read_text())
    fps, w, h = meta["fps"], meta["width"], meta["height"]
    t0 = meta.get("started_at")
    n = len(sorted(frames.glob("f*.jpg")))
    total = n / fps
    print(f"take: {n} frames · {total:.1f}s · {w}x{h} @ {fps}fps · pool {meta['pool_id']}")

    # The event log is cached next to the frames the moment a take finishes, so a cut can
    # be re-made with no network — which is the normal case here, not the exception.
    events = []
    cache = frames / "pool.json"
    if cache.exists():
        events = json.loads(cache.read_text()).get("events", [])
        print(f"  events: {len(events)} from {cache}")
    elif t0:
        try:
            doc = httpx.get(f"{args.api}/api/pools/{meta['pool_id']}", timeout=30).json()
            events = doc["events"]
            cache.write_text(json.dumps(doc))
        except Exception as exc:  # noqa: BLE001
            print(f"  (no events: {exc}; cues anchored to events will be dropped)")

    work = Path("/tmp/potluck-video")
    work.mkdir(exist_ok=True)

    # 1. the take, as one clip
    raw = work / "raw.mp4"
    run([FFMPEG, "-y", "-loglevel", "error", "-framerate", fps, "-i", frames / "f%06d.jpg",
         "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p", raw])

    body = raw
    # 2. optional trims, given as keep-ranges in source seconds
    if args.keep:
        parts = []
        for i, rng in enumerate(args.keep.split(",")):
            a, b = (float(x) for x in rng.split("-"))
            seg = work / f"seg{i}.mp4"
            run([FFMPEG, "-y", "-loglevel", "error", "-i", raw, "-ss", a, "-to", b,
                 "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p", seg])
            parts.append(seg)
        lst = work / "parts.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in parts))
        body = work / "body.mp4"
        run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
             "-c", "copy", body])

    # 3. the twin coda, taken from the tail of its own recording
    final_src = body
    if args.coda:
        coda_dir = Path(args.coda)
        cmeta = json.loads((coda_dir / "meta.json").read_text())
        cn = len(sorted(coda_dir.glob("f*.jpg")))
        start = max(0.0, cn / cmeta["fps"] - args.coda_tail)
        craw = work / "coda_raw.mp4"
        run([FFMPEG, "-y", "-loglevel", "error", "-framerate", cmeta["fps"],
             "-i", coda_dir / "f%06d.jpg", "-c:v", "libx264", "-crf", "17",
             "-preset", "medium", "-pix_fmt", "yuv420p", craw])
        coda = work / "coda.mp4"
        run([FFMPEG, "-y", "-loglevel", "error", "-i", craw, "-ss", start,
             "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p", coda])
        lst = work / "all.txt"
        lst.write_text(f"file '{body}'\nfile '{coda}'\n")
        final_src = work / "joined.mp4"
        run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
             "-c", "copy", final_src])

    # 4. captions, anchored to the events that justify them
    cue_defs = json.loads(Path(args.cues).read_text()) if args.cues else []
    cues: list[tuple[float, float, str]] = []
    dropped = 0
    for c in cue_defs:
        at = resolve_anchor(c["anchor"], events, t0, total) if c["anchor"] else None
        if at is None:
            dropped += 1
            continue
        start = at + c.get("offset", 0.0)
        cues.append((start, start + c.get("hold", 4.0), c["text"]))
    cues.sort()
    if dropped:
        print(f"  ({dropped} cue(s) had no matching event and were dropped)")
    ass_path = work / "caps.ass"
    ass_path.write_text(build_ass(cues, w, h))
    print(f"captions: {len(cues)} cues -> {ass_path}")

    # 5. the scrim and the burn. A soft alpha gradient, not a box: the ledger runs to the
    #    bottom of the frame and a hard edge across it reads as a bug.
    scrim = work / "scrim.png"
    run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"color=c=black:s={w}x{args.scrim}",
         "-vf", f"format=rgba,geq=r=0:g=0:b=0:a='250*pow(Y/{args.scrim - 1},0.55)'",
         "-frames:v", "1", scrim])

    out = Path(args.out)
    vf = (f"[0:v][1:v]overlay=0:{h - args.scrim}[bg];"
          f"[bg]ass={ass_path}:fontsdir=/System/Library/Fonts[v]")
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", final_src, "-i", scrim]
    if args.music:
        cmd += ["-i", args.music]
    cmd += ["-filter_complex", vf, "-map", "[v]"]
    if args.music:
        cmd += ["-map", "2:a", "-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "slow", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", out]
    run(cmd)

    dur = subprocess.run(
        [str(FFMPEG).replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries",
         "format=duration", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True).stdout.strip()
    size = out.stat().st_size / 1_000_000
    print(f"\n{out} · {dur}s · {size:.1f} MB")
    if dur and float(dur) > 179:
        print("!! OVER THREE MINUTES — judges are not required to watch past 3:00")
    return 0


if __name__ == "__main__":
    sys.exit(main())
