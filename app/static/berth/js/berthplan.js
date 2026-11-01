/* ═══════════════════════════════════════════════════════════
   TCN Berth Plan — rendu interactif temps réel
   Axe X = temps, axe Y = longueur du quai (m), navire dessiné à l'échelle de sa LOA
   ═══════════════════════════════════════════════════════════ */
(() => {
    'use strict';
    const root = document.getElementById('berthPlan');
    if (!root) return;

    const DAY = 864e5, HOUR = 36e5;
    const QUAY_H = 26;      // bande du quai (px)
    const LANE_H = 330;     // hauteur du plan d'eau (px)
    const REFRESH_MS = 20000;

    const cfg = {
        api: root.dataset.api,
        urls: {
            edit: root.dataset.editUrl, cancel: root.dataset.cancelUrl,
            berth: root.dataset.berthUrl, complete: root.dataset.completeUrl,
        },
        csrf: root.dataset.csrf,
        staff: root.dataset.staff === '1',
        focus: root.dataset.focus ? parseInt(root.dataset.focus, 10) : null,
    };

    const $ = (id) => document.getElementById(id);
    const els = {
        days: root.querySelector('.bp-days'),
        canvas: root.querySelector('.bp-canvas'),
        scroll: root.querySelector('.bp-scroll'),
        axis: root.querySelector('.bp-axis-ticks'),
        range: $('bpRange'), live: $('bpLive'), liveText: $('bpLiveText'),
        tooltip: $('bpTooltip'), drawer: $('bpDrawer'), drawerBody: $('bpDrawerBody'),
        drawerHead: $('bpDrawerHead'), backdrop: $('bpBackdrop'),
    };

    const state = { start: addDays(startOfDay(new Date()), -1), days: 7, data: null, mineOnly: false, autoScrolled: false };

    /* ── utilitaires ─────────────────────────────── */
    function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
    function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
    const pad = (n) => String(n).padStart(2, '0');
    const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const fmt = (iso) => { const d = new Date(iso); return `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`; };
    const fmtTime = (iso) => { const d = new Date(iso); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const urlFor = (tpl, id) => tpl.replace(/\/0\//, `/${id}/`);
    function duration(min) {
        const h = Math.floor(min / 60), m = Math.round(min % 60);
        if (h >= 24) return `${Math.floor(h / 24)} j ${h % 24} h`;
        return m ? `${h} h ${pad(m)}` : `${h} h`;
    }
    function rng(seed) { let s = (seed * 2654435761) % 4294967296; return () => ((s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296); }

    /* ── dessin du navire porte-conteneurs (vue de profil) ── */
    function shipSVG(w, h, color, seed) {
        const W = Math.max(w, 8), H = Math.max(h, 24);
        if (W < 44) {
            return `<svg class="bp-ship" width="${W}" height="${H}" aria-hidden="true">
                <rect x="1" y="${H * 0.35}" width="${W - 2}" height="${H * 0.6}" rx="3" fill="#16203a" stroke="${color}" stroke-width="1.5"/></svg>`;
        }
        const R = rng(seed);
        const deck = H * 0.52, keel = H * 0.96;
        const bowTop = deck - H * 0.05;
        const bowLen = Math.min(36, W * 0.12), stern = Math.min(9, W * 0.035);
        const bowX = (y) => W - bowLen * (y - bowTop) / (keel - bowTop);
        const sternX = (y) => stern * (y - deck) / (keel - deck);
        const wl = keel - (keel - deck) * 0.3;

        let s = `<svg class="bp-ship" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">`;
        // coque
        s += `<path d="M0.5 ${deck} L${W - 0.5} ${bowTop} L${W - bowLen} ${keel} L${stern} ${keel} Z" fill="#16203a" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/>`;
        // ligne de flottaison (couleur de la ligne)
        s += `<path d="M${sternX(wl)} ${wl} L${bowX(wl)} ${wl} L${W - bowLen} ${keel} L${stern} ${keel} Z" fill="${color}" opacity=".55"/>`;
        // passerelle (château arrière)
        const bx = stern + W * 0.025, bw = Math.max(11, Math.min(36, W * 0.07)), bTop = H * 0.12;
        s += `<rect x="${bx}" y="${bTop}" width="${bw}" height="${deck - bTop}" rx="1.5" fill="#26324f" stroke="${color}" stroke-opacity=".5"/>`;
        s += `<rect x="${bx + 2}" y="${bTop + 3}" width="${bw - 4}" height="3" fill="#f0b429" opacity=".85"/>`;
        s += `<rect x="${bx + 2}" y="${bTop + 9}" width="${bw - 4}" height="2" fill="#3fb6e8" opacity=".55"/>`;
        // cheminée
        const fw = Math.max(5, bw * 0.4), fx = bx + bw * 0.3;
        s += `<rect x="${fx}" y="${Math.max(1, bTop - H * 0.09)}" width="${fw}" height="${H * 0.09}" fill="${color}"/>`;
        s += `<rect x="${fx}" y="${Math.max(1, bTop - H * 0.09)}" width="${fw}" height="2.5" fill="#070a12" opacity=".7"/>`;
        // conteneurs
        const cs = bx + bw + 4, ce = W - bowLen - 4;
        const cw = 15, gap = 2;
        const rows = H > 120 ? 4 : H > 80 ? 3 : 2;
        const rowH = Math.min(12, (deck - H * 0.14) / rows - 1.5);
        const cols = Math.max(0, Math.floor((ce - cs) / (cw + gap)));
        const palette = [color, color, color, '#f0b429', '#e5533d', '#3fb6e8', '#2ec4a6', '#c9d2e3'];
        for (let i = 0; i < cols; i++) {
            const stack = rows - (R() < 0.28 ? 1 : 0) - (R() < 0.08 ? 1 : 0);
            for (let r = 0; r < stack; r++) {
                const y = deck - (r + 1) * (rowH + 1.5);
                const fill = palette[Math.floor(R() * palette.length)];
                s += `<rect x="${cs + i * (cw + gap)}" y="${y}" width="${cw}" height="${rowH}" rx="1" fill="${fill}" opacity=".9"/>`;
            }
        }
        // mât avant + feu
        const mx = W - bowLen * 0.75;
        s += `<line x1="${mx}" y1="${bowTop}" x2="${mx}" y2="${bowTop - H * 0.2}" stroke="${color}" stroke-width="1.5"/>`;
        s += `<circle class="bp-mast-light" cx="${mx}" cy="${bowTop - H * 0.2}" r="2.5" fill="#f0b429"/>`;
        return s + '</svg>';
    }

    /* ── chargement ──────────────────────────────── */
    async function load() {
        try {
            const res = await fetch(`${cfg.api}?start=${ymd(state.start)}&days=${state.days}`, {
                headers: { Accept: 'application/json' }, credentials: 'same-origin',
            });
            if (res.redirected || !res.ok) throw new Error(res.status);
            state.data = await res.json();
            render();
            setLive(true);
        } catch (e) {
            setLive(false);
        }
    }

    function setLive(ok) {
        els.live.classList.toggle('off', !ok);
        els.liveText.textContent = ok ? `En direct, mis à jour à ${fmtTime(new Date().toISOString())}` : 'Connexion perdue, nouvelle tentative…';
    }

    /* ── rendu ───────────────────────────────────── */
    function render() {
        const d = state.data;
        if (!d) return;
        const rs = +new Date(d.range.start);
        const avail = els.scroll.clientWidth || 900;
        const dayW = Math.max(avail / state.days, state.days >= 14 ? 120 : 150);
        const totalW = Math.round(dayW * state.days);
        const px = dayW / DAY;
        const X = (t) => (+new Date(t) - rs) * px;
        const mPx = (LANE_H - 20) / d.quay_length;
        const H = QUAY_H + LANE_H;
        const todayYmd = ymd(new Date());

        // en-tête des jours (fenêtres prioritaires)
        els.days.style.width = totalW + 'px';
        els.days.innerHTML = d.windows.map((w) => {
            const L = d.lines[w.owner];
            return `<div class="bp-day ${w.date === todayYmd ? 'is-today' : ''}" style="width:${dayW}px;--line:${L.color}">
                <div class="bp-day-name">${w.day_name}<b>${w.num}</b></div>
                <div class="bp-day-owner"><span class="dot"></span>Fenêtre ${w.owner}</div></div>`;
        }).join('');

        let html = '';
        d.windows.forEach((w, i) => {
            html += `<div class="bp-window" style="left:${i * dayW}px;width:${dayW}px;--line:${d.lines[w.owner].color}"><span class="bp-window-tag">${w.owner}</span></div>`;
        });
        const step = state.days > 14 ? 12 : 6;
        for (let h = step; h < state.days * 24; h += step) {
            if (h % 24 === 0) continue;
            html += `<div class="bp-hour" style="left:${h * HOUR * px}px"><span>${pad(h % 24)}h</span></div>`;
        }
        html += `<div class="bp-quay" style="height:${QUAY_H}px"><span class="bp-quay-label">Quai TCN · ${d.quay_length} m</span></div>`;

        let visible = 0;
        for (const c of d.calls) {
            const top = QUAY_H + 4;
            const h = Math.max(36, c.loa * mPx);
            const left = X(c.start);
            const w = Math.max(X(c.end) - left, 8);
            if (left + w < 0 || left > totalW) continue;
            visible++;

            if (c.shifted && c.status === 'PLANNED') {
                const gx = X(c.eta), gw = X(c.etd) - gx;
                html += `<div class="bp-ghost" style="left:${gx}px;width:${gw}px;top:${top}px;height:${h}px;--line:${c.color}">ETA demandé ${esc(c.vessel)}</div>`;
                if (left - gx > 12) {
                    html += `<div class="bp-shift" style="left:${gx}px;width:${left - gx}px;top:${top + h + 14}px"><span>+${duration(c.delay_minutes)}</span></div>`;
                }
            }

            const cls = ['bp-vessel', `is-${c.status.toLowerCase()}`];
            if (c.mine) cls.push('is-mine');
            if (state.mineOnly && !c.mine) cls.push('is-dimmed');
            const badges = [];
            if (c.status === 'WORKING') badges.push('<span class="bp-badge working">En opération</span>');
            if (c.is_priority && c.status === 'PLANNED') badges.push(`<span class="bp-badge prio">Prioritaire ${c.line}</span>`);
            if (c.mine) badges.push('<span class="bp-badge mine">Mon navire</span>');

            html += `<div class="${cls.join(' ')}" data-id="${c.id}" tabindex="0" role="button"
                        aria-label="${esc(c.vessel)}, ${esc(c.line_name)}, ${fmt(c.start)} à ${fmt(c.end)}"
                        style="left:${left}px;width:${w}px;top:${top}px;height:${h}px;--line:${c.color}">
                ${shipSVG(w, h, c.color, c.id)}
                ${w > 90 ? `<div class="bp-label"><b>${esc(c.vessel)}</b><span>${c.line} · ${c.loa} m${c.voyage ? ' · V.' + esc(c.voyage) : ''}</span><span>${fmt(c.start)} → ${fmt(c.end)}</span></div>` : ''}
                ${badges.length && w > 60 ? `<div class="bp-badges">${badges.join('')}</div>` : ''}
            </div>`;
        }

        const nx = X(d.now);
        if (nx >= 0 && nx <= totalW) html += `<div class="bp-now" style="left:${nx}px"><span>${fmtTime(d.now)}</span></div>`;
        if (!visible) html += `<div class="bp-empty"><div>Aucun navire sur cette période. Le quai est disponible.</div></div>`;

        els.canvas.style.width = totalW + 'px';
        els.canvas.style.height = H + 'px';
        els.canvas.innerHTML = html;

        // axe des mètres
        let ticks = '';
        for (let m = 0; m <= d.quay_length; m += 50) {
            ticks += `<span class="bp-axis-tick" style="top:${QUAY_H + 4 + m * mPx}px">${m} m</span>`;
        }
        els.axis.innerHTML = ticks;
        els.axis.style.height = H + 'px';

        const last = addDays(new Date(d.range.end), -1);
        els.range.textContent = `${fmt(d.range.start).slice(0, 5)} au ${pad(last.getDate())}/${pad(last.getMonth() + 1)}`;

        if (!state.autoScrolled) {
            state.autoScrolled = true;
            const focusEl = cfg.focus && els.canvas.querySelector(`.bp-vessel[data-id="${cfg.focus}"]`);
            if (focusEl) {
                els.scroll.scrollLeft = Math.max(0, focusEl.offsetLeft - avail * 0.3);
                focusEl.classList.add('is-focus');
                const c = d.calls.find((x) => x.id === cfg.focus);
                if (c) openDrawer(c);
                cfg.focus = null;
            } else if (nx > 0) {
                els.scroll.scrollLeft = Math.max(0, nx - avail * 0.25);
            }
        }
    }

    /* ── tooltip ─────────────────────────────────── */
    function tooltipHTML(c) {
        return `<h4>${esc(c.vessel)}</h4>
            <dl><dt>Ligne</dt><dd>${esc(c.line_name)}</dd>
            <dt>IMO / LOA</dt><dd>${c.imo} · ${c.loa} m</dd>
            <dt>Accostage</dt><dd>${fmt(c.start)}</dd>
            <dt>Départ</dt><dd>${fmt(c.end)}</dd>
            <dt>Statut</dt><dd>${esc(c.status_label)}</dd></dl>
            ${c.shift_reason ? `<div class="warn">${esc(c.shift_reason)}</div>` : ''}`;
    }
    const findCall = (el) => el && state.data?.calls.find((c) => c.id === +el.dataset.id);

    els.canvas.addEventListener('mousemove', (e) => {
        const c = findCall(e.target.closest('.bp-vessel'));
        if (!c) { els.tooltip.style.display = 'none'; return; }
        els.tooltip.innerHTML = tooltipHTML(c);
        els.tooltip.style.setProperty('--line', c.color);
        els.tooltip.style.display = 'block';
        const r = els.tooltip.getBoundingClientRect();
        let x = e.clientX + 16, y = e.clientY + 16;
        if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 16;
        if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 16;
        els.tooltip.style.left = x + 'px';
        els.tooltip.style.top = y + 'px';
    });
    els.canvas.addEventListener('mouseleave', () => { els.tooltip.style.display = 'none'; });
    els.canvas.addEventListener('click', (e) => {
        const c = findCall(e.target.closest('.bp-vessel'));
        if (c) openDrawer(c);
    });
    els.canvas.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        const c = findCall(e.target.closest('.bp-vessel'));
        if (c) { e.preventDefault(); openDrawer(c); }
    });

    /* ── tiroir de détail ────────────────────────── */
    const postForm = (url, label, cls, confirmMsg) =>
        `<form method="post" action="${url}" ${confirmMsg ? `onsubmit="return confirm('${confirmMsg}')"` : ''}>
            <input type="hidden" name="csrfmiddlewaretoken" value="${cfg.csrf}">
            <button class="btn ${cls}" type="submit">${label}</button></form>`;

    function openDrawer(c) {
        els.tooltip.style.display = 'none';
        els.drawer.style.setProperty('--line', c.color);
        els.drawerHead.innerHTML = `
            <span class="line-pill" style="--line:${c.color}"><span class="dot"></span>${esc(c.line_name)}</span>
            <h3 style="margin-top:.5rem">${esc(c.vessel)}</h3>
            <div class="drawer-ship">${shipSVG(370, 110, c.color, c.id)}</div>`;

        let notes = '';
        if (c.status === 'WORKING') notes += `<div class="note note-warn"><i class="fas fa-anchor"></i><div>Navire à quai, opérations en cours. Aucune saisie n'est possible sur cette période.</div></div>`;
        if (c.shift_reason) notes += `<div class="note note-warn"><i class="fas fa-clock-rotate-left"></i><div>${esc(c.shift_reason)}. Décalage de ${duration(c.delay_minutes)} par rapport à l'ETA demandé.</div></div>`;
        else if (c.is_priority && c.status === 'PLANNED') notes += `<div class="note note-ok"><i class="fas fa-star"></i><div>ETA dans la fenêtre prioritaire ${c.line}. Ce navire passe avant les autres lignes ce jour-là.</div></div>`;
        else if (c.status === 'PLANNED' && c.window_owner !== c.line) notes += `<div class="note note-info"><i class="fas fa-circle-info"></i><div>ETA dans la fenêtre ${c.window_owner}. Si un navire ${c.window_owner} est déclaré sur ce créneau, ce navire sera décalé.</div></div>`;

        let actions = '';
        if (c.can_edit) {
            actions += `<a class="btn" href="${urlFor(cfg.urls.edit, c.id)}"><i class="fas fa-pen"></i> Modifier</a>`;
            actions += postForm(urlFor(cfg.urls.cancel, c.id), '<i class="fas fa-xmark"></i> Annuler l\'escale', 'btn-danger', 'Annuler cette escale ?');
        }
        if (cfg.staff && c.status === 'PLANNED' && !state.data.working_id) {
            actions += postForm(urlFor(cfg.urls.berth, c.id), '<i class="fas fa-anchor"></i> Démarrer les opérations', 'btn-ok', 'Confirmer l\'accostage de ce navire ?');
        }
        if (cfg.staff && c.status === 'WORKING') {
            actions += postForm(urlFor(cfg.urls.complete, c.id), '<i class="fas fa-flag-checkered"></i> Terminer les opérations', 'btn-ok', 'Confirmer la fin des opérations ?');
        }

        els.drawerBody.innerHTML = `${notes}
            <dl class="facts">
                <div><dt>N° IMO</dt><dd>${c.imo}</dd></div>
                <div><dt>LOA</dt><dd>${c.loa} m</dd></div>
                <div><dt>Voyage</dt><dd>${esc(c.voyage) || '—'}</dd></div>
                <div><dt>Agent</dt><dd>${esc(c.agent)}</dd></div>
                <div><dt>ETA demandé</dt><dd>${fmt(c.eta)}</dd></div>
                <div><dt>ETD demandé</dt><dd>${fmt(c.etd)}</dd></div>
                <div><dt>Accostage prévu</dt><dd>${fmt(c.start)}</dd></div>
                <div><dt>Départ prévu</dt><dd>${fmt(c.end)}</dd></div>
                <div><dt>Statut</dt><dd><span class="status status-${c.status}">${esc(c.status_label)}</span></dd></div>
                <div><dt>Fenêtre du jour</dt><dd>${c.window_owner}</dd></div>
            </dl>
            ${actions ? `<div class="drawer-actions">${actions}</div>` : ''}`;
        els.drawer.classList.add('open');
        els.backdrop.classList.add('open');
        els.drawer.querySelector('.drawer-close').focus();
    }
    function closeDrawer() { els.drawer.classList.remove('open'); els.backdrop.classList.remove('open'); }
    els.drawer.querySelector('.drawer-close').addEventListener('click', closeDrawer);
    els.backdrop.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

    /* ── barre d'outils ──────────────────────────── */
    document.querySelectorAll('[data-bp-nav]').forEach((b) => b.addEventListener('click', () => {
        const a = b.dataset.bpNav;
        if (a === 'prev') state.start = addDays(state.start, -Math.max(1, Math.floor(state.days / 2)));
        if (a === 'next') state.start = addDays(state.start, Math.max(1, Math.floor(state.days / 2)));
        if (a === 'today') { state.start = addDays(startOfDay(new Date()), -1); state.autoScrolled = false; }
        load();
    }));
    document.querySelectorAll('[data-bp-days]').forEach((b) => b.addEventListener('click', () => {
        document.querySelectorAll('[data-bp-days]').forEach((x) => x.classList.remove('on'));
        b.classList.add('on');
        state.days = +b.dataset.bpDays;
        state.autoScrolled = false;
        load();
    }));
    const mine = $('bpMine');
    if (mine) mine.addEventListener('change', () => { state.mineOnly = mine.checked; render(); });

    let rt;
    window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(render, 150); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
    setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
    load();
})();