from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse
import ipaddress
import os
import re
import shutil

from PIL import Image, ImageDraw, ImageFont

from app.core.paths import DATA_DIR, OUTPUT_DIR, SAIDA_DIR
from app.core.tenant_context import tenant_snapshot_dir


A4_W = 2480
A4_H = 3508
MARGIN_X = 120
MARGIN_Y = 90

ProgressCb = Optional[Callable[[int, int, str], None]]

# Teto de seguranca (nao um limite pratico): cada pagina e desenhada e
# gravada em disco como JPEG temporario assim que fica pronta (ver
# _PageSink), entao a memoria usada nao cresce com o numero de fotos --
# um inventario com 1000+ cameras funciona, so demora mais (por isso o
# endpoint de relatorio roda como job com progresso em vez de request
# sincrono). Este numero e so uma trava contra um pedido acidentalmente
# gigantesco.
MAX_PHOTOS = 4000

# Paleta neutra com leve tom frio, para nao competir com a cor de destaque
# (report_color, escolhida por tenant/relatorio).
INK = "#101828"
INK_MUTED = "#5b6779"
BORDER = "#dbe2ec"
BORDER_SOFT = "#e8edf5"
PAGE_BG = "#f6f8fb"
CARD_BG = "#ffffff"
ROW_STRIPE = "#eef2f8"
SHADOW = "#c7d1e0"

STATUS_OK_BG = "#e3f6ea"
STATUS_OK_FG = "#0d7a3f"
STATUS_BAD_BG = "#fbe7e6"
STATUS_BAD_FG = "#b3261e"
STATUS_MUTED_BG = "#eef1f6"
STATUS_MUTED_FG = "#51607a"


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = []
    if bold:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/segoeuib.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/segoeui.ttf",
            ]
        )
    for fp in candidates:
        try:
            return ImageFont.truetype(fp, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _load_mono_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = []
    if bold:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationMono-Bold.ttf",
                "C:/Windows/Fonts/consolab.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
                "C:/Windows/Fonts/consola.ttf",
            ]
        )
    for fp in candidates:
        try:
            return ImageFont.truetype(fp, size=size)
        except Exception:
            continue
    return _load_font(size, bold=bold)


def _to_text(v: Any) -> str:
    return str(v or "").strip()


def _report_color(value: str = "") -> str:
    raw = _to_text(value)
    if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return raw
    if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        return "#" + raw
    return "#0b2242"


def _hex_to_rgb(color: str) -> Tuple[int, int, int]:
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _shade(color: str, amount: float) -> str:
    """amount<0 escurece, amount>0 clareia (fracao de 0 a 1)."""
    r, g, b = _hex_to_rgb(color)
    if amount >= 0:
        r = int(r + (255 - r) * amount)
        g = int(g + (255 - g) * amount)
        b = int(b + (255 - b) * amount)
    else:
        r = int(r * (1 + amount))
        g = int(g * (1 + amount))
        b = int(b * (1 + amount))
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def _fit_text(draw: ImageDraw.ImageDraw, txt: str, font: ImageFont.ImageFont, max_w: int) -> str:
    text = _to_text(txt)
    if not text:
        return "-"
    if draw.textlength(text, font=font) <= max_w:
        return text
    base = text
    while base:
        base = base[:-1]
        candidate = base + "..."
        if draw.textlength(candidate, font=font) <= max_w:
            return candidate
    return "..."


def _status_colors(status: str) -> Tuple[str, str]:
    s = (status or "").strip().lower()
    if s == "online":
        return STATUS_OK_BG, STATUS_OK_FG
    if s in ("offline", "auth_failed", "camera_offline"):
        return STATUS_BAD_BG, STATUS_BAD_FG
    return STATUS_MUTED_BG, STATUS_MUTED_FG


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font: ImageFont.ImageFont,
    bg: str,
    fg: str,
    pad_x: int = 16,
    pad_y: int = 7,
) -> int:
    """Desenha um badge arredondado e retorna a largura ocupada."""
    label = text if text else "-"
    tw = draw.textlength(label, font=font)
    asc, desc = font.getmetrics()
    th = asc + desc
    w = int(tw + pad_x * 2)
    h = th + pad_y * 2
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h / 2, fill=bg)
    draw.text((x + pad_x, y + pad_y - 1), label, font=font, fill=fg)
    return w


def _snapshot_dirs(source: str) -> list:
    """Diretorios onde a foto pode estar, do mais provavel pro legado.

    O photo_store grava em DATA_DIR/tenants/<slug>/<source>_snapshot; o codigo
    antigo so olhava o global e por isso o relatorio saia sem imagem.
    """
    legado = {"dvr": "dvr_snapshot", "nvr": "nvr_snapshot"}.get(source, "snapshot")
    dirs = []
    try:
        d = tenant_snapshot_dir(source)
        if d:
            dirs.append(d)
    except Exception:
        pass
    dirs.append(DATA_DIR / legado)
    dirs.append(SAIDA_DIR / legado)
    vistos, saida = set(), []
    for d in dirs:
        chave = str(d)
        if chave not in vistos:
            vistos.add(chave)
            saida.append(d)
    return saida


def _achar_em_dirs(source: str, nome: str):
    if not nome:
        return None
    for d in _snapshot_dirs(source):
        try:
            p = d / nome
            if p.exists():
                return p
        except Exception:
            continue
    return None


def _ip_snapshot_name(ip: str) -> str:
    stem = _to_text(ip).replace(".", "_").replace(":", "__")
    return f"{stem}.jpg"


def _path_from_snapshot_url(url: str) -> Optional[Path]:
    raw = _to_text(url)
    if not raw:
        return None
    # tenta primeiro nos diretorios do tenant (onde as fotos realmente estao)
    _nome = raw.replace("\\", "/").rsplit("/", 1)[-1].split("?")[0]
    if _nome:
        _src = "dvr" if "dvr_snapshot" in raw else ("nvr" if "nvr_snapshot" in raw else "ip")
        _p = _achar_em_dirs(_src, _nome)
        if _p:
            return _p
    try:
        p = Path(raw)
        if p.is_file():
            return p
    except Exception:
        pass
    try:
        parsed = urlparse(raw)
        path = parsed.path or raw
    except Exception:
        path = raw
    if "/data/snapshot/" in path:
        name = path.rsplit("/", 1)[-1]
        p = DATA_DIR / "snapshot" / name
        if p.exists():
            return p
    if "/data/dvr_snapshot/" in path:
        name = path.rsplit("/", 1)[-1]
        p = DATA_DIR / "dvr_snapshot" / name
        if p.exists():
            return p
    if "/data/nvr_snapshot/" in path:
        name = path.rsplit("/", 1)[-1]
        p = DATA_DIR / "nvr_snapshot" / name
        if p.exists():
            return p
    if "/saida/snapshot/" in path:
        name = path.rsplit("/", 1)[-1]
        p = SAIDA_DIR / "snapshot" / name
        if p.exists():
            return p
    return None


