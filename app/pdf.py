"""
Export PDF du berth plan (vectoriel, net à toutes les échelles).
Format A3 paysage, fond clair pour l'impression.
Quai à l'horizontale (métrage + bollards), temps à la verticale, 7 jours maximum par page.
"""
import math
import random
from datetime import datetime, time, timedelta
from io import BytesIO

from django.db.models import Q
from django.utils import timezone
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfgen import canvas

from .models import DAY_OWNER, LINE_COLORS, PRIORITY_DAYS, ShippingLine, VesselCall, berth_span, get_quay
from .scheduler import working_interval

DAY_NAMES = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
DAY_SHORT = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
S = VesselCall.Status

INK = HexColor("#1b2233")
MUTED = HexColor("#6b7489")
GRID = HexColor("#d9dee8")
NAVY = HexColor("#1f2b4d")
GOLD = HexColor("#d99a12")
RED = HexColor("#e5533d")
BOX_COLORS = ["#d64532", "#2f78c4", "#e8b33a", "#3aa57a", "#c9d0dc", "#6c7a96", "#b5562b"]

M = 26          # marge de page
HEAD = 64       # en-tête
AXIS = 86       # colonne des dates
RULER = 50      # règle du quai
FOOT = 22       # pied de page


def _local(dt):
    return timezone.localtime(dt)


def _num(v):
    v = round(float(v), 1)
    return f"{v:.0f}" if v == int(v) else f"{v:.1f}".replace(".", ",")


def _fit(c, text, font, size, max_w):
    """Coupe le texte avec … pour tenir dans max_w."""
    if c.stringWidth(text, font, size) <= max_w:
        return text
    while text and c.stringWidth(text + "…", font, size) > max_w:
        text = text[:-1]
    return (text + "…") if text else ""


# ─────────────────────────────────────────────────────────────
#  Dessin d'un porte-conteneurs (vue de profil)
# ─────────────────────────────────────────────────────────────
def _quad(path, p0, q, p2):
    """Courbe quadratique convertie en cubique (reportlab ne gère que les cubiques)."""
    c1 = (p0[0] + 2 / 3 * (q[0] - p0[0]), p0[1] + 2 / 3 * (q[1] - p0[1]))
    c2 = (p2[0] + 2 / 3 * (q[0] - p2[0]), p2[1] + 2 / 3 * (q[1] - p2[1]))
    path.curveTo(c1[0], c1[1], c2[0], c2[1], p2[0], p2[1])


def _hull_path(c, x, top, w, h):
    Y = lambda v: top - v  # v mesuré vers le bas depuis le haut du dessin
    deck, wl, keel = h * 0.50, h * 0.80, h * 0.97
    sheer = deck - h * 0.07
    bow, stern = min(40, w * 0.14), min(12, w * 0.045)
    p = c.beginPath()
    p.moveTo(x, Y(deck))
    p.lineTo(x + w - bow, Y(deck))
    _quad(p, (x + w - bow, Y(deck)), (x + w - bow * 0.4, Y(deck)), (x + w, Y(sheer)))
    p.lineTo(x + w - bow * 0.55, Y(wl))
    _quad(p, (x + w - bow * 0.55, Y(wl)), (x + w - bow * 0.08, Y(wl + (keel - wl) * 0.15)),
          (x + w - bow * 0.16, Y(wl + (keel - wl) * 0.6)))
    _quad(p, (x + w - bow * 0.16, Y(wl + (keel - wl) * 0.6)), (x + w - bow * 0.3, Y(keel)),
          (x + w - bow * 0.75, Y(keel)))
    p.lineTo(x + stern * 1.6, Y(keel))
    _quad(p, (x + stern * 1.6, Y(keel)), (x + stern * 0.4, Y(keel)), (x + stern * 0.25, Y(wl)))
    p.lineTo(x, Y(deck))
    p.close()
    return p


