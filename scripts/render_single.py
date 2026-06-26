# scripts/render_single.py
# ═══════════════════════════════════════════════════════
# ULTIMATE VERSION — All optimizations applied
# Fix 1: numpy<2.0.0 (moviepy 1.0.3 compatibility)
# Fix 2: GPU auto-detect for Whisper
# Fix 3: os.cpu_count() for threads
# Fix 4: wait_for_selector instead of fixed timeout
# Fix 5: RAM pipe instead of disk I/O for frames
# Fix 6: Capital letter fix for images/audio
# Fix 7: Zero brand leak — secrets only
# ═══════════════════════════════════════════════════════

import os
import gc
import re
import io
import json
import time
import shutil
import subprocess

import numpy as np
import whisper
import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

from moviepy.editor import (
    AudioFileClip,
    CompositeAudioClip,
    ImageClip,
    CompositeVideoClip,
)
from moviepy.audio.fx.all import audio_loop
from playwright.sync_api import sync_playwright

# ═══════════════════════════════════════════════════════
# ENV VARS
# ═══════════════════════════════════════════════════════
FOLDER_NAME    = os.environ.get('FOLDER_NAME',    '1')
VIDEO_FORMAT   = os.environ.get('VIDEO_FORMAT',   'youtube')
BRANDING_ALIAS = os.environ.get('BRANDING_ALIAS', 'none')
FPS            = int(os.environ.get('FPS_INPUT',     '24'))
JPEG_QUALITY   = int(os.environ.get('QUALITY_INPUT', '85'))
MUSIC_VOLUME   = 0.12

# ═══════════════════════════════════════════════════════
# SECRETS → REAL VALUES
# ═══════════════════════════════════════════════════════
ALIAS_MAP = {
    'BRAND_A': os.environ.get('SECRET_BRAND_A', ''),
    'BRAND_B': os.environ.get('SECRET_BRAND_B', ''),
    'none'   : 'none',
}
BRANDING    = ALIAS_MAP.get(BRANDING_ALIAS, 'none')
BRAND_UPPER = BRANDING.upper()

TYPOS_STR = os.environ.get(f'SECRET_{BRANDING_ALIAS}_TYPOS', '')
WORD_FIXES = {}
if BRAND_UPPER and TYPOS_STR:
    for typo in TYPOS_STR.split(','):
        clean = typo.strip().upper()
        if clean:
            WORD_FIXES[clean] = BRAND_UPPER

# ═══════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════
REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEOS_PATH = os.path.join(REPO_ROOT, 'videos')
ASSETS_PATH = os.path.join(REPO_ROOT, 'assets')
OUTPUT_PATH = os.path.join(REPO_ROOT, 'output')
FOLDER_PATH = os.path.join(VIDEOS_PATH, FOLDER_NAME)
MUSIC_PATH  = os.path.join(ASSETS_PATH, 'music.mp3')

# ═══════════════════════════════════════════════════════
# BRANDING → LOGO + TEMPLATE
# ═══════════════════════════════════════════════════════
if BRANDING_ALIAS != 'none':
    alias_lower = BRANDING_ALIAS.lower()
    logo_path   = os.path.join(ASSETS_PATH, f'logo_{alias_lower}.png')
    html_path   = os.path.join(ASSETS_PATH, f'template_{alias_lower}.html')
else:
    alias_lower = 'none'
    logo_path   = None
    html_path   = os.path.join(ASSETS_PATH, 'template_default.html')

# ═══════════════════════════════════════════════════════
# FORMAT
# ═══════════════════════════════════════════════════════
FORMATS = {
    'youtube':  {'w': 1920, 'h': 1080, 'class': 'fmt-16-9'},
    'shorts':   {'w': 1080, 'h': 1920, 'class': 'fmt-9-16'},
    'facebook': {'w': 1080, 'h': 1350, 'class': 'fmt-4-5'},
}
config = FORMATS.get(VIDEO_FORMAT, FORMATS['youtube'])

# ── Fix 3: Auto CPU count ─────────────────────────────
CPU_THREADS = os.cpu_count() or 2