def _pick_image_path(row: Dict[str, Any]) -> Optional[Path]:
    # --- tenant primeiro: e onde o photo_store grava hoje ---
    _snap = _to_text(row.get("snapshot_file") or row.get("snapshot_path"))
    if _snap:
        _nome = _snap.replace("\\", "/").rsplit("/", 1)[-1]
        for _src in ("ip", "dvr", "nvr"):
            _p = _achar_em_dirs(_src, _nome)
            if _p:
                return _p
    _ip = _to_text(row.get("ip") or row.get("IP"))
    if _ip:
        _p = _achar_em_dirs("ip", _ip_snapshot_name(_ip))
        if _p:
            return _p
    _host = _to_text(row.get("host")).split(":", 1)[0].strip()
    try:
        _ch = int(row.get("channel") or 0)
        _porta = int(row.get("http_port") or 80)
    except Exception:
        _ch, _porta = 0, 80
    if _host and _ch > 0:
        _nome = f"{_host.replace('.', '_')}_{_porta}_ch{_ch:02d}.jpg"
        for _src in ("dvr", "nvr"):
            _p = _achar_em_dirs(_src, _nome)
            if _p:
                return _p

    snap_file = _to_text(row.get("snapshot_file")).replace("\\", "/").strip()
    if snap_file.startswith("/"):
        snap_file = snap_file.lstrip("/")
    if snap_file.startswith("data/"):
        snap_file = snap_file[5:]
    if snap_file.startswith("saida/"):
        snap_file = snap_file[6:]
    if snap_file:
        # Caminho relativo direto
        for base in (DATA_DIR, SAIDA_DIR):
            p0 = base / snap_file
            if p0.exists():
                return p0
        # Apenas nome do arquivo (fallback)
        fname_only = Path(snap_file).name
        for d in ("snapshot", "dvr_snapshot", "nvr_snapshot"):
            p1 = DATA_DIR / d / fname_only
            if p1.exists():
                return p1
            p1b = SAIDA_DIR / d / fname_only
            if p1b.exists():
                return p1b

    ip = _to_text(row.get("ip") or row.get("IP"))
    if ip:
        p = DATA_DIR / "snapshot" / _ip_snapshot_name(ip)
        if p.exists():
            return p
        p2 = SAIDA_DIR / "snapshot" / _ip_snapshot_name(ip)
        if p2.exists():
            return p2
    for key in ("snapshot_url", "imgbb_url"):
        p3 = _path_from_snapshot_url(_to_text(row.get(key)))
        if p3 and p3.exists():
            return p3

    # Fallback DVR/NVR por padrao de arquivo: <host>_<porta>_chNN.jpg
    host = _to_text(row.get("host"))
    if ":" in host:
        host = host.split(":", 1)[0].strip()
    channel = int(row.get("channel") or 0)
    http_port = int(row.get("http_port") or 80)
    if host and channel > 0:
        fname = f"{host.replace('.', '_')}_{http_port}_ch{channel:02d}.jpg"
        p4 = DATA_DIR / "dvr_snapshot" / fname
        if p4.exists():
            return p4
        p5 = DATA_DIR / "nvr_snapshot" / fname
        if p5.exists():
            return p5
    return None


def _title_num(row: Dict[str, Any]) -> int:
    t = _to_text(row.get("titulo") or row.get("title") or row.get("nome"))
    if not t:
        return 10**9
    m = re.match(r"^\s*(\d{1,4})\b", t)
    if not m:
        return 10**9
    try:
        return int(m.group(1))
    except Exception:
        return 10**9


def _ip_num_key(row: Dict[str, Any]) -> Tuple[int, int]:
    ip_txt = _to_text(row.get("ip") or row.get("IP"))
    if not ip_txt:
        return (1, 2**32 - 1)
    try:
        return (0, int(ipaddress.ip_address(ip_txt)))
    except Exception:
        return (1, 2**32 - 1)


def _sort_inventory_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Ordenacao principal por IP numerico (crescente), evitando erro textual (ex.: .100 < .20).
    # Desempate por numero inicial do titulo (01, 02...) e depois titulo.
    return sorted(
        rows,
        key=lambda r: (
            _ip_num_key(r),
            _title_num(r),
            _to_text(r.get("titulo") or r.get("title") or r.get("nome")).lower(),
        ),
    )


def _new_page() -> Tuple[Image.Image, ImageDraw.ImageDraw]:
    page = Image.new("RGB", (A4_W, A4_H), PAGE_BG)
    draw = ImageDraw.Draw(page)
    return page, draw


def _new_page_landscape() -> Tuple[Image.Image, ImageDraw.ImageDraw]:
    page = Image.new("RGB", (A4_H, A4_W), PAGE_BG)
    draw = ImageDraw.Draw(page)
    return page, draw


class _PageSink:
    """Recebe paginas prontas e grava cada uma em disco na hora, descartando
    o bitmap da memoria em seguida. E o que permite montar um relatorio com
    milhares de paginas sem acumular gigabytes de imagens RGBA em RAM --
    antes, todas as paginas ficavam numa lista em memoria ate o fim."""

    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir
        self.paths: List[Path] = []

    def add(self, page: Image.Image) -> None:
        p = self.tmp_dir / f"page_{len(self.paths) + 1:05d}.jpg"
        page.convert("RGB").save(p, "JPEG", quality=88)
        self.paths.append(p)

    def __len__(self) -> int:
        return len(self.paths)


def _draw_header(
    page: Image.Image,
    draw: ImageDraw.ImageDraw,
    title: str,
    subtitle: str,
    company_name: str = "",
    logo_path: Optional[Path] = None,
    report_color: str = "",
) -> int:
    f_title = _load_font(46, bold=True)
    f_sub = _load_font(23, bold=False)
    f_company = _load_font(24, bold=True)
    color = _report_color(report_color)
    color_dark = _shade(color, -0.28)
    page_w = int(page.size[0])

    y = MARGIN_Y
    band_h = 172
    band_w = page_w - (2 * MARGIN_X)
    band_radius = 26

    # Gradiente vertical desenhado numa imagem a parte (simulado em fatias),
    # depois recortado com uma mascara arredondada e colado na pagina --
    # e a unica forma de ter cantos redondos com um preenchimento em
    # degrade no Pillow, que nao tem fill de gradiente nativo.
    band_img = Image.new("RGB", (band_w, band_h), color)
    band_draw = ImageDraw.Draw(band_img)
    steps = 32
    c0 = _hex_to_rgb(color_dark)
    c1 = _hex_to_rgb(color)
    for i in range(steps):
        blend = i / (steps - 1)
        rgb = tuple(int(c0[k] + (c1[k] - c0[k]) * blend) for k in range(3))
        y0 = int(band_h * i / steps)
        y1 = int(band_h * (i + 1) / steps) + 1
        band_draw.rectangle((0, y0, band_w, y1), fill=rgb)

    mask = Image.new("L", (band_w, band_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, band_w, band_h), radius=band_radius, fill=255)
    page.paste(band_img, (MARGIN_X, y), mask)
    draw = ImageDraw.Draw(page)

    # Contorno fino, efeito "capa"
    draw.rounded_rectangle((MARGIN_X, y, page_w - MARGIN_X, y + band_h), radius=band_radius, outline=_shade(color, -0.4), width=2)

    pad = 40
    draw.text((MARGIN_X + pad, y + 34), title, font=f_title, fill="#ffffff")
    draw.text((MARGIN_X + pad, y + 96), subtitle, font=f_sub, fill="#dbe6fb")

    company = _to_text(company_name)
    if company:
        label = company.upper()
        draw.text(
            (MARGIN_X + pad, y + band_h - 44),
            _fit_text(draw, label, f_company, page_w - (2 * MARGIN_X) - 260),
            font=f_company,
            fill="#c9d8f5",
        )

    if logo_path is not None and logo_path.exists():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            box_w, box_h = 190, 110
            logo.thumbnail((box_w, box_h))
            plate_w, plate_h = logo.width + 32, logo.height + 24
            ox = page_w - MARGIN_X - pad - plate_w
            oy = y + (band_h - plate_h) // 2
            draw.rounded_rectangle((ox, oy, ox + plate_w, oy + plate_h), radius=16, fill="#ffffff")
            page.paste(logo, (ox + 16, oy + 12), logo)
        except Exception:
            pass

    return y + band_h + 44


