"""Record a live Potluck pool as a frame sequence, by driving it and watching it at once.

Screen-recording by hand gives you one take, at whatever frame rate the machine felt like,
with a cursor in it. This drives the pool through the API on a schedule we choose and
grabs frames over the DevTools protocol at a fixed cadence, so a run is repeatable: if the
agent says something better on the third take, the third take is cut the same way as the
first.

Chrome's own --screenshot flag cannot be used here. The pool page holds an SSE connection
open for its whole life, so the page never reaches the idle state that flag waits for, and
the call simply hangs. Page.captureScreenshot over CDP has no such notion and returns
immediately, which is also what gives us an even frame rate.

    cd backend && ../planning/video/record.py --scenario unwind --out /tmp/frames
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333

GOAL = ("We're getting Dana a leaving present. Five of us, $45 each, so $180 in total. "
        "I need it by Friday morning because the party is Friday lunchtime.")
POLICY_AMOUNT = ("Only buy the gift if we actually reach $180 by Friday. If we fall short, "
                 "give everyone their money back — I'd rather no gift than a half-paid one.")
# The twin's whole job is to DISAGREE with the amount reading on identical arithmetic.
# "at least 4 of 5" does not: with four payers at $45 the count and the total are satisfied
# together, so both policies release and the comparison proves nothing. Requiring all five
# makes four payers hit $180 and still fail the condition.
POLICY_COUNT = ("This only works if the whole team is in it, so refund everyone unless all "
                "five of us actually chip in. Even if the money adds up to $180 with four "
                "payers, give it all back — I don't want a gift that one person skipped.")

SCENARIOS = {
    # the main narrative: the pool falls short and everyone is refunded
    "unwind": {"policy": POLICY_AMOUNT, "pay": [("c1", 14), ("c2", 24), ("c3", 36)]},
    # the twin: identical numbers, one phrase changed, opposite branch
    "release": {"policy": POLICY_AMOUNT, "pay": [("c1", 14), ("c2", 23), ("c3", 32), ("c4", 41)]},
    # the falsifier: four pay, the target is met, and a COUNT condition still releases
    # identical to "release" in every number: four payers, $180 collected. Only the
    # sentence changes, and the branch changes with it.
    "twin": {"policy": POLICY_COUNT, "pay": [("c1", 14), ("c2", 23), ("c3", 32), ("c4", 41)]},
}


class Chrome:
    """A headless Chrome we can talk to, and are sure to close."""

    def __init__(self, url: str, width: int, height: int):
        self.url, self.width, self.height = url, width, height
        self.proc: subprocess.Popen | None = None
        self.profile = f"/tmp/potluck-chrome-{os.getpid()}"

    def __enter__(self):
        shutil.rmtree(self.profile, ignore_errors=True)
        # Old headless on purpose. --headless=new dies on the first captureScreenshot
        # here, and --disable-extensions keeps the machine's own browser extensions out
        # of the frame — they load into a fresh profile anyway and one of them paints.
        self.proc = subprocess.Popen(
            [CHROME, "--headless", f"--remote-debugging-port={PORT}",
             f"--user-data-dir={self.profile}", "--disable-gpu", "--hide-scrollbars",
             "--disable-extensions", "--disable-sync", "--no-first-run",
             "--no-default-browser-check",
             # No --default-background-color and no --force-device-scale-factor: either
             # one stops old headless from ever creating the page target, so /json comes
             # back with extension workers and nothing to attach to. The page paints its
             # own background anyway.
             f"--window-size={self.width},{self.height}", self.url],
            stdout=open(self.profile + ".log", "w"), stderr=subprocess.STDOUT)
        return self

    def __exit__(self, *_exc):
        if self.proc and self.proc.poll() is not None:
            print(f"[chrome exited with {self.proc.returncode}; log: {self.profile}.log]")
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)

    async def ws_url(self, timeout: float = 25.0) -> str:
        """Wait for OUR page, not merely for a page. Matching on the url avoids attaching
        to an about:blank that is about to be navigated away from."""
        want = self.url.split("://", 1)[-1]
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=3.0) as c:
            while time.monotonic() < deadline:
                try:
                    targets = (await c.get(f"http://127.0.0.1:{PORT}/json")).json()
                    for t in targets:
                        if (t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                                and want in t.get("url", "")):
                            return t["webSocketDebuggerUrl"]
                except Exception:  # noqa: BLE001 — chrome is still coming up
                    pass
                await asyncio.sleep(0.3)
        raise RuntimeError("chrome never exposed a debuggable page")


class Session:
    """A CDP session several coroutines can use at once.

    The obvious implementation — write a message, then read until you see your own id —
    is wrong the moment two tasks share the socket: the screenshot loop and the typing
    task each swallow replies meant for the other, and both then wait for a message that
    has already been thrown away. The first version of this file did exactly that, and
    because the typing task's failure was never awaited, the recording silently captured
    three minutes of an untouched form.

    So: one reader, and a future per request id.
    """

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — socket died; wake everyone waiting
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(exc)
            self._pending.clear()

    async def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mine = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mine] = fut
        await self.ws.send(json.dumps({"id": mine, "method": method, "params": params or {}}))
        msg = await fut
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    async def close(self) -> None:
        self._reader.cancel()
        try:
            await self._reader
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


CLEAR_AND_FOCUS = """
(() => {
  const el = document.querySelectorAll('textarea')[%d];
  if (!el) return 'no textarea';
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype, 'value').set;
  setter.call(el, '');
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.focus();
  return 'ok';
})()
"""

CLICK_START = """
(() => {
  const b = [...document.querySelectorAll('button')]
    .find(x => /hand it to the agent/i.test(x.textContent || ''));
  if (!b) return 'no button';
  b.click();
  return 'clicked';
})()
"""

POOL_ID_FROM_URL = "location.pathname.replace('/p/','')"


async def type_into(s: "Session", index: int, text: str, cps: float = 42.0):
    """Type into a textarea the way a person would — one character at a time.

    Setting .value and firing one input event would be faster and would look like a
    paste. The whole point of the opening shot is that the success condition is free
    text somebody wrote, so it has to be seen being written.
    """
    await s.send("Runtime.evaluate", {"expression": CLEAR_AND_FOCUS % index,
                                      "returnByValue": True})
    delay = 1.0 / cps
    for ch in text:
        await s.send("Input.dispatchKeyEvent", {"type": "char", "text": ch})
        await asyncio.sleep(delay)


async def create_pool(base: str, policy: str) -> str:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{base}/api/pools",
                         json={"goal_text": GOAL, "policy_text": policy, "demo": True})
        r.raise_for_status()
        return r.json()["pool_id"]


async def driver(base: str, pool_id: str, plan: list[tuple[str, int]], t0: float, log):
    """Pay as each contributor at its scheduled second. Staggered like people, not a script."""
    for cid, at in plan:
        await asyncio.sleep(max(0.0, t0 + at - time.monotonic()))
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post(f"{base}/api/pools/{pool_id}/pay/{cid}", json={})
            log(f"paid as {cid} -> {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            log(f"pay {cid} failed: {exc}")


async def watcher(base: str, pool_id: str, done: asyncio.Event, log):
    """Stop the recording a beat after the pool closes, not on a guessed duration."""
    async with httpx.AsyncClient(timeout=20.0) as c:
        while not done.is_set():
            await asyncio.sleep(3)
            try:
                phase = (await c.get(f"{base}/api/pools/{pool_id}")).json()["phase"]
            except Exception:  # noqa: BLE001
                continue
            if phase in ("CLOSED", "ESCALATED"):
                log(f"pool {phase} — recording 6s of the final frame, then stopping")
                await asyncio.sleep(6)
                done.set()


async def record(args) -> int:
    scenario = SCENARIOS[args.scenario]
    base = args.url.rstrip("/")
    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    t_start = time.monotonic()
    t_wall = time.time()          # frames land at t_wall + n/fps; captions align on it

    def log(msg: str):
        print(f"[{time.monotonic() - t_start:6.1f}s] {msg}", flush=True)

    if args.cold_open:
        pool_id = None                       # the page will create it, on camera
        start_url = f"{base}/"
        log(f"cold open on {start_url} ({args.scenario})")
    else:
        pool_id = await create_pool(base, scenario["policy"])
        start_url = f"{base}/p/{pool_id}"
        log(f"pool {pool_id} ({args.scenario})")

    with Chrome(start_url, args.width, args.height) as chrome:
        ws_url = await chrome.ws_url()
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            s = Session(ws)
            await s.send("Page.enable")
            await asyncio.sleep(2.5)          # let the first paint and the fonts settle

            done = asyncio.Event()
            t0 = time.monotonic()
            tasks: list[asyncio.Task] = []

            async def opening():
                """The first twenty seconds: somebody states the goal and hands it over."""
                nonlocal pool_id
                try:
                    await _opening()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log(f"!! the opening failed: {type(exc).__name__}: {exc}")
                    done.set()

            async def _opening():
                nonlocal pool_id
                await asyncio.sleep(2.0)
                await type_into(s, 0, GOAL)
                await asyncio.sleep(1.2)
                await type_into(s, 1, scenario["policy"])
                await asyncio.sleep(1.6)
                await s.send("Runtime.evaluate", {"expression": CLICK_START,
                                                  "returnByValue": True})
                for _ in range(40):           # wait for the page to land on /p/{id}
                    await asyncio.sleep(0.25)
                    r = await s.send("Runtime.evaluate",
                                     {"expression": POOL_ID_FROM_URL, "returnByValue": True})
                    val = (r.get("result") or {}).get("value") or ""
                    if val.startswith("pool_"):
                        pool_id = val
                        log(f"pool {pool_id} created on camera")
                        tasks.append(asyncio.create_task(
                            driver(base, pool_id, scenario["pay"], time.monotonic(), log)))
                        tasks.append(asyncio.create_task(watcher(base, pool_id, done, log)))
                        return
                log("!! the page never reached a pool url")
                done.set()

            if args.cold_open:
                tasks.append(asyncio.create_task(opening()))
            else:
                tasks.append(asyncio.create_task(driver(base, pool_id, scenario["pay"], t0, log)))
                tasks.append(asyncio.create_task(watcher(base, pool_id, done, log)))

            n, period, hard_stop = 0, 1.0 / args.fps, t0 + args.max_s
            nxt = t0
            try:
                while not done.is_set() and time.monotonic() < hard_stop:
                    nxt += period
                    shot = await s.send("Page.captureScreenshot",
                                        {"format": "jpeg", "quality": args.quality,
                                         "captureBeyondViewport": False})
                    (out / f"f{n:06d}.jpg").write_bytes(base64.b64decode(shot["data"]))
                    n += 1
                    if n % (args.fps * 10) == 0:
                        log(f"{n} frames")
                    sleep = nxt - time.monotonic()
                    if sleep > 0:
                        await asyncio.sleep(sleep)
                    else:
                        nxt = time.monotonic()     # we fell behind; do not spiral
            finally:
                done.set()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    secs = n / args.fps
    log(f"{n} frames = {secs:.1f}s at {args.fps}fps -> {out}")
    (out / "meta.json").write_text(json.dumps(
        {"pool_id": pool_id, "scenario": args.scenario, "frames": n, "fps": args.fps,
         "cold_open": bool(args.cold_open), "started_at": t_wall,
         "width": args.width, "height": args.height, "url": f"{base}/p/{pool_id}"}, indent=2))
    print(f"\npool page: {base}/p/{pool_id}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Record a live Potluck pool to a frame sequence.")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="unwind")
    ap.add_argument("--url", default="https://potluck.overpro.fit")
    ap.add_argument("--out", default="/tmp/potluck-frames")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--quality", type=int, default=88)
    ap.add_argument("--max-s", dest="max_s", type=float, default=200.0)
    ap.add_argument("--cold-open", dest="cold_open", action="store_true",
                    help="start on the home page and type the goal in, on camera")
    return asyncio.run(record(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
