from __future__ import annotations

import io
import math
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    CondPageBreak,
    KeepTogether,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from xml.sax.saxutils import escape


GREEN = colors.HexColor("#07865F")
GREEN_DARK = colors.HexColor("#086047")
GREEN_SOFT = colors.HexColor("#EAF7F2")
INK = colors.HexColor("#17232B")
MUTED = colors.HexColor("#667782")
BORDER = colors.HexColor("#D8E1E5")
SOFT = colors.HexColor("#F4F7F8")
RED = colors.HexColor("#C92A2A")
TYPE_LABELS = {
    "box": "Caixa de CFTV", "camera": "Câmera", "onu": "ONU", "ont": "ONT", "olt": "OLT",
    "switch": "Switch", "injector": "Injetor PoE", "cto": "CTO", "recorder": "Gravador",
    "pole": "Poste", "rack": "Rack", "dio": "DIO", "other": "Outro",
}
# Mesmos padroes default do "Calcular cabo UTP" no front (margem sobre a
# rota e folga tecnica por camera) -- sem isso, camera no mesmo poste da
# caixa mostraria 0,0 m, o que nao existe na pratica (sempre sobra um
# trecho de cabo, folga, curva etc.).
CABLE_ROUTE_MARGIN_PCT = 15.0
CABLE_SLACK_METERS = 5.0


def _text(value: Any, fallback: str = "—") -> str:
    value = str(value or "").strip()
    return value or fallback


def _model(item: Dict[str, Any]) -> str:
    values = [_text(item.get("manufacturer"), ""), _text(item.get("model"), "")]
    return " / ".join(value for value in values if value) or "A definir"


def _coords(item: Dict[str, Any]) -> str:
    try:
        return f"{float(item.get('latitude')):.7f}, {float(item.get('longitude')):.7f}"
    except (TypeError, ValueError):
        return "Não informadas"


def _straight_line_meters(a: Dict[str, Any], b: Dict[str, Any]) -> float | None:
    """Distancia aerea caixa->camera, mesma formula usada no calculo de cabo
    do front -- serve de estimativa quando ninguem mediu o percurso na rua."""
    try:
        lat1, lon1 = math.radians(float(a.get("latitude"))), math.radians(float(a.get("longitude")))
        lat2, lon2 = math.radians(float(b.get("latitude"))), math.radians(float(b.get("longitude")))
    except (TypeError, ValueError):
        return None
    earth = 6371000
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return earth * 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))