def draw_ship(c, x, top, w, h, color_hex, seed, status):
    color = HexColor(color_hex)
    Y = lambda v: top - v
    if w < 40 or h < 18:
        c.setFillColor(color)
        c.roundRect(x, top - h, w, h, 2, stroke=0, fill=1)
        return

    rnd = random.Random(seed)
    deck, wl, keel = h * 0.50, h * 0.80, h * 0.97
    sheer = deck - h * 0.07
    bow, stern = min(40, w * 0.14), min(12, w * 0.045)

    # Coque peinte (découpée dans la forme de coque)
    c.saveState()
    c.clipPath(_hull_path(c, x, top, w, h), stroke=0, fill=0)
    c.setFillColor(NAVY)
    c.rect(x, Y(h), w, h, stroke=0, fill=1)
    c.setFillColor(color)
    band_bottom = deck + (wl - deck) * 0.2
    c.rect(x, Y(band_bottom), w, band_bottom - sheer + 2, stroke=0, fill=1)
    c.setFillColor(HexColor("#b33a2c"))
    c.rect(x, Y(h), w, h - wl, stroke=0, fill=1)
    c.setFillColor(white)
    c.rect(x, Y(wl + 0.9), w, 1.8, stroke=0, fill=1)
    c.restoreState()
    c.setStrokeColor(HexColor("#0f1730"))
    c.setLineWidth(0.6)
    c.drawPath(_hull_path(c, x, top, w, h), stroke=1, fill=0)

    # Château blanc + passerelle
    bx, bw, btop = x + stern + w * 0.02, max(10, min(32, w * 0.075)), h * 0.12
    c.setFillColor(HexColor("#f4f6fa"))
    c.setStrokeColor(HexColor("#8b96ad"))
    c.setLineWidth(0.4)
    c.rect(bx, Y(deck), bw, deck - btop, stroke=1, fill=1)
    c.rect(bx - 2, Y(btop + 2), bw + 4, 2, stroke=1, fill=1)
    c.setStrokeColor(HexColor("#2a3652"))
    c.setLineWidth(1.1)
    c.setDash(1.6, 1)
    yy = btop + 4
    while yy < deck - 2:
        c.line(bx + 1.5, Y(yy), bx + bw - 1.5, Y(yy))
        yy += 3.6
    c.setDash()
    # Cheminée
    fw, fx, ftop = max(5, bw * 0.38), bx + bw * 0.55, max(1, btop - h * 0.1)
    c.setFillColor(color)
    c.rect(fx, Y(btop), fw, btop - ftop, stroke=0, fill=1)
    c.setFillColor(INK)
    c.rect(fx, Y(ftop + 1.6), fw, 1.6, stroke=0, fill=1)

    # Conteneurs en baies de 40'
    cs, ce = bx + bw + 4, x + w - bow * 0.85
    cw, gap = 9, 2.2
    bay_w = cw * 2 + 0.8
    rows = 5 if h > 110 else 4 if h > 80 else 3 if h > 50 else 2
    row_h = max(3, min(8, (deck - h * 0.1) / rows - 0.8))
    bays = max(0, int((ce - cs) // (bay_w + gap)))
    palette = [color_hex] * 3 + BOX_COLORS
    for b in range(bays):
        x0 = cs + b * (bay_w + gap)
        near_bow = b >= bays - 2
        for col in range(2):
            stack = rows - (1 if near_bow else 0) - (1 if rnd.random() < 0.22 else 0)
            for r in range(stack):
                cx = x0 + col * (cw + 0.8)
                cy = deck - 1.5 - (r + 1) * (row_h + 0.7)
                c.setFillColor(HexColor(rnd.choice(palette)))
                c.rect(cx, Y(cy + row_h), cw, row_h, stroke=0, fill=1)

    # Mât avant
    mx = x + w - bow * 0.7
    c.setStrokeColor(HexColor("#8b96ad"))
    c.setLineWidth(0.8)
    c.line(mx, Y(deck), mx, Y(deck - h * 0.22))

    # Portiques de quai si le navire est en opération
    if status == S.WORKING and bays >= 3:
        c.setStrokeColor(GOLD)
        for p in (0.3, 0.68):
            gx = cs + (ce - cs) * p
            c.setLineWidth(1.4)
            c.line(gx, Y(h * 0.03), gx, Y(deck - h * 0.06))
            c.line(gx + 8, Y(h * 0.03), gx + 8, Y(deck - h * 0.06))
            c.setLineWidth(1.8)
            c.line(gx - 14, Y(h * 0.03), gx + 26, Y(h * 0.03))


# ─────────────────────────────────────────────────────────────
#  Page du plan
# ─────────────────────────────────────────────────────────────
def _draw_page(c, page_start, page_days, calls, quay, now, page_no, page_count, range_label):
    W, H = landscape(A3)
    L = float(quay.length_m)
    bollards = list(quay.bollards.all())

    x0, x1 = M + AXIS, W - M
    top = H - M - HEAD - RULER
    bottom = M + FOOT
    m_pt = (x1 - x0 - 16) / L
    XM = lambda m: x0 + 8 + m * m_pt
    hours = page_days * 24
    h_pt = (top - bottom) / hours
    t0 = timezone.make_aware(datetime.combine(page_start, time.min))
    t1 = t0 + timedelta(days=page_days)
    Yt = lambda dt: top - (dt - t0).total_seconds() / 3600 * h_pt

    # ── En-tête
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(M, H - M - 18, f"TCN Berth Plan · {quay.name}")
    c.setFont("Helvetica", 10)
    c.setFillColor(MUTED)
    c.drawString(M, H - M - 34, f"{range_label}   ·   Quai {_num(L)} m (métrage {_num(quay.start_m)} – "
                                f"{_num(float(quay.start_m) + L)} m)   ·   TE max {_num(quay.max_draft_m)} m   ·   "
                                f"{len(bollards)} bollards   ·   un navire à la fois")
    # Légende des fenêtres
    lx = M
    for code, label in ShippingLine.choices:
        days = ", ".join(DAY_SHORT[d] for d in PRIORITY_DAYS[code])
        c.setFillColor(HexColor(LINE_COLORS[code]))
        c.roundRect(lx, H - M - 54, 10, 10, 2, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(lx + 14, H - M - 52, code)
        c.setFont("Helvetica", 9)
        c.setFillColor(MUTED)
        txt = f"{label} ({days})"
        c.drawString(lx + 14 + c.stringWidth(code + " ", "Helvetica-Bold", 9), H - M - 52, txt)
        lx += 40 + c.stringWidth(code + txt, "Helvetica", 9)
    c.setFont("Helvetica", 8)
    c.drawRightString(W - M, H - M - 18, f"Édité le {_local(now):%d/%m/%Y à %H:%M}")
    c.drawRightString(W - M, H - M - 30, f"Page {page_no} / {page_count}")

    # ── Bandes des jours (fenêtres prioritaires) + axe des dates
    for i in range(page_days):
        d = page_start + timedelta(days=i)
        owner = DAY_OWNER[d.weekday()]
        col = HexColor(LINE_COLORS[owner])
        ytop, ybot = top - i * 24 * h_pt, top - (i + 1) * 24 * h_pt
        c.saveState()
        c.setFillColor(col)
        c.setFillAlpha(0.07)
        c.rect(x0, ybot, x1 - x0, ytop - ybot, stroke=0, fill=1)
        c.setFillAlpha(1)
        c.rect(M, ybot, 3, ytop - ybot, stroke=0, fill=1)
        c.restoreState()
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(M + 8, ytop - 13, f"{DAY_SHORT[d.weekday()]} {d:%d/%m}")
        c.setFillColor(col)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(M + 8, ytop - 24, f"Fenêtre {owner}")
        # Heures
        step = 2 if h_pt * 2 >= 10 else 3 if h_pt * 3 >= 10 else 6
        c.setFont("Helvetica", 6.5)
        for hh in range(step, 24, step):
            y = ytop - hh * h_pt
            if ytop - y < 28:
                continue
            c.setFillColor(MUTED)
            c.drawRightString(x0 - 4, y - 2, f"{hh:02d}:00")
            c.setStrokeColor(GRID)
            c.setLineWidth(0.3)
            c.setDash(1, 2)
            c.line(x0, y, x1, y)
            c.setDash()
        c.setStrokeColor(HexColor("#aab3c5"))
        c.setLineWidth(0.6)
        c.line(M, ybot, x1, ybot)

    # Cadre
    c.setStrokeColor(HexColor("#aab3c5"))
    c.setLineWidth(0.8)
    c.rect(M, bottom, x1 - M, top + RULER - bottom, stroke=1, fill=0)
    c.line(x0, bottom, x0, top + RULER)
    c.line(M, top, x1, top)
    c.setFont("Helvetica", 7)
    c.setFillColor(MUTED)
    c.drawString(M + 6, top + RULER - 14, "Date (vers le bas)")
    c.drawString(M + 6, top + RULER - 26, "Quai (m) vers la droite")

    # ── Règle du quai : métrage + bollards
    c.setFillColor(HexColor("#e6eaf1"))
    c.rect(XM(0), top, L * m_pt, 9, stroke=0, fill=1)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(XM(0), top + 9, XM(L), top + 9)
    step = next((s for s in (10, 20, 25, 50, 100, 200, 500) if s * m_pt >= 40), 500)
    c.setFont("Helvetica", 7.5)
    m = 0.0
    while m < L - 0.01:
        if (L - m) * m_pt >= 34:
            c.setStrokeColor(HexColor("#8b96ad"))
            c.setLineWidth(0.5)
            c.line(XM(m), top + 21, XM(m), top + 30)
            c.setFillColor(INK)
            c.drawString(XM(m) + 2, top + 32, _num(float(quay.start_m) + m))
        m += step
    c.line(XM(L), top + 21, XM(L), top + 30)
    c.drawRightString(XM(L) - 2, top + 32, _num(float(quay.start_m) + L))

    min_gap = min((float(b.position_m) - float(a.position_m)) * m_pt
                  for a, b in zip(bollards, bollards[1:])) if len(bollards) > 1 else 40
    every = max(1, math.ceil(22 / max(min_gap, 1)))
    for i, b in enumerate(bollards):
        bxp = XM(float(b.position_m))
        c.setFillColor(GOLD)
        c.roundRect(bxp - 2.5, top + 1, 5, 7, 1.2, stroke=0, fill=1)
        c.setStrokeColor(HexColor("#e7c46a"))
        c.setLineWidth(0.3)
        c.setDash(1, 2)
        c.line(bxp, bottom, bxp, top)
        c.setDash()
        if i % every == 0:
            c.setFillColor(HexColor("#9a6b00"))
            c.setFont("Helvetica-Bold", 6)
            c.drawCentredString(bxp, top + 12, b.name)

    # ── Navires
    for call in calls:
        start, end = call.planned_start or call.eta, call.planned_end or call.etd
        if call.status == S.WORKING:
            start, end = working_interval(call, now)
        if end <= t0 or start >= t1:
            continue
        pos_from, pos_to, b_from, b_to = berth_span(call, quay)
        color = LINE_COLORS.get(call.line, "#8b96ad")
        bx, bw = XM(pos_from), max((pos_to - pos_from) * m_pt, 20)
        ys, ye = Yt(max(start, t0)), Yt(min(end, t1))
        ys, ye = ys - 1.5, ye + 1.5  # petit espace entre deux escales consécutives
        bh = ys - ye
        if bh < 4:
            continue

        # Pas de cadre : le navire seul, puis un trait de séjour jusqu'au départ
        if call.status == S.COMPLETED:
            c.saveState()
            c.setFillAlpha(0.45)
            c.setStrokeAlpha(0.45)

        continued = start < t0
        text_top = ys - 3
        if not continued:
            ship_h = min(bh if bh < 50 else bh * 0.62, bw * 0.38, 90)
            draw_ship(c, bx, ys, bw, ship_h, color, call.pk, call.status)
            if bw > 70 and ship_h >= 30:
                hull_y = ys - ship_h * 0.62
                c.setFillColor(white)
                c.setFont("Helvetica-Bold", 7.5)
                c.drawString(bx + bw * 0.13, hull_y,
                             _fit(c, call.vessel.name, "Helvetica-Bold", 7.5, bw * 0.68))
                c.setFont("Helvetica", 6)
                info = f"{call.line} · {_num(call.vessel.loa)} m" + (f" · TE {_num(call.draft)} m" if call.draft else "")
                c.drawString(bx + bw * 0.13, hull_y - 8, _fit(c, info, "Helvetica", 6, bw * 0.68))
            text_top = ys - ship_h - 3
            if bh - ship_h > 8:
                c.setStrokeColor(HexColor(color))
                c.setLineWidth(1.2)
                c.line(bx + 4, ys - ship_h - 1, bx + 4, ye)
                c.line(bx + 1.5, ye, bx + 6.5, ye)
        else:
            c.setFillColor(INK)
            c.setFont("Helvetica-Oblique", 6.5)
            c.drawString(bx + 4, ys - 9, "suite de la page précédente")
            text_top = ys - 12

        # Informations sous le navire
        lines = []
        if continued or bw <= 70:
            lines.append(("Helvetica-Bold", call.vessel.name))
        lines.append(("Helvetica", f"{_local(start):%d/%m %H:%M} – {_local(end):%d/%m %H:%M}"))
        if b_from and b_to:
            lines.append(("Helvetica", f"Bollards {b_from.name} – {b_to.name} · "
                                       f"{_num(float(quay.start_m) + pos_from)}–{_num(float(quay.start_m) + pos_to)} m"))
        if call.status == S.WORKING:
            lines.append(("Helvetica-Bold", "EN OPÉRATION"))
        elif call.shift_reason:
            delay = (start - call.eta).total_seconds() / 3600
            lines.append(("Helvetica-Bold", f"Décalé +{delay:.0f} h (ETA demandé {_local(call.eta):%d/%m %H:%M})"))
            lines.append(("Helvetica-Oblique", call.shift_reason))
        elif call.is_priority and call.status == S.PLANNED:
            lines.append(("Helvetica-Bold", f"Prioritaire {call.line}"))
        y = text_top - 7
        for font, txt in lines:
            if y < ye + 3:
                break
            c.setFillColor(INK)
            c.setFont(font, 6.5)
            c.drawString(bx + 10, y, _fit(c, txt, font, 6.5, bw - 14))
            y -= 8.5
        if call.status == S.COMPLETED:
            c.restoreState()

    # Heure actuelle
    if t0 <= now <= t1:
        y = Yt(now)
        c.setStrokeColor(RED)
        c.setLineWidth(1.2)
        c.line(x0, y, x1, y)
        c.setFillColor(RED)
        c.roundRect(x0 - 34, y - 5, 32, 10, 2, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(x0 - 18, y - 2.5, f"{_local(now):%H:%M}")

    # Pied de page
    c.setFont("Helvetica", 7.5)
    c.setFillColor(MUTED)
    c.drawString(M, M + 6, "Navire dessiné à l'échelle de sa LOA à son accostage ; le trait vertical indique le séjour à quai "
                           "jusqu'au départ.")
    c.drawRightString(W - M, M + 6, "TCN Berth Plan")


def build_plan_pdf(start_date, days):
    quay = get_quay()
    now = timezone.now()
    range_start = timezone.make_aware(datetime.combine(start_date, time.min))
    range_end = range_start + timedelta(days=days)
    calls = list(
        VesselCall.objects.select_related("vessel")
        .exclude(status=S.CANCELLED)
        .filter(Q(status=S.WORKING) | Q(planned_start__lt=range_end, planned_end__gt=range_start))
        .order_by("planned_start")
    )

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=landscape(A3))
    c.setTitle(f"Berth plan TCN du {start_date:%d/%m/%Y}")
    c.setAuthor("TCN Berth Plan")

    per_page = 7 if days > 7 else days
    page_count = math.ceil(days / per_page)
    last = start_date + timedelta(days=days - 1)
    range_label = f"Du {DAY_NAMES[start_date.weekday()].lower()} {start_date:%d/%m/%Y} au " \
                  f"{DAY_NAMES[last.weekday()].lower()} {last:%d/%m/%Y}"
    for p in range(page_count):
        page_start = start_date + timedelta(days=p * per_page)
        page_days = min(per_page, days - p * per_page)
        _draw_page(c, page_start, page_days, calls, quay, now, p + 1, page_count, range_label)
        c.showPage()
    c.save()
    return buf.getvalue()