import io
import os
import re
import uuid
import urllib.request
from pathlib import Path

import fitz
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = Path(__file__).resolve().parent
load_dotenv(SERVICE_ROOT / ".env")

OUTPUT_DIR = Path(os.environ.get("PDF_TEMPLATE_OUTPUT_DIR", SERVICE_ROOT / "outputs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("PDF_TEMPLATE_PORT") or os.environ.get("PORT") or "5055")
BASE_URL = os.environ.get("PDF_TEMPLATE_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
CANELA_DIR = Path(os.environ.get("CANELA_FONT_DIR", ROOT / "Canela_Collection"))
SERVICE_API_KEY = os.environ.get("PDF_TEMPLATE_API_KEY", "").strip()
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("PDF_TEMPLATE_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

app = Flask(__name__)
CORS(app, origins=ALLOWED_ORIGINS or "*")


@app.before_request
def require_service_api_key():
    if not SERVICE_API_KEY or request.method == "OPTIONS" or not request.path.startswith("/api/"):
        return None
    provided = request.headers.get("x-pdf-template-api-key", "").strip()
    if provided != SERVICE_API_KEY:
        return jsonify({"success": False, "message": "Unauthorized PDF template request"}), 401
    return None


def cloudinary_enabled():
    return bool(
        os.environ.get("CLOUDINARY_CLOUD_NAME")
        and os.environ.get("CLOUDINARY_API_KEY")
        and os.environ.get("CLOUDINARY_API_SECRET")
    )


if cloudinary_enabled():
    import cloudinary
    import cloudinary.uploader

    cloudinary.config(
        cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
        api_key=os.environ.get("CLOUDINARY_API_KEY"),
        api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
        secure=True,
    )


def upload_bytes(data: bytes, filename: str, resource_type: str, folder: str) -> str:
    if cloudinary_enabled():
        result = cloudinary.uploader.upload(
            io.BytesIO(data),
            resource_type=resource_type,
            folder=folder,
            public_id=Path(filename).stem,
            overwrite=True,
        )
        return result.get("secure_url")

    path = OUTPUT_DIR / filename
    path.write_bytes(data)
    return f"{BASE_URL}/outputs/{filename}"


def download_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def clean_font_name(name: str) -> str:
    value = name or ""
    if "+" in value:
        value = value.split("+", 1)[1]
    value = re.sub(
        r"[-,](BoldItalic|BoldOblique|Regular|Italic|Oblique|Medium|Light|Thin|Black|Bold|Roman)$",
        "",
        value,
        flags=re.I,
    )
    return value.strip() or "Arial"


def font_style(name: str) -> str:
    lower = (name or "").lower()
    bold = any(token in lower for token in ["bold", "black", "heavy"])
    italic = any(token in lower for token in ["italic", "oblique"])
    if bold and italic:
        return "bold italic"
    if bold:
        return "bold"
    if italic:
        return "italic"
    return "normal"


def color_to_hex(color_int: int) -> str:
    color_int = color_int or 0
    return f"#{(color_int >> 16) & 255:02x}{(color_int >> 8) & 255:02x}{color_int & 255:02x}"


def hex_to_rgb(value: str):
    raw = (value or "#000000").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if not re.match(r"^[0-9a-fA-F]{6}$", raw):
        raw = "000000"
    return tuple(int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))


def render_preview(page: fitz.Page, prefix: str) -> str:
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    filename = f"{prefix}_{uuid.uuid4().hex[:10]}.png"
    return upload_bytes(pix.tobytes("png"), filename, "image", "beatific/template-previews")


def extract_text(page: fitz.Page):
    extracted = []
    for block in page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = (span.get("text") or "").strip()
                if not text:
                    continue
                x0, y0, x1, y1 = span.get("bbox") or [0, 0, 0, 0]
                font = span.get("font") or "Arial"
                extracted.append(
                    {
                        "id": str(uuid.uuid4()),
                        "text": text,
                        "x": round(x0, 2),
                        "y": round(y0, 2),
                        "width": round(max(1, x1 - x0), 2),
                        "height": round(max(1, y1 - y0), 2),
                        "fontSize": round(span.get("size") or 12, 2),
                        "fontFamily": clean_font_name(font),
                        "fontStyle": font_style(font),
                        "fill": color_to_hex(span.get("color") or 0),
                    }
                )
    return extracted


def find_canela_font(family: str, style: str, font_file: str | None = None) -> str | None:
    if font_file and CANELA_DIR.exists():
        requested = Path(str(font_file)).name
        direct = CANELA_DIR / requested
        if direct.exists():
            return str(direct)
        for path in list(CANELA_DIR.rglob("*.otf")) + list(CANELA_DIR.rglob("*.ttf")):
            if path.name.lower() == requested.lower():
                return str(path)

    if not family or "canela" not in family.lower() or not CANELA_DIR.exists():
        return None
    family_key = re.sub(r"\s+", "", family).lower()
    wants_italic = "italic" in (style or "").lower()
    wants_bold = "bold" in (style or "").lower()

    candidates = list(CANELA_DIR.rglob("*.otf")) + list(CANELA_DIR.rglob("*.ttf"))
    scored = []
    for path in candidates:
        stem = path.stem.lower()
        score = 0
        compact = stem.replace("-", "").replace("_", "")
        if family_key in compact or compact in family_key:
            score += 10
        if wants_italic == ("italic" in stem):
            score += 3
        if wants_bold and any(token in stem for token in ["bold", "black"]):
            score += 3
        if not wants_bold and "regular" in stem:
            score += 1
        scored.append((score, str(path)))
    scored.sort(reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else None


def safe_float(value, fallback: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def insert_fitted_textbox(
    page: fitz.Page,
    rect: fitz.Rect,
    value: str,
    field: dict,
    kwargs: dict,
    snapped_rotation: int,
    warnings: list[str],
) -> None:
    label = field.get("label") or field.get("key") or "Template field"
    original_size = safe_float(kwargs.get("fontsize"), 12)
    min_size = min(original_size, max(6.0, original_size * 0.65))
    font_size = original_size
    last_result = 0

    while font_size >= min_size - 0.01:
        attempt_kwargs = {**kwargs, "fontsize": round(font_size, 2)}
        result = page.insert_textbox(rect, value, rotate=snapped_rotation, **attempt_kwargs)
        last_result = result if isinstance(result, (int, float)) else 0
        if not isinstance(result, (int, float)) or result >= 0:
            if font_size < original_size - 0.01:
                warnings.append(
                    f"{label} was fitted by reducing font size from {original_size:g} pt to {font_size:g} pt"
                )
            return
        font_size -= 0.5

    warnings.append(
        f"{label} does not fit its marked text box even at {min_size:g} pt; "
        f"increase the marked box size or shorten the value (overflow {abs(last_result):.1f} pt)."
    )


def apply_template_fields(doc: fitz.Document, target: str, fields: list[dict], values: dict, warnings: list[str]):
    if doc.page_count == 0:
        return
    page = doc[0]
    target_fields = [field for field in fields if field.get("target") == target]

    for field in target_fields:
        box = field.get("replacementBox")
        if box:
            rect = fitz.Rect(box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"])
            page.add_redact_annot(rect, fill=(1, 1, 1))
    if any(field.get("replacementBox") for field in target_fields):
        page.apply_redactions()

    for field in target_fields:
        key = field.get("key")
        value = str(values.get(key, "") or "")
        if field.get("required") and not value.strip():
            warnings.append(f"Missing required field: {field.get('label') or key}")
            continue

        rect = fitz.Rect(
            field.get("x", 0),
            field.get("y", 0),
            field.get("x", 0) + max(1, field.get("width", 100)),
            field.get("y", 0) + max(1, field.get("height", field.get("fontSize", 12) * 1.4)),
        )
        align = {"left": 0, "center": 1, "right": 2}.get(field.get("align", "left"), 0)
        fontsize = float(field.get("fontSize") or 12)
        family = field.get("fontFamily") or "Arial"
        style = field.get("fontStyle") or "normal"
        fontfile = find_canela_font(family, style, field.get("fontFile"))
        rotation = float(field.get("rotation") or 0)
        snapped_rotation = int(round(rotation / 90.0)) * 90

        kwargs = {
            "fontsize": fontsize,
            "color": hex_to_rgb(field.get("fill") or "#000000"),
            "align": align,
            "lineheight": safe_float(field.get("lineHeight"), 1.2),
        }
        if fontfile:
            kwargs["fontfile"] = fontfile
            kwargs["fontname"] = f"F{uuid.uuid4().hex[:8]}"
        else:
            kwargs["fontname"] = "helv"

        insert_fitted_textbox(page, rect, value, field, kwargs, snapped_rotation, warnings)


def save_pdf(doc: fitz.Document, filename: str) -> str:
    data = doc.tobytes(deflate=True, garbage=4)
    return upload_bytes(data, filename, "raw", "beatific/generated-pdfs")


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "beatific-pdf-template", "cloudinary": cloudinary_enabled()})


@app.get("/outputs/<path:filename>")
def outputs(filename):
    return send_from_directory(OUTPUT_DIR, filename)


@app.post("/api/decompose-template")
def decompose_template():
    if "file" not in request.files:
        return jsonify({"success": False, "message": "No PDF uploaded"}), 400
    uploaded = request.files["file"]
    raw = uploaded.read()
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        if doc.page_count == 0:
            raise ValueError("PDF has no pages")
        page = doc[0]
        data = {
            "pageWidth": round(page.rect.width, 2),
            "pageHeight": round(page.rect.height, 2),
            "pageCount": doc.page_count,
            "previewImageUrl": render_preview(page, "template_import"),
            "extractedText": extract_text(page),
        }
        doc.close()
        return jsonify({"success": True, "data": data})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.post("/api/render-template")
def render_template():
    payload = request.get_json(force=True) or {}
    fields = payload.get("fields") or []
    values = payload.get("values") or {}
    warnings: list[str] = []
    result = {
        "coverPdfUrl": None,
        "interiorPdfUrl": None,
        "coverPreviewUrl": None,
        "interiorPreviewUrl": None,
        "warnings": warnings,
    }

    try:
        cover_url = payload.get("coverPdfUrl")
        if cover_url:
            cover_doc = fitz.open(stream=download_bytes(cover_url), filetype="pdf")
            if cover_doc.page_count > 1:
                cover_doc.select([0])
            apply_template_fields(cover_doc, "cover", fields, values, warnings)
            result["coverPreviewUrl"] = render_preview(cover_doc[0], "cover_preview")
            result["coverPdfUrl"] = save_pdf(cover_doc, f"cover_{uuid.uuid4().hex}.pdf")
            cover_doc.close()

        interior_url = payload.get("interiorPdfUrl")
        if interior_url:
            interior_doc = fitz.open(stream=download_bytes(interior_url), filetype="pdf")
            apply_template_fields(interior_doc, "interiorFirstPage", fields, values, warnings)
            result["interiorPreviewUrl"] = render_preview(interior_doc[0], "interior_preview")
            result["interiorPdfUrl"] = save_pdf(interior_doc, f"interior_{uuid.uuid4().hex}.pdf")
            interior_doc.close()

        return jsonify({"success": True, "data": result})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
