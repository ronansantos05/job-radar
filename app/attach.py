"""Anexo de uma pergunta: print (imagem) ou documento (PDF, DOCX, texto).

O arquivo não é guardado: a imagem vai para o modelo e o documento vira texto. O tipo é decidido pela
assinatura do arquivo, nunca pelo nome nem pelo tipo que o navegador informa.
"""
from __future__ import annotations

import html
import io
import re
import zipfile

IMAGE_MAX = 3 * 1024 * 1024      # a Groq aceita até 4 MB em base64, o que dá uns 3 MB de arquivo
DOC_MAX = 8 * 1024 * 1024
TEXT_MAX = 12000                 # caracteres do documento que vão para o modelo
PDF_PAGES = 15
DOCX_XML_MAX = 6 * 1024 * 1024   # tamanho já descompactado: barra arquivo feito para estourar a memória


class AttachError(ValueError):
    """Mensagem pronta para mostrar à pessoa."""


def image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def safe_name(name: str | None) -> str:
    name = re.sub(r"[^\w .()-]+", "", (name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    return name[:60] or "anexo"


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise AttachError("O PDF está protegido por senha. Envie uma versão sem senha ou um print.")
        parts, size = [], 0
        for page in reader.pages[:PDF_PAGES]:
            text = page.extract_text() or ""
            parts.append(text)
            size += len(text)
            if size > TEXT_MAX:
                break
        return "\n".join(parts)
    except AttachError:
        raise
    except Exception:
        raise AttachError("Não consegui ler esse PDF. Tente enviar um print da parte que interessa.")


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > DOCX_XML_MAX:
                raise AttachError("O documento é grande demais. Envie só a parte que interessa.")
            xml = z.read(info).decode("utf-8", "replace")
    except AttachError:
        raise
    except Exception:
        raise AttachError("Não consegui ler esse arquivo. Envie PDF, DOCX, texto ou um print.")
    # sem interpretar o XML (nada de entidades externas): só troca fim de parágrafo por quebra e tira as tags
    xml = re.sub(r"</w:p>|<w:br\s*/?>", "\n", xml)
    xml = re.sub(r"<w:tab\s*/?>", " ", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def read(filename: str | None, data: bytes) -> dict:
    """Devolve {"name", "kind": "image", "mime", "data"} ou {"name", "kind": "text", "text"}."""
    name = safe_name(filename)
    if not data:
        raise AttachError("O arquivo está vazio.")
    if mime := image_mime(data):
        if len(data) > IMAGE_MAX:
            raise AttachError("A imagem passa de 3 MB. Recorte o print ou salve em qualidade menor.")
        return {"name": name, "kind": "image", "mime": mime, "data": data}
    if len(data) > DOC_MAX:
        raise AttachError("O arquivo passa de 8 MB. Envie só a parte que interessa.")
    if data.startswith(b"%PDF"):
        text = _pdf_text(data)
        if len(text.strip()) < 20:
            raise AttachError("Esse PDF não tem texto (deve ser escaneado). Envie um print da página.")
    elif data.startswith(b"PK\x03\x04"):
        text = _docx_text(data)
    elif b"\x00" not in data[:4000]:
        text = data.decode("utf-8", "replace")
    else:
        raise AttachError("Tipo de arquivo não aceito. Envie imagem (PNG, JPG, WebP), PDF, DOCX ou texto.")
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()
    if not text:
        raise AttachError("Não encontrei texto nesse arquivo.")
    return {"name": name, "kind": "text", "text": text[:TEXT_MAX]}
