# scripts/render_single.py
# ═══════════════════════════════════════════════════════
# FINAL PERFECT VERSION
# Tweak 1: escape_ffmpeg_text ခိုင်မာအောင် ပြင်ပြီး
# Tweak 2: WORDS_PER_LINE safe range (4/6) သတ်မှတ်ပြီး
# Fail-safe 1: FFmpeg crash detect + stderr print
# Fail-safe 2: -filter_script:v သုံး (OS limit safe)
# ═══════════════════════════════════════════════════════

import os
import gc
import re
import time
import signal
import shutil
import subprocess
import tempfile

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
# SECRETS
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
# BRANDING
# ═══════════════════════════════════════════════════════
if BRANDING_ALIAS != 'none':
    alias_lower = BRANDING_ALIAS.lower()
    logo_path   = os.path.join(ASSETS_PATH, f'logo_{alias_lower}.png')
else:
    alias_lower = 'none'
    logo_path   = None

# ═══════════════════════════════════════════════════════
# FORMAT
# ═══════════════════════════════════════════════════════
FORMATS = {
    'youtube':  {'w': 1920, 'h': 1080},
    'shorts':   {'w': 1080, 'h': 1920},
    'facebook': {'w': 1080, 'h': 1350},
}
config      = FORMATS.get(VIDEO_FORMAT, FORMATS['youtube'])
W           = config['w']
H           = config['h']
CPU_THREADS = os.cpu_count() or 2

# GPU detect
try:
    import torch
    WHISPER_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
except ImportError:
    WHISPER_DEVICE = "cpu"

# ═══════════════════════════════════════════════════════
# CAPTION STYLE CONFIG
# ═══════════════════════════════════════════════════════
# Font path — GitHub Actions Ubuntu တွင် ရှိသော font
FONT_PATH = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
if not os.path.exists(FONT_PATH):
    FONT_PATH = "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"
if not os.path.exists(FONT_PATH):
    FONT_PATH = None

# Caption Y position
CAPTION_Y = int(H * 0.78)

# Font sizes by format
FONT_SIZES = {
    'youtube':  72,
    'shorts':   80,
    'facebook': 68,
}
FONT_SIZE = FONT_SIZES.get(VIDEO_FORMAT, 72)

# ── Tweak 2: WORDS_PER_LINE safe range ────────────────
# shorts=4, others=6
# ⚠️ WARNING: ဘယ်တော့မှ 8 ထက်မပိုရ
# 10+ → FFmpeg filter string crash
WORDS_PER_LINE = 4 if VIDEO_FORMAT == 'shorts' else 6

# ═══════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════
def natural_sort_key(s):
    return [
        int(t) if t.isdigit() else t.lower()
        for t in re.split(r'([0-9]+)', s)
    ]

def get_images(folder):
    exts = ('.png', '.jpg', '.jpeg', '.webp')
    imgs = [
        f for f in os.listdir(folder)
        if f.lower().endswith(exts)
        and not f.startswith('.')
    ]
    imgs.sort(key=natural_sort_key)
    return imgs

def get_audio(folder):
    return sorted([
        f for f in os.listdir(folder)
        if f.lower().endswith('.mp3')
    ])

def fmt_dur(sec):
    sec = int(sec)
    m, s = divmod(sec, 60)
    return f"{m}m {s}s" if m else f"{s}s"

def fmt_time(sec):
    return time.strftime('%H:%M:%S', time.gmtime(sec))

def get_audio_duration(path):
    r = subprocess.run([
        'ffprobe', '-v', 'quiet',
        '-show_entries', 'format=duration',
        '-of', 'csv=p=0', path
    ], capture_output=True, text=True)
    return float(r.stdout.strip() or "0")

# ── Tweak 1: escape_ffmpeg_text ခိုင်မာအောင် ───────────
def escape_ffmpeg_text(text):
    """
    FFmpeg drawtext အတွက် special chars escape
    - Single quote → smart quote
    - Colon escape
    - ရှုပ်ထွေးသော သင်္ကေတများ ဖြုတ်ချ
    """
    if not text:
        return ""
    text = text.replace("'", "\u2019")
    text = text.replace(':', '\\:')
    text = re.sub(r'[\[\]{}|\\^~]', '', text)
    return text

