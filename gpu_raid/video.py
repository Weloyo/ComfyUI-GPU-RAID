"""ffmpeg-утилиты для Long Video: пробы, извлечение кадров, склейка/монтаж.

ffmpeg ищется в imageio-ffmpeg (ставится вместе с VideoHelperSuite) или в PATH.
Все функции асинхронные (create_subprocess_exec на event loop).
"""

import asyncio
import logging
import os
import re
import shutil

log = logging.getLogger("gpu_raid")

_FFMPEG = None


def find_ffmpeg():
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    try:
        import imageio_ffmpeg

        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        return _FFMPEG
    except Exception:
        pass
    _FFMPEG = shutil.which("ffmpeg")
    return _FFMPEG


class FfmpegError(RuntimeError):
    pass


async def _run(args, timeout=1800):
    exe = find_ffmpeg()
    if not exe:
        raise FfmpegError(
            "ffmpeg не найден: установите imageio-ffmpeg "
            "(python_embeded\\python.exe -m pip install imageio-ffmpeg) или добавьте ffmpeg в PATH"
        )
    proc = await asyncio.create_subprocess_exec(
        exe, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise FfmpegError("ffmpeg: таймаут")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def probe(path):
    """{duration_s: float, fps: float} по stderr `ffmpeg -i`."""
    _, _, err = await _run(["-hide_banner", "-i", path], timeout=60)
    duration = 0.0
    fps = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", err)
    if m:
        fps = float(m.group(1))
    return {"duration_s": duration, "fps": fps}


async def extract_last_frame(src, dest_png):
    os.makedirs(os.path.dirname(dest_png), exist_ok=True)
    code, _, err = await _run(
        ["-y", "-sseof", "-0.5", "-i", src, "-frames:v", "1", "-update", "1", "-q:v", "2", dest_png],
        timeout=120,
    )
    if code != 0 or not os.path.exists(dest_png):
        # страховка: перемотка от начала на (duration - кадр)
        info = await probe(src)
        ss = max(0.0, info["duration_s"] - (1.0 / info["fps"] if info["fps"] else 0.1))
        code, _, err = await _run(
            ["-y", "-ss", f"{ss:.3f}", "-i", src, "-frames:v", "1", "-update", "1", "-q:v", "2", dest_png],
            timeout=120,
        )
        if code != 0 or not os.path.exists(dest_png):
            raise FfmpegError(f"не удалось извлечь последний кадр из {os.path.basename(src)}: {err[-400:]}")
    return dest_png


async def concat_copy(files, out_path):
    """Быстрая склейка без перекодирования (одинаковые кодек/параметры)."""
    list_path = out_path + ".list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            f.write("file '" + os.path.abspath(p).replace("\\", "/").replace("'", "'\\''") + "'\n")
    code, _, err = await _run(
        ["-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out_path],
        timeout=600,
    )
    try:
        os.remove(list_path)
    except OSError:
        pass
    if code != 0:
        raise FfmpegError(f"concat: {err[-400:]}")
    return out_path


async def render_edit(items, out_path, crossfade_s=0.0, fps=None):
    """Монтаж с тримами и кроссфейдом (перекодирование libx264).

    items: [{file, in_s?, out_s?}] в порядке воспроизведения. Аудио отбрасывается.
    """
    if not items:
        raise FfmpegError("нет сегментов для экспорта")
    durations = []
    for it in items:
        info = await probe(it["file"])
        start = float(it.get("in_s") or 0.0)
        end = float(it.get("out_s") or 0.0) or info["duration_s"]
        end = min(end, info["duration_s"]) if info["duration_s"] else end
        if end - start <= 0.05:
            raise FfmpegError(f"сегмент {os.path.basename(it['file'])}: пустой после трима")
        durations.append(end - start)
        if fps is None and info["fps"]:
            fps = info["fps"]
    fps = fps or 24.0

    args = ["-y"]
    for it in items:
        args += ["-i", it["file"]]

    chains = []
    for i, it in enumerate(items):
        start = float(it.get("in_s") or 0.0)
        end = start + durations[i]
        chains.append(
            f"[{i}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,fps={fps:g}[v{i}]"
        )

    n = len(items)
    if n == 1:
        chains.append("[v0]null[vout]")
    elif crossfade_s and crossfade_s > 0.01:
        fade = float(crossfade_s)
        prev = "v0"
        offset = durations[0] - fade
        for i in range(1, n):
            label = "vout" if i == n - 1 else f"x{i}"
            chains.append(
                f"[{prev}][v{i}]xfade=transition=fade:duration={fade:.3f}:offset={max(0.0, offset):.3f}[{label}]"
            )
            prev = label
            offset += durations[i] - fade
    else:
        chains.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vout]")

    args += [
        "-filter_complex", ";".join(chains),
        "-map", "[vout]",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
        out_path,
    ]
    code, _, err = await _run(args, timeout=3600)
    if code != 0:
        raise FfmpegError(f"export: {err[-500:]}")
    return out_path
