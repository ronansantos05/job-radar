"""Currículo e carta em PDF, gerados no servidor: sem cabeçalho nem rodapé do navegador.

O layout é próprio (não depende de o modelo acertar o Markdown): nome, linha de contatos, seções com
fio, cargo com a data à direita, tópicos com recuo. Texto alinhado à esquerda, que é o que leitores
automáticos (ATS) e recrutadores leem melhor.
"""
import io
import re
import unicodedata
from pathlib import Path

from fpdf import FPDF

# Fonte com acentos e símbolos. Na imagem Docker vem do pacote fonts-liberation.
FONT_SETS = (
    ("/usr/share/fonts/truetype/liberation/LiberationSans-{}.ttf", ("Regular", "Bold", "Italic", "BoldItalic")),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans{}.ttf", ("", "-Bold", "-Oblique", "-BoldOblique")),
    ("C:/Windows/Fonts/arial{}.ttf", ("", "bd", "i", "bi")),
)
# Caracteres que os modelos gostam de usar e que somem ou quebram em PDF: viram o equivalente simples.
ALWAYS = {"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u00ad": "", "\u2212": "-", "\u202f": " ",
          "\u2009": " ", "\u200a": " ", "\u2007": " ", "\u200b": "", "\ufeff": "", "\t": " "}
# Sem fonte no sistema, usa a Helvetica embutida (Latin-1) trocando o que ela não tem.
PLAIN = {"–": "-", "—": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "•": "-", "…": "...",
         "\u00a0": " ", "≥": ">=", "≤": "<=", "→": "->", "✓": "-", "●": "-", "·": "-"}
PHOTO_W, PHOTO_H, GAP = 26, 33, 6  # mm
INK, SOFT, RULE = (25, 25, 25), (85, 85, 85), (150, 150, 150)
SCALES = (1.0, 0.95, 0.9, 0.86)  # tenta caber em uma página antes de aceitar duas

BULLET = re.compile(r"^\s*(?:[-*•●▪◦]|\d{1,2}[.)])\s+(.*)$")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s*(.+?)\s*#*$")
BOLD_LINE = re.compile(r"^\*\*([^*]{2,60})\*\*:?$")
INLINE = re.compile(r"(\*\*\*.+?\*\*\*|\*\*.+?\*\*|(?<![\w*])\*[^*\s](?:[^*]*[^*\s])?\*(?![\w*])|\[[^\]]+\]\([^)\s]+\))")
YEAR = r"(?:[A-Za-zçÇ]{3,9}\.?\s+)?(?:19|20)\d{2}"
DATE_TAIL = re.compile(r"^(.*?)\s*(?:\||·|—|–|,|\()\s*(" + YEAR + r"(?:\s*[-–—]\s*(?:" + YEAR +
                       r"|[A-Za-zÀ-ú]{3,12})?)?)\)?\s*$")


def clean(text: str) -> str:
    for a, b in ALWAYS.items():
        text = text.replace(a, b)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def blocks(text: str) -> list[tuple[str, str]]:
    """Markdown solto -> blocos (tipo, texto). Tolera o que os modelos erram: tópico sem linha em branco
    antes, seção escrita só em negrito, linha quebrada no meio do tópico."""
    out: list[tuple[str, str]] = []
    for raw in clean(text).split("\n"):
        line = raw.rstrip()
        if not line.strip():
            out.append(("gap", ""))
            continue
        if re.fullmatch(r"\s*([-*_=])\1{2,}\s*", line) or line.strip().startswith("```"):
            continue
        if m := HEADING.match(line):
            level = len(m.group(1))
            out.append(("h1" if level == 1 else "h2" if level == 2 else "h3", m.group(2).strip("* ")))
        elif m := BOLD_LINE.match(line.strip()):
            out.append(("h2", m.group(1).strip()))
        elif m := BULLET.match(line):
            out.append(("bullet", m.group(1).strip()))
        elif raw.startswith("  ") and out and out[-1][0] == "bullet":
            out[-1] = ("bullet", out[-1][1] + " " + line.strip())
        else:
            out.append(("p", line.strip()))
    return out


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


def _spans(text: str, base: str = "") -> list[tuple[str, str]]:
    """Negrito, itálico e links do Markdown -> trechos (estilo, texto). Link vira só o texto visível."""
    spans = []
    for part in INLINE.split(text):
        if not part:
            continue
        style = base
        if part.startswith("[") and (m := re.fullmatch(r"\[([^\]]+)\]\(([^)\s]+)\)", part)):
            part = m.group(1)
        elif part.startswith("***") and part.endswith("***") and len(part) > 6:
            part, style = part[3:-3], "BI"
        elif part.startswith("**") and part.endswith("**") and len(part) > 4:
            part, style = part[2:-2], "B" if "I" not in base else "BI"
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            part, style = part[1:-1], "I" if "B" not in base else "BI"
        spans.append((style, part.replace("**", "").replace("`", "")))
    return spans