# ═══════════════════════════════════════════════════════
# CAPTION FILTER BUILDER
# ═══════════════════════════════════════════════════════
def build_drawtext_filters(
    caption_lines, font_path, font_size, cap_y, W
):
    """
    Beautiful caption layers:
    Layer 1 — Dark shadow (depth effect)
    Layer 2 — White text + black border (readability)
    Layer 3 — Yellow highlight per word (sync)
    """
    filters  = []
    font_opt = f":fontfile='{font_path}'" if font_path else ""
    char_w   = font_size * 0.55  # approx px per char

    for line in caption_lines:
        text    = escape_ffmpeg_text(line['text'])
        t_start = line['start']
        t_end   = line['end']
        enable  = f"between(t,{t_start:.3f},{t_end:.3f})"

        # ── Layer 1: Shadow ────────────────────────────
        filters.append(
            f"drawtext=text='{text}'"
            f"{font_opt}"
            f":fontsize={font_size}"
            f":fontcolor=0x000000AA"
            f":x=(w-text_w)/2+4"
            f":y={cap_y}+4"
            f":enable='{enable}'"
        )

        # ── Layer 2: White text + border ───────────────
        filters.append(
            f"drawtext=text='{text}'"
            f"{font_opt}"
            f":fontsize={font_size}"
            f":fontcolor=white"
            f":borderw=4"
            f":bordercolor=black"
            f":x=(w-text_w)/2"
            f":y={cap_y}"
            f":enable='{enable}'"
        )

        # ── Layer 3: Per-word yellow highlight ─────────
        words        = line['words']
        full_text    = line['text']
        total_w_px   = len(full_text) * char_w
        prefix_chars = 0

        for word_info in words:
            w_text   = escape_ffmpeg_text(word_info['word'])
            w_start  = word_info['start']
            w_end    = word_info['end']
            w_enable = f"between(t,{w_start:.3f},{w_end:.3f})"
            word_x   = f"(w-{total_w_px:.0f})/2+{prefix_chars * char_w:.0f}"

            filters.append(
                f"drawtext=text='{w_text}'"
                f"{font_opt}"
                f":fontsize={font_size}"
                f":fontcolor=yellow"
                f":borderw=4"
                f":bordercolor=0x00000088"
                f":x={word_x}"
                f":y={cap_y}"
                f":enable='{w_enable}'"
            )

            prefix_chars += len(word_info['word']) + 1

    return ','.join(filters)

# ═══════════════════════════════════════════════════════
# START
# ═══════════════════════════════════════════════════════
start_time = time.time()

print(f"\n{'='*55}")
print(f"  Folder      : {FOLDER_NAME}")
print(f"  Format      : {VIDEO_FORMAT} {W}x{H}")
print(f"  Alias       : {BRANDING_ALIAS}")
print(f"  FPS         : {FPS}")
print(f"  Threads     : {CPU_THREADS}")
print(f"  Device      : {WHISPER_DEVICE}")
print(f"  Font        : {FONT_PATH or 'system default'}")
print(f"  Words/line  : {WORDS_PER_LINE}  ← safe (max 6)")
print(f"{'='*55}\n")

os.makedirs(OUTPUT_PATH, exist_ok=True)

# ── Validate ──────────────────────────────────────────
mp3_files = get_audio(FOLDER_PATH)
if not mp3_files:
    raise SystemExit(f"ERROR: No mp3 in {FOLDER_PATH}")

audio_path  = os.path.join(FOLDER_PATH, mp3_files[0])
output_file = os.path.join(OUTPUT_PATH, f"{FOLDER_NAME}.mp4")
images      = get_images(FOLDER_PATH)

if not images:
    raise SystemExit(f"ERROR: No images in {FOLDER_PATH}")

print(f"  Audio   : {mp3_files[0]}")
print(f"  Images  : {len(images)} → {images[:3]}")

# ═══════════════════════════════════════════════════════
# PHASE 1 — DURATION
# ═══════════════════════════════════════════════════════
duration  = get_audio_duration(audio_path)
slide_dur = duration / len(images)

print(f"\n  Duration  : {fmt_dur(duration)}")
print(f"  Slide dur : {slide_dur:.2f}s × {len(images)}")

