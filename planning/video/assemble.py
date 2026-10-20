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
import re
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


def frame_times(frames: Path) -> list[float]:
    """When each frame was actually written.

    The capture cannot hold a perfectly even cadence — a screenshot that takes 90ms on a
    busy page costs a slot — so 177 seconds of pool produced 145 seconds of film. Deriving
    a caption's frame from (timestamp x fps) would therefore drift further out of place
    the longer the take ran. The file times are the ground truth.
    """
    return [f.stat().st_mtime for f in sorted(frames.glob("f*.jpg"))]


def wall_to_video(mtimes: list[float], fps: int):
    import bisect

    def vt(wall: float) -> float:
        return min(bisect.bisect_left(mtimes, wall), len(mtimes) - 1) / fps

    return vt


def cut_mapper(keeps: list[tuple[float, float]]):
    """Pre-trim video seconds -> post-trim seconds, or None if that moment was cut."""
    def mapped(v: float) -> float | None:
        out = 0.0
        for a, b in keeps:
            if v < a:
                return None
            if v <= b:
                return out + (v - a)
            out += b - a
        return None
    return mapped


def resolve_anchor(sel: str, events: list[dict], vt, body_end: float,
                   coda_start: float) -> float | None:
    """Turn an event selector into a time in PRE-TRIM video seconds.

    Selectors: "start+N", "end-N", "coda+N", or "event_type" optionally narrowed as
    "event_type:field=value" — which matters because several episode_start events exist
    and only the one triggered by the deadline is the climax.
    """
    if sel.startswith("coda"):
        return coda_start + float(sel[4:] or 0)
    if sel.startswith("start"):
        return float(sel[5:] or 0)
    if sel.startswith("end"):
        return body_end + float(sel[3:] or 0)
    etype, _, cond = sel.partition(":")
    key, _, val = cond.partition("=")
    for ev in events:
        if ev.get("type") != etype:
            continue
        if cond and str(ev.get(key)) != val:
            continue
        return vt(float(ev["ts"]))
    return None


FFPROBE = Path(str(FFMPEG).replace("ffmpeg", "ffprobe"))


def probe(path, what: str) -> str:
    """Size or duration, without assuming ffprobe is installed.

    The static ffmpeg build here ships without ffprobe, so fall back to parsing what
    ffmpeg itself prints about an input it then refuses to encode.
    """
    if FFPROBE.exists():
        key = "stream=width,height" if what == "size" else "format=duration"
        sel = ["-select_streams", "v:0"] if what == "size" else []
        r = subprocess.run([str(FFPROBE), "-v", "error", *sel, "-show_entries", key,
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    r = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(path)],
                       capture_output=True, text=True)
    text = r.stderr
    if what == "size":
        m = re.search(r"Video:.*?,\s(\d{2,5})x(\d{2,5})", text)
        if m:
            return f"{m.group(1)},{m.group(2)}"
        raise SystemExit(f"could not read the size of {path}")
    m = re.search(r"Duration:\s(\d+):(\d+):(\d+\.\d+)", text)
    return f"{int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)):.2f}" if m else ""


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
    ap.add_argument("--endcard", default="", help="a still to hold at the end")
    ap.add_argument("--endcard-s", dest="endcard_s", type=float, default=4.0)
    ap.add_argument("--keep", default="", help="comma list of start-end seconds to keep")
    args = ap.parse_args()

    frames = Path(args.frames)
    meta = json.loads((frames / "meta.json").read_text())
    fps = meta["fps"]
    # The real frame size, not the one we asked Chrome for: headless hands back a
    # viewport a little shorter than the window, and 937 is both wrong for the overlay
    # maths and an odd number, which libx264 refuses outright.
    src_w, src_h = (int(x) for x in
                    probe(sorted(Path(args.frames).glob("f*.jpg"))[0], "size").split(","))
    w, h = 1920, 1080
    pad = f"scale={src_w}:{src_h},pad={w}:{h}:0:0:color=0x09090b"
    print(f"  frames are {src_w}x{src_h}; padding to {w}x{h} on the page's own background")
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
         "-vf", pad, "-c:v", "libx264", "-crf", "17", "-preset", "medium",
         "-pix_fmt", "yuv420p", raw])

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
             "-i", coda_dir / "f%06d.jpg", "-vf", pad, "-c:v", "libx264", "-crf", "17",
             "-preset", "medium", "-pix_fmt", "yuv420p", craw])
        coda = work / "coda.mp4"
        run([FFMPEG, "-y", "-loglevel", "error", "-i", craw, "-ss", start,
             "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p", coda])
        lst = work / "all.txt"
        lst.write_text(f"file '{body}'\nfile '{coda}'\n")
        final_src = work / "joined.mp4"
        run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
             "-c", "copy", final_src])

    # 3b. an end card, held long enough to read the URL
    if args.endcard:
        card = work / "card.mp4"
        run([FFMPEG, "-y", "-loglevel", "error", "-loop", "1", "-framerate", fps,
             "-i", args.endcard, "-t", args.endcard_s, "-c:v", "libx264", "-crf", "17",
             "-preset", "medium", "-pix_fmt", "yuv420p", "-vf", f"scale={w}:{h}", card])
        lst = work / "withcard.txt"
        lst.write_text(f"file '{final_src}'\nfile '{card}'\n")
        joined = work / "joined2.mp4"
        run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
             "-c", "copy", joined])
        final_src = joined

    # 4. captions, anchored to the events that justify them
    cue_defs = json.loads(Path(args.cues).read_text()) if args.cues else []
    vt = wall_to_video(frame_times(frames), fps)
    keeps = ([tuple(float(x) for x in r.split("-")) for r in args.keep.split(",")]
             if args.keep else [(0.0, total)])
    body_len = sum(b - a for a, b in keeps)
    mapped = cut_mapper(keeps)
    cues: list[tuple[float, float, str]] = []
    dropped = []
    for c in cue_defs:
        pre = resolve_anchor(c["anchor"], events, vt, total, total)
        if pre is None:
            dropped.append(c["anchor"])
            continue
        if c["anchor"].startswith("coda"):
            start = body_len + float(c["anchor"][4:] or 0) + c.get("offset", 0.0)
        else:
            at = mapped(pre + c.get("offset", 0.0))
            if at is None:
                dropped.append(f"{c['anchor']} (inside a cut)")
                continue
            start = at
        cues.append((start, start + c.get("hold", 4.0), c["text"]))
    cues.sort()
    for d in dropped:
        print(f"  ! cue dropped — no anchor: {d}")
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

    dur = probe(out, "duration")
    size = out.stat().st_size / 1_000_000
    print(f"\n{out} · {dur}s · {size:.1f} MB")
    if dur and float(dur) > 179:
        print("!! OVER THREE MINUTES — judges are not required to watch past 3:00")
    return 0


if __name__ == "__main__":
    sys.exit(main())
