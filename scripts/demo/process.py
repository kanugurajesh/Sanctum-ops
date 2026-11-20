"""Cut the recorded waits out of the demo video, encode MP4, and build a GIF tour from screenshots."""
import json
import subprocess
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"
IMG = DOCS / "images"
meta = json.loads((Path(__file__).parent / "cuts.json").read_text())
src = meta["video"]
cuts = sorted(meta["cuts"])

# merge overlapping cuts
merged = []
for a, b in cuts:
    if merged and a <= merged[-1][1]:
        merged[-1][1] = max(merged[-1][1], b)
    else:
        merged.append([a, b])

expr = "+".join(f"between(t,{a},{b})" for a, b in merged) or "0"
vf = f"select='not({expr})',setpts=N/FRAME_RATE/TB,fps=25"
out = DOCS / "demo.mp4"
subprocess.run([
    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-vf", vf, "-an",
    "-c:v", "libx264", "-preset", "slow", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
], check=True)

dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True).stdout.strip())
print(f"demo.mp4: {dur:.0f}s, {out.stat().st_size / 1e6:.1f} MB (raw {meta['duration']:.0f}s, cut {len(merged)} waits)")

# GIF tour from key screenshots
frames = ["01-pharmacist-row-cap", "05-admin-discount-preview", "06-typed-count-confirm", "07-executed-and-audited",
          "08-access-change-preview", "10-cross-tenant-refusal", "03-injection-as-data"]
lst = DOCS / "_gif.txt"
lst.write_text("".join(f"file '{(IMG / f'{f}.png').as_posix()}'\nduration 2.6\n" for f in frames)
               + f"file '{(IMG / f'{frames[-1]}.png').as_posix()}'\n")
gif = DOCS / "demo-tour.gif"
subprocess.run([
    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
    "-vf", "fps=10,scale=1000:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
    "-loop", "0", str(gif),
], check=True)
lst.unlink()
print(f"demo-tour.gif: {gif.stat().st_size / 1e6:.1f} MB")

# poster frame for the README video link
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "3", "-i", str(out), "-frames:v", "1",
                str(IMG / "demo-poster.png")], check=True)
sys.exit(0)