class _Doc:
    def __init__(self, scale: float, title: str, photo: bytes | None):
        self.k = scale
        self.pdf = pdf = FPDF(format="A4", unit="mm")
        self.margin = 16 * scale + 1
        pdf.set_margins(self.margin, 14 * scale, self.margin)
        pdf.set_auto_page_break(True, 13 * scale)
        pdf.set_title(title)
        self.family = _fonts(pdf)
        self.fix = _latin1 if self.family == "helvetica" else (lambda s: s)
        self.size = 10.2 * scale
        self.lh = self.size * 0.3528 * 1.36  # altura da linha em mm
        pdf.add_page()
        pdf.set_text_color(*INK)
        self.photo_bottom = 0.0
        if photo:
            try:
                pdf.image(io.BytesIO(photo), x=pdf.w - self.margin - PHOTO_W, y=pdf.t_margin, w=PHOTO_W,
                          h=PHOTO_H, keep_aspect_ratio=True)
                self.photo_bottom = pdf.t_margin + PHOTO_H + 2
                pdf.set_right_margin(self.margin + PHOTO_W + GAP)
            except Exception:  # foto ilegível: o documento sai sem ela
                pass

    def font(self, style: str = "", size: float | None = None, color=INK) -> None:
        self.pdf.set_font(self.family, style, size or self.size)
        self.pdf.set_text_color(*color)

    def space(self, mm: float) -> None:
        self.pdf.set_y(self.pdf.get_y() + mm * self.k)

    def text(self, text: str, base: str = "", size: float | None = None, color=INK, indent: float = 0,
             lh: float | None = None) -> None:
        pdf = self.pdf
        pdf.set_left_margin(self.margin + indent)
        pdf.set_x(self.margin + indent)
        for style, part in _spans(self.fix(text), base):
            self.font(style, size, color)
            pdf.write(lh or self.lh, part)
        pdf.ln(lh or self.lh)
        pdf.set_left_margin(self.margin)

    def keep(self, mm: float) -> None:
        """Evita título sozinho no pé da página."""
        if self.pdf.get_y() + mm > self.pdf.h - self.pdf.b_margin:
            self.pdf.add_page()

    def end_header(self) -> None:
        if self.photo_bottom:
            self.pdf.set_right_margin(self.margin)
            if self.pdf.page == 1 and self.pdf.get_y() < self.photo_bottom:
                self.pdf.set_y(self.photo_bottom)
            self.photo_bottom = 0.0

    def section(self, title: str) -> None:
        pdf = self.pdf
        self.end_header()
        self.keep(self.lh * 3.2)
        self.space(3.4)
        self.font("B", self.size * 1.02)
        pdf.set_char_spacing(0.35)
        pdf.set_x(self.margin)
        pdf.cell(0, self.lh, self.fix(title.upper()), new_x="LMARGIN", new_y="NEXT")
        pdf.set_char_spacing(0)
        y = pdf.get_y() + 0.5
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.25)
        pdf.line(self.margin, y, pdf.w - pdf.r_margin, y)
        pdf.set_y(y + 1.5 * self.k)

    def entry(self, title: str) -> None:
        """Cargo/projeto em negrito; se a linha termina com o período, ele vai alinhado à direita."""
        pdf = self.pdf
        self.end_header()
        self.keep(self.lh * 2.6)
        self.space(1.7)
        plain = re.sub(r"[*`]", "", title)
        date = ""
        if m := DATE_TAIL.match(plain):
            head, tail = m.group(1).strip(" |·—–,("), m.group(2).strip()
            self.font("", self.size)
            wd = pdf.get_string_width(self.fix(tail))
            self.font("B")
            if head and pdf.get_string_width(self.fix(head)) + wd + 6 < pdf.w - pdf.l_margin - pdf.r_margin:
                plain, date = head, tail
        y = pdf.get_y()
        if date:
            self.font("", self.size, SOFT)
            pdf.set_xy(self.margin, y)
            pdf.cell(0, self.lh, self.fix(date), align="R")
            pdf.set_xy(self.margin, y)
        self.text(plain, "B")

    def bullet(self, text: str) -> None:
        pdf = self.pdf
        self.space(0.45)
        self.font()
        pdf.set_x(self.margin + 1.2)
        pdf.cell(3.2, self.lh, "•" if self.family == "doc" else "-")
        self.text(text, indent=4.4)


def _render(text: str, title: str, photo: bytes | None, scale: float) -> FPDF:
    d = _Doc(scale, title, photo)
    items = blocks(text)
    is_cv = any(kind == "h1" for kind, _ in items)
    in_header = is_cv
    prev = "start"
    for kind, body in items:
        if kind == "gap":
            if prev == "p" and not in_header:
                d.space(1.6 if is_cv else 2.6)
            prev = "gap"
            continue
        if kind == "h1":
            d.text(body, "B", 20 * scale, lh=9 * scale)
            d.space(0.6)
        elif kind == "h2":
            in_header = False
            d.section(body)
        elif kind == "h3":
            in_header = False
            d.entry(body)
        elif kind == "bullet":
            if in_header:
                in_header = False
                d.end_header()
            d.bullet(body)
        elif in_header:  # linhas logo abaixo do nome: cargo-alvo e contatos
            d.text(body, size=d.size * 0.96, color=SOFT)
        else:
            d.text(body)
        prev = kind
    return d.pdf


def render(text: str, title: str, photo: bytes | None = None) -> bytes:
    """Devolve o PDF. Se passar pouco de uma página, aperta um pouco a letra para caber em uma."""
    first = None
    for scale in SCALES:
        pdf = _render(text, title, photo, scale)
        first = first or pdf
        if pdf.page == 1:
            return bytes(pdf.output())
        if first.page > 2:
            break
    return bytes(first.output())


def filename(*parts: str) -> str:
    """Nome de arquivo só com ASCII seguro, para o cabeçalho Content-Disposition."""
    name = " - ".join(p.strip() for p in parts if p and p.strip())
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9 ._-]+", "", name)
    return (re.sub(r"\s+", " ", name).strip(" .")[:90] or "documento") + ".pdf"