def _draw_footer(draw: ImageDraw.ImageDraw, page_num: int, total_pages: int, note: str = "") -> None:
    f_note = _load_font(19, bold=False)
    f_page = _load_font(19, bold=True)
    page_w, page_h = getattr(draw, "_image").size
    fy = page_h - MARGIN_Y + 8
    draw.line((MARGIN_X, fy - 18, page_w - MARGIN_X, fy - 18), fill=BORDER, width=2)
    if note:
        draw.text((MARGIN_X, fy), note, font=f_note, fill=INK_MUTED)
    page_label = f"Pagina {page_num} de {total_pages}"
    tw = draw.textlength(page_label, font=f_page)
    draw.text((page_w - MARGIN_X - tw, fy), page_label, font=f_page, fill=INK_MUTED)


def _column_set(include_switch: bool, include_olt: bool) -> List[Tuple[str, int]]:
    if include_switch:
        return [
            ("IP", 220),
            ("Titulo", 410),
            ("Status", 145),
            ("Local", 210),
            ("Modelo", 230),
            ("MAC", 310),
            ("Switch", 290),
            ("Switch IP", 190),
            ("Porta", 110),
            ("VLAN", 105),
        ]
    if include_olt:
        return [
            ("IP", 215),
            ("Titulo", 430),
            ("Status", 150),
            ("Local", 180),
            ("Modelo", 210),
            ("MAC", 280),
            ("PON", 85),
            ("ONU ID", 95),
            ("ONU Name", 235),
            ("ONU Serial", 285),
        ]
    return [
        ("IP", 290),
        ("Titulo", 590),
        ("Status", 200),
        ("Local", 260),
        ("Modelo", 330),
        ("MAC", 590),
    ]


def _draw_table_pages(
    rows: List[Dict[str, Any]],
    sink: "_PageSink",
    site_label: str,
    company_name: str = "",
    logo_path: Optional[Path] = None,
    include_olt: bool = True,
    include_switch: bool = False,
    module_label: str = "Cameras IP",
    report_color: str = "",
    progress_cb: ProgressCb = None,
) -> None:
    f_h = _load_font(23, bold=True)
    f = _load_font(23, bold=False)
    f_status = _load_font(19, bold=True)
    f_mono = _load_mono_font(22, bold=False)

    color = _report_color(report_color)
    cols = _column_set(include_switch, include_olt)
    line_h = 54
    header_h = 58

    idx = 0
    total = len(rows)
    while idx < total or (total == 0 and idx == 0):
        page, draw = _new_page()
        y = _draw_header(
            page,
            draw,
            f"Relatorio de inventario | {module_label}",
            f"Gerado em {datetime.now().strftime('%d/%m/%Y as %H:%M')}  ·  Site: {site_label}  ·  {total} camera{'s' if total != 1 else ''}",
            company_name=company_name,
            logo_path=logo_path,
            report_color=report_color,
        )
        draw = ImageDraw.Draw(page)
        draw.text((MARGIN_X, y), "Inventario detalhado", font=_load_font(28, bold=True), fill=INK)
        y += 50

        x0 = MARGIN_X
        w = A4_W - (2 * MARGIN_X)
        draw.rounded_rectangle((x0, y, x0 + w, y + header_h), radius=12, fill=color)
        x = x0 + 20
        for name, cw in cols:
            draw.text((x, y + 17), _fit_text(draw, name.upper(), f_h, cw - 18), font=f_h, fill="#ffffff")
            x += cw
        y += header_h + 6

        if total == 0:
            draw.rounded_rectangle((x0, y, x0 + w, y + 90), radius=12, fill=CARD_BG, outline=BORDER_SOFT, width=2)
            draw.text((x0 + 24, y + 32), "Nenhuma camera encontrada para o filtro atual.", font=f, fill=INK_MUTED)
            sink.add(page)
            break

        row_top = y
        while idx < total and y + line_h < A4_H - MARGIN_Y - 30:
            r = rows[idx]
            bg = CARD_BG if (idx % 2 == 0) else ROW_STRIPE
            draw.rectangle((x0, y, x0 + w, y + line_h), fill=bg)
            x = x0 + 20
            for col_name, cw in cols:
                if col_name == "IP":
                    val = _to_text(r.get("ip") or r.get("IP"))
                    draw.text((x, y + 15), _fit_text(draw, val, f_mono, cw - 20), font=f_mono, fill=INK)
                elif col_name == "MAC":
                    val = _to_text(r.get("mac") or r.get("MAC"))
                    draw.text((x, y + 16), _fit_text(draw, val, f_mono, cw - 20), font=f_mono, fill=INK_MUTED)
                elif col_name == "Status":
                    val = _to_text(r.get("status"))
                    bgp, fgp = _status_colors(val)
                    _draw_pill(draw, x, y + 8, (val or "-").upper(), f_status, bgp, fgp, pad_x=14, pad_y=6)
                else:
                    field_map = {
                        "Titulo": _to_text(r.get("titulo") or r.get("title") or r.get("nome")),
                        "Local": _to_text(r.get("local") or r.get("LOCAL")),
                        "Modelo": _to_text(r.get("modelo")),
                        "Switch": _to_text(r.get("switch_name") or r.get("switch") or r.get("switch_label")),
                        "Switch IP": _to_text(r.get("switch_ip")),
                        "Porta": _to_text(r.get("switch_port")),
                        "VLAN": _to_text(r.get("switch_vlan") or r.get("vlan")),
                        "PON": _to_text(r.get("pon") or r.get("PON")),
                        "ONU ID": _to_text(r.get("onu_id") or r.get("ONU_ID") or r.get("onuid")),
                        "ONU Name": _to_text(r.get("onu_name") or r.get("ONU_NAME")),
                        "ONU Serial": _to_text(r.get("onu_serial") or r.get("ONU_SERIAL")),
                    }
                    val = field_map.get(col_name, "")
                    fnt = f_h if col_name == "Titulo" else f
                    fill = INK if col_name == "Titulo" else INK_MUTED
                    draw.text((x, y + 15), _fit_text(draw, val, fnt, cw - 20), font=fnt, fill=fill)
                x += cw
            y += line_h
            idx += 1

        draw.rectangle((x0, row_top, x0 + w, y), outline=BORDER_SOFT, width=2)
        sink.add(page)
        if progress_cb:
            progress_cb(idx, total, "tabela")