def _safe(value: Any, fallback: str = "—") -> str:
    return escape(_text(value, fallback))


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "cover_label": ParagraphStyle("CoverLabel", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=9,
                                      leading=11, textColor=GREEN, spaceAfter=5, uppercase=True),
        "cover_title": ParagraphStyle("CoverTitle", parent=base["Title"], fontName="Helvetica-Bold", fontSize=25,
                                      leading=29, textColor=INK, alignment=TA_LEFT, spaceAfter=5),
        "cover_subtitle": ParagraphStyle("CoverSubtitle", parent=base["Normal"], fontName="Helvetica", fontSize=11,
                                         leading=15, textColor=MUTED, spaceAfter=12),
        "cover_client": ParagraphStyle("CoverClient", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=11,
                                        leading=14, textColor=INK, spaceAfter=3),
        "cover_description": ParagraphStyle("CoverDescription", parent=base["Normal"], fontName="Helvetica", fontSize=9.5,
                                             leading=13, textColor=MUTED),
        "cover_meta_label": ParagraphStyle("CoverMetaLabel", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=6.5,
                                            leading=8, textColor=GREEN, alignment=TA_LEFT, spaceAfter=3),
        "cover_meta_value": ParagraphStyle("CoverMetaValue", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=10,
                                            leading=12, textColor=INK, alignment=TA_LEFT),
        "hero_label": ParagraphStyle("HeroLabel", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8.5,
                                      leading=11, textColor=colors.HexColor("#BFE8D8"), spaceAfter=7),
        "hero_title": ParagraphStyle("HeroTitle", parent=base["Title"], fontName="Helvetica-Bold", fontSize=25,
                                      leading=29, textColor=colors.white, alignment=TA_LEFT, spaceAfter=7),
        "hero_subtitle": ParagraphStyle("HeroSubtitle", parent=base["Normal"], fontName="Helvetica", fontSize=10.5,
                                         leading=14, textColor=colors.HexColor("#E7F4EF")),
        "flow_step": ParagraphStyle("FlowStep", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8,
                                     leading=10, textColor=INK, alignment=TA_CENTER),
        "flow_number": ParagraphStyle("FlowNumber", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=6.5,
                                       leading=8, textColor=GREEN, alignment=TA_CENTER, spaceAfter=3),
        "flow_arrow": ParagraphStyle("FlowArrow", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=13,
                                      leading=15, textColor=GREEN, alignment=TA_CENTER),
        "h1": ParagraphStyle("H1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=16,
                             leading=20, textColor=INK, spaceBefore=4, spaceAfter=9),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=11,
                             leading=14, textColor=INK, spaceBefore=8, spaceAfter=5),
        "body": ParagraphStyle("Body", parent=base["BodyText"], fontName="Helvetica", fontSize=9,
                               leading=13, textColor=MUTED, spaceAfter=7),
        "small": ParagraphStyle("Small", parent=base["BodyText"], fontName="Helvetica", fontSize=7.4,
                                leading=9.5, textColor=INK),
        "small_bold": ParagraphStyle("SmallBold", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=7.4,
                                     leading=9.5, textColor=INK),
        "th": ParagraphStyle("TH", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=6.5,
                             leading=8, textColor=MUTED),
        "kpi_label": ParagraphStyle("KpiLabel", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=6.7,
                                    leading=8, textColor=MUTED, alignment=TA_LEFT),
        "kpi_value": ParagraphStyle("KpiValue", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=16,
                                    leading=18, textColor=INK, alignment=TA_LEFT),
        "right": ParagraphStyle("Right", parent=base["Normal"], fontName="Helvetica", fontSize=7.4,
                                leading=9.5, textColor=INK, alignment=TA_RIGHT),
        "center": ParagraphStyle("Center", parent=base["Normal"], fontName="Helvetica", fontSize=7.4,
                                 leading=9.5, textColor=INK, alignment=TA_CENTER),
    }


def _p(value: Any, style: ParagraphStyle, fallback: str = "—") -> Paragraph:
    return Paragraph(_safe(value, fallback), style)


def _header_footer(canvas, doc, title: str) -> None:
    canvas.saveState()
    width, height = A4
    canvas.setFillColor(GREEN)
    canvas.rect(0, height - 4 * mm, width, 4 * mm, fill=1, stroke=0)
    canvas.setStrokeColor(BORDER)
    canvas.line(16 * mm, height - 15 * mm, width - 16 * mm, height - 15 * mm)
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.setFillColor(GREEN_DARK)
    canvas.drawString(16 * mm, height - 12 * mm, "SIGHTOPS  •  DOCUMENTO TÉCNICO DE REDE")
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(width - 16 * mm, height - 12 * mm, title[:72])
    canvas.line(16 * mm, 13 * mm, width - 16 * mm, 13 * mm)
    canvas.drawString(16 * mm, 8.8 * mm, "Projeto planejado — validar percurso, postes, energia e autorizações em campo")
    canvas.drawRightString(width - 16 * mm, 8.8 * mm, f"Página {doc.page}")
    canvas.restoreState()


def _table(data: List[List[Any]], widths: List[float], header: bool = True, alignments: Dict[int, str] | None = None) -> LongTable:
    table = LongTable(data, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT", splitByRow=1)
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.45, BORDER),
    ]
    if header:
        commands.extend([
            ("BACKGROUND", (0, 0), (-1, 0), SOFT),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, BORDER),
        ])
    for row in range(1 if header else 0, len(data)):
        if row % 2 == 0:
            commands.append(("BACKGROUND", (0, row), (-1, row), colors.HexColor("#FBFCFC")))
    for column, alignment in (alignments or {}).items():
        commands.append(("ALIGN", (column, 0), (column, -1), alignment))
    table.setStyle(TableStyle(commands))
    return table


