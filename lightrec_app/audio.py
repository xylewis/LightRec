"""WASAPI audio capture workers and COM lifetime helpers."""

from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import sys
import threading
import time


def initialize_audio_com() -> bool:
    """Initialize COM for the current WASAPI worker thread, if needed."""
    if os.name != "nt":
        return False
    ole32 = ctypes.OleDLL("ole32")
    co_initialize = ole32.CoInitializeEx
    co_initialize.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    co_initialize.restype = ctypes.c_long
    hr = co_initialize(None, 0)
    if hr >= 0:
        return True
    if hr == ctypes.c_long(0x80010106).value:
        return False
    raise OSError(f"COM 初始化失败 (0x{hr & 0xFFFFFFFF:08x})")


def uninitialize_audio_com(initialized: bool) -> None:
    if initialized and os.name == "nt":
        ctypes.OleDLL("ole32").CoUninitialize()


def capture_audio(encoder: subprocess.Popen, stop: threading.Event, started: threading.Event,
                  clock: list[float | None], errors: queue.Queue[str], source: str) -> None:
    """Capture selected Windows audio devices and feed stereo PCM to AAC."""
    com_initialized = False
    try:
        import numpy as np
        first_soundcard_import = os.name == "nt" and "soundcard.mediafoundation" not in sys.modules
        if not first_soundcard_import:
            com_initialized = initialize_audio_com()
        import soundcard as sc

        # SoundCard imports Media Foundation on this thread the first time.
        # Prevent its module-global COM token from being released on Tk shutdown.
        if first_soundcard_import:
            from soundcard import mediafoundation
            if getattr(mediafoundation._com, "com_loaded", False):
                mediafoundation._com.com_loaded = False

        recorders = []
        if source in ("电脑声音", "电脑声音 + 麦克风"):
            speaker = sc.default_speaker()
            if speaker is None:
                raise RuntimeError("Windows 没有默认扬声器设备。")
            loopback = sc.get_microphone(id=speaker.name, include_loopback=True)
            if loopback is None:
                raise RuntimeError("无法打开默认扬声器的系统声音回环输入。")
            recorders.append(loopback)
        if source in ("麦克风", "电脑声音 + 麦克风"):
            microphone = sc.default_microphone()
            if microphone is None:
                raise RuntimeError("Windows 没有默认麦克风设备。")
            recorders.append(microphone)
        if not recorders:
            raise RuntimeError("请先选择电脑声音、麦克风或两者作为声音来源。")
        if encoder.stdin is None:
            raise RuntimeError("The AAC encoder input pipe is unavailable.")

        chunks = [queue.Queue(maxsize=8) for _ in recorders]
        ready: queue.Queue[Exception | None] = queue.Queue()
        reader_threads = []

        def read_source(device, output: queue.Queue) -> None:
            initialized = False
            reader_com_initialized = False
            try:
                reader_com_initialized = initialize_audio_com()
                with device.recorder(samplerate=48_000, channels=2) as recorder:
                    ready.put(None)
                    initialized = True
                    while not stop.is_set():
                        samples = recorder.record(numframes=4_800)
                        while not stop.is_set():
                            try:
                                output.put(samples, timeout=0.2)
                                break
                            except queue.Full:
                                continue
            except Exception as exc:
                if not initialized:
                    ready.put(exc)
                else:
                    errors.put(str(exc))
            finally:
                uninitialize_audio_com(reader_com_initialized)

        for device, output in zip(recorders, chunks):
            reader = threading.Thread(target=read_source, args=(device, output), daemon=True)
            reader.start()
            reader_threads.append(reader)
        for _ in recorders:
            deadline = time.monotonic() + 5
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("等待声音设备启动超时。")
                try:
                    issue = ready.get(timeout=min(0.1, remaining))
                    break
                except queue.Empty:
                    continue
            else:
                return
            if issue:
                raise issue

        clock[0] = time.perf_counter()
        started.set()
        pending: list[object | None] = [None] * len(chunks)
        while not stop.is_set():
            for index, (source_chunks, reader) in enumerate(zip(chunks, reader_threads)):
                if pending[index] is not None:
                    continue
                try:
                    pending[index] = source_chunks.get(timeout=0.2)
                except queue.Empty:
                    if stop.is_set():
                        break
                    if not errors.empty():
                        raise RuntimeError(errors.get_nowait())
                    if not reader.is_alive():
                        raise RuntimeError("一个声音来源提前停止。")
                    break
            if stop.is_set() or any(samples is None for samples in pending):
                continue
            samples_list = pending
            pending = [None] * len(chunks)
            frames = min(len(samples) for samples in samples_list)
            samples = sum(samples[:frames] for samples in samples_list) / len(samples_list)
            pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2", copy=False)
            encoder.stdin.write(pcm.tobytes())
            encoder.stdin.flush()

    except ModuleNotFoundError as exc:
        errors.put(f"缺少声音组件 {exc.name}。请在 LightRec 文件夹运行：py -3 -m pip install -r requirements-audio.txt")
        started.set()
    except Exception as exc:
        errors.put(str(exc))
        started.set()
    finally:
        stop.set()
        for reader in locals().get("reader_threads", ()):
            if reader.is_alive():
                reader.join(timeout=1)
        if encoder.stdin and not encoder.stdin.closed:
            try:
                encoder.stdin.close()
            except OSError:
                pass
        uninitialize_audio_com(com_initialized)