# ── Fix 2: Auto GPU detect ───────────────────────────
try:
    import torch
    WHISPER_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
except ImportError:
    WHISPER_DEVICE = "cpu"

# ═══════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════
def natural_sort_key(s):
    return [
        int(t) if t.isdigit() else t.lower()
        for t in re.split(r'([0-9]+)', s)
    ]

def get_images(folder):
    # Capital letter fix — .JPG .PNG .WEBP all work
    exts = ('.png', '.jpg', '.jpeg', '.webp')
    imgs = [
        f for f in os.listdir(folder)
        if f.lower().endswith(exts)
        and not f.startswith('.')
    ]
    imgs.sort(key=natural_sort_key)
    return imgs

def get_audio(folder):
    # Capital letter fix — .MP3 works
    mp3_files = sorted([
        f for f in os.listdir(folder)
        if f.lower().endswith('.mp3')
    ])
    return mp3_files

def fmt_dur(sec):
    sec = int(sec)
    m, s = divmod(sec, 60)
    return f"{m}m {s}s" if m else f"{s}s"

def fmt_time(sec):
    return time.strftime('%H:%M:%S', time.gmtime(sec))

# ═══════════════════════════════════════════════════════
# START
# ═══════════════════════════════════════════════════════
start_time = time.time()

print(f"\n{'='*55}")
print(f"  Folder  : {FOLDER_NAME}")
print(f"  Format  : {VIDEO_FORMAT} {config['w']}x{config['h']}")
print(f"  Alias   : {BRANDING_ALIAS}")
print(f"  FPS     : {FPS}")
print(f"  Quality : {JPEG_QUALITY}")
print(f"  Threads : {CPU_THREADS}")
print(f"  Device  : {WHISPER_DEVICE}")
print(f"  Typos   : {len(WORD_FIXES)} words")
print(f"{'='*55}\n")

os.makedirs(OUTPUT_PATH, exist_ok=True)

# ── Validate ──────────────────────────────────────────
mp3_files = get_audio(FOLDER_PATH)
if not mp3_files:
    raise SystemExit(f"ERROR: No .mp3/.MP3 in {FOLDER_PATH}")

audio_path  = os.path.join(FOLDER_PATH, mp3_files[0])
output_file = os.path.join(OUTPUT_PATH, f"{FOLDER_NAME}.mp4")
images      = get_images(FOLDER_PATH)

if not images:
    raise SystemExit(f"ERROR: No images in {FOLDER_PATH}")
if not os.path.exists(html_path):
    raise SystemExit(f"ERROR: Template missing: {html_path}")

print(f"  Audio   : {mp3_files[0]}")
print(f"  Images  : {len(images)} files")
print(f"  Sample  : {images[:3]}")
print(f"  HTML    : {os.path.basename(html_path)}")

# ═══════════════════════════════════════════════════════
# PHASE 1 — TRANSCRIBE
# Fix 2: GPU auto-detect
# ═══════════════════════════════════════════════════════
print(f"\n[1/4] Transcribing ({WHISPER_DEVICE})...")
t1 = time.time()

model  = whisper.load_model("small", device=WHISPER_DEVICE)
result = model.transcribe(
    audio_path,
    word_timestamps=True,
    language='en',
    temperature=0.0,
)

segments = []
for si, seg in enumerate(result['segments']):
    words = []
    for w in seg.get('words', []):
        raw   = w['word'].strip()
        check = raw.upper().replace('.', '').replace(',', '').strip()
        words.append({
            'word' : WORD_FIXES.get(check, raw),
            'start': round(w['start'], 3),
            'end'  : round(w['end'],   3),
        })
    if words:
        segments.append({
            'id'   : si,
            'start': round(seg['start'], 3),
            'end'  : round(seg['end'],   3),
            'words': words,
        })

total_words = sum(len(s['words']) for s in segments)
print(f"  Done: {total_words} words | {fmt_time(time.time()-t1)}")
del model
gc.collect()

# ═══════════════════════════════════════════════════════
# PHASE 2 — DURATION
# ═══════════════════════════════════════════════════════
vc       = AudioFileClip(audio_path)
duration = vc.duration
vc.close()

