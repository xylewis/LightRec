"""FFmpeg discovery, encoder probing, and command construction."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def find_ffmpeg() -> str:
    if getattr(sys, "frozen", False):
        extracted = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / "ffmpeg.exe"
        if extracted.is_file():
            return str(extracted)
        app_dir = Path(sys.executable).resolve().parent
    else:
        app_dir = Path(__file__).resolve().parent.parent
    bundled = app_dir / "ffmpeg.exe"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("ffmpeg") or ""


def ffmpeg_has_encoder(ffmpeg: str, name: str) -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0 and name in proc.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def probe_encoder(ffmpeg: str, encoder: str) -> bool:
    """Check that a compiled encoder can actually initialize on this PC."""
    try:
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=size=640x360:rate=1,format=nv12", "-frames:v", "1", "-an",
                "-c:v", encoder, "-f", "null", "NUL"]
        if encoder.endswith("_amf"):
            args[args.index("-c:v"):args.index("-c:v")] = ["-usage", "transcoding", "-quality", "quality"]
        elif encoder.endswith("_qsv"):
            args[args.index("-c:v"):args.index("-c:v")] = ["-preset", "veryfast"]
        elif encoder.endswith("_nvenc"):
            args[args.index("-c:v"):args.index("-c:v")] = ["-preset", "p4"]
        elif encoder.endswith("_mf"):
            args[args.index("-c:v"):args.index("-c:v")] = ["-hw_encoding", "1", "-rate_control", "cbr"]
        elif encoder == "libx264":
            args[args.index("-c:v"):args.index("-c:v")] = ["-preset", "veryfast"]
        elif encoder == "libx265":
            args[args.index("-c:v"):args.index("-c:v")] = ["-preset", "ultrafast"]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def choose_encoder(ffmpeg: str, hevc: bool, after: str | None = None) -> str | None:
    candidates = ("hevc_amf", "hevc_qsv", "hevc_nvenc", "hevc_mf", "libx265") if hevc else (
        "h264_amf", "h264_qsv", "h264_nvenc", "h264_mf", "libx264")
    if after in candidates:
        candidates = candidates[candidates.index(after) + 1:]
    return next((name for name in candidates
                 if ffmpeg_has_encoder(ffmpeg, name) and probe_encoder(ffmpeg, name)), None)


def encoder_label(encoder: str | None) -> str:
    if not encoder:
        return "未知编码器"
    backend = ("AMD AMF" if encoder.endswith("_amf") else
               "Intel QSV" if encoder.endswith("_qsv") else
               "NVIDIA NVENC" if encoder.endswith("_nvenc") else
               "Windows MF" if encoder.endswith("_mf") else "CPU")
    codec = "HEVC" if encoder.startswith(("hevc_", "libx265")) else "H.264"
    return f"{backend} {codec}"


def video_bitrate_for(total_bitrate: int, audio_bitrate: int) -> int:
    return max(200, total_bitrate - audio_bitrate)


def ffmpeg_has_filter(ffmpeg: str, name: str) -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0 and name in proc.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def build_video_args(*, ffmpeg: str, encoder: str, fps: int, video_bitrate: int,
                     duplicate_frames: int, display: int,
                     region: tuple[int, int, int, int] | None,
                     output_path: Path, direct: bool) -> list[str]:
    args = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning"]
    if direct:
        args += ["-init_hw_device", "d3d11va=dev:0", "-filter_hw_device", "dev"]
        if encoder.endswith("_amf"):
            args += ["-init_hw_device", "amf=amf@dev"]
        elif encoder.endswith("_qsv"):
            args += ["-init_hw_device", "qsv=qsv@dev"]
    capture_options = [f"output_idx={display}", f"framerate={fps}",
                       "draw_mouse=1", f"dup_frames={duplicate_frames}"]
    if region:
        x, y, width, height = region
        capture_options.extend((f"video_size={width}x{height}", f"offset_x={x}", f"offset_y={y}"))
    args += ["-f", "lavfi", "-i", "ddagrab=" + ":".join(capture_options)]
    if direct and encoder == "h264_amf":
        args += ["-vf", "scale_d3d11=format=nv12"]
    elif not direct:
        pixel_format = "nv12" if encoder.endswith(("_qsv", "_nvenc", "_mf")) else "yuv420p"
        args += ["-vf", f"hwdownload,format=bgra,format={pixel_format}"]
    args += ["-an", "-c:v", encoder]
    if encoder.endswith("_amf"):
        args += ["-usage", "transcoding", "-quality", "quality", "-rc", "cbr",
                 "-minrate", f"{video_bitrate}k", "-enforce_hrd", "true", "-filler_data", "true"]
    elif encoder.endswith("_qsv"):
        args += ["-preset", "veryfast"]
    elif encoder.endswith("_nvenc"):
        args += ["-preset", "p4"]
    elif encoder.endswith("_mf"):
        args += ["-hw_encoding", "1", "-rate_control", "cbr"]
    elif encoder == "libx264":
        args += ["-preset", "veryfast"]
    elif encoder == "libx265":
        args += ["-preset", "ultrafast"]
    args += ["-b:v", f"{video_bitrate}k", "-maxrate", f"{video_bitrate}k",
             "-g", str(fps * 2), "-fps_mode", "vfr",
             "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
             "-f", "mp4", str(output_path)]
    return args
