(() => {
  const form = document.getElementById('campaign-form')
  if (!form) return
  const radios = [...form.querySelectorAll('input[name="modo"]')]
  const panels = [...form.querySelectorAll('[data-mode-panel]')]
  const quotas = [...form.querySelectorAll('[data-quota]')]
  const manualCount = document.getElementById('manual-total')
  const quotaTotal = document.getElementById('quota-total')
  const preservedQuotas = document.getElementById('preservar-quantidades')
  const preservedCompanies = document.getElementById('preservar-empresa-ids')
  const results = document.getElementById('company-results')
  const search = document.getElementById('company-search')
  const classification = document.getElementById('company-classification')
  const resultCount = document.getElementById('company-result-count')
  const errorBox = document.getElementById('company-list-error')
  const pagination = document.getElementById('company-pagination')
  const pageValue = document.getElementById('company-page-value')
  const summary = document.getElementById('selected-companies-summary')
  const selectedNodes = new Map()
  const selected = new Set(JSON.parse(preservedCompanies?.value || '[]').map(Number))
  const metadata = new Map()
  try { JSON.parse(document.getElementById('company-page-data')?.textContent || '[]').forEach(item => metadata.set(item.id, item)) } catch (_) {}
  try { JSON.parse(document.getElementById('selected-companies-data')?.textContent || '[]').forEach(item => metadata.set(item.id, item)) } catch (_) {}
  let page = Number(results?.dataset.page || 1)
  let totalPages = Number(results?.dataset.totalPages || 1)
  let generation = 0
  let controller
  let timer
  let summaryPage = 1

  function quotaState() {
    let state = {}
    try { state = JSON.parse(preservedQuotas?.value || '{}') || {} } catch (_) {}
    quotas.forEach(field => field.value.trim() ? state[field.name] = field.value : delete state[field.name])
    return state
  }
  function preserveSelectionState() {
    if (preservedQuotas) preservedQuotas.value = JSON.stringify(quotaState())
    if (preservedCompanies) preservedCompanies.value = JSON.stringify([...selected])
  }
  function updateCounts() {
    if (manualCount) manualCount.textContent = String(selected.size)
    if (quotaTotal) quotaTotal.textContent = String(quotas.reduce((sum, field) => {
      const value = Number(field.value)
      return field.value.trim() && Number.isInteger(value) && value >= 0 ? sum + value : sum
    }, 0))
    const container = summary.querySelector('div')
    container.replaceChildren()
    const offPage = [...selected].filter(id => !selectedNodes.has(id))
    const pages = Math.max(1, Math.ceil(offPage.length / 50))
    summaryPage = Math.min(summaryPage, pages)
    summary.hidden = offPage.length === 0
    form.querySelectorAll('[data-group-count]').forEach(counter => {
      const key = counter.dataset.groupCount
      counter.textContent = String([...selected].filter(id =>
        (metadata.get(id)?.classificacoes || []).some(tag => tag.chave === key)).length)
    })
    summary.querySelector('h3').textContent = `Selecionadas fora da página (${offPage.length})`
    ;offPage.slice((summaryPage - 1) * 50, summaryPage * 50).forEach(id => {
      const item = metadata.get(id)
      if (!item) return
      const label = document.createElement('label')
      label.className = 'company-choice'
      const checkbox = document.createElement('input')
      checkbox.type = 'checkbox'; checkbox.checked = true; checkbox.dataset.summaryId = String(id)
      const span = document.createElement('span'), name = document.createElement('strong'), phone = document.createElement('small')
      name.textContent = item.nome; phone.textContent = `${item.telefone || 'Sem telefone'}${item.classificacoes?.length ? ` · ${item.classificacoes.map(tag => tag.nome).join(', ')}` : ''}`
      span.append(name, phone); label.append(checkbox, span); container.append(label)
      checkbox.addEventListener('change', () => setSelected(id, false))
    })
    let pager = summary.querySelector('[data-summary-pagination]')
    if (offPage.length > 50) {
      if (!pager) { pager = document.createElement('nav'); pager.dataset.summaryPagination = ''; pager.setAttribute('aria-label', 'Páginas de selecionadas'); summary.append(pager) }
      pager.replaceChildren()
      const previous = document.createElement('button'); previous.type = 'button'; previous.textContent = 'Anterior'; previous.disabled = summaryPage <= 1
      const label = document.createElement('span'); label.textContent = `${summaryPage} / ${pages}`
      const next = document.createElement('button'); next.type = 'button'; next.textContent = 'Próxima'; next.disabled = summaryPage >= pages
      previous.addEventListener('click', () => { summaryPage--; updateCounts() })
      next.addEventListener('click', () => { summaryPage++; updateCounts() })
      pager.append(previous, label, next)
    } else pager?.remove()
  }
  function setSelected(id, checked) {
    if (checked && !selected.has(id) && selected.size >= 1000) {
      const box = [...form.querySelectorAll('input[data-empresa-id]')].find(item => Number(item.value) === id)
      if (box) box.checked = false
      errorBox.textContent = 'A seleção manual permite no máximo 1000 empresas.'; errorBox.hidden = false
      return
    }
    checked ? selected.add(id) : selected.delete(id)
    selectedNodes.get(id)?.forEach(node => { node.checked = checked })
    preserveSelectionState(); updateCounts()
  }
  function commonParams() {
    const params = new URLSearchParams()
    ;['cidade', 'com_telefone', 'ultimo_disparo', 'desde'].forEach(name => {
      const field = form.elements.namedItem(name)
      if (field) params.set(name, field.value)
    })
    return params
  }
  function renderCompanies(data) {
    results.replaceChildren()
    selectedNodes.clear()
    data.empresas.forEach(item => {
      metadata.set(item.id, item)
      const label = document.createElement('label'); label.className = 'company-choice'; label.dataset.companyRow = ''; label.dataset.companyId = item.id
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.value = item.id; checkbox.dataset.empresaId = item.id; checkbox.checked = selected.has(item.id)
      checkbox.addEventListener('change', () => setSelected(item.id, checkbox.checked))
      const span = document.createElement('span'), name = document.createElement('strong'), phone = document.createElement('small')
      name.textContent = item.nome; phone.textContent = `${item.telefone || 'Sem telefone'}${item.classificacoes?.length ? ` · ${item.classificacoes.map(tag => tag.nome).join(', ')}` : ''}`; span.append(name, phone); label.append(checkbox, span); results.append(label)
      selectedNodes.set(item.id, [checkbox])
    })
    if (!data.empresas.length) { const empty = document.createElement('p'); empty.className = 'muted empty-group'; empty.textContent = 'Nenhuma empresa com os filtros atuais.'; results.append(empty) }
    page = data.pagina; totalPages = data.total_paginas
    if (pageValue) pageValue.value = String(page)
    resultCount.textContent = `${data.total} empresas`
    pagination.querySelector('span').textContent = `${page} / ${totalPages}`
    pagination.querySelector('[data-page-step="-1"]').disabled = page <= 1
    pagination.querySelector('[data-page-step="1"]').disabled = page >= totalPages
    updateCounts()
  }
  async function loadCompanies(nextPage = 1) {
    if (!results) return
    controller?.abort(); controller = new AbortController(); const request = ++generation
    const params = commonParams(); params.set('q', search.value); params.set('classificacao', classification.value); params.set('pagina', nextPage)
    try {
      const response = await fetch(`${results.dataset.endpoint}?${params}`, {signal: controller.signal, headers: {'Accept': 'application/json'}})
      const data = await response.json()
      if (request !== generation) return
      if (!response.ok) throw new Error(Object.values(data.erros || {}).flat().join(' '))
      errorBox.hidden = true; renderCompanies(data)
    } catch (error) {
      if (error.name === 'AbortError' || request !== generation) return
      errorBox.textContent = error.message || 'Não foi possível atualizar a lista.'; errorBox.hidden = false
    }
  }
  function updateMode() {
    const mode = radios.find(radio => radio.checked)?.value || 'quantidades'
    panels.forEach(panel => {
      const active = panel.dataset.modePanel === mode
      panel.hidden = !active
      panel.querySelectorAll('input, select, textarea, button').forEach(control => control.disabled = !active)
    })
    updateCounts()
  }
  form.querySelectorAll('input[data-empresa-id]').forEach(box => {
    const id = Number(box.value); metadata.has(id) || metadata.set(id, {id, nome: box.parentElement.querySelector('strong').textContent, telefone: box.parentElement.querySelector('small').textContent})
    selectedNodes.set(id, [box]); box.addEventListener('change', () => setSelected(id, box.checked))
  })
  quotas.forEach(field => field.addEventListener('input', () => { updateCounts(); preserveSelectionState() }))
  radios.forEach(radio => radio.addEventListener('change', updateMode))
  search?.addEventListener('input', () => { generation++; controller?.abort(); clearTimeout(timer); timer = setTimeout(() => loadCompanies(1), 300) })
  classification?.addEventListener('change', () => { generation++; controller?.abort(); loadCompanies(1) })
  pagination?.querySelectorAll('[data-page-step]').forEach(button => button.addEventListener('click', () => loadCompanies(page + Number(button.dataset.pageStep))))
  form.addEventListener('submit', event => {
    preserveSelectionState()
    const manual = radios.find(radio => radio.checked)?.value === 'manual'
    form.querySelectorAll('input[data-empresa-id]').forEach(box => { box.disabled = true; box.removeAttribute('name') })
    form.querySelectorAll('[data-generated-company-id]').forEach(input => input.remove())
    if (manual) selected.forEach(id => {
      const input = document.createElement('input'); input.type = 'hidden'; input.name = 'empresa_ids'; input.value = id; input.dataset.generatedCompanyId = ''; form.append(input)
    })
  })
  updateMode(); updateCounts()
})()
