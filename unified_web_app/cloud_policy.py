"""Cloud-drive policy. Limits are bytes; Flask config overrides environment.

CLOUD_USER_QUOTA_BYTES defaults to 256 MiB, CLOUD_SITE_QUOTA_BYTES to 2 GiB,
and CLOUD_MAX_FILE_BYTES to 16 MiB (also capped by MAX_CONTENT_LENGTH).
The process lock covers uploads through commit for the one-worker deployment.
Multiple worker processes would require a shared lock/quota reservation system.
"""

import os
import unicodedata
from pathlib import Path
from threading import Lock


UPLOAD_LOCK = Lock()
ALLOWED_EXTENSIONS = frozenset(
    "pdf doc docx xls xlsx csv tsv ppt pptx odt ods odp rtf "
    "png jpg jpeg gif bmp webp tif tiff ico "
    "zip 7z rar tar gz tgz bz2 xz "
    "py c cpp cc cxx h hpp ino rs go java json yaml yml toml ini cfg md txt log "
    "step stp stl iges igs dxf dwg obj mtl 3mf f3d f3z fcstd blend "
    "kicad_sch kicad_pcb kicad_pro kicad_mod kicad_sym sch pcb brd "
    "gbr gerber drl asc net cir spice lib bin hex uf2 elf axf hd".split()
)


class CloudUploadError(ValueError):
    """A safe, user-facing upload rejection."""


def display_filename(value):
    # Browser filenames may use either OS's path separators. Never use this
    # display name on disk. Remove control/format characters including bidi.
    name = unicodedata.normalize("NFC", value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C")).strip()
    if not name or name in {".", ".."} or len(name) > 255 or len(name.encode("utf-8")) > 255:
        raise CloudUploadError("文件名无效或过长，请缩短后重试")
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix not in ALLOWED_EXTENSIONS:
        raise CloudUploadError("不支持此文件格式，请选择下方支持的项目资料格式；不接受 HTML、SVG、EXE、BAT、PS1、JS 等活动内容")
    return name, suffix


def _limit(config, name, default):
    value = config.get(name, os.environ.get(name, default))
    try:
        if isinstance(value, bool):
            raise ValueError
        result = int(value)
        if str(result) != str(value).strip() or result < 0:
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a non-negative integer number of bytes") from None


def limits(config):
    user = _limit(config, "CLOUD_USER_QUOTA_BYTES", 256 * 1024**2)
    site = _limit(config, "CLOUD_SITE_QUOTA_BYTES", 2 * 1024**3)
    single = _limit(config, "CLOUD_MAX_FILE_BYTES", 16 * 1024**2)
    if config.get("MAX_CONTENT_LENGTH") is not None:
        single = min(single, int(config["MAX_CONTENT_LENGTH"]))
    return user, site, single


def readable_size(size):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:g} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024


def check_capacity(size, user_used, site_used, quota):
    user_limit, site_limit, file_limit = quota
    if size > file_limit:
        raise CloudUploadError(f"文件过大：单文件上限 {readable_size(file_limit)}")
    if user_used + size > user_limit:
        raise CloudUploadError(f"个人云盘空间不足：已用 {readable_size(user_used)}，配额 {readable_size(user_limit)}")
    if site_used + size > site_limit:
        raise CloudUploadError(f"全站云盘空间不足：已用 {readable_size(site_used)}，总配额 {readable_size(site_limit)}，请联系管理员")
