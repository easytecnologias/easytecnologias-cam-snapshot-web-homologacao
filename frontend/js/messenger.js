let _accessMessengerConversations = [];
let _accessMessengerSelected = '';
let _accessMessengerMessages = [];
let _accessMessengerTimer = null;
let _accessMessengerBindingDone = false;
let _accessMessengerLoadingConv = false;
let _accessMessengerLoadingThread = false;

const ACCESS_MESSENGER_REFRESH_MS = 6000;

function accessMessengerVisible() {
  return !document.getElementById('viewAccessMessenger')?.classList.contains('hidden');
}

function accessMessengerParseDate(iso) {
  if (!iso) return null;
  let value = String(iso).trim().replace(' ', 'T');
  // O backend grava em UTC. SQLite nao poe fuso nenhum (falta o 'Z');
  // Postgres poe, mas `+00` (sem os minutos) tambem e um fuso valido --
  // testar so "+-\d\d:\d\d$" perdia esse caso e o Date() dava Invalid Date.
  // Sem alguma dessas duas normalizacoes o navegador le a data como hora
  // local e o horario mostrado fica errado (ou invalido).
  const temFuso = /Z$/i.test(value) || /[+-]\d{2}(:?\d{2})?$/.test(value.slice(10));
  if (!temFuso) value += 'Z';
  const d = new Date(value);
  return isNaN(d.getTime()) ? null : d;
}

function accessMessengerTime(iso) {
  const d = accessMessengerParseDate(iso);
  if (!d) return '';
  return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
}