raw_dur          = duration / len(images)
slide_dur        = 4.5 if raw_dur >= 3.0 else max(2.5, raw_dur)
frames_per_slide = int(slide_dur * FPS)
total_frames     = int(duration * FPS)

print(f"\n  Duration : {fmt_dur(duration)}")
print(f"  Frames   : {total_frames}")
print(f"  Estimate : ~{fmt_dur(total_frames * 0.15)} (RAM pipe mode)")

# ═══════════════════════════════════════════════════════
# PHASE 3 — PLAYWRIGHT + FFMPEG PIPE
# Fix 5: RAM pipe — no disk I/O for frames
# Fix 4: wait_for_selector instead of fixed timeout
# ═══════════════════════════════════════════════════════
print(f"\n[2/4] Rendering {total_frames} frames → FFmpeg pipe...")
t2 = time.time()

W = config['w']
H = config['h']

# ── FFmpeg process — reads raw frames from stdin ──────
# rawvideo = no encoding overhead per frame
# yz_sender streams directly to mp4
ffmpeg_cmd = [
    'ffmpeg', '-y',
    '-f', 'rawvideo',
    '-vcodec', 'rawvideo',
    '-s', f'{W}x{H}',
    '-pix_fmt', 'rgb24',
    '-r', str(FPS),
    '-i', 'pipe:0',          # stdin = frame stream
    '-i', audio_path,         # audio input
    '-c:v', 'libx264',
    '-preset', 'ultrafast',
    '-crf', '23',
    '-pix_fmt', 'yuv420p',
    '-c:a', 'aac',
    '-b:a', '192k',
    '-movflags', '+faststart',
    '-threads', str(CPU_THREADS),
    '-shortest',
    output_file,
]

ffmpeg_proc = subprocess.Popen(
    ffmpeg_cmd,
    stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

# ── Add background music if exists ───────────────────
# Note: music mixing done via FFmpeg filter
# for pipe mode — simpler than MoviePy mixing

MAX_FRAME_RETRY = 3
log_every       = max(1, total_frames // 20)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--disable-dev-shm-usage',
                '--no-sandbox',
                '--disable-gpu',
                '--disable-web-security',
                '--font-render-hinting=none',
            ]
        )
        try:
            ctx  = browser.new_context(
                viewport={'width': W, 'height': H},
                device_scale_factor=1,
            )
            page = ctx.new_page()
            page.goto(f"file://{html_path}", wait_until='networkidle')
            page.evaluate(f"document.body.className = '{config['class']}'")
            page.evaluate(f"setDuration({duration})")

            for i, img in enumerate(images):
                fp = os.path.join(FOLDER_PATH, img).replace('\\', '/')
                page.evaluate(f"""(function() {{
                    var bg = document.createElement('img');
                    bg.src = 'file://{fp}';
                    bg.className = 'bg-img';
                    document.getElementById('bg-slider').appendChild(bg);
                    var sl = document.createElement('img');
                    sl.src = 'file://{fp}';
                    sl.className = 'slide-img';
                    sl.dataset.index = '{i}';
                    document.getElementById('slider').appendChild(sl);
                }})();""")

            page.evaluate(f"loadCaptions({json.dumps(segments)})")

            # Fix 4: wait_for_selector — not fixed timeout
            # Wait until first slide image is visible in DOM
            page.wait_for_selector(
                '.slide-img[data-index="0"]',
                state='attached',
                timeout=30000,
            )
            # Small buffer for CSS animations to settle
            page.wait_for_timeout(500)

            cur_slide = -1

            for i in range(total_frames):
                t     = i / FPS
                slide = i // frames_per_slide
                idx   = slide % len(images)

                if slide != cur_slide:
                    cur_slide = slide
                    page.evaluate(f"showSlide({idx})")

                page.evaluate(f"updateTime({t:.4f})")

                # Fix 5: screenshot to bytes (RAM) not disk
                for attempt in range(MAX_FRAME_RETRY):
                    try:
                        # Returns PNG bytes in memory
                        img_bytes = page.screenshot(
                            type='png',
                            clip={
                                'x': 0, 'y': 0,
                                'width':  W,
                                'height': H,
                            },
                            full_page=False,
                        )
                        break
                    except Exception as e:
                        if attempt == MAX_FRAME_RETRY - 1:
                            raise RuntimeError(
                                f"Frame {i} failed: {e}"
                            )
                        time.sleep(0.3)

                # Convert PNG bytes → RGB numpy → pipe to FFmpeg
                pil_img  = PIL.Image.open(io.BytesIO(img_bytes)).convert('RGB')
                rgb_data = np.array(pil_img, dtype=np.uint8).tobytes()
                ffmpeg_proc.stdin.write(rgb_data)

                if (i + 1) % log_every == 0:
                    elapsed = time.time() - t2
                    pct     = (i + 1) / total_frames
                    eta     = (elapsed / pct) - elapsed
                    print(
                        f"  {pct*100:5.1f}% "
                        f"({i+1}/{total_frames}) "
                        f"ETA: {fmt_time(eta)}"
                    )

        finally:
            browser.close()

    # Close FFmpeg stdin → triggers encoding finish
    ffmpeg_proc.stdin.close()
    ffmpeg_proc.wait()

    if ffmpeg_proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {ffmpeg_proc.returncode}")

    print(f"  Done: {total_frames} frames | {fmt_time(time.time()-t2)}")