def _draw_photo_pages(
    rows: List[Dict[str, Any]],
    sink: "_PageSink",
    site_label: str,
    company_name: str = "",
    logo_path: Optional[Path] = None,
    include_olt: bool = True,
    include_switch: bool = False,
    module_label: str = "Cameras IP",
    report_color: str = "",
    landscape: bool = False,
    progress_cb: ProgressCb = None,
) -> None:
    f_txt = _load_font(19, bold=False)
    f_txt_b = _load_font(19, bold=True)
    f_cap = _load_font(25, bold=True)
    f_ip = _load_mono_font(19, bold=False)
    f_status = _load_font(15, bold=True)
    f_note = _load_font(17, bold=False)

    color = _report_color(report_color)
    # Grade 3x5 (15 fotos/pagina): card dimensionado pro conteudo real (foto
    # + 2 linhas de info), sem sobra vazia embaixo como na versao 2x3 antiga.
    cols_n, rows_n = (4, 3) if landscape else (3, 5)
    gap_x, gap_y = 56, 40
    page_width = A4_H if landscape else A4_W
    page_height = A4_W if landscape else A4_H
    page_w = page_width - (2 * MARGIN_X)
    card_w = (page_w - gap_x * (cols_n - 1)) // cols_n
    card_h = 500 if landscape else 460
    x_start = MARGIN_X
    y_start_base = MARGIN_Y + 250
    per_page = cols_n * rows_n

    photo_rows = [r for r in rows if _pick_image_path(r) is not None]
    truncated = len(photo_rows) > MAX_PHOTOS
    photo_rows = photo_rows[:MAX_PHOTOS]
    idx = 0
    total = len(photo_rows)

    while idx < total or (total == 0 and idx == 0):
        page, draw = _new_page_landscape() if landscape else _new_page()
        y0 = _draw_header(
            page,
            draw,
            f"Galeria de snapshots | {module_label}",
            f"Gerado em {datetime.now().strftime('%d/%m/%Y as %H:%M')}  ·  Site: {site_label}  ·  {total} foto{'s' if total != 1 else ''}",
            company_name=company_name,
            logo_path=logo_path,
            report_color=report_color,
        )
        draw = ImageDraw.Draw(page)

        if total == 0:
            draw.text((MARGIN_X, y0), "Nenhuma foto disponivel para o recorte atual.", font=f_txt, fill=INK_MUTED)
            sink.add(page)
            if progress_cb:
                progress_cb(0, 0, "fotos")
            break

        for slot in range(per_page):
            if idx >= total:
                break
            col = slot % cols_n
            row = slot // cols_n
            x = x_start + col * (card_w + gap_x)
            y = y_start_base + row * (card_h + gap_y)

            # Sombra suave: retangulo levemente maior e deslocado, atras do card.
            shadow_off = 6
            draw.rounded_rectangle(
                (x + shadow_off, y + shadow_off, x + card_w + shadow_off, y + card_h + shadow_off),
                radius=16,
                fill=SHADOW,
            )
            draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=16, fill=CARD_BG, outline=BORDER, width=2)

            r = photo_rows[idx]
            title = _to_text(r.get("titulo") or r.get("title") or r.get("nome") or r.get("ip") or "Camera")
            ip = _to_text(r.get("ip") or r.get("IP"))
            local = _to_text(r.get("local") or r.get("LOCAL"))
            st = _to_text(r.get("status"))

            pad_x = 18
            head_y = y + 14
            draw.text((x + pad_x, head_y), _fit_text(draw, title, f_cap, card_w - (pad_x * 2)), font=f_cap, fill=INK)

            sub_y = head_y + 32
            bgp, fgp = _status_colors(st)
            pill_w = _draw_pill(draw, x + pad_x, sub_y, (st or "-").upper(), f_status, bgp, fgp, pad_x=10, pad_y=4)
            id_text = _fit_text(draw, ip or "-", f_ip, card_w - (pad_x * 2) - pill_w - 14)
            draw.text((x + pad_x + pill_w + 14, sub_y + 3), id_text, font=f_ip, fill=INK_MUTED)

            img_top = y + 66
            img_h = 330 if landscape else 300
            img_box = (x + pad_x, img_top, x + card_w - pad_x, img_top + img_h)
            draw.rounded_rectangle(img_box, radius=12, outline=BORDER, width=2, fill="#eef2f8")
            inner_pad = 8
            inner_box = (
                img_box[0] + inner_pad,
                img_box[1] + inner_pad,
                img_box[2] - inner_pad,
                img_box[3] - inner_pad,
            )
            p = _pick_image_path(r)
            try:
                if p is not None and p.exists():
                    im = Image.open(p).convert("RGB")
                    bw = inner_box[2] - inner_box[0]
                    bh = inner_box[3] - inner_box[1]
                    im.thumbnail((bw, bh))
                    ox = inner_box[0] + (bw - im.width) // 2
                    oy = inner_box[1] + (bh - im.height) // 2
                    page.paste(im, (ox, oy))
                    draw = ImageDraw.Draw(page)
                    del im
                else:
                    draw.text((inner_box[0] + 14, inner_box[1] + 14), "Sem snapshot", font=f_txt, fill=INK_MUTED)
            except Exception:
                draw.text((inner_box[0] + 14, inner_box[1] + 14), "Falha na imagem", font=f_txt, fill=STATUS_BAD_FG)

            info_y = img_top + img_h + 12
            info_bits: List[str] = []
            if local:
                info_bits.append(local)
            modelo = _to_text(r.get("modelo"))
            if modelo:
                info_bits.append(modelo)
            if info_bits:
                draw.text((x + pad_x, info_y), _fit_text(draw, "  ·  ".join(info_bits), f_txt, card_w - (pad_x * 2)), font=f_txt, fill=INK_MUTED)
                info_y += 26

            detail = ""
            if include_switch:
                detail = f"Switch {(_to_text(r.get('switch_name')) or '-')}  ·  Porta {(_to_text(r.get('switch_port')) or '-')}  ·  VLAN {(_to_text(r.get('switch_vlan') or r.get('vlan')) or '-')}"
            elif include_olt:
                detail = f"PON {(_to_text(r.get('pon') or r.get('PON')) or '-')}  ·  ONU {(_to_text(r.get('onu_id') or r.get('ONU_ID')) or '-')}  ·  SN {(_to_text(r.get('onu_serial') or r.get('ONU_SERIAL')) or '-')}"
            if detail:
                draw.text((x + pad_x, info_y), _fit_text(draw, detail, f_txt_b, card_w - (pad_x * 2)), font=f_txt_b, fill=INK)

            idx += 1
            if progress_cb:
                progress_cb(idx, total, "fotos")

        note = f"Galeria limitada as primeiras {MAX_PHOTOS} fotos do recorte atual." if truncated else ""
        if note:
            draw.text((MARGIN_X, page_height - MARGIN_Y - 14), note, font=_load_font(19, False), fill=INK_MUTED)
        sink.add(page)