function accessMessengerDateTime(iso) {
  const d = accessMessengerParseDate(iso);
  if (!d) return '';
  return d.toLocaleString('pt-BR', {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function accessMessengerDayKey(d) {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function accessMessengerDayLabel(iso) {
  const d = accessMessengerParseDate(iso);
  if (!d) return '';
  const hoje = new Date();
  const ontem = new Date(hoje);
  ontem.setDate(ontem.getDate() - 1);
  if (accessMessengerDayKey(d) === accessMessengerDayKey(hoje)) return 'Hoje';
  if (accessMessengerDayKey(d) === accessMessengerDayKey(ontem)) return 'Ontem';
  return d.toLocaleDateString('pt-BR', { weekday: 'long', day: '2-digit', month: 'long', year: 'numeric' });
}

function accessMessengerStatusMark(status) {
  const key = String(status || '').toLowerCase();
  if (key === 'read') return '<span class="access-messenger-tick read" title="Lida">✓✓</span>';
  if (key === 'delivered') return '<span class="access-messenger-tick" title="Entregue">✓✓</span>';
  if (key === 'whatsapp_sent' || key === 'sent' || key === 'accepted') return '<span class="access-messenger-tick" title="Enviada">✓</span>';
  if (key === 'whatsapp_failed' || key === 'failed') return '<span class="access-messenger-tick failed" title="Falhou">!</span>';
  return '';
}

function accessMessengerContactLabel(conv) {
  if (conv.person_name) {
    const extra = [conv.class_name, conv.site].filter(Boolean).join(' - ');
    return { title: conv.person_name, subtitle: extra || formatBrPhone(conv.contact_number), named: true };
  }
  if (conv.guardian_name) return { title: conv.guardian_name, subtitle: formatBrPhone(conv.contact_number), named: true };
  return { title: formatBrPhone(conv.contact_number), subtitle: '', named: false };
}

// Paleta fixa (nao aleatoria) pra cada contato sempre cair na mesma cor --
// so pra diferenciar visualmente as conversas na lista, nada semantico.
const ACCESS_MESSENGER_AVATAR_PALETTE = ['p1', 'p2', 'p3', 'p4', 'p5', 'p6'];

function accessMessengerAvatarClass(numero) {
  let hash = 0;
  const str = String(numero || '');
  for (let i = 0; i < str.length; i++) hash = (hash * 31 + str.charCodeAt(i)) >>> 0;
  return ACCESS_MESSENGER_AVATAR_PALETTE[hash % ACCESS_MESSENGER_AVATAR_PALETTE.length];
}

function accessMessengerInitial(title, named) {
  if (!named) return '';
  const letra = String(title || '').trim().charAt(0).toUpperCase();
  return /[A-ZÀ-Ú]/.test(letra) ? letra : '';
}

function accessMessengerAvatar(numero, title, named) {
  const inicial = accessMessengerInitial(title, named);
  const cls = accessMessengerAvatarClass(numero);
  const conteudo = inicial ? esc(inicial) : '<i data-lucide="user-round"></i>';
  return `<div class="access-messenger-avatar ${cls}">${conteudo}</div>`;
}

function accessMessengerFilterConversations(list, search) {
  const termo = String(search || '').trim().toLowerCase();
  if (!termo) return list;
  const soDigitos = termo.replace(/\D/g, '');
  return list.filter(c => {
    const { title } = accessMessengerContactLabel(c);
    const bateuNome = title.toLowerCase().includes(termo);
    // so compara numero se a busca realmente tiver digito -- "".includes('')
    // e sempre true em JS, e sem essa guarda uma busca so de letras deixava
    // passar TODO MUNDO (era exatamente o bug: digitar nome nao filtrava nada).
    const bateuNumero = soDigitos.length > 0 && c.contact_number.includes(soDigitos);
    return bateuNome || bateuNumero;
  });
}

function renderAccessMessengerConversations(list) {
  const box = document.getElementById('accessMessengerConvList');
  const count = document.getElementById('accessMessengerConvCount');
  if (count) count.textContent = String(list.length);
  if (!box) return;
  if (!list.length) {
    box.innerHTML = '<div class="access-messenger-empty">Nenhuma conversa ainda.</div>';
    return;
  }
  box.innerHTML = list.map(conv => {
    const { title, subtitle, named } = accessMessengerContactLabel(conv);
    const preview = String(conv.last_body || '').replace(/\n+/g, ' ').trim().slice(0, 80);
    const out = conv.last_direction === 'out';
    const dirIcon = `<i data-lucide="${out ? 'arrow-up-right' : 'arrow-down-left'}" class="access-messenger-dir-icon ${out ? 'out' : 'in'}"></i>`;
    const active = conv.contact_number === _accessMessengerSelected ? ' active' : '';
    return `
      <button type="button" class="access-messenger-conv-item${active}" data-numero="${esc(conv.contact_number)}">
        ${accessMessengerAvatar(conv.contact_number, title, named)}
        <div class="access-messenger-conv-info">
          <div class="access-messenger-conv-top">
            <strong>${esc(title)}</strong>
            <span class="access-messenger-conv-time">${accessMessengerDateTime(conv.last_at)}</span>
          </div>
          <div class="access-messenger-conv-preview">${dirIcon}<span>${esc(preview) || '(sem texto)'}</span></div>
          ${subtitle ? `<div class="access-messenger-conv-sub">${esc(subtitle)}</div>` : ''}
        </div>
      </button>`;
  }).join('');
  lucide.createIcons();
}

function renderAccessMessengerThread(numero, messages) {
  const thread = document.getElementById('accessMessengerThread');
  const titleEl = document.getElementById('accessMessengerChatTitle');
  const subEl = document.getElementById('accessMessengerChatSubtitle');
  const avatarSlot = document.getElementById('accessMessengerChatAvatar');
  const conv = _accessMessengerConversations.find(c => c.contact_number === numero);
  const label = conv ? accessMessengerContactLabel(conv) : { title: formatBrPhone(numero), named: false };
  if (titleEl) titleEl.textContent = label.title;
  if (subEl) subEl.textContent = formatBrPhone(numero);
  if (avatarSlot) {
    avatarSlot.innerHTML = accessMessengerAvatar(numero, label.title, label.named);
    lucide.createIcons();
  }
  if (!thread) return;
  if (!messages.length) {
    thread.innerHTML = '<div class="access-messenger-empty">Sem mensagens com este contato.</div>';
    return;
  }
  let diaAnterior = '';
  const partes = [];
  messages.forEach(m => {
    const dia = accessMessengerDayLabel(m.created_at);
    if (dia && dia !== diaAnterior) {
      partes.push(`<div class="access-messenger-day-divider"><span>${esc(dia)}</span></div>`);
      diaAnterior = dia;
    }
    const mine = m.direction === 'out';
    const errorLine = m.error ? `<div class="access-messenger-bubble-error">${esc(m.error)}</div>` : '';
    partes.push(`
      <div class="access-messenger-bubble-row ${mine ? 'out' : 'in'}">
        <div class="access-messenger-bubble ${mine ? 'out' : 'in'}">
          <div class="access-messenger-bubble-text">${esc(m.body) || '<em>(sem texto)</em>'}</div>
          ${errorLine}
          <div class="access-messenger-bubble-meta">
            <span>${accessMessengerTime(m.created_at)}</span>
            ${mine ? accessMessengerStatusMark(m.status) : ''}
          </div>
        </div>
      </div>`);
  });
  thread.innerHTML = partes.join('');
  thread.scrollTop = thread.scrollHeight;
}

async function loadAccessMessengerThread(numero, { silent = false } = {}) {
  if (!numero || _accessMessengerLoadingThread) return;
  _accessMessengerLoadingThread = true;
  try {
    const res = await apiJson(`/api/access-control/whatsapp/conversations/${encodeURIComponent(numero)}/messages`, {
      forceRefresh: true, cacheTtl: 0,
    });
    _accessMessengerMessages = res?.messages || [];
    if (_accessMessengerSelected === numero) renderAccessMessengerThread(numero, _accessMessengerMessages);
  } catch (err) {
    if (!silent) showToast(err?.message || 'Nao foi possivel carregar a conversa.', true);
  } finally {
    _accessMessengerLoadingThread = false;
  }
}

function selectAccessMessengerConversation(numero) {
  _accessMessengerSelected = numero;
  renderAccessMessengerConversations(_accessMessengerConversations);
  loadAccessMessengerThread(numero);
}

function renderAccessMessengerStats(list) {
  const box = document.getElementById('accessMessengerStats');
  if (!box) return;
  const hojeChave = accessMessengerDayKey(new Date());
  let hoje = 0;
  let falharam = 0;
  let recebidas = 0;
  list.forEach(conv => {
    const d = accessMessengerParseDate(conv.last_at);
    if (d && accessMessengerDayKey(d) === hojeChave) hoje += 1;
    if (String(conv.last_status || '').includes('fail')) falharam += 1;
    if (conv.last_direction === 'in') recebidas += 1;
  });
  const chip = (icone, texto, tom) => `<span class="access-messenger-stat-chip ${tom || ''}"><i data-lucide="${icone}"></i>${esc(texto)}</span>`;
  box.innerHTML = [
    chip('users', `${list.length} conversa${list.length === 1 ? '' : 's'}`),
    chip('calendar-clock', `${hoje} hoje`, hoje ? 'ok' : ''),
    chip('reply', `${recebidas} com resposta do responsavel`),
    falharam ? chip('alert-triangle', `${falharam} com falha no ultimo envio`, 'warn') : '',
  ].join('');
  lucide.createIcons();
}

async function loadAccessMessengerConversations(force = false) {
  if (_accessMessengerLoadingConv) return;
  if (!accessMessengerVisible() && !force) return;
  _accessMessengerLoadingConv = true;
  const btn = document.getElementById('btnAccessMessengerRefresh');
  const oldHtml = btn?.innerHTML;
  if (btn && force) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Atualizando';
    lucide.createIcons();
  }
  try {
    const res = await apiJson('/api/access-control/whatsapp/conversations', { forceRefresh: true, cacheTtl: 0 });
    _accessMessengerConversations = res?.conversations || [];
    const search = document.getElementById('accessMessengerSearch')?.value || '';
    renderAccessMessengerConversations(accessMessengerFilterConversations(_accessMessengerConversations, search));
    renderAccessMessengerStats(_accessMessengerConversations);
    const syncEl = document.getElementById('accessMessengerLastSync');
    if (syncEl) syncEl.textContent = `Atualizado as ${new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
    if (_accessMessengerSelected) loadAccessMessengerThread(_accessMessengerSelected, { silent: true });
  } catch (err) {
    console.warn('SightOps messenger failed', err);
    if (force) showToast(err?.message || 'Nao foi possivel carregar as conversas.', true);
  } finally {
    _accessMessengerLoadingConv = false;
    if (btn && force) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="refresh-cw"></i> Atualizar';
      lucide.createIcons();
    }
  }
}

async function loadAccessMessenger(force = true) {
  await loadAccessMessengerConversations(force);
}

function bindAccessMessenger() {
  if (_accessMessengerBindingDone) return;
  _accessMessengerBindingDone = true;
  document.getElementById('btnAccessMessengerRefresh')?.addEventListener('click', () => loadAccessMessengerConversations(true));
  document.getElementById('accessMessengerSearch')?.addEventListener('input', ev => {
    renderAccessMessengerConversations(accessMessengerFilterConversations(_accessMessengerConversations, ev.target.value));
  });
  document.getElementById('accessMessengerConvList')?.addEventListener('click', ev => {
    const btn = ev.target.closest?.('[data-numero]');
    if (!btn) return;
    selectAccessMessengerConversation(btn.dataset.numero);
  });
  if (!_accessMessengerTimer) {
    _accessMessengerTimer = setInterval(() => {
      if (!accessMessengerVisible()) return;
      loadAccessMessengerConversations(false);
    }, ACCESS_MESSENGER_REFRESH_MS);
  }
}

document.addEventListener('DOMContentLoaded', bindAccessMessenger);