except Exception as e:
    ffmpeg_proc.stdin.close()
    ffmpeg_proc.kill()
    raise e

# ═══════════════════════════════════════════════════════
# PHASE 4 — ADD MUSIC + LOGO (if needed)
# Only runs if music.mp3 or logo exists
# ═══════════════════════════════════════════════════════
needs_postprocess = (
    os.path.exists(MUSIC_PATH) or
    (logo_path and os.path.exists(logo_path))
)

if needs_postprocess:
    print(f"\n[3/4] Post-processing (music/logo)...")
    t3 = time.time()

    temp_output = output_file.replace('.mp4', '_temp.mp4')
    os.rename(output_file, temp_output)

    from moviepy.editor import VideoFileClip, ImageSequenceClip

    clip  = VideoFileClip(temp_output)
    audio = clip.audio

    if os.path.exists(MUSIC_PATH):
        voice = AudioFileClip(audio_path)
        bg    = AudioFileClip(MUSIC_PATH)
        bg    = audio_loop(bg, duration=duration + 2) \
                if bg.duration < duration \
                else bg.subclip(0, duration)
        audio = CompositeAudioClip([bg.volumex(MUSIC_VOLUME), voice])
        print(f"  Music mixed")

    if logo_path and os.path.exists(logo_path):
        logo_h = int(config['h'] * 0.09)
        logo   = (
            ImageClip(logo_path)
            .set_duration(duration)
            .resize(height=logo_h)
            .margin(right=22, top=22, opacity=0)
            .set_pos(('right', 'top'))
        )
        clip = CompositeVideoClip([clip, logo])
        print(f"  Logo added: {alias_lower}")

    clip.set_audio(audio).write_videofile(
        output_file,
        codec="libx264",
        audio_codec="aac",
        bitrate="4000k",
        threads=CPU_THREADS,
        logger=None,
        preset="ultrafast",
        ffmpeg_params=[
            "-movflags", "+faststart",
            "-pix_fmt",  "yuv420p",
        ],
    )

    clip.close()
    os.remove(temp_output)
    gc.collect()
    print(f"  Done | {fmt_time(time.time()-t3)}")

else:
    print(f"\n[3/4] No music/logo — skipping post-process")

# ═══════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════
total_time = time.time() - start_time
size_mb    = os.path.getsize(output_file) / (1024 * 1024)

print(f"\n{'='*55}")
print(f"  COMPLETE!")
print(f"  File     : {FOLDER_NAME}.mp4")
print(f"  Size     : {size_mb:.1f} MB")
print(f"  Duration : {fmt_dur(duration)}")
print(f"  Time     : {fmt_time(total_time)}")
print(f"  Speed    : {duration/total_time:.1f}x realtime")
print(f"{'='*55}\n")