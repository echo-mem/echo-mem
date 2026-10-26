"""Render the demo as frames and hand them to ffmpeg.

Every line of output in here is REAL: captured from the commands running
against a throwaway database, not written to look plausible. The project's
whole argument is that its numbers are measured, and a demo that fakes its
own output would be the one place that is not true.
"""
from __future__ import annotations

import pathlib
import shutil

from PIL import Image, ImageDraw, ImageFont

S = pathlib.Path("/private/tmp/claude-501/-Users-ayush-Desktop-mypc-work-echo-mem/53a848e1-7c93-4dc2-823e-c1cb3e2d668f/scratchpad")
OUT = S / "frames"
W, H = 1040, 712
PAD, LH, FS = 34, 25, 15.5

BG, CHROME = (11, 14, 19), (22, 26, 33)
DIM, FG = (107, 116, 132), (231, 235, 243)
RED, GREEN, BLUE = (224, 72, 63), (104, 224, 136), (122, 162, 247)

mono = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", FS)
bold = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", FS, index=1)

# (text, colour, bold). None = blank line.
SCRIPT = [
    ("# Claude Code records a decision, mid task", DIM, 0),
    ("", None, 0),
    ("write_episode(", BLUE, 1),
    ('  facts=[{"source": "billing", "target": "Razorpay",', FG, 0),
    ('            "relation_type": "uses", "causal_hint": "caused_by",', FG, 0),
    ('            "fact": "billing moved from Stripe to Razorpay because', FG, 0),
    ('                     Stripe does not support UPI mandates in India"}]', FG, 0),
    (")", BLUE, 1),
    ("", None, 0),
    ("stored: true   edges_created: 1   model calls: 0", GREEN, 1),
    ("", None, 0),
    ("# three days later, in Cursor. different tool, same memory.", DIM, 0),
    ("", None, 0),
    ("$ echo-memory recall \"why are we on Razorpay\"", FG, 1),
    ("", None, 0),
    ("Echo Memory already knows this, retrieved for the prompt above.", DIM, 0),
    ("- billing moved from Stripe to Razorpay because Stripe does not", FG, 0),
    ("  support UPI mandates in India", FG, 0),
    ("  [written by claude-code, fact_id 1125899906842625]", GREEN, 1),
    ("", None, 0),
    ("$ echo-memory --scope shared why 1125899906842625", FG, 1),
    ("", None, 0),
    ("History for fact 1125899906842625:", DIM, 0),
    ("  2026-10-26T13:27:15Z: created: billing moved from Stripe to", FG, 0),
    ("  Razorpay because Stripe does not support UPI mandates in India", FG, 0),
]

def frame(n_lines: int, partial: str | None, cursor: bool) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 34], fill=CHROME)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([18 + i * 19, 12, 28 + i * 19, 22], fill=c)
    d.text((W // 2 - 62, 10), "echo memory", font=mono, fill=DIM)

    y = 34 + PAD
    for i in range(n_lines):
        text, colour, is_bold = SCRIPT[i]
        if colour is None:
            y += LH; continue
        d.text((PAD, y), text, font=bold if is_bold else mono, fill=colour)
        y += LH
    if partial is not None:
        text, colour, is_bold = SCRIPT[n_lines]
        f = bold if is_bold else mono
        d.text((PAD, y), partial, font=f, fill=colour or FG)
        if cursor:
            x = PAD + d.textlength(partial, font=f)
            d.rectangle([x + 2, y + 2, x + 10, y + FS + 4], fill=GREEN)
    return im

def main() -> None:
    if OUT.exists(): shutil.rmtree(OUT)
    OUT.mkdir()
    n = 0
    def save(img):
        nonlocal n
        img.save(OUT / f"{n:05d}.png"); n += 1

    for i, (text, colour, _) in enumerate(SCRIPT):
        if colour is None or not text:
            save(frame(i + 1, None, False)); continue
        typed = text.startswith(("$", "#", "write_episode"))
        if typed:
            step = 3
            for k in range(0, len(text) + 1, step):
                save(frame(i, text[:k], True))
            save(frame(i, text, True))
        save(frame(i + 1, None, False))
        # Let the two lines that carry the argument breathe.
        if "written by claude-code" in text or "model calls: 0" in text:
            for _ in range(26): save(frame(i + 1, None, False))
        elif text.startswith("$"):
            for _ in range(6): save(frame(i + 1, None, False))

    for _ in range(34): save(frame(len(SCRIPT), None, False))
    print(f"{n} frames")

main()