def build_inventory_pdf_report(
    rows: Iterable[Dict[str, Any]],
    site: str = "",
    company_name: str = "",
    logo_path: Optional[Path] = None,
    include_olt: bool = True,
    include_switch: bool = False,
    module_label: str = "Cameras IP",
    report_color: str = "",
    include_photos: bool = True,
    progress_cb: ProgressCb = None,
) -> Path:
    rows_list = [dict(r) for r in rows if isinstance(r, dict)]
    rows_list = _sort_inventory_rows(rows_list)
    site_label = _to_text(site) or "Todos os sites"

    reports_dir = OUTPUT_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname_site = site_label.replace(" ", "_").replace("/", "_")
    out = reports_dir / f"inventory-report-{fname_site}-{ts}.pdf"
    tmp_dir = reports_dir / f".tmp-{ts}-{os.getpid()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        sink = _PageSink(tmp_dir)
        if progress_cb:
            progress_cb(0, len(rows_list), "tabela")
        _draw_table_pages(
            rows_list,
            sink,
            site_label,
            company_name=company_name,
            logo_path=logo_path,
            include_olt=include_olt,
            include_switch=include_switch,
            module_label=module_label,
            report_color=report_color,
            progress_cb=progress_cb,
        )
        if include_photos:
            _draw_photo_pages(
                rows_list,
                sink,
                site_label,
                company_name=company_name,
                logo_path=logo_path,
                include_olt=include_olt,
                include_switch=include_switch,
                module_label=module_label,
                report_color=report_color,
                progress_cb=progress_cb,
            )

        total_pages = len(sink)
        note = f"{len(rows_list)} camera{'s' if len(rows_list) != 1 else ''} no total"
        if progress_cb:
            progress_cb(0, total_pages, "finalizando")
        # Reabre cada pagina do disco uma de cada vez so pra carimbar o
        # rodape (precisa saber o total de paginas, que so existe depois
        # que todas ja foram desenhadas) -- ainda uma pagina por vez em
        # memoria, nunca o documento inteiro.
        for i, p in enumerate(sink.paths, start=1):
            img = Image.open(p)
            img.load()
            draw = ImageDraw.Draw(img)
            _draw_footer(draw, i, total_pages, note=note)
            img.save(p, "JPEG", quality=88)
            img.close()
            if progress_cb:
                progress_cb(i, total_pages, "finalizando")

        # Monta o PDF final lendo cada pagina do disco sob demanda: os
        # objetos abaixo ficam "preguicosos" (Image.open sem .load()), entao
        # o Pillow decodifica uma pagina por vez durante o save, em vez de
        # manter o documento inteiro rasterizado na RAM ao mesmo tempo --
        # e o que permite um relatorio com milhares de paginas.
        imgs = [Image.open(p) for p in sink.paths]
        first, rest = imgs[0], imgs[1:]
        first.save(out, "PDF", save_all=True, append_images=rest, resolution=200.0)
        for im in imgs:
            im.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return out


def _recorder_host_text(row: Dict[str, Any]) -> str:
    host = _to_text(row.get("host") or row.get("recorder_host") or row.get("ip"))
    if ":" in host:
        host = host.split(":", 1)[0].strip()
    return host


def _recorder_host_sort_key(row: Dict[str, Any]) -> Tuple[int, int]:
    host = _recorder_host_text(row)
    if not host:
        return (1, 2**32 - 1)
    try:
        return (0, int(ipaddress.ip_address(host)))
    except Exception:
        return (1, 2**32 - 1)


def _sort_recorder_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        rows,
        key=lambda r: (
            _recorder_host_sort_key(r),
            int(r.get("channel") or 0),
            _to_text(r.get("title") or r.get("titulo")).lower(),
        ),
    )


def _first_text(rows: List[Dict[str, Any]], *keys: str) -> str:
    for row in rows:
        for key in keys:
            val = _to_text(row.get(key))
            if val:
                return val
    return ""


def _yes_no(value: Any) -> str:
    if isinstance(value, bool):
        return "sim" if value else "nao"
    txt = _to_text(value)
    if not txt:
        return "nao informado"
    return txt