# ═══════════════════════════════════════════════════════
# PHASE 2 — WHISPER
# ═══════════════════════════════════════════════════════
print(f"\n[1/4] Transcribing...")
t1 = time.time()

timeout_sec = max(300, min(900, int(duration * 3)))
print(f"  Model   : tiny | Device: {WHISPER_DEVICE}")
print(f"  Timeout : {fmt_dur(timeout_sec)}")

def _timeout_handler(signum, frame):
    raise TimeoutError(f"Whisper timeout {fmt_dur(timeout_sec)}")

signal.signal(signal.SIGALRM, _timeout_handler)
signal.alarm(timeout_sec)

try:
    model  = whisper.load_model("tiny", device=WHISPER_DEVICE)
    print(f"  Model loaded | {fmt_time(time.time()-t1)}")

    result = model.transcribe(
        audio_path,
        word_timestamps=True,
        language='en',
        temperature=0.0,
        verbose=False,
        fp16=False,
        condition_on_previous_text=False,
    )
    print(f"  Transcribed  | {fmt_time(time.time()-t1)}")

finally:
    signal.alarm(0)

# ── Word list ──────────────────────────────────────────
all_words = []
for seg in result['segments']:
    for w in seg.get('words', []):
        raw = w['word'].strip()
        if not raw:
            continue
        check = raw.upper().replace('.','').replace(',','').strip()
        all_words.append({
            'word' : WORD_FIXES.get(check, raw),
            'start': round(w['start'], 3),
            'end'  : round(w['end'],   3),
        })

print(f"  Words   : {len(all_words)} | {fmt_time(time.time()-t1)}")
del model
gc.collect()

# ═══════════════════════════════════════════════════════
# PHASE 3 — BUILD CAPTION LINES + FILTER FILE
# ═══════════════════════════════════════════════════════
print(f"\n[2/4] Building captions...")

caption_lines = []
i = 0
while i < len(all_words):
    chunk = all_words[i : i + WORDS_PER_LINE]
    caption_lines.append({
        'text' : ' '.join(w['word'] for w in chunk),
        'start': chunk[0]['start'],
        'end'  : chunk[-1]['end'],
        'words': chunk,
    })
    i += WORDS_PER_LINE

print(f"  Lines   : {len(caption_lines)}")

caption_filter = build_drawtext_filters(
    caption_lines, FONT_PATH, FONT_SIZE, CAPTION_Y, W
)

filter_size_kb = len(caption_filter) / 1024
print(f"  Filter  : {filter_size_kb:.1f} KB")

if filter_size_kb > 500:
    print(f"  ⚠️  Filter > 500KB — reduce WORDS_PER_LINE if crash")

# ═══════════════════════════════════════════════════════
# PHASE 4 — FFMPEG RENDER
# ═══════════════════════════════════════════════════════
print(f"\n[3/4] FFmpeg rendering...")
t3 = time.time()

tmp_dir = tempfile.mkdtemp(prefix="render_")

