from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DEFAULT_STORAGE_DIR_NAME, IMAGES_WEB_PATH_PREFIX
from .models import NotificationRecord
from .store import NotificationStore

_LOGGER = logging.getLogger(__name__)

_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _images_root(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("www", DEFAULT_STORAGE_DIR_NAME))


async def _fetch_image(hass: HomeAssistant, entity_id: str) -> tuple[bytes, str] | None:
    domain = entity_id.split(".", 1)[0]
    try:
        if domain == "camera":
            from homeassistant.components import camera

            image = await camera.async_get_image(hass, entity_id)
        elif domain == "image":
            from homeassistant.components import image as image_component

            image = await image_component.async_get_image(hass, entity_id)
        else:
            _LOGGER.warning(
                "K93 ANS: image_entity '%s' is neither a camera nor an image entity, ignoring",
                entity_id,
            )
            return None
    except Exception:
        _LOGGER.exception("K93 ANS failed fetching image from %s", entity_id)
        return None
    return image.content, image.content_type


def _write_image(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


async def async_capture_entity_image(
    hass: HomeAssistant, record: NotificationRecord, entity_id: str
) -> None:
    fetched = await _fetch_image(hass, entity_id)
    if fetched is None:
        return
    content, content_type = fetched
    extension = _CONTENT_TYPE_EXTENSIONS.get(content_type, "jpg")

    timestamp = dt_util.utcnow().strftime("%Y%m%dT%H%M%S%f")
    relative_path = Path(record["channel"]) / f"{record['id']}_{timestamp}.{extension}"
    absolute_path = _images_root(hass) / relative_path

    await hass.async_add_executor_job(_write_image, absolute_path, content)

    record["image"] = f"{IMAGES_WEB_PATH_PREFIX}{DEFAULT_STORAGE_DIR_NAME}/{relative_path.as_posix()}"
    record["image_managed"] = True


def _local_url_to_path(hass: HomeAssistant, local_url: str) -> Path | None:
    prefix = f"{IMAGES_WEB_PATH_PREFIX}{DEFAULT_STORAGE_DIR_NAME}/"
    if not local_url.startswith(prefix):
        return None
    return _images_root(hass) / local_url[len(prefix) :]


def _delete_unreferenced(images_root: Path, referenced: set[Path]) -> int:
    if not images_root.exists():
        return 0
    removed = 0
    for path in images_root.rglob("*"):
        if path.is_file() and path not in referenced:
            try:
                path.unlink()
                removed += 1
            except OSError:
                _LOGGER.exception("K93 ANS failed deleting orphaned image %s", path)
    for channel_dir in images_root.iterdir():
        if channel_dir.is_dir():
            try:
                channel_dir.rmdir()
            except OSError:
                pass
    return removed


async def async_prune_orphaned_images(hass: HomeAssistant, store: NotificationStore) -> None:
    referenced: set[Path] = set()
    for record in store.async_list():
        if not record.get("image_managed") or not record.get("image"):
            continue
        path = _local_url_to_path(hass, record["image"])
        if path is not None:
            referenced.add(path)

    removed = await hass.async_add_executor_job(
        _delete_unreferenced, _images_root(hass), referenced
    )
    if removed:
        _LOGGER.info("K93 ANS removed %d orphaned notification image file(s)", removed)