def _recorder_groups(rows: List[Dict[str, Any]]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        host = _recorder_host_text(row) or "Sem host"
        groups.setdefault(host, []).append(row)
    ordered = []
    for host, items in groups.items():
        ordered.append((host, _sort_recorder_rows(items)))
    ordered.sort(key=lambda item: _recorder_host_sort_key({"host": item[0]}))
    return ordered


def _recorder_channel_status(row: Dict[str, Any]) -> str:
    status = _to_text(row.get("status")).lower()
    if row.get("video_loss"):
        return "video loss"
    if status in ("online", "ok"):
        return "online"
    if status in ("sem_camera", "no_camera"):
        return "sem camera"
    if status in ("camera_offline", "offline", "auth_failed"):
        return "offline"
    return status or "nao informado"


def _recorder_channel_empty(row: Dict[str, Any]) -> bool:
    status = _recorder_channel_status(row)
    if status in ("sem camera", "no_camera"):
        return True
    if status in ("online", "offline", "video loss"):
        return False
    title = _to_text(row.get("title") or row.get("titulo")).strip().lower()
    default_title = bool(re.fullmatch(r"(?:ch\s*)?\d*\s*-?\s*canal\s*\d*", title) or re.fullmatch(r"canal\s*\d*", title))
    if _to_text(row.get("camera_ip")) or _to_text(row.get("camera_model") or row.get("modelo")) or _to_text(row.get("camera_mac") or row.get("mac")):
        return False
    if title and not default_title:
        return False
    return not _recorder_photo_available(row)


def _recorder_channel_in_use(row: Dict[str, Any]) -> bool:
    return _recorder_channel_status(row) == "online"


def _recorder_channel_offline(row: Dict[str, Any]) -> bool:
    return (not _recorder_channel_empty(row)) and _recorder_channel_status(row) in ("offline", "video loss")


def _recorder_recording_text(row: Dict[str, Any]) -> str:
    for key in ("recording", "is_recording", "gravando"):
        if key in row:
            val = row.get(key)
            if isinstance(val, bool):
                return "sim" if val else "nao"
            parsed = str(val or "").strip().lower()
            if parsed in ("true", "1", "yes", "sim", "ok", "gravando", "recording", "active"):
                return "sim"
            if parsed in ("false", "0", "no", "nao", "parado", "stopped", "idle", "sem gravacao recente"):
                return "nao"
    status = _to_text(row.get("recording_status")).lower()
    if status:
        # "nao confirmado pelo playback": online mas o playback nao provou gravacao.
        # Tem que vir ANTES dos tokens de "sim", senao o fallback afirmava gravacao.
        if "nao confirmado" in status or "not confirmed" in status:
            return "n/c"
        if any(token in status for token in ("recording", "gravando", "playback encontrado", "active", "normal", "configurado")):
            return "sim"
        if any(token in status for token in ("stopped", "parado", "idle", "sem playback", "sem gravacao", "disabled", "disable", "sem camera")):
            return "nao"
    # Sem status conhecido: nao afirma "sim" so por estar online -> deixa indeterminado.
    return "n/c" if _recorder_channel_status(row) == "online" else "nao"


def _recorder_recording_known(row: Dict[str, Any]) -> bool:
    return _recorder_recording_text(row).lower() in ("sim", "nao")


def _clean_platform_label(value: Any) -> str:
    # O backend as vezes anexa "(true)"/"(false)" ao status da plataforma.
    txt = _to_text(value)
    txt = re.sub(r"\s*\((?:true|false|1|0)\)\s*$", "", txt, flags=re.IGNORECASE)
    return txt.strip()


def _recorder_photo_available(row: Dict[str, Any]) -> bool:
    if _pick_image_path(row) is not None:
        return True
    r = dict(row)
    host = _recorder_host_text(r)
    r["ip"] = _to_text(r.get("camera_ip")) or host
    r["modelo"] = _to_text(r.get("camera_model") or r.get("modelo"))
    r["mac"] = _to_text(r.get("camera_mac") or r.get("mac"))
    return _pick_image_path(r) is not None


def _draw_kpi_card(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    label: str,
    value: str,
    color: str,
) -> None:
    f_label = _load_font(18, bold=True)
    f_val = _load_font(34, bold=True)
    draw.rounded_rectangle((x, y, x + w, y + h), radius=18, fill=CARD_BG, outline=BORDER_SOFT, width=2)
    draw.text((x + 22, y + 18), label.upper(), font=f_label, fill=INK_MUTED)
    draw.text((x + 22, y + 52), _fit_text(draw, value, f_val, w - 44), font=f_val, fill=color)


def _draw_recorder_overview_pages(
    rows: List[Dict[str, Any]],
    sink: "_PageSink",
    site_label: str,
    company_name: str = "",
    logo_path: Optional[Path] = None,
    recorder_type: str = "nvr",
    report_color: str = "",
    progress_cb: ProgressCb = None,
) -> None:
    groups = _recorder_groups(rows)
    label = "NVR" if recorder_type == "nvr" else "DVR"
    total = len(rows)
    online = sum(1 for r in rows if _recorder_channel_status(r) == "online")
    offline = sum(1 for r in rows if _recorder_channel_offline(r))
    vloss = sum(1 for r in rows if bool(r.get("video_loss")) or _recorder_channel_status(r) == "video loss")
    photos = sum(1 for r in rows if _recorder_photo_available(r))
    in_use = sum(1 for r in rows if _recorder_channel_in_use(r))
    no_camera = sum(1 for r in rows if _recorder_channel_empty(r))
    recording_total = sum(1 for r in rows if _recorder_recording_text(r) == "sim")
    recording_known = sum(1 for r in rows if _recorder_recording_known(r))

    page, draw = _new_page_landscape()
    page_w, page_h = page.size
    y = _draw_header(
        page,
        draw,
        f"Relatorio tecnico | Gravadores {label}",
        f"Gerado em {datetime.now().strftime('%d/%m/%Y as %H:%M')}  ·  Site: {site_label}  ·  {len(groups)} gravador{'es' if len(groups) != 1 else ''}",
        company_name=company_name,
        logo_path=logo_path,
        report_color=report_color,
    )
    draw = ImageDraw.Draw(page)
    color = _report_color(report_color)
    card_gap = 26
    card_w = (page_w - 2 * MARGIN_X - 3 * card_gap) // 4
    card_h = 118
    _draw_kpi_card(draw, MARGIN_X, y, card_w, card_h, "Canais", str(total), color)
    _draw_kpi_card(draw, MARGIN_X + (card_w + card_gap), y, card_w, card_h, "Em uso", str(in_use), STATUS_OK_FG)
    _draw_kpi_card(draw, MARGIN_X + 2 * (card_w + card_gap), y, card_w, card_h, "Offline", str(offline), STATUS_BAD_FG)
    _draw_kpi_card(draw, MARGIN_X + 3 * (card_w + card_gap), y, card_w, card_h, "Vazios", str(no_camera), color)
    y += card_h + 42

    draw.text((MARGIN_X, y), "Resumo dos gravadores", font=_load_font(30, bold=True), fill=INK)
    y += 48

    f_title = _load_font(25, bold=True)
    f = _load_font(21, bold=False)
    f_b = _load_font(21, bold=True)
    f_mono = _load_mono_font(20, bold=False)
    card_h2 = 220
    for idx, (host, items) in enumerate(groups):
        if y + card_h2 > page_h - MARGIN_Y - 80:
            sink.add(page)
            page, draw = _new_page_landscape()
            page_w, page_h = page.size
            y = _draw_header(
                page,
                draw,
                f"Resumo tecnico | Gravadores {label}",
                f"Site: {site_label}  ·  continuacao",
                company_name=company_name,
                logo_path=logo_path,
                report_color=report_color,
            )
            draw = ImageDraw.Draw(page)
            draw.text((MARGIN_X, y), "Resumo dos gravadores", font=_load_font(30, bold=True), fill=INK)
            y += 48

        x = MARGIN_X
        w = page_w - 2 * MARGIN_X
        draw.rounded_rectangle((x, y, x + w, y + card_h2), radius=18, fill=CARD_BG, outline=BORDER, width=2)
        draw.text((x + 24, y + 20), f"{label} {host}", font=f_title, fill=INK)
        model = _first_text(items, "nvr_model", "recorder_model", "modelo", "model")
        serial = _first_text(items, "equip_serial", "serial", "serial_number")
        local = _first_text(items, "local", "site", "site_name")
        # MAC do gravador: nunca cai no MAC da camera do canal (rotulo diz NVR).
        mac = _first_text(items, "nvr_mac")
        status_ok = sum(1 for r in items if _recorder_channel_status(r) == "online")
        status_bad = sum(1 for r in items if _recorder_channel_offline(r))
        rec_sim = sum(1 for r in items if _recorder_recording_text(r) == "sim")
        rec_nao = sum(1 for r in items if _recorder_recording_text(r) == "nao")
        rec_nc = max(0, len(items) - rec_sim - rec_nao)
        rec_known = sum(1 for r in items if _recorder_recording_known(r))
        rec_unknown = len(items) - rec_known
        used_count = sum(1 for r in items if _recorder_channel_in_use(r))
        no_camera_count = sum(1 for r in items if _recorder_channel_empty(r))

        col1 = x + 24
        col2 = x + 1130
        col3 = x + 2230
        row_y = y + 68
        # hdd_status e network_status ja vem como resumo pronto do backend
        # (ex.: "normal - 10.9 TB - 100% usado"). Nao recompor por cima, senao duplica.
        hdd_count = _first_text(items, "hdd_count")
        hdd = _first_text(items, "hdd_status", "disk_status", "storage_status") or _first_text(items, "hdd_total", "disk_total", "storage_total")
        if hdd and hdd_count and "disco" not in hdd.lower():
            hdd = f"{hdd_count} disco(s) - {hdd}"
        retention = _first_text(items, "recording_days", "retention_days", "retention")
        platform = _clean_platform_label(_first_text(items, "hik_connect_status", "p2p_status", "platform_status", "cloud_status"))
        nvr_ip = _first_text(items, "nvr_ip")
        nvr_gw = _first_text(items, "nvr_gateway", "gateway")
        network = _first_text(items, "network_status") or nvr_ip
        pending = []
        if not hdd:
            pending.append("HD")
        if not retention:
            pending.append("retencao")
        if rec_unknown:
            pending.append("gravacao")
        if not platform:
            pending.append("plataforma")
        if not network:
            pending.append("rede")
        pairs = [
            (col1, row_y, "Modelo", model or "-", f),
            (col1, row_y + 34, "Serial", serial or "-", f_mono),
            (col1, row_y + 68, "Local", local or "-", f),
            (col1, row_y + 102, "MAC NVR", mac or "-", f_mono),
            (col2, row_y, "Canais", f"{len(items)} total - {used_count} em uso - {status_bad} offline - {no_camera_count} vazios", f),
            (col2, row_y + 34, "Video loss", str(sum(1 for r in items if bool(r.get('video_loss')))), f),
            (col2, row_y + 68, "Fotos", f"{sum(1 for r in items if _recorder_photo_available(r) and not _recorder_channel_empty(r))} com imagem", f),
            (col2, row_y + 102, "Gravacao", f"{rec_sim} sim - {rec_nao} nao - {rec_nc} n/c", f),
            (col3, row_y, "HD", hdd or "pendente de coleta", f),
            (col3, row_y + 34, "Rede", network or "pendente de coleta", f_mono if (nvr_ip or nvr_gw) else f),
            (col3, row_y + 68, "Plataforma", platform or "pendente de coleta", f),
            (col3, row_y + 102, "Retencao", retention or "pendente de coleta", f),
        ]
        for px, py, k, v, font_value in pairs:
            draw.text((px, py), f"{k}: ", font=f_b, fill=INK)
            kw = int(draw.textlength(f"{k}: ", font=f_b))
            draw.text((px + kw, py), _fit_text(draw, v, font_value, 930 - kw), font=font_value, fill=INK_MUTED)
        y += card_h2 + 22
        if progress_cb:
            progress_cb(idx + 1, max(1, len(groups)), "resumo")

    if not groups:
        draw.text((MARGIN_X, y), "Nenhum gravador encontrado para o filtro atual.", font=f, fill=INK_MUTED)
    sink.add(page)


def _draw_recorder_channel_table_pages(
    rows: List[Dict[str, Any]],
    sink: "_PageSink",
    site_label: str,
    company_name: str = "",
    logo_path: Optional[Path] = None,
    recorder_type: str = "nvr",
    report_color: str = "",
    progress_cb: ProgressCb = None,
) -> None:
    label = "NVR" if recorder_type == "nvr" else "DVR"
    cols = [
        ("HOST", 245),
        ("CH", 70),
        ("TITULO", 690),
        ("STATUS", 150),
        ("REC", 90),
        ("LOCAL", 260),
        ("IP CAM", 245),
        ("MODELO", 350),
        ("MAC CAM", 340),
        ("FOTO", 95),
        ("ALERTAS", 733),
    ]
    f_h = _load_font(24, bold=True)
    f = _load_font(25, bold=False)
    f_b = _load_font(25, bold=True)
    f_mono = _load_mono_font(24, bold=False)
    f_status = _load_font(21, bold=True)
    line_h = 88
    header_h = 64
    idx = 0
    total = len(rows)
    color = _report_color(report_color)
    while idx < total or (total == 0 and idx == 0):
        page, draw = _new_page_landscape()
        page_w, page_h = page.size
        y = _draw_header(
            page,
            draw,
            f"Canais e cameras | Gravadores {label}",
            f"Site: {site_label}  ·  {total} canal{'is' if total != 1 else ''}",
            company_name=company_name,
            logo_path=logo_path,
            report_color=report_color,
        )
        draw = ImageDraw.Draw(page)
        x0 = MARGIN_X
        w = page_w - 2 * MARGIN_X
        draw.rounded_rectangle((x0, y, x0 + w, y + header_h), radius=12, fill=color)
        x = x0
        for name, cw in cols:
            draw.text((x + 10, y + 18), _fit_text(draw, name, f_h, cw - 20), font=f_h, fill="#ffffff")
            x += cw
        y += header_h + 6
        row_top = y
        if total == 0:
            draw.rounded_rectangle((x0, y, x0 + w, y + 90), radius=12, fill=CARD_BG, outline=BORDER_SOFT, width=2)
            draw.text((x0 + 24, y + 32), "Nenhum canal encontrado para o filtro atual.", font=f, fill=INK_MUTED)
            sink.add(page)
            break
        while idx < total and y + line_h < page_h - MARGIN_Y - 30:
            r = rows[idx]
            draw.rectangle((x0, y, x0 + w, y + line_h), fill=CARD_BG if idx % 2 == 0 else ROW_STRIPE)
            alerts: List[str] = []
            st = _recorder_channel_status(r)
            photo_ok = _recorder_photo_available(r)
            if st != "online":
                alerts.append(st)
            if not photo_ok:
                alerts.append("sem foto")
            rec_display = _recorder_recording_text(r)
            rec_key = rec_display.lower()
            if rec_key == "nao":
                alerts.append("sem gravacao")
            if recorder_type == "nvr" and not _to_text(r.get("camera_ip")):
                alerts.append("sem IP cam")
            values = [
                (_recorder_host_text(r), f_mono, INK),
                (_to_text(r.get("channel")), f_mono, INK),
                (_to_text(r.get("title") or r.get("titulo")), f_b, INK),
                (st.upper(), f_status, STATUS_OK_FG if st == "online" else STATUS_BAD_FG),
                (rec_display, f_status, STATUS_OK_FG if rec_key == "sim" else (STATUS_BAD_FG if rec_key == "nao" else INK_MUTED)),
                (_to_text(r.get("local")), f, INK_MUTED),
                (_to_text(r.get("camera_ip")) if recorder_type == "nvr" else "analogico", f_mono, INK_MUTED),
                (_to_text(r.get("camera_model") or r.get("modelo")), f, INK_MUTED),
                (_to_text(r.get("camera_mac") or r.get("mac")), f_mono, INK_MUTED),
                ("sim" if photo_ok else "nao", f, INK_MUTED),
                ("; ".join(alerts) if alerts else "ok", f, INK_MUTED),
            ]
            x = x0
            for (text, font, fill), (_, cw) in zip(values, cols):
                draw.text((x + 10, y + 30), _fit_text(draw, text, font, cw - 20), font=font, fill=fill)
                x += cw
            y += line_h
            idx += 1
        draw.rectangle((x0, row_top, x0 + w, y), outline=BORDER_SOFT, width=2)
        sink.add(page)
        if progress_cb:
            progress_cb(idx, total, "tabela")


def build_recorder_pdf_report(
    rows: Iterable[Dict[str, Any]],
    site: str = "",
    company_name: str = "",
    logo_path: Optional[Path] = None,
    recorder_type: str = "nvr",
    module_label: str = "",
    report_color: str = "",
    include_photos: bool = True,
    progress_cb: ProgressCb = None,
) -> Path:
    rows_list = _sort_recorder_rows([dict(r) for r in rows if isinstance(r, dict)])
    site_label = _to_text(site) or "Todos os sites"
    rec_label = (module_label or ("Gravadores NVR" if recorder_type == "nvr" else "Gravadores DVR")).strip()

    reports_dir = OUTPUT_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname_site = site_label.replace(" ", "_").replace("/", "_")
    out = reports_dir / f"recorder-report-{recorder_type}-{fname_site}-{ts}.pdf"
    tmp_dir = reports_dir / f".tmp-rec-{ts}-{os.getpid()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        sink = _PageSink(tmp_dir)
        if progress_cb:
            progress_cb(0, len(rows_list), "resumo")
        _draw_recorder_overview_pages(
            rows_list,
            sink,
            site_label,
            company_name=company_name,
            logo_path=logo_path,
            recorder_type=recorder_type,
            report_color=report_color,
            progress_cb=progress_cb,
        )
        _draw_recorder_channel_table_pages(
            rows_list,
            sink,
            site_label,
            company_name=company_name,
            logo_path=logo_path,
            recorder_type=recorder_type,
            report_color=report_color,
            progress_cb=progress_cb,
        )
        if include_photos:
            photo_rows = []
            for row in rows_list:
                # Canal vazio/sem camera: o NVR devolve um frame generico reaproveitado
                # (mesma imagem em todos). Nao entra na galeria -> "snapshot que nao existe".
                if _recorder_channel_empty(row):
                    continue
                r = dict(row)
                host = _recorder_host_text(r)
                ch = int(r.get("channel") or 0)
                r["titulo"] = f"{host} CH{ch:02d} - {_to_text(r.get('title') or r.get('titulo'))}"
                r["ip"] = _to_text(r.get("camera_ip")) or host
                r["modelo"] = _to_text(r.get("camera_model") or r.get("modelo"))
                r["mac"] = _to_text(r.get("camera_mac") or r.get("mac"))
                photo_rows.append(r)
            _draw_photo_pages(
                photo_rows,
                sink,
                site_label,
                company_name=company_name,
                logo_path=logo_path,
                include_olt=False,
                include_switch=False,
                module_label=rec_label,
                report_color=report_color,
                landscape=True,
                progress_cb=progress_cb,
            )

        total_pages = len(sink)
        note = f"{len(_recorder_groups(rows_list))} gravador(es) · {len(rows_list)} canal(is)"
        if progress_cb:
            progress_cb(0, total_pages, "finalizando")
        for i, p in enumerate(sink.paths, start=1):
            img = Image.open(p)
            img.load()
            draw = ImageDraw.Draw(img)
            _draw_footer(draw, i, total_pages, note=note)
            img.save(p, "JPEG", quality=88)
            img.close()
            if progress_cb:
                progress_cb(i, total_pages, "finalizando")
        imgs = [Image.open(p) for p in sink.paths]
        first, rest = imgs[0], imgs[1:]
        first.save(out, "PDF", save_all=True, append_images=rest, resolution=200.0)
        for im in imgs:
            im.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return out


def build_inventory_preview_image(
    rows: Iterable[Dict[str, Any]],
    site: str = "",
    company_name: str = "",
    logo_path: Optional[Path] = None,
    include_olt: bool = True,
    include_switch: bool = False,
    module_label: str = "Cameras IP",
    report_color: str = "",
) -> Path:
    rows_list = [dict(r) for r in rows if isinstance(r, dict)]
    rows_list = _sort_inventory_rows(rows_list)
    site_label = _to_text(site) or "Todos os sites"

    # Preview leve: somente primeira pagina de tabela com limite de linhas.
    page, draw = _new_page()
    y = _draw_header(
        page,
        draw,
        f"Preview do relatorio | {module_label}",
        f"Gerado em {datetime.now().strftime('%d/%m/%Y as %H:%M')}  ·  Site: {site_label}",
        company_name=company_name,
        logo_path=logo_path,
        report_color=report_color,
    )
    draw = ImageDraw.Draw(page)
    draw.text((MARGIN_X, y), "Inventario detalhado (preview)", font=_load_font(28, bold=True), fill=INK)
    y += 50

    color = _report_color(report_color)
    f_h = _load_font(23, bold=True)
    f = _load_font(23, bold=False)
    f_mono = _load_mono_font(22, bold=False)
    f_status = _load_font(19, bold=True)
    cols = _column_set(include_switch, include_olt)
    line_h = 54
    header_h = 58
    x0 = MARGIN_X
    w = A4_W - (2 * MARGIN_X)
    draw.rounded_rectangle((x0, y, x0 + w, y + header_h), radius=12, fill=color)
    x = x0 + 20
    for name, cw in cols:
        draw.text((x, y + 17), _fit_text(draw, name.upper(), f_h, cw - 18), font=f_h, fill="#ffffff")
        x += cw
    y += header_h + 6

    max_rows = min(26, len(rows_list))
    row_top = y
    if max_rows == 0:
        draw.rounded_rectangle((x0, y, x0 + w, y + 90), radius=12, fill=CARD_BG, outline=BORDER_SOFT, width=2)
        draw.text((x0 + 24, y + 32), "Nenhuma camera encontrada para o filtro atual.", font=f, fill=INK_MUTED)
    else:
        for idx in range(max_rows):
            r = rows_list[idx]
            bg = CARD_BG if (idx % 2 == 0) else ROW_STRIPE
            draw.rectangle((x0, y, x0 + w, y + line_h), fill=bg)
            x = x0 + 20
            for col_name, cw in cols:
                if col_name == "IP":
                    val = _to_text(r.get("ip") or r.get("IP"))
                    draw.text((x, y + 15), _fit_text(draw, val, f_mono, cw - 20), font=f_mono, fill=INK)
                elif col_name == "MAC":
                    val = _to_text(r.get("mac") or r.get("MAC"))
                    draw.text((x, y + 16), _fit_text(draw, val, f_mono, cw - 20), font=f_mono, fill=INK_MUTED)
                elif col_name == "Status":
                    val = _to_text(r.get("status"))
                    bgp, fgp = _status_colors(val)
                    _draw_pill(draw, x, y + 8, (val or "-").upper(), f_status, bgp, fgp, pad_x=14, pad_y=6)
                else:
                    field_map = {
                        "Titulo": _to_text(r.get("titulo") or r.get("title") or r.get("nome")),
                        "Local": _to_text(r.get("local") or r.get("LOCAL")),
                        "Modelo": _to_text(r.get("modelo")),
                        "Switch": _to_text(r.get("switch_name") or r.get("switch") or r.get("switch_label")),
                        "Switch IP": _to_text(r.get("switch_ip")),
                        "Porta": _to_text(r.get("switch_port")),
                        "VLAN": _to_text(r.get("switch_vlan") or r.get("vlan")),
                        "PON": _to_text(r.get("pon") or r.get("PON")),
                        "ONU ID": _to_text(r.get("onu_id") or r.get("ONU_ID") or r.get("onuid")),
                        "ONU Name": _to_text(r.get("onu_name") or r.get("ONU_NAME")),
                        "ONU Serial": _to_text(r.get("onu_serial") or r.get("ONU_SERIAL")),
                    }
                    val = field_map.get(col_name, "")
                    fnt = f_h if col_name == "Titulo" else f
                    fill = INK if col_name == "Titulo" else INK_MUTED
                    draw.text((x, y + 15), _fit_text(draw, val, fnt, cw - 20), font=fnt, fill=fill)
                x += cw
            y += line_h
        draw.rectangle((x0, row_top, x0 + w, y), outline=BORDER_SOFT, width=2)

    _draw_footer(draw, 1, 1, note=f"Preview rapido · {len(rows_list)} camera{'s' if len(rows_list) != 1 else ''} no total")

    # Reduz resolucao para carregar rapido no browser
    preview = page.resize((1240, 1754))
    reports_dir = OUTPUT_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / "inventory-report-preview.jpg"
    preview.save(out, "JPEG", quality=78, optimize=True)
    return out
