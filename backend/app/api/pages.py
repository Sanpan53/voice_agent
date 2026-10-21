"""静态调试页。"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent / "resources" / "static"


@router.get("/voice-test.html", include_in_schema=False)
async def voice_test_page() -> FileResponse:
    """单文件 H5 语音调试页，浏览器直接访问 ``/voice-test.html``。"""
    path = STATIC_DIR / "voice-test.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="voice-test.html 不存在")
    return FileResponse(path, media_type="text/html")
