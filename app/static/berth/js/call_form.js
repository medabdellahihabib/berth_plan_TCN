(() => {
    'use strict';
    const read = (id) => { const el = document.getElementById(id); return el ? JSON.parse(el.textContent) : null; };
    const vessels = read('vessels-data') || [];
    const lines = read('lines-data') || {};
    const dayOwner = read('day-owner-data') || {};
    const working = read('working-data');
    const form = document.getElementById('callForm');
    if (!form) return;

    const f = (name) => form.querySelector(`[name="${name}"]`);
    const sel = f('vessel'), lineSel = f('line');
    const eta = f('eta'), etd = f('etd');
    const out = document.getElementById('windowCheck');
    const dur = document.getElementById('durationText');
    const submit = document.getElementById('submitBtn');
    let myLine = form.dataset.line || (lineSel && lineSel.value) || '';

    const pad = (n) => String(n).padStart(2, '0');
    const fmt = (d) => `${pad(d.getDate())}/${pad(d.getMonth() + 1)} à ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // ── navire existant → remplit les champs ──
    function fillVessel() {
        const v = vessels.find((x) => String(x.id) === sel.value);
        ['name', 'imo', 'loa'].forEach((k) => {
            const input = f(k);
            if (v) { input.value = v[k]; input.readOnly = true; }
            else if (input.readOnly) { input.value = ''; input.readOnly = false; }
        });
    }
    if (sel) { sel.addEventListener('change', fillVessel); if (sel.value) fillVessel(); }
    if (lineSel) lineSel.addEventListener('change', () => { myLine = lineSel.value; check(); });

    // ── contrôle en direct : fenêtre du jour + navire en opération ──
    function check() {
        const a = eta.value ? new Date(eta.value) : null;
        const b = etd.value ? new Date(etd.value) : null;
        document.querySelectorAll('.wk').forEach((el) => el.classList.toggle('sel', !!a && el.dataset.date === ymd(a)));

        dur.innerHTML = '';
        if (a && b) {
            const min = (b - a) / 6e4;
            dur.innerHTML = min > 0
                ? `Durée d'escale : <b>${Math.floor(min / 60)} h ${pad(Math.round(min % 60))}</b>`
                : `<span style="color:#f39a8c">L'ETD doit être après l'ETA.</span>`;
        }

        let html = '', blocked = false;
        if (a && working) {
            const ws = new Date(working.start), we = new Date(working.end);
            const end = b && b > a ? b : new Date(+a + 36e5);
            if (a < we && end > ws) {
                blocked = true;
                html += `<div class="note note-err"><i class="fas fa-ban"></i><div><strong>Tu ne peux pas : il y a un vessel working in berth.</strong><br>
                    ${esc(working.name)} (${working.line}) est en opération au quai jusqu'au ${fmt(we)}. Choisis un ETA après la fin des opérations.</div></div>`;
            }
        }
        if (a && !blocked) {
            const pyDay = (a.getDay() + 6) % 7;          // JS dimanche=0 → Python lundi=0
            const owner = dayOwner[String(pyDay)];
            const L = lines[owner];
            if (owner === myLine) {
                html += `<div class="note note-ok"><i class="fas fa-star"></i><div><strong>Fenêtre prioritaire ${owner}.</strong> Votre navire passe avant les autres lignes ce jour-là : ceux qui chevauchent seront décalés après votre ETD.</div></div>`;
            } else if (owner) {
                html += `<div class="note note-info"><i class="fas fa-circle-info"></i><div><strong>Fenêtre ${owner} (${esc(L?.name)}).</strong> Si aucun navire ${owner} n'est déclaré, vous accostez à votre ETA. Sinon, votre navire sera décalé après l'ETD du navire ${owner}.</div></div>`;
            }
        }
        out.innerHTML = html;
        submit.disabled = blocked;
    }
    eta.addEventListener('input', check);
    etd.addEventListener('input', check);
    check();

    form.addEventListener('submit', () => {
        submit.disabled = true;
        submit.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i> Enregistrement…';
    });
})();