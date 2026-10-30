// Mini Workday widgets for test fixtures, mirroring the My Information markup captured live on the Revantage
// tenant (2026-10-30, signed in):
//   - a single-select is <button aria-haspopup="listbox" id="x--x" name="x"> showing the current value as text
//     ("India", "Select One"); clicking it opens a body-level popup of [role=option][data-automation-id=menuItem]
//     rows; clicking a row sets the button text and closes the popup; Escape closes it;
//   - a multi-select prompt is <input type="text" id="x--x" placeholder="Search"> inside
//     [data-automation-id=multiSelectContainer]; typed text + Enter opens a popup of [data-automation-id=promptOption]
//     rows (categories expand to their children); a chosen leaf becomes a [data-automation-id=selectedItem] chip;
//     the popup stays open until Escape, and an open popup counts as "expanded, empty" on Save and Continue.
window.wdWidgets = (function () {
  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  const attr = v => esc(JSON.stringify(v));

  function wrap(label, auto, inner, required) {
    return `<div data-automation-id="formField-${auto}"><label for="${auto}--${auto}">${label}</label>${inner}</div>`;
  }

  // listboxHtml("Phone Device Type *", "phoneType", ["Mobile", "Home"], {required: true, value: "Select One"})
  function listboxHtml(label, auto, options, o) {
    o = o || {};
    const btn = `<button type="button" aria-haspopup="listbox" aria-expanded="false" id="${auto}--${auto}" name="${auto}"` +
      ` data-automation-id="${auto}" data-wd="listbox" data-options="${attr(options)}" data-opens="0"` +
      `${o.required ? ' aria-required="true"' : ''}>${esc(o.value || 'Select One')}</button>`;
    return wrap(label, auto, btn, o.required);
  }

  // promptHtml("How Did You Hear About Us? *", "source", ["Career Fair", {"Online": ["Job Board"]}], {chips: []})
  function promptHtml(label, auto, tree, o) {
    o = o || {};
    const chips = (o.chips || []).map(chipHtml).join('');
    const inner = `<div data-automation-id="multiSelectContainer" data-wd="prompt" data-tree="${attr(tree)}">` +
      `<ul data-automation-id="selectedItemList" role="listbox">${chips}</ul>` +
      `<input type="text" id="${auto}--${auto}" name="${auto}" placeholder="Search" autocomplete="off" aria-expanded="false"` +
      `${o.required ? ' aria-required="true"' : ''}>` +
      `<div class="wd-popup" data-automation-id="promptPopup" hidden></div></div>`;
    return wrap(label, auto, inner, o.required);
  }

  function chipHtml(text) {
    // Live chips are promptOption rows too (selectedItemList > li.menuItem > div.selectedItem > p.promptOption).
    return `<li data-automation-id="menuItem" role="presentation"><div data-automation-id="selectedItem" role="option">` +
      `<p data-automation-id="promptOption"><span>${esc(text)}</span></p>` +
      `<button type="button" aria-label="Remove ${esc(text)}"><svg width="8" height="8"></svg></button></div></li>`;
  }

  // radioHtml("Have you ... intern?", "previousWorker", ["Yes", "No"], {required: true})
  function radioHtml(question, name, options, o) {
    o = o || {};
    // styled: the input is covered by a span like Workday's (a pointer click on the input is intercepted)
    const cover = o.styled ? '<span style="position:absolute;inset:0;background:#fff;border:1px solid #888;border-radius:50%"></span>' : '';
    const rows = options.map((opt, i) => `<div><span style="position:relative;display:inline-block;width:16px;height:16px">` +
      `<input type="radio" id="${name}-${i}" name="${name}" value="${esc(opt)}"${o.required ? ' required' : ''}` +
      `${o.styled ? ' style="position:absolute;inset:0;margin:0;opacity:0"' : ''}>${cover}</span>` +
      `<label for="${name}-${i}">${esc(opt)}</label></div>`).join('');
    return `<div data-automation-id="formField-${name}"><fieldset><legend>${question}</legend>` +
      `<div role="radiogroup">${rows}</div></fieldset></div>`;
  }

  // -- behaviour

  function closeListbox(btn) {
    const pop = document.getElementById(btn.id + '-popup');
    if (pop) pop.remove();
    btn.setAttribute('aria-expanded', 'false');
  }

  function openListbox(btn) {
    closeListbox(btn);
    btn.dataset.opens = String(Number(btn.dataset.opens || 0) + 1);
    const options = JSON.parse(btn.dataset.options || '[]');
    const pop = document.createElement('div');
    pop.id = btn.id + '-popup';
    pop.setAttribute('data-automation-id', 'selectWidgetPopup');
    pop.style.cssText = 'position:absolute;left:300px;top:100px;background:#fff;border:1px solid #333;z-index:5';
    pop.innerHTML = `<ul role="listbox">${options.map(x => `<li role="option" data-automation-id="menuItem">${esc(x)}</li>`).join('')}</ul>`;
    document.body.appendChild(pop);
    btn.setAttribute('aria-expanded', 'true');
    for (const li of pop.querySelectorAll('li')) {
      li.addEventListener('click', () => { btn.textContent = li.textContent; closeListbox(btn); });
    }
  }

  function promptState(box) {
    if (!box._wd) box._wd = { path: [], query: '' };
    return box._wd;
  }

  function entriesAt(tree, path) {
    let level = tree;
    for (const name of path) {
      const cat = level.find(e => typeof e === 'object' && Object.keys(e)[0] === name);
      level = cat ? cat[name] : [];
    }
    return level;
  }

  const nameOf = e => (typeof e === 'object' ? Object.keys(e)[0] : e);
  const isCat = e => typeof e === 'object';
  const leavesOf = e => (isCat(e) ? e[nameOf(e)].flatMap(leavesOf) : [e]);

  function renderPrompt(box) {
    const st = promptState(box);
    const tree = JSON.parse(box.dataset.tree || '[]');
    const input = box.querySelector('input');
    const pop = box.querySelector('[data-automation-id="promptPopup"]');
    const q = st.query.trim().toLowerCase();
    let entries = entriesAt(tree, st.path);
    if (q && !st.path.length) {
      // Like the live search: an entry whose own name matches, or a category with a matching leaf (the
      // category is shown, not expanded).
      entries = entries.filter(e => nameOf(e).toLowerCase().includes(q) ||
        (isCat(e) && leavesOf(e).some(l => l.toLowerCase().includes(q))));
    }
    pop.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    if (!entries.length) {
      pop.innerHTML = '<div class="wd-notice">No matches found</div>';
      return;
    }
    pop.innerHTML = entries.map(e => `<div data-automation-id="promptOption" role="option"` +
      `${isCat(e) ? ' aria-haspopup="true"' : ''}><span>${esc(nameOf(e))}</span>${isCat(e) ? '<svg class="chevron" width="8" height="8"></svg>' : ''}</div>`).join('');
    Array.from(pop.querySelectorAll('[data-automation-id="promptOption"]')).forEach((row, i) => {
      row.addEventListener('click', () => {
        const e = entries[i];
        if (isCat(e)) {  // live Workday loads the level a moment later
          st.path.push(nameOf(e)); st.query = '';
          pop.innerHTML = '<div class="wd-notice">Loading...</div>';
          setTimeout(() => renderPrompt(box), 600);
          return;
        }
        const list = box.querySelector('[data-automation-id="selectedItemList"]');
        if (!Array.from(list.querySelectorAll('[data-automation-id="selectedItem"] span')).some(s => s.textContent === e)) {
          list.insertAdjacentHTML('beforeend', chipHtml(e));
          wireChips(box);
        }
        input.value = '';
        st.query = '';
        renderPrompt(box);  // a multi-select keeps its popup open after a pick
      });
    });
  }

  function closePrompt(box) {
    const st = promptState(box);
    st.path = []; st.query = '';
    box.querySelector('[data-automation-id="promptPopup"]').hidden = true;
    box.querySelector('input').setAttribute('aria-expanded', 'false');
  }

  function wireChips(box) {
    for (const b of box.querySelectorAll('[data-automation-id="selectedItem"] button')) {
      if (b._wired) continue;
      b._wired = true;
      b.addEventListener('click', () => (b.closest('[data-automation-id="menuItem"]') || b.closest('[data-automation-id="selectedItem"]')).remove());
    }
  }

  function wire(root) {
    root = root || document;
    for (const btn of root.querySelectorAll('button[data-wd="listbox"]')) {
      if (btn._wired) continue;
      btn._wired = true;
      btn.addEventListener('click', () => {
        if (btn.getAttribute('aria-expanded') === 'true') closeListbox(btn); else openListbox(btn);
      });
      btn.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeListbox(btn); });
    }
    for (const box of root.querySelectorAll('[data-wd="prompt"]')) {
      if (box._wired) continue;
      box._wired = true;
      const input = box.querySelector('input');
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); promptState(box).query = input.value; renderPrompt(box); }
        else if (e.key === 'Escape') { e.preventDefault(); closePrompt(box); }
      });
      wireChips(box);
    }
  }

  // {values: {label: value}, missing: [label]} for every widget under `root`, like the live step validation:
  // a listbox on its placeholder, a prompt without chips or left open ("Expanded") is missing when required.
  function values(root) {
    const out = { values: {}, missing: [] };
    const labelOf = (el) => (el.labels && el.labels[0] ? el.labels[0].innerText : el.id).trim();
    for (const btn of root.querySelectorAll('button[data-wd="listbox"]')) {
      const text = btn.textContent.trim();
      const v = /^select one$/i.test(text) ? '' : text;
      out.values[labelOf(btn)] = v;
      if (!v && btn.getAttribute('aria-required') === 'true') out.missing.push(labelOf(btn));
    }
    for (const box of root.querySelectorAll('[data-wd="prompt"]')) {
      const input = box.querySelector('input');
      const chips = Array.from(box.querySelectorAll('[data-automation-id="selectedItem"] span')).map(s => s.textContent.trim());
      const open = input.getAttribute('aria-expanded') === 'true';
      const v = open ? '' : chips.join('; ');
      out.values[labelOf(input)] = open ? 'Expanded' : v;
      if (!v && input.getAttribute('aria-required') === 'true') out.missing.push(labelOf(input));
    }
    return out;
  }

  return { listboxHtml, promptHtml, radioHtml, wire, values };
})();
