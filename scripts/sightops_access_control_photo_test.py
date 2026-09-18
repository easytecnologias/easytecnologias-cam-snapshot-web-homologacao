from __future__ import annotations

import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_face_photo_is_tenant_scoped_and_normalized_for_controller() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ["DATA_DIR"] = str(tmp)
        os.environ["SIGHTOPS_DB_PATH"] = str(tmp / "sightops.db")
        os.environ["DATABASE_BACKEND"] = "sqlite"
        os.environ.pop("DATABASE_URL", None)

        from app.core import tenant_context
        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services import db_store
        from app.services.access_control_photos import save_person_face_photo
        from app.services.access_control_store import save_person

        db_store.SIGHTOPS_DB_PATH = tmp / "sightops.db"
        tenant_context.DATA_DIR = tmp
        token = set_current_tenant_slug("escola-fotos")
        try:
            person = save_person({"full_name": "Aluno Foto", "document_id": "11111111111", "controller_user_id": "1001"})
            src = Image.new("RGB", (900, 1200), "#f0c090")
            raw = BytesIO()
            src.save(raw, format="PNG")

            result = save_person_face_photo(person["id"], raw.getvalue())

            assert result["person"]["face_photo_path"] == f"access-control/faces/{person['id']}.jpg"
            saved = tmp / "tenants" / "escola-fotos" / result["face_photo_path"]
            assert saved.exists()
            with Image.open(saved) as img:
                assert img.format == "JPEG"
                assert img.size == (360, 480)
        finally:
            reset_current_tenant_slug(token)


if __name__ == "__main__":
    test_face_photo_is_tenant_scoped_and_normalized_for_controller()
    print("OK access control photo")