def _kpis(values: List[tuple[str, str]], styles: Dict[str, ParagraphStyle]) -> Table:
    cells = []
    for label, value in values:
        cells.append([_p(label.upper(), styles["kpi_label"]), _p(value, styles["kpi_value"])])
    rows = [cells[index:index + 4] for index in range(0, len(cells), 4)]
    table = Table(rows, colWidths=[42.4 * mm] * 4, rowHeights=[20 * mm] * len(rows), hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SOFT),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def generate_project_network_pdf(
    project: Dict[str, Any],
    route_margin_pct: float = CABLE_ROUTE_MARGIN_PCT,
    slack_meters: float = CABLE_SLACK_METERS,
) -> bytes:
    devices = list(project.get("devices") or [])
    sites = list(project.get("sites") or [])
    title = _text(project.get("name"), "Projeto de CFTV")
    styles = _styles()
    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=20 * mm, bottomMargin=18 * mm, title=f"{title} — Documento técnico de rede",
        author="SightOps", subject="Projeto técnico de rede e CFTV",
    )
    story: List[Any] = []
    counts = Counter(str(item.get("device_type") or "other") for item in devices)
    children: Dict[int, List[Dict[str, Any]]] = {}
    for item in devices:
        if item.get("parent_id"):
            children.setdefault(int(item["parent_id"]), []).append(item)

    def descendants(parent_id: int) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for child in children.get(parent_id, []):
            result.append(child)
            result.extend(descendants(int(child["id"])))
        return result

    client = _text(project.get("client_name"), "Cliente não informado")
    description = _text(project.get("description"), "Projeto técnico de infraestrutura de CFTV e rede GPON.")
    hero = Table([[
        [
            Paragraph("PLANEJAMENTO DE INFRAESTRUTURA", styles["cover_label"]),
            Paragraph(escape(title), styles["cover_title"]),
            Spacer(1, 3 * mm),
            Paragraph(escape(client), styles["cover_client"]),
            Paragraph(escape(description), styles["cover_description"]),
        ],
        [
            Paragraph("DOCUMENTO", styles["cover_meta_label"]),
            Paragraph("TÉCNICO DE REDE", styles["cover_meta_value"]),
            Spacer(1, 5 * mm),
            Paragraph("ESCOPO", styles["cover_meta_label"]),
            Paragraph("CFTV + REDE GPON", styles["cover_meta_value"]),
            Spacer(1, 5 * mm),
            Paragraph("PLATAFORMA", styles["cover_meta_label"]),
            Paragraph("SIGHTOPS", styles["cover_meta_value"]),
        ],
    ]], colWidths=[116 * mm, 53.6 * mm], hAlign="LEFT")
    hero.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), colors.white),
        ("BACKGROUND", (1, 0), (1, 0), SOFT),
        ("LINEBEFORE", (0, 0), (0, 0), 3, GREEN),
        ("LINEBEFORE", (1, 0), (1, 0), 0.6, BORDER),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, GREEN),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 8 * mm),
        ("RIGHTPADDING", (0, 0), (0, 0), 10 * mm),
        ("LEFTPADDING", (1, 0), (1, 0), 7 * mm),
        ("RIGHTPADDING", (1, 0), (1, 0), 7 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 8 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8 * mm),
    ]))

    # A "Arquitetura do projeto" reflete o que o projeto REALMENTE tem, e nao
    # um template GPON fixo (REDE ÓPTICA -> CAIXA -> ONU -> POE -> CÂMERAS)
    # que aparecia igual mesmo quando o projeto era so switch + câmeras.
    # Cada etapa entra apenas se existir equipamento daquele tipo no cadastro.
    stage_defs = [
        ("REDE ÓPTICA", {"olt"}),
        ("CTO / FIBRA", {"cto", "dio"}),
        ("ONU / ONT", {"onu", "ont"}),
        ("CAIXA DE CFTV", {"box"}),
        ("RACK", {"rack"}),
        ("DISTRIBUIÇÃO POE", {"switch", "injector"}),
        ("GRAVAÇÃO", {"recorder"}),
        ("CÂMERAS", {"camera"}),
    ]
    present_types = {str(item.get("device_type") or "") for item in devices}
    stages = [label for label, types in stage_defs if types & present_types]

    flow = None
    if stages:
        flow_cells: List[Any] = []
        for number, label in enumerate(stages, 1):
            flow_cells.append([
                Paragraph(f"ETAPA {number:02d}", styles["flow_number"]),
                Paragraph(label, styles["flow_step"]),
            ])
            if number < len(stages):
                flow_cells.append(Paragraph("›", styles["flow_arrow"]))
        arrow_w = 3.65 * mm
        stage_w = (169.6 * mm - arrow_w * (len(stages) - 1)) / len(stages)
        col_widths: List[float] = []
        for idx in range(len(stages)):
            col_widths.append(stage_w)
            if idx < len(stages) - 1:
                col_widths.append(arrow_w)
        flow = Table([flow_cells], colWidths=col_widths, rowHeights=[20 * mm], hAlign="LEFT")
        flow_style = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        for column in range(0, len(col_widths), 2):
            flow_style.extend([
                ("BACKGROUND", (column, 0), (column, 0), GREEN_SOFT),
                ("BOX", (column, 0), (column, 0), 0.6, colors.HexColor("#B9DDD0")),
            ])
        flow.setStyle(TableStyle(flow_style))

    purpose = Table([[
        [
            _p("Finalidade do documento", styles["h2"]),
            _p("Consolidar a arquitetura, os vínculos, as coordenadas e os quantitativos do projeto em uma referência única para apresentação, orçamento e implantação.", styles["body"]),
            _p("Os percursos e pontos de instalação devem ser validados em campo antes da execução.", styles["small_bold"]),
        ],
        [
            _p("Controle documental", styles["h2"]),
            Paragraph(f"<b>Emissão</b><br/>{datetime.now().strftime('%d/%m/%Y %H:%M')}", styles["small"]),
            Paragraph(f"<b>Status</b><br/>{escape(_text(project.get('status'), 'Planejado').title())}", styles["small"]),
            Paragraph("<b>Revisão</b><br/>Cadastro atual do SightOps", styles["small"]),
        ],
    ]], colWidths=[107 * mm, 62.6 * mm], hAlign="LEFT")
    purpose.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), SOFT),
        ("BACKGROUND", (1, 0), (1, 0), GREEN_SOFT),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("LINEBEFORE", (1, 0), (1, 0), 0.6, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))

    story.extend([
        Spacer(1, 8 * mm),
        hero,
        Spacer(1, 7 * mm),
        _kpis([
            ("Sites", str(len(sites))), ("Caixas", str(counts["box"])), ("Câmeras", str(counts["camera"])),
            ("ONUs / ONTs", str(counts["onu"] + counts["ont"])), ("Switches", str(counts["switch"])),
            ("Injetores PoE", str(counts["injector"])), ("CTOs", str(counts["cto"])), ("Total planejado", str(len(devices))),
        ], styles),
        Spacer(1, 7 * mm),
    ])
    if flow is not None:
        story.extend([_p("Arquitetura do projeto", styles["h1"]), flow, Spacer(1, 7 * mm)])
    story.append(purpose)

    def _emit_topology_node(anchor: Dict[str, Any], number: int, internal_label: str) -> None:
        members = descendants(int(anchor["id"]))
        internal = [item for item in members if item.get("device_type") != "camera"]
        cameras = [item for item in members if item.get("device_type") == "camera"]
        story.extend([
            CondPageBreak(58 * mm),
            _p(f"{number:02d}. {_text(anchor.get('name'))}", styles["h1"]),
            _table([
                [_p("SITE", styles["th"]), _p("COORDENADAS", styles["th"]), _p("EQUIP. INTERNOS", styles["th"]), _p("CÂMERAS", styles["th"])],
                [_p(anchor.get("site_name"), styles["small_bold"], "Sem site"), _p(_coords(anchor), styles["small"]), _p(str(len(internal)), styles["center"]), _p(str(len(cameras)), styles["center"])],
            ], [38 * mm, 72 * mm, 31 * mm, 28.6 * mm], alignments={2: "CENTER", 3: "CENTER"}),
            _p(internal_label, styles["h2"]),
        ])
        internal_rows = [[_p("TIPO", styles["th"]), _p("NOME", styles["th"]), _p("FABRICANTE / MODELO", styles["th"]), _p("SERIAL", styles["th"]), _p("MAC", styles["th"]), _p("PORTA", styles["th"]), _p("LIGADO A", styles["th"])]]
        internal_rows.extend([
            [_p(TYPE_LABELS.get(str(item.get("device_type")), item.get("device_type")), styles["small"]),
             _p(item.get("name"), styles["small_bold"]), _p(_model(item), styles["small"]),
             _p((item.get("metadata") or {}).get("serial"), styles["small"]),
             _p((item.get("metadata") or {}).get("mac"), styles["small"]),
             _p((f"Porta {_text((item.get('metadata') or {}).get('port_number'), '')}".strip() if _text((item.get('metadata') or {}).get('port_number'), '') else "-"), styles["small"]),
             _p(item.get("parent_name"), styles["small"], anchor.get("name"))]
            for item in internal
        ] or [[_p("Nenhum equipamento interno", styles["small"])] + [_p("—", styles["small"]) for _ in range(6)]] )
        story.extend([_table(internal_rows, [16 * mm, 30 * mm, 38 * mm, 20 * mm, 27 * mm, 14 * mm, 33.6 * mm]), _p("Câmeras atendidas", styles["h2"])])
        camera_rows = [[_p("CÂMERA", styles["th"]), _p("PORTA", styles["th"]), _p("IP", styles["th"]), _p("MAC", styles["th"]), _p("FABRICANTE / MODELO", styles["th"]), _p("ROTA", styles["th"]), _p("COORDENADAS", styles["th"])]]
        for camera in cameras:
            metadata = camera.get("metadata") or {}
            route = metadata.get("route_distance_m", metadata.get("distance_to_box_m"))
            if route in (None, ""):
                straight = _straight_line_meters(anchor, camera)
                estimate = straight * (1 + route_margin_pct / 100) + slack_meters if straight is not None else None
                route_label = f"{estimate:.1f} m (estimado)" if estimate is not None else "A definir"
            else:
                route_label = f"{float(route):.1f} m"
            port_txt = _text(metadata.get("port_number"), "")
            port_label = f"Porta {port_txt}" if port_txt else "-"
            camera_rows.append([
                _p(camera.get("name"), styles["small_bold"]), _p(port_label, styles["small"]),
                _p(camera.get("ip"), styles["small"], "A definir"),
                _p(metadata.get("mac"), styles["small"]),
                _p(_model(camera), styles["small"]), _p(route_label, styles["right"]),
                _p(_coords(camera), styles["small"]),
            ])
        if len(camera_rows) == 1:
            camera_rows.append([_p("Nenhuma câmera vinculada", styles["small"])] + [_p("—", styles["small"]) for _ in range(6)])
        story.extend([_table(camera_rows, [40 * mm, 15 * mm, 19 * mm, 27 * mm, 30 * mm, 21 * mm, 26.6 * mm], alignments={5: "RIGHT"}), Spacer(1, 3 * mm)])

    child_ids = {int(pid) for pid in children.keys()}
    boxes = sorted((item for item in devices if item.get("device_type") == "box"), key=lambda item: _text(item.get("name")))
    # Equipamento de topo (sem pai) que agrega cameras/equipamentos mas nao e
    # caixa nem rack -- tipicamente um switch/injetor/CTO/gravador ligado direto
    # no rack. Sem esta secao, um projeto cujo no raiz e um switch renderizava a
    # "Topologia por caixa" vazia e jogava o switch em "Itens sem vinculo",
    # mesmo com cameras vinculadas a ele (bug relatado no UFV-RODOANEL).
    hubs = sorted(
        (item for item in devices
         if not item.get("parent_id")
         and item.get("device_type") not in ("box", "rack")
         and int(item["id"]) in child_ids),
        key=lambda item: _text(item.get("name")),
    )
    if boxes:
        story.extend([
            PageBreak(),
            _p("Topologia por caixa", styles["h1"]),
            _p("Relação física e lógica entre cada caixa, seus equipamentos internos e as câmeras atendidas.", styles["body"]),
        ])
        for number, box in enumerate(boxes, 1):
            _emit_topology_node(box, number, "Equipamentos dentro da caixa")
    if hubs:
        story.extend([
            PageBreak(),
            _p("Topologia por equipamento", styles["h1"]),
            _p("Equipamentos de topo (switch, distribuidor ou gravador) que atendem câmeras diretamente, sem uma caixa como intermediária.", styles["body"]),
        ])
        for number, hub in enumerate(hubs, 1):
            _emit_topology_node(hub, number, "Equipamentos ligados")

    racks = sorted((item for item in devices if item.get("device_type") == "rack"), key=lambda item: _text(item.get("name")))
    if racks:
        story.extend([
            PageBreak(),
            _p("Topologia por rack", styles["h1"]),
            _p("Relação física e lógica entre cada rack e os equipamentos que ele abriga (ONU/ONT, switch/roteador e gravadores).", styles["body"]),
        ])
    for number, rack in enumerate(racks, 1):
        members = descendants(int(rack["id"]))
        story.extend([
            CondPageBreak(58 * mm),
            _p(f"{number:02d}. {_text(rack.get('name'))}", styles["h1"]),
            _table([
                [_p("SITE", styles["th"]), _p("COORDENADAS", styles["th"]), _p("EQUIP. INTERNOS", styles["th"])],
                [_p(rack.get("site_name"), styles["small_bold"], "Sem site"), _p(_coords(rack), styles["small"]), _p(str(len(members)), styles["center"])],
            ], [58 * mm, 91.6 * mm, 28.6 * mm], alignments={2: "CENTER"}),
            _p("Equipamentos dentro do rack", styles["h2"]),
        ])
        rack_rows = [[_p("TIPO", styles["th"]), _p("NOME", styles["th"]), _p("FABRICANTE / MODELO", styles["th"]), _p("SERIAL", styles["th"]), _p("MAC", styles["th"]), _p("LIGADO A", styles["th"])]]
        rack_rows.extend([
            [_p(TYPE_LABELS.get(str(item.get("device_type")), item.get("device_type")), styles["small"]),
             _p(item.get("name"), styles["small_bold"]), _p(_model(item), styles["small"]),
             _p((item.get("metadata") or {}).get("serial"), styles["small"]),
             _p((item.get("metadata") or {}).get("mac"), styles["small"]),
             _p(item.get("parent_name"), styles["small"], rack.get("name"))]
            for item in members
        ] or [[_p("—", styles["small"]), _p("Nenhum equipamento interno", styles["small"]), _p("—", styles["small"]), _p("—", styles["small"]), _p("—", styles["small"]), _p("—", styles["small"])]])
        story.extend([_table(rack_rows, [18 * mm, 34 * mm, 42 * mm, 22 * mm, 28 * mm, 34.6 * mm]), Spacer(1, 3 * mm)])

    story.extend([PageBreak(), _p("Quantitativos e especificações", styles["h1"]), _p("Consolidação dos equipamentos que compõem a mesma revisão apresentada no KMZ.", styles["body"])])
    grouped = Counter((TYPE_LABELS.get(str(item.get("device_type")), _text(item.get("device_type"))), _model(item)) for item in devices)
    quantity_rows = [[_p("TIPO", styles["th"]), _p("FABRICANTE / MODELO", styles["th"]), _p("QUANTIDADE", styles["th"])]]
    quantity_rows.extend([[_p(kind, styles["small_bold"]), _p(model, styles["small"]), _p(str(count), styles["center"])] for (kind, model), count in sorted(grouped.items())])
    story.append(_table(quantity_rows, [48 * mm, 93 * mm, 28.6 * mm], alignments={2: "CENTER"}))

    unlinked = [
        item for item in devices
        if item.get("device_type") not in ("box", "rack")
        and not item.get("parent_id")
        and int(item["id"]) not in child_ids
    ]
    if unlinked:
        story.extend([_p("Itens sem vínculo", styles["h1"]), _p("Estes equipamentos existem no projeto, mas ainda não estão ligados a uma caixa ou equipamento pai.", styles["body"])])
        unlinked_rows = [[_p("TIPO", styles["th"]), _p("NOME", styles["th"]), _p("SITE", styles["th"]), _p("COORDENADAS", styles["th"])]]
        unlinked_rows.extend([[_p(TYPE_LABELS.get(str(item.get("device_type")), "Outro"), styles["small"]), _p(item.get("name"), styles["small_bold"]), _p(item.get("site_name"), styles["small"], "Sem site"), _p(_coords(item), styles["small"])] for item in unlinked])
        story.append(_table(unlinked_rows, [31 * mm, 60 * mm, 31 * mm, 47.6 * mm]))

    doc.build(story, onFirstPage=lambda canvas, current: _header_footer(canvas, current, title),
              onLaterPages=lambda canvas, current: _header_footer(canvas, current, title))
    return output.getvalue()
