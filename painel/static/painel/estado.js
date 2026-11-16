(() => {
  const body = document.body
  const section = body.dataset.stateSection
  const endpoint = body.dataset.stateUrl
  if (!section || !endpoint) return

  let busy = false
  let timer
  let request = null
  let generation = 0
  let failures = 0
  const backoff = [6000, 12000, 24000, 30000]
  const connection = document.getElementById('connection-state')
  const stale = document.getElementById('state-stale')
  const initialObserved = new Date(document.getElementById('state-observed-at')?.dataset.time || Date.now()).getTime()
  let lastConnectionAt = Number.isFinite(initialObserved) ? initialObserved : Date.now()
  let resumePending = false
  let staleTimer

  const setText = (node, value) => { if (node) node.textContent = value == null || value === '' ? '—' : String(value) }
  const date = value => {
    if (!value) return '—'
    const parsed = new Date(value)
    return Number.isNaN(parsed.getTime()) ? '—' : new Intl.DateTimeFormat('pt-BR', {
      timeZone: 'America/Sao_Paulo', dateStyle: 'short', timeStyle: 'short'
    }).format(parsed)
  }
  const connectionText = state => state.conectado === true ? 'WhatsApp conectado'
    : state.conectado === false ? 'WhatsApp desconectado' : 'WhatsApp indisponível'

  function paintConnection(state) {
    const qrPanel = document.querySelector('[data-qr-panel]')
    const qrImage = document.querySelector('[data-qr-image]')
    if (!state?.consulta_ok) {
      if (qrImage) { qrImage.removeAttribute('src'); qrImage.dataset.loadedAt = '0' }
      if (qrPanel) qrPanel.hidden = true
      return
    }
    lastConnectionAt = Date.now()
    const observed = document.getElementById('state-observed-at')
    if (observed && state.verificado_em) observed.dataset.time = state.verificado_em
    connection.dataset.connected = state.conectado === null ? 'unknown' : String(state.conectado)
    setText(connection.querySelector('[data-connection-label]'), connectionText(state))
    connection.querySelector('[data-connection-dot]')?.classList.toggle('online', state.conectado === true)
    setText(document.querySelector('[data-dashboard-connection]'), state.conectado === true ? 'Conectado' : state.conectado === false ? 'Desconectado' : 'Indisponível')
    setText(document.querySelector('[data-connection-status]'), state.status_legivel)
    setText(document.querySelector('[data-connection-checked]'), date(state.verificado_em))
    const connectForm = document.querySelector('[data-connect-form]')
    if (connectForm) connectForm.hidden = state.conectado !== false
    if (qrPanel && qrImage) {
      if (state.qr_disponivel) {
        qrPanel.hidden = false
        const loadedAt = Number(qrImage.dataset.loadedAt || (qrImage.getAttribute('src') ? Date.now() : 0))
        if (!qrImage.getAttribute('src') || Date.now() - loadedAt >= 15000) {
          qrImage.removeAttribute('src'); qrImage.dataset.loadedAt = String(Date.now())
          qrImage.src = `/whatsapp/qr/?v=${Date.now()}`
        }
      } else {
        qrImage.removeAttribute('src'); qrImage.dataset.loadedAt = '0'
        qrPanel.hidden = true
      }
    }
  }
  function hideQr() {
    const panel = document.querySelector('[data-qr-panel]'), image = document.querySelector('[data-qr-image]')
    if (image) { image.removeAttribute('src'); image.dataset.loadedAt = '0' }
    if (panel) panel.hidden = true
  }
  const qrImage = document.querySelector('[data-qr-image]')
  if (qrImage?.getAttribute('src')) qrImage.dataset.loadedAt = String(Date.now())
  qrImage?.addEventListener('load', () => { qrImage.dataset.loadedAt = String(Date.now()) })
  qrImage?.addEventListener('error', hideQr)

  function pesquisaTableRow(item) {
    const row = document.createElement('tr'); row.dataset.researchRow = item.id
    for (const field of ['termos', 'status', 'processadas', 'criada', 'erro']) {
      const cell = document.createElement('td'); cell.dataset.field = field
      if (field === 'status') { const badge = document.createElement('span'); badge.className = 'badge'; badge.dataset.field = field; cell.append(badge) }
      row.append(cell)
    }
    return row
  }
  function dashboardResearchRow(item) {
    const row = document.createElement('a'); row.className = 'activity-row'; row.href = '/pesquisas/'; row.dataset.researchRow = item.id
    const icon = document.createElement('span'); icon.className = 'activity-icon'; icon.textContent = '⌕'
    const copy = document.createElement('span'); copy.className = 'activity-copy'
    const terms = document.createElement('strong'); const progress = document.createElement('small'); progress.dataset.researchProgress = ''
    copy.append(terms, progress); const status = document.createElement('span'); status.className = 'badge'; status.dataset.researchStatus = ''
    row.append(icon, copy, status); return row
  }
  function updateResearch(items, recut = false) {
    const ids = new Set(items.map(item => String(item.id)))
    items.forEach(item => {
      document.querySelectorAll(`[data-research-row="${item.id}"]`).forEach(row => {
        setText(row.querySelector('[data-field="status"], [data-research-status]'), item.rotulo)
        setText(row.querySelector('[data-field="processadas"]'), item.processadas)
        setText(row.querySelector('[data-field="termos"]'), item.termos)
        setText(row.querySelector('[data-field="criada"]'), date(item.criada_em))
        setText(row.querySelector('[data-field="erro"]'), item.erro)
        setText(row.querySelector('[data-research-progress]'), `${item.processadas} processadas · ${date(item.inicio || item.criada_em)}`)
      })
      const table = document.querySelector('[data-research-table]')
      if (table && !table.querySelector(`[data-research-row="${item.id}"]`)) {
        table.querySelector('[colspan]')?.closest('tr')?.remove()
        const row = pesquisaTableRow(item); table.append(row)
        setText(row.querySelector('[data-field="status"]'), item.rotulo)
        setText(row.querySelector('[data-field="processadas"]'), item.processadas)
        setText(row.querySelector('[data-field="termos"]'), item.termos)
        setText(row.querySelector('[data-field="criada"]'), date(item.criada_em))
        setText(row.querySelector('[data-field="erro"]'), item.erro)
      }
      let dashboard = document.querySelector('[data-dashboard-research-list]')
      if (!dashboard) {
        const panel = document.querySelector('.dashboard-grid .panel')
        if (panel) {
          dashboard = document.createElement('div'); dashboard.className = 'activity-list'; dashboard.dataset.dashboardResearchList = ''
          panel.querySelector('.empty-state')?.remove(); panel.append(dashboard)
        }
      }
      if (dashboard && !dashboard.querySelector(`[data-research-row="${item.id}"]`)) {
        if (!dashboard.querySelector('.activity-row')) {
          const panel = dashboard.closest('.panel')
          dashboard.remove()
          const list = document.createElement('div'); list.className = 'activity-list'; list.dataset.dashboardResearchList = ''
          panel.querySelector('.empty-state')?.remove(); panel.append(list)
          dashboard = list
          list.append(dashboardResearchRow(item))
          const row = list.querySelector(`[data-research-row="${item.id}"]`)
          setText(row.querySelector('[data-research-status]'), item.rotulo)
          setText(row.querySelector('[data-research-progress]'), `${item.processadas} processadas · ${date(item.inicio || item.criada_em)}`)
          setText(row.querySelector('.activity-copy strong'), item.termos)
          return
        }
        const row = dashboardResearchRow(item); dashboard.querySelector('.empty-state')?.remove(); dashboard.prepend(row)
        setText(row.querySelector('[data-research-status]'), item.rotulo)
        setText(row.querySelector('[data-research-progress]'), `${item.processadas} processadas · ${date(item.inicio || item.criada_em)}`)
        setText(row.querySelector('.activity-copy strong'), item.termos)
      }
    })
    if (recut) document.querySelectorAll('[data-research-table] [data-research-row], [data-dashboard-research-list] [data-research-row]')
      .forEach(row => { if (!ids.has(row.dataset.researchRow)) row.remove() })
  }

  function updateDashboard(data) {
    setText(document.querySelector('[data-dashboard-total-empresas]'), data.total_empresas)
    setText(document.querySelector('[data-dashboard-total-pesquisas]'), data.total_pesquisas)
    setText(document.querySelector('[data-dashboard-campanhas-ativas]'), data.campanhas_ativas)
    updateResearch(data.pesquisas_recentes || [], true)
  }

  function updateCampaigns(items) {
    const table = document.querySelector('[data-campaign-table]')
    const ids = new Set(items.map(item => String(item.id)))
    items.forEach(item => {
      let row = document.querySelector(`[data-campaign-row="${item.id}"]`)
      if (!row && table) {
        table.querySelector('[colspan]')?.closest('tr')?.remove()
        row = document.createElement('tr'); row.dataset.campaignRow = item.id
        for (const field of ['nome', 'status', 'total', 'criada']) { const cell = document.createElement('td'); if (field !== 'status') cell.dataset.field = field; if (field === 'status') { const badge = document.createElement('span'); badge.className = 'badge'; badge.dataset.field = field; cell.append(badge); const pause = document.createElement('small'); pause.dataset.field = 'pause'; cell.append(pause) } row.append(cell) }
        const linkCell = document.createElement('td'), link = document.createElement('a'); link.href = `/campanhas/${item.id}/`; link.textContent = 'Abrir revisão →'; linkCell.append(link); row.append(linkCell); table.prepend(row)
      }
      if (!row) return
      setText(row.querySelector('[data-field="nome"]'), item.nome)
      setText(row.querySelector('span[data-field="status"]'), item.rotulo)
      setText(row.querySelector('[data-field="total"]'), item.destinatarios_total)
      setText(row.querySelector('[data-field="criada"]'), date(item.criado_em))
      setText(row.querySelector('[data-field="pause"]'), item.motivo_pausa)
    })
    if (table) table.querySelectorAll('[data-campaign-row]').forEach(row => { if (!ids.has(row.dataset.campaignRow)) row.remove() })
  }

  function updateCampaign(data) {
    setText(document.querySelector('[data-campaign-status]'), data.rotulo)
    const pauseReason = document.querySelector('[data-pause-reason]')
    if (pauseReason) pauseReason.textContent = data.motivo_pausa || ''
    const action = data.estado === 'rascunho' ? 'iniciar' : data.estado === 'ativa' ? 'pausar' : data.estado === 'pausada' ? 'retomar' : ''
    document.querySelectorAll('[data-campaign-action]').forEach(form => { form.hidden = form.dataset.campaignAction !== action })
    setText(document.querySelector('[data-recipient-total]'), `${data.contagens.destinatarios_total} destinatários preparados`)
    Object.entries(data.contagens).forEach(([key, value]) => {
      const node = document.querySelector(`[data-count="${key}"]`)
      if (node && typeof value === 'number') setText(node, value)
      if (value && typeof value === 'object') Object.entries(value).forEach(([estado, total]) => {
        setText(document.querySelector(`[data-count="${key}:${estado}"]`), total)
      })
    })
    data.destinatarios.forEach(item => {
      const row = document.querySelector(`[data-recipient-row="${item.id}"]`)
      const cell = row?.querySelector('[data-recipient-state]')
      if (cell) setText(cell, item.bloqueado_motivo ? `${item.rotulo} · ${item.bloqueado_motivo}` : item.rotulo)
    })
    data.tentativas.forEach(item => {
      let row = document.querySelector(`[data-attempt-row="${item.id}"]`)
      if (!row && document.querySelector('[data-attempt-table]')) {
        const table = document.querySelector('[data-attempt-table]'); table.querySelector('[data-empty-attempts]')?.remove()
        row = document.createElement('tr'); row.dataset.attemptRow = item.id
        for (const field of ['telefone', 'status', 'confirmado', 'erro']) { const cell = document.createElement('td'); cell.dataset.field = field; row.append(cell) }
        table.prepend(row)
      }
      if (!row) return
      setText(row.querySelector('[data-field="telefone"]'), item.telefone)
      setText(row.querySelector('[data-field="status"]'), item.rotulo)
      setText(row.querySelector('[data-field="confirmado"]'), date(item.confirmado_em))
      setText(row.querySelector('[data-field="erro"]'), item.erro)
    })
  }

  function reenvioAction(row, item) {
    if (item.estado !== 'falha' || row.querySelector('form[action*="/reenviar/"]')) return
    const details = document.createElement('details'), summary = document.createElement('summary'); summary.textContent = 'Reenviar'
    const form = document.createElement('form'); form.method = 'post'; form.action = `/tentativas/${item.id}/reenviar/`
    const csrf = document.querySelector('#state-csrf input[name="csrfmiddlewaretoken"]')?.value
    if (csrf) { const token = document.createElement('input'); token.type = 'hidden'; token.name = 'csrfmiddlewaretoken'; token.value = csrf; form.append(token) }
    const label = document.createElement('label'); label.textContent = 'Motivo'
    const textarea = document.createElement('textarea'); textarea.name = 'motivo'; textarea.required = true; textarea.minLength = 3; textarea.maxLength = 500; label.append(textarea)
    const button = document.createElement('button'); button.type = 'submit'; button.className = 'button outline'; button.textContent = 'Preparar reenvio'
    form.append(label, button); details.append(summary, form); row.lastElementChild.append(details)
  }
  function updateHistory(data) {
    const table = document.querySelector('[data-history-attempts]')
    const ids = new Set(data.tentativas.map(item => String(item.id)))
    data.tentativas.forEach(item => {
      let row = document.querySelector(`[data-history-attempts] [data-attempt-row="${item.id}"]`)
      if (!row && table) {
        table.querySelector('[colspan]')?.closest('tr')?.remove()
        row = document.createElement('tr'); row.dataset.attemptRow = item.id
        for (const value of [item.empresa_nome, item.campanha_nome, item.telefone]) { const cell = document.createElement('td'); setText(cell, value); row.append(cell) }
        for (const field of ['status', 'atualizado', 'erro']) { const cell = document.createElement('td'); cell.dataset.field = field; row.append(cell) }
        row.append(document.createElement('td')); table.prepend(row)
      }
      if (!row) return
      reenvioAction(row, item)
      setText(row.querySelector('[data-field="status"]'), item.rotulo)
      setText(row.querySelector('[data-field="atualizado"]'), date(item.atualizado_em))
      setText(row.querySelector('[data-field="erro"]'), item.erro)
    })
    if (table) table.querySelectorAll('[data-attempt-row]').forEach(row => {
      if (!ids.has(row.dataset.attemptRow) && !row.contains(document.activeElement) && ![...row.querySelectorAll('details')].some(details => details.open)) row.remove()
    })
    const legacyIds = new Set(data.reservas_antigas.map(item => String(item.id)))
    data.reservas_antigas.forEach(item => {
      let row = document.querySelector(`[data-legacy-row="${item.id}"]`)
      const legacyTable = document.querySelector('[data-history-legacy]')
      if (!row && legacyTable) {
        legacyTable.querySelector('[colspan]')?.closest('tr')?.remove()
        row = document.createElement('tr'); row.dataset.legacyRow = item.id
        for (const value of [item.empresa_id, item.telefone, item.rotulo, date(item.criado_em), item.erro]) {
          const cell = document.createElement('td'); setText(cell, value); row.append(cell)
        }
        legacyTable.prepend(row)
      } else if (row) setText(row.querySelector('td:nth-child(3)'), item.rotulo)
    })
    document.querySelectorAll('[data-history-legacy] [data-legacy-row]').forEach(row => {
      if (!legacyIds.has(row.dataset.legacyRow)) row.remove()
    })
  }

  function update(data) {
    if (section === 'inicio') updateDashboard(data.inicio)
    if (section === 'pesquisas') updateResearch(data.pesquisas?.itens || [], true)
    if (section === 'campanhas') updateCampaigns(data.campanhas?.itens || [])
    if (section === 'campanha') updateCampaign(data.campanha)
    if (section === 'historico') updateHistory(data.historico)
    paintConnection(data.conexao)
  }

  function staleCheck(failure = false) {
    const observed = new Date(document.getElementById('state-observed-at')?.dataset.time || lastConnectionAt)
    const elapsed = Date.now() - lastConnectionAt
    stale.hidden = !failure && elapsed < 15000
    stale.textContent = failure
      ? `Falha ao atualizar o estado. Última observação válida: ${date(observed)}.`
      : elapsed >= 15000
      ? `Estado sem atualização há mais de 15 segundos. Última observação válida: ${date(observed)}.`
      : ''
    clearTimeout(staleTimer)
    const remaining = 15000 - elapsed
    if (remaining > 0) staleTimer = setTimeout(staleCheck, remaining)
  }
  function schedule(delay) {
    clearTimeout(timer)
    if (!document.hidden) timer = setTimeout(poll, delay)
  }
  async function poll() {
    if (busy || document.hidden) return
    busy = true
    const run = ++generation
    request = new AbortController()
    let timedOut = false
    const timeout = setTimeout(() => { timedOut = true; request?.abort() }, 15000)
    const params = new URLSearchParams({secao: section})
    if (section === 'campanha') params.set('campanha_id', body.dataset.campaignId)
    try {
      const response = await fetch(`${endpoint}?${params}`, {signal: request.signal, headers: {'Accept': 'application/json'}})
      if (!response.ok) throw new Error('Resposta de estado inválida')
      const data = await response.json()
      if (run !== generation) return
      if (data.conexao?.consulta_ok) {
        failures = 0; stale.hidden = true; clearTimeout(staleTimer)
      } else {
        failures++
        staleCheck(true)
      }
      update(data)
      if (data.conexao?.consulta_ok) staleCheck()
      schedule(data.conexao?.consulta_ok ? 3000 : backoff[Math.min(failures - 1, backoff.length - 1)])
    } catch (error) {
      if (section === 'whatsapp') hideQr()
      if (run === generation && (error.name !== 'AbortError' || timedOut)) failures++
      if (run === generation) {
        staleCheck(true)
        schedule(backoff[Math.min(failures - 1, backoff.length - 1)])
      }
    } finally {
      clearTimeout(timeout); busy = false; request = null
      if (resumePending && !document.hidden) { resumePending = false; schedule(0) }
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(timer); clearTimeout(staleTimer); generation++; request?.abort()
    } else if (!busy) {
      generation++; poll()
    } else {
      resumePending = true
    }
  })
  document.addEventListener('submit', () => { generation++; request?.abort() }, true)
  poll()
})()
