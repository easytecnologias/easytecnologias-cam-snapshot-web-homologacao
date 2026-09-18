"""Relatorio de gravadores (NVR/DVR) em PDF com TEXTO REAL (reportlab).

Substitui o relatorio antigo que era montado como imagem (PIL) -- aquele nao
deixava selecionar/copiar texto e desperdicava pagina inteira quando havia
poucos gravadores. Aqui o texto e selecionavel/copiavel (da pra colar a tabela
no Excel) e as paginas acompanham o conteudo.

Reaproveita toda a logica de dados (status do canal, gravacao, fotos, etc.) do
modulo pdf_inventory_report -- so a renderizacao muda.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    CondPageBreak,
    Image as RLImage,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from xml.sax.saxutils import escape

from app.core.paths import OUTPUT_DIR
from app.services.pdf_inventory_report import (
    _clean_platform_label,
    _first_text,
    _pick_image_path,
    _recorder_channel_empty,
    _recorder_channel_in_use,
    _recorder_channel_offline,
    _recorder_channel_status,
    _recorder_groups,
    _recorder_host_text,
    _recorder_photo_available,
    _recorder_recording_known,
    _recorder_recording_text,
    _sort_recorder_rows,
    _to_text,
)

ProgressCb = Optional[Callable[[int, int, str], None]]

INK = colors.HexColor("#17232B")
MUTED = colors.HexColor("#667782")
BORDER = colors.HexColor("#D8E1E5")
SOFT = colors.HexColor("#F4F7F8")
STRIPE = colors.HexColor("#FBFCFC")
OK = colors.HexColor("#0D7A3F")
BAD = colors.HexColor("#B3261E")


def _accent(value: str = "") -> colors.Color:
    raw = _to_text(value)
    if raw and not raw.startswith("#"):
        raw = "#" + raw
    try:
        return colors.HexColor(raw)
    except Exception:
        return colors.HexColor("#0b2242")


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("Title", parent=base["Title"], fontName="Helvetica-Bold", fontSize=17,
                                 leading=20, textColor=INK, alignment=TA_LEFT, spaceAfter=2),
        "subtitle": ParagraphStyle("Subtitle", parent=base["Normal"], fontName="Helvetica", fontSize=8.5,
                                   leading=11, textColor=MUTED, spaceAfter=8),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=11,
                             leading=14, textColor=INK, spaceBefore=8, spaceAfter=5),
        "kpi_label": ParagraphStyle("KpiLabel", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=6.7,
                                    leading=8, textColor=MUTED),
        "kpi_value": ParagraphStyle("KpiValue", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=16,
                                    leading=18, textColor=INK),
        "small": ParagraphStyle("Small", parent=base["BodyText"], fontName="Helvetica", fontSize=7.4,
                                leading=9.5, textColor=INK),
        "small_b": ParagraphStyle("SmallB", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=7.4,
                                  leading=9.5, textColor=INK),
        "mono": ParagraphStyle("Mono", parent=base["BodyText"], fontName="Courier", fontSize=7.2,
                               leading=9.5, textColor=INK),
        "th": ParagraphStyle("TH", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=6.6,
                             leading=8, textColor=colors.white),
        "kv_k": ParagraphStyle("KvK", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=7.6,
                               leading=10, textColor=MUTED),
        "kv_v": ParagraphStyle("KvV", parent=base["Normal"], fontName="Helvetica", fontSize=7.8,
                               leading=10, textColor=INK),
        "cap": ParagraphStyle("Cap", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=7.2,
                              leading=9, textColor=INK),
    }


def _p(value: Any, style: ParagraphStyle, fallback: str = "-", color: Optional[colors.Color] = None) -> Paragraph:
    txt = escape(_to_text(value) or fallback)
    if color is not None:
        style = ParagraphStyle("c", parent=style, textColor=color)
    return Paragraph(txt, style)


def _header_footer_factory(title: str, accent: colors.Color, subtitle_right: str):
    def _draw(canvas, doc):
        canvas.saveState()
        width, height = landscape(A4)
        canvas.setFillColor(accent)
        canvas.rect(0, height - 4 * mm, width, 4 * mm, fill=1, stroke=0)
        canvas.setStrokeColor(BORDER)
        canvas.line(14 * mm, height - 14 * mm, width - 14 * mm, height - 14 * mm)
        canvas.setFont("Helvetica-Bold", 7.5)
        canvas.setFillColor(accent)
        canvas.drawString(14 * mm, height - 11 * mm, "SIGHTOPS  -  RELATORIO DE GRAVADORES")
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(width - 14 * mm, height - 11 * mm, subtitle_right[:90])
        canvas.line(14 * mm, 11 * mm, width - 14 * mm, 11 * mm)
        canvas.drawString(14 * mm, 7.4 * mm, title[:90])
        canvas.drawRightString(width - 14 * mm, 7.4 * mm, f"Pagina {doc.page}")
        canvas.restoreState()
    return _draw


def _kpis(values: List[tuple], styles: Dict[str, ParagraphStyle], usable_w: float) -> Table:
    cells = [[_p(lbl.upper(), styles["kpi_label"]), _p(val, styles["kpi_value"], color=col)]
             for lbl, val, col in values]
    col_w = usable_w / len(cells)
    table = Table([cells], colWidths=[col_w] * len(cells), rowHeights=[16 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SOFT),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _kv_table(pairs: List[tuple], styles: Dict[str, ParagraphStyle], usable_w: float) -> Table:
    """Grade de 3 colunas de pares chave/valor (resumo do gravador)."""
    rows: List[List[Any]] = []
    per_col = (len(pairs) + 2) // 3
    columns = [pairs[i:i + per_col] for i in range(0, len(pairs), per_col)]
    while len(columns) < 3:
        columns.append([])
    height = max(len(c) for c in columns) or 1
    for r in range(height):
        row = []
        for c in range(3):
            if r < len(columns[c]):
                k, v = columns[c][r]
                row.append([_p(k, styles["kv_k"]), _p(v, styles["kv_v"])])
            else:
                row.append("")
        rows.append(row)
    col_w = usable_w / 3
    table = Table(rows, colWidths=[col_w] * 3)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def _channel_table(rows: List[Dict[str, Any]], styles: Dict[str, ParagraphStyle],
                   accent: colors.Color, recorder_type: str, usable_w: float) -> LongTable:
    headers = ["HOST", "CH", "TITULO", "STATUS", "REC", "LOCAL", "IP CAM", "MODELO", "MAC", "FOTO", "ALERTAS"]
    weights = [22, 8, 44, 17, 11, 24, 21, 31, 34, 9, 33]
    total = sum(weights)
    widths = [usable_w * w / total for w in weights]
    data: List[List[Any]] = [[_p(h, styles["th"]) for h in headers]]
    for r in rows:
        st = _recorder_channel_status(r)
        photo_ok = _recorder_photo_available(r)
        rec = _recorder_recording_text(r)
        rk = rec.lower()
        alerts: List[str] = []
        if st != "online":
            alerts.append(st)
        if not photo_ok:
            alerts.append("sem foto")
        if rk == "nao":
            alerts.append("sem gravacao")
        if recorder_type == "nvr" and not _to_text(r.get("camera_ip")):
            alerts.append("sem IP cam")
        st_color = OK if st == "online" else BAD
        rec_color = OK if rk == "sim" else (BAD if rk == "nao" else MUTED)
        ip_cam = _to_text(r.get("camera_ip")) if recorder_type == "nvr" else "analogico"
        data.append([
            _p(_recorder_host_text(r), styles["mono"]),
            _p(r.get("channel"), styles["mono"]),
            _p(r.get("title") or r.get("titulo"), styles["small_b"]),
            _p(st.upper(), styles["small_b"], color=st_color),
            _p(rec, styles["small_b"], color=rec_color),
            _p(r.get("local"), styles["small"]),
            _p(ip_cam, styles["mono"]),
            _p(r.get("camera_model") or r.get("modelo"), styles["small"]),
            _p(r.get("camera_mac") or r.get("mac"), styles["mono"]),
            _p("sim" if photo_ok else "nao", styles["small"]),
            _p("; ".join(alerts) if alerts else "ok", styles["small"]),
        ])
    table = LongTable(data, colWidths=widths, repeatRows=1, splitByRow=1, hAlign="LEFT")
    cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, BORDER),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            cmds.append(("BACKGROUND", (0, i), (-1, i), STRIPE))
    table.setStyle(TableStyle(cmds))
    return table


def _photo_flowables(rows: List[Dict[str, Any]], styles: Dict[str, ParagraphStyle], usable_w: float) -> List[Any]:
    cards: List[Any] = []
    cols = 4
    cell_w = usable_w / cols
    img_w = cell_w - 8
    for r in rows:
        if _recorder_channel_empty(r):
            continue
        rr = dict(r)
        rr["ip"] = _to_text(rr.get("camera_ip")) or _recorder_host_text(rr)
        rr["modelo"] = _to_text(rr.get("camera_model") or rr.get("modelo"))
        rr["mac"] = _to_text(rr.get("camera_mac") or rr.get("mac"))
        p = _pick_image_path(rr)
        host = _recorder_host_text(r)
        ch = int(r.get("channel") or 0)
        caption = f"{host} CH{ch:02d} - {_to_text(r.get('title') or r.get('titulo'))}"
        inner: List[Any] = [_p(caption, styles["cap"])]
        if p is not None:
            try:
                from PIL import Image as PILImage
                with PILImage.open(p) as im:
                    ratio = (im.height / im.width) if im.width else 0.5625
                img = RLImage(str(p), width=img_w, height=img_w * ratio)
                inner.append(img)
            except Exception:
                inner.append(_p("sem imagem", styles["small"]))
        else:
            inner.append(_p("sem imagem", styles["small"]))
        cards.append(inner)
    if not cards:
        return []
    grid: List[List[Any]] = []
    for i in range(0, len(cards), cols):
        row = cards[i:i + cols]
        while len(row) < cols:
            row.append("")
        grid.append(row)
    table = Table(grid, colWidths=[cell_w] * cols)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [table]


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
    label = "NVR" if recorder_type == "nvr" else "DVR"
    accent = _accent(report_color)

    reports_dir = OUTPUT_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname_site = site_label.replace(" ", "_").replace("/", "_")
    out = reports_dir / f"recorder-report-{recorder_type}-{fname_site}-{ts}.pdf"

    styles = _styles()
    page_w, page_h = landscape(A4)
    margin = 14 * mm
    usable_w = page_w - 2 * margin

    subtitle_right = f"{site_label}  -  {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    header = _header_footer_factory(module_label or f"Gravadores {label}", accent, subtitle_right)

    doc = SimpleDocTemplate(
        str(out), pagesize=landscape(A4), leftMargin=margin, rightMargin=margin,
        topMargin=18 * mm, bottomMargin=14 * mm,
        title=f"Relatorio de gravadores {label} - {site_label}",
        author="SightOps", subject="Relatorio tecnico de gravadores",
    )

    story: List[Any] = []
    groups = _recorder_groups(rows_list)
    total = len(rows_list)
    in_use = sum(1 for r in rows_list if _recorder_channel_in_use(r))
    offline = sum(1 for r in rows_list if _recorder_channel_offline(r))
    empty = sum(1 for r in rows_list if _recorder_channel_empty(r))

    comp = f"  -  {company_name}" if _to_text(company_name) else ""
    story.append(_p(f"Relatorio tecnico | Gravadores {label}", styles["title"]))
    story.append(_p(f"Gerado em {datetime.now().strftime('%d/%m/%Y as %H:%M')}  -  Site: {site_label}  -  "
                    f"{len(groups)} gravador{'es' if len(groups) != 1 else ''}{comp}", styles["subtitle"]))
    story.append(_kpis([
        ("Canais", str(total), INK), ("Em uso", str(in_use), OK),
        ("Offline", str(offline), BAD), ("Vazios", str(empty), INK),
    ], styles, usable_w))
    story.append(Spacer(1, 6 * mm))

    # ---- Resumo por gravador ----
    story.append(_p("Resumo dos gravadores", styles["h2"]))
    for host, items in groups:
        model = _first_text(items, "nvr_model", "recorder_model", "modelo", "model")
        serial = _first_text(items, "equip_serial", "serial", "serial_number")
        local = _first_text(items, "local", "site", "site_name")
        mac = _first_text(items, "nvr_mac")
        used = sum(1 for r in items if _recorder_channel_in_use(r))
        bad = sum(1 for r in items if _recorder_channel_offline(r))
        no_cam = sum(1 for r in items if _recorder_channel_empty(r))
        vloss = sum(1 for r in items if bool(r.get("video_loss")))
        photos = sum(1 for r in items if _recorder_photo_available(r) and not _recorder_channel_empty(r))
        rec_sim = sum(1 for r in items if _recorder_recording_text(r) == "sim")
        rec_nao = sum(1 for r in items if _recorder_recording_text(r) == "nao")
        rec_nc = max(0, len(items) - rec_sim - rec_nao)
        hdd_count = _first_text(items, "hdd_count")
        hdd = _first_text(items, "hdd_status", "disk_status", "storage_status") or _first_text(items, "hdd_total")
        if hdd and hdd_count and "disco" not in hdd.lower():
            hdd = f"{hdd_count} disco(s) - {hdd}"
        # O IP real do NVR e o host (endereco por onde o sistema o alcanca). A
        # interface parseada as vezes traz um IP interno/PoE errado (ex.: 192.168.x
        # com gw 0.0.0.0). Entao lidera com o host e descarta gw/dns zerados.
        nvr_gw = _first_text(items, "nvr_gateway", "gateway")
        nvr_dns = _first_text(items, "nvr_dns")
        net_bits = [host]
        if nvr_gw and nvr_gw not in ("0.0.0.0",):
            net_bits.append(f"gw {nvr_gw}")
        if nvr_dns and nvr_dns not in ("0.0.0.0",):
            net_bits.append(f"dns {nvr_dns}")
        network = " - ".join(net_bits)
        platform = _clean_platform_label(_first_text(items, "hik_connect_status", "p2p_status", "platform_status", "cloud_status"))
        retention = _first_text(items, "recording_days", "retention_days", "retention")
        pairs = [
            ("Modelo", model or "-"), ("Serial", serial or "-"), ("Local", local or "-"), ("MAC NVR", mac or "-"),
            ("Canais", f"{len(items)} total - {used} em uso - {bad} offline - {no_cam} vazios"),
            ("Video loss", str(vloss)), ("Fotos", f"{photos} com imagem"),
            ("Gravacao", f"{rec_sim} sim - {rec_nao} nao - {rec_nc} n/c"),
            ("HD", hdd or "pendente de coleta"), ("Rede", network or "pendente de coleta"),
            ("Plataforma", platform or "pendente de coleta"), ("Retencao", retention or "pendente de coleta"),
        ]
        card = [
            _p(f"{label} {host}", styles["h2"]),
            _kv_table(pairs, styles, usable_w - 12),
        ]
        box = Table([[card]], colWidths=[usable_w])
        box.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ]))
        story.append(box)
        story.append(Spacer(1, 3 * mm))

    # ---- Tabela de canais ----
    # Sem PageBreak forcado: a tabela flui logo apos o resumo e preenche o espaco
    # que sobrava; o CondPageBreak so evita o titulo orfao no rodape.
    story.append(Spacer(1, 5 * mm))
    story.append(CondPageBreak(45 * mm))
    story.append(_p(f"Canais e cameras | Gravadores {label}", styles["title"]))
    story.append(_p(f"Site: {site_label}  -  {total} canal{'is' if total != 1 else ''}", styles["subtitle"]))
    if rows_list:
        story.append(_channel_table(rows_list, styles, accent, recorder_type, usable_w))
    else:
        story.append(_p("Nenhum canal encontrado para o filtro atual.", styles["small"]))

    # ---- Galeria de fotos ----
    if include_photos:
        photo_flows = _photo_flowables(rows_list, styles, usable_w)
        if photo_flows:
            story.append(PageBreak())
            story.append(_p(f"Galeria de snapshots | Gravadores {label}", styles["title"]))
            story.append(_p(f"Site: {site_label}", styles["subtitle"]))
            story.extend(photo_flows)

    if progress_cb:
        progress_cb(total, max(1, total), "pdf")

    doc.build(story, onFirstPage=header, onLaterPages=header)
    return out