try:
    # ── Resize images ──────────────────────────────────
    print(f"  Resizing {len(images)} images...")
    resized_paths = []

    for idx, img_name in enumerate(images):
        src = os.path.join(FOLDER_PATH, img_name)
        dst = os.path.join(tmp_dir, f"slide_{idx:04d}.jpg")

        subprocess.run([
            'ffmpeg', '-y', '-i', src,
            '-vf', (
                f'scale={W}:{H}:'
                f'force_original_aspect_ratio=decrease,'
                f'pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black'
            ),
            '-q:v', str(max(2, 10 - JPEG_QUALITY // 10)),
            dst,
        ], check=True,
           stdout=subprocess.DEVNULL,
           stderr=subprocess.DEVNULL)

        resized_paths.append(dst)

        if (idx + 1) % 5 == 0 or idx == len(images) - 1:
            print(f"  Resized {idx+1}/{len(images)}")

    # ── Concat file ────────────────────────────────────
    concat_file = os.path.join(tmp_dir, "concat.txt")
    with open(concat_file, 'w') as f:
        for idx, rp in enumerate(resized_paths):
            if idx == len(resized_paths) - 1:
                dur = max(duration - (slide_dur * idx), 0.5)
            else:
                dur = slide_dur
            f.write(f"file '{rp}'\n")
            f.write(f"duration {dur:.4f}\n")
        f.write(f"file '{resized_paths[-1]}'\n")

    # ── Fail-safe 2: Filter → file ─────────────────────
    # -filter_script:v သုံး — OS ARG_MAX limit safe
    filter_file = os.path.join(tmp_dir, "caption.filter")
    with open(filter_file, 'w', encoding='utf-8') as f:
        f.write(caption_filter)

    print(f"  Filter file : {filter_size_kb:.1f} KB → {filter_file}")

    # ── FFmpeg command ─────────────────────────────────
    print(f"  Encoding...")
    t_enc = time.time()

    ffmpeg_cmd = [
        'ffmpeg', '-y',
        '-f', 'concat',
        '-safe', '0',
        '-i', concat_file,
        '-i', audio_path,
        # ── Fail-safe 2: file မှ filter ဖတ် ──────────
        # -vf တိုက်ရိုက်မပေးဘဲ filter_script သုံး
        # OS command line limit လုံးဝ မကျော်တော့
        '-filter_script:v', filter_file,
        '-c:v', 'libx264',
        '-preset', 'ultrafast',
        '-crf', '23',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-movflags', '+faststart',
        '-threads', str(CPU_THREADS),
        '-r', str(FPS),
        '-shortest',
        output_file,
    ]

    # ── Fail-safe 1: FFmpeg crash detect ──────────────
    ffmpeg_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    print(f"  FFmpeg PID  : {ffmpeg_proc.pid}")
    ffmpeg_proc.wait()

    if ffmpeg_proc.returncode != 0:
        err = ffmpeg_proc.stderr.read().decode(errors='replace')
        print(f"\n  ❌ FFmpeg crashed! Code: {ffmpeg_proc.returncode}")
        print(f"  Error tail:\n{err[-3000:]}")
        raise RuntimeError(
            f"FFmpeg failed (code {ffmpeg_proc.returncode})"
        )

    print(f"  Encoded     : {fmt_time(time.time()-t_enc)}")
    print(f"  Total phase : {fmt_time(time.time()-t3)}")

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════
# PHASE 5 — MUSIC + LOGO
# ═══════════════════════════════════════════════════════
needs_postprocess = (
    os.path.exists(MUSIC_PATH) or
    (logo_path and os.path.exists(logo_path))
)

if needs_postprocess:
    print(f"\n[4/4] Post-processing...")
    t4 = time.time()

    temp_out = output_file.replace('.mp4', '_tmp.mp4')
    os.rename(output_file, temp_out)

    from moviepy.editor import VideoFileClip

    clip  = VideoFileClip(temp_out)
    audio = clip.audio

    if os.path.exists(MUSIC_PATH):
        voice = AudioFileClip(audio_path)
        bg    = AudioFileClip(MUSIC_PATH)
        bg    = (
            audio_loop(bg, duration=duration + 2)
            if bg.duration < duration
            else bg.subclip(0, duration)
        )
        audio = CompositeAudioClip(
            [bg.volumex(MUSIC_VOLUME), voice]
        )
        print(f"  Music mixed")

    if logo_path and os.path.exists(logo_path):
        logo_h = int(H * 0.09)
        logo   = (
            ImageClip(logo_path)
            .set_duration(duration)
            .resize(height=logo_h)
            .margin(right=22, top=22, opacity=0)
            .set_pos(('right', 'top'))
        )
        clip = CompositeVideoClip([clip, logo])
        print(f"  Logo added  : {alias_lower}")

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
    os.remove(temp_out)
    gc.collect()
    print(f"  Done        : {fmt_time(time.time()-t4)}")

else:
    print(f"\n[4/4] No music/logo — skipping")

# ═══════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════
total_time = time.time() - start_time
size_mb    = os.path.getsize(output_file) / (1024 * 1024)

print(f"\n{'='*55}")
print(f"  COMPLETE!")
print(f"  File      : {FOLDER_NAME}.mp4")
print(f"  Size      : {size_mb:.1f} MB")
print(f"  Duration  : {fmt_dur(duration)}")
print(f"  Time      : {fmt_time(total_time)}")
print(f"  Speed     : {duration/total_time:.1f}x realtime")
print(f"{'='*55}\n")