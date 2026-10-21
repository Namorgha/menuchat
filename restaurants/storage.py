"""Menu photo storage on Vercel Blob (Vercel functions have no writable disk).

Speaks the same REST API as the official @vercel/blob SDK: PUT to upload, POST /delete to remove.
Files are public; the stored name is the blob pathname and the URL is built from the store ID in the token.
"""
import os
import secrets

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

API_URL = "https://vercel.com/api/blob"
API_VERSION = "13"
TIMEOUT = 20


class PhotoUploadError(Exception):
    """The photo couldn't be stored; the upload should be refused, not crash the request."""


@deconstructible
class VercelBlobStorage(Storage):
    def __init__(self, token=None):
        self.token = token or settings.BLOB_READ_WRITE_TOKEN
        # Tokens look like vercel_blob_rw_<storeId>_<secret>.
        parts = self.token.split("_")
        self.store_id = parts[3] if len(parts) > 3 else ""

    def _headers(self, **extra):
        return {
            "authorization": f"Bearer {self.token}",
            "x-api-version": API_VERSION,
            "x-vercel-blob-store-id": self.store_id,
            **extra,
        }

    def get_available_name(self, name, max_length=None):
        # Names get a random suffix, so they never collide and nothing needs to be checked remotely.
        root, ext = os.path.splitext(name)
        suffix = f"-{secrets.token_hex(4)}{ext}"
        if max_length:
            root = root[: max_length - len(suffix)]
        return f"{root}{suffix}"

    def _save(self, name, content):
        content.seek(0)
        try:
            response = requests.put(
                f"{API_URL}/",
                params={"pathname": name},
                data=content.read(),
                headers=self._headers(**{
                    "x-vercel-blob-access": "public",
                    "x-content-type": getattr(content, "content_type", None) or "application/octet-stream",
                    "x-add-random-suffix": "0",
                    "x-allow-overwrite": "0",
                }),
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            return response.json()["pathname"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise PhotoUploadError(f"Vercel Blob upload failed: {exc}") from exc

    def delete(self, name):
        try:
            requests.post(
                f"{API_URL}/delete", json={"urls": [self.url(name)]}, headers=self._headers(), timeout=TIMEOUT
            ).raise_for_status()
        except requests.RequestException as exc:
            raise PhotoUploadError(f"Vercel Blob delete failed: {exc}") from exc

    def _open(self, name, mode="rb"):
        response = requests.get(self.url(name), timeout=TIMEOUT)
        response.raise_for_status()
        return ContentFile(response.content, name=name)

    def exists(self, name):
        return False

    def url(self, name):
        return f"https://{self.store_id.lower()}.public.blob.vercel-storage.com/{name}"
