"""ffmpeg-based audio normalization: any input -> 16 kHz mono FLAC, optional silence removal."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import AudioConfig

SAMPLE_RATE = 16000
_DURATION_RE = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")


class AudioError(Exception):
    """The input could not be decoded as audio."""


@dataclass
class PreparedAudio:
    path: Path
    input_ms: int | None  # duration of the uploaded audio, if ffmpeg could tell
    speech_ms: int  # duration after VAD, i.e. what is sent to ASR


def ffmpeg_version() -> str:
    """Return the ffmpeg version line, or raise RuntimeError if ffmpeg is unusable."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found on PATH (install it: apt install ffmpeg / brew install ffmpeg)")
    out = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg is not working: {out.stderr.strip()[:200]}")
    return out.stdout.splitlines()[0] if out.stdout else "ffmpeg"


def _vad_filter(cfg: AudioConfig) -> str:
    th = f"{cfg.silence_threshold_db}dB"
    return (
        "silenceremove="
        f"start_periods=1:start_threshold={th}:start_silence={cfg.keep_silence_s}:"
        f"stop_periods=-1:stop_duration={cfg.min_silence_s}:stop_threshold={th}:stop_silence={cfg.keep_silence_s}"
    )


def _flac_duration_ms(path: Path) -> int:
    """Read total samples / sample rate from the FLAC STREAMINFO block."""
    with path.open("rb") as f:
        head = f.read(42)
    if len(head) < 42 or head[:4] != b"fLaC":
        raise AudioError("ffmpeg produced an invalid FLAC file")
    packed = int.from_bytes(head[18:26], "big")
    rate = packed >> 44
    samples = packed & ((1 << 36) - 1)
    return int(samples * 1000 / rate) if rate else 0


def prepare(src: Path, dst: Path, cfg: AudioConfig) -> PreparedAudio:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-i", str(src), "-map", "0:a:0", "-vn",
           "-ac", "1", "-ar", str(SAMPLE_RATE)]
    if cfg.vad:
        cmd += ["-af", _vad_filter(cfg)]
    cmd += ["-c:a", "flac", "-f", "flac", str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.decode_timeout_s)
    except subprocess.TimeoutExpired as e:
        raise AudioError("audio decoding timed out") from e
    if proc.returncode != 0 or not dst.exists():
        lines = [l for l in proc.stderr.strip().splitlines() if l.strip()]
        raise AudioError("could not decode audio" + (f": {lines[-1][:200]}" if lines else ""))

    input_ms = None
    if m := _DURATION_RE.search(proc.stderr):
        h, mi, s = m.groups()
        input_ms = int((int(h) * 3600 + int(mi) * 60 + float(s)) * 1000)
    return PreparedAudio(path=dst, input_ms=input_ms, speech_ms=_flac_duration_ms(dst))
