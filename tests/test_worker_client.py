"""Тесты клиента воркера (чистые части, без сети).

Крупные передачи через бесплатный cloudflared идут stdlib-клиентом: aiohttp
на мегабайтном теле встаёт (скачивание) и рвёт соединение (загрузка). Тело
multipart при этом собирается потоком — бандлы шардинга бывают под сотню
мегабайт, держать их копию в памяти мастера незачем.
"""

import io
import sys
import types

# worker_client тянет aiohttp; в окружении портабла он есть, но тест не должен
# от этого зависеть — если нет, подставляем заглушку
try:
    import aiohttp  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")

from gpu_raid.worker_client import _MultipartStream  # noqa: E402

HEAD = b"--boundary\r\nheaders\r\n\r\n"
TAIL = b"\r\n--boundary--\r\n"


def _drain(stream, size):
    out = b""
    while True:
        chunk = stream.read(size)
        if not chunk:
            return out
        out += chunk


def test_stream_assembles_parts_in_order():
    body = b"x" * 1000
    got = _drain(_MultipartStream(HEAD, io.BytesIO(body), TAIL), 64)
    assert got == HEAD + body + TAIL


def test_stream_respects_requested_size():
    """urllib читает кусками: отдать больше запрошенного — испортить тело."""
    stream = _MultipartStream(HEAD, io.BytesIO(b"y" * 500), TAIL)
    first = stream.read(10)
    assert len(first) == 10 and first == HEAD[:10]
    rest = _drain(stream, 7)
    assert HEAD[10:] + b"y" * 500 + TAIL == rest


def test_stream_handles_empty_file():
    got = _drain(_MultipartStream(HEAD, io.BytesIO(b""), TAIL), 32)
    assert got == HEAD + TAIL


def test_stream_size_matches_content_length():
    """Content-Length считается по размеру файла — поток обязан совпасть,
    иначе запрос повиснет на недосланных байтах."""
    body = b"z" * 4096
    declared = len(HEAD) + len(body) + len(TAIL)
    assert len(_drain(_MultipartStream(HEAD, io.BytesIO(body), TAIL), 1024)) == declared
