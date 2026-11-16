"""Currículo e carta em PDF, gerados no servidor: sem cabeçalho nem rodapé do navegador."""
import html
import io
import re
import unicodedata
from pathlib import Path

import markdown as md
from fpdf import FPDF
from fpdf.fonts import FontFace, TextStyle

# Fonte com acentos e símbolos. Na imagem Docker vem do pacote fonts-liberation.
FONT_SETS = (
    ("/usr/share/fonts/truetype/liberation/LiberationSans-{}.ttf", ("Regular", "Bold", "Italic", "BoldItalic")),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans{}.ttf", ("", "-Bold", "-Oblique", "-BoldOblique")),
    ("C:/Windows/Fonts/arial{}.ttf", ("", "bd", "i", "bi")),
)
# Sem fonte no sistema, usa a Helvetica embutida (Latin-1) trocando o que ela não tem.
PLAIN = {"–": "-", "—": "-", "‑": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "•": "-", "…": "...",
         "\u00a0": " ", "≥": ">=", "≤": "<=", "→": "->", "✓": "-", "●": "-", "·": "-"}
PHOTO_W, PHOTO_H, GAP = 28, 36, 6  # mm
MARGIN = 17


def _fonts(pdf: FPDF) -> str:
    for pattern, names in FONT_SETS:
        files = [Path(pattern.format(n)) for n in names]
        if all(f.is_file() for f in files):
            for style, f in zip(("", "B", "I", "BI"), files):
                pdf.add_font("doc", style, str(f))
            return "doc"
    return "helvetica"


def _latin1(text: str) -> str:
    for a, b in PLAIN.items():
        text = text.replace(a, b)
    return text.encode("latin-1", "replace").decode("latin-1")


def _html(text: str) -> str:
    """Markdown -> HTML sem nada vindo do texto além de formatação (o texto veio de um LLM)."""
    out = md.markdown(html.escape(text, quote=False))
    out = re.sub(r"<img[^>]*>", "", out)
    out = re.sub(r"<hr\s*/?>", "", out)
    out = re.sub(r'href="(?!https?://|mailto:)[^"]*"', 'href="#"', out)
    return re.sub(r"<(p|ul|ol)>", lambda m: f'<{m.group(1)} line-height="1.3">', out)


def render(text: str, title: str, photo: bytes | None = None) -> bytes:
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_margins(MARGIN, 15, MARGIN)
    pdf.set_auto_page_break(True, 15)
    pdf.set_title(title)
    family = _fonts(pdf)
    if family == "helvetica":
        text = _latin1(text)
    pdf.add_page()
    pdf.set_font(family, size=10.5)
    pdf.set_text_color(20, 20, 20)
    dark = (20, 20, 20)
    def head(size, top):
        return TextStyle(font_family=family, font_style="B", font_size_pt=size, color=dark, t_margin=top,
                         b_margin=0.15)

    styles = {
        "h1": head(19, 0), "h2": head(12, 4), "h3": head(10.5, 2.5), "h4": head(10.5, 2.5),
        "p": TextStyle(t_margin=1.2), "ul": TextStyle(t_margin=0.6), "ol": TextStyle(t_margin=0.6),
        "li": TextStyle(l_margin=4, t_margin=0.8),
        "a": FontFace(color=dark),
    }

    def write(part: str) -> None:
        if part.strip():
            pdf.write_html(_html(part), tag_styles=styles, font_family=family,
                           ul_bullet_char="•" if family == "doc" else "-",
                           li_prefix_color=dark)

    head, rest = text, ""
    if photo:
        # A foto fica no canto; só o cabeçalho (até a primeira seção) se estreita para dar lugar a ela.
        m = re.search(r"^##\s", text, flags=re.M)
        if m:
            head, rest = text[:m.start()], text[m.start():]
        try:
            pdf.image(io.BytesIO(photo), x=pdf.w - MARGIN - PHOTO_W, y=pdf.t_margin, w=PHOTO_W, h=PHOTO_H,
                      keep_aspect_ratio=True)
        except Exception:  # foto ilegível: o currículo sai sem ela
            photo = None
    if photo:
        pdf.set_right_margin(MARGIN + PHOTO_W + GAP)
        write(head)
        pdf.set_right_margin(MARGIN)
        if rest and pdf.page == 1 and pdf.get_y() < pdf.t_margin + PHOTO_H + 2:
            pdf.set_y(pdf.t_margin + PHOTO_H + 2)
        write(rest)
    else:
        write(text)
    return bytes(pdf.output())


def filename(*parts: str) -> str:
    """Nome de arquivo só com ASCII seguro, para o cabeçalho Content-Disposition."""
    name = " - ".join(p.strip() for p in parts if p and p.strip())
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9 ._-]+", "", name)
    return (re.sub(r"\s+", " ", name).strip(" .")[:90] or "documento") + ".pdf"
