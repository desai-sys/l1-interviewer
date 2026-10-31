"""Turn uploaded resume / job-description files into text."""
import io

from docx import Document
from pypdf import PdfReader


class ExtractionError(ValueError):
    pass


def extract_text(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:4] == b"%PDF":
        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        except Exception as exc:  # malformed PDF
            raise ExtractionError(f"Could not read PDF: {exc}") from exc
    if name.endswith(".docx"):
        try:
            doc = Document(io.BytesIO(data))
        except Exception as exc:
            raise ExtractionError(f"Could not read DOCX: {exc}") from exc
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts).strip()
    if name.endswith((".txt", ".md")) or not name:
        return data.decode("utf-8", errors="replace").strip()
    raise ExtractionError("Unsupported file type. Upload a PDF, DOCX or TXT file.")


def is_pdf(filename: str, data: bytes) -> bool:
    return (filename or "").lower().endswith(".pdf") or data[:4] == b"%PDF"
