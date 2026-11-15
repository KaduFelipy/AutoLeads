(() => {
  const form = document.getElementById('campaign-form')
  if (!form) return

  const radios = [...form.querySelectorAll('input[name="modo"]')]
  const panels = [...form.querySelectorAll('[data-mode-panel]')]
  const checkboxes = [...form.querySelectorAll('input[data-empresa-id]')]
  const quotas = [...form.querySelectorAll('[data-quota]')]
  const manualCount = document.getElementById('manual-total')
  const quotaTotal = document.getElementById('quota-total')
  const search = document.getElementById('company-search')
  const preservedQuotas = document.getElementById('preservar-quantidades')
  const preservedCompanies = document.getElementById('preservar-empresa-ids')

  function quotaState() {
    let state = {}
    try { state = JSON.parse(preservedQuotas?.value || '{}') || {} } catch (_) { state = {} }
    quotas.forEach(field => {
      if (field.value.trim()) state[field.name] = field.value
      else delete state[field.name]
    })
    return state
  }

  function companyState() {
    return [...new Set(checkboxes.filter(box => box.checked).map(box => Number(box.dataset.empresaId)))]
  }

  function preserveSelectionState() {
    if (preservedQuotas) preservedQuotas.value = JSON.stringify(quotaState())
    if (preservedCompanies) preservedCompanies.value = JSON.stringify(companyState())
  }

  function updateMode() {
    const mode = radios.find(radio => radio.checked)?.value || 'quantidades'
    panels.forEach(panel => {
      const active = panel.dataset.modePanel === mode
      panel.hidden = !active
      panel.querySelectorAll('input, select, textarea, button').forEach(control => {
        control.disabled = !active
      })
    })
    updateCounts()
  }

  function updateCounts() {
    const selected = new Set(checkboxes.filter(box => box.checked).map(box => box.dataset.empresaId))
    if (manualCount) manualCount.textContent = String(selected.size)
    form.querySelectorAll('[data-group-key]').forEach(group => {
      const ids = new Set([...group.querySelectorAll('input[data-empresa-id]:checked')]
        .map(box => box.dataset.empresaId))
      const counter = group.querySelector('[data-group-count]')
      if (counter) counter.textContent = String(ids.size)
    })
    if (quotaTotal) {
      const total = quotas.reduce((sum, field) => {
        if (!field.value.trim()) return sum
        const value = Number(field.value)
        return Number.isInteger(value) && value >= 0 ? sum + value : sum
      }, 0)
      quotaTotal.textContent = String(total)
    }
  }

  checkboxes.forEach(box => box.addEventListener('change', () => {
    checkboxes.filter(other => other.dataset.empresaId === box.dataset.empresaId)
      .forEach(other => { other.checked = box.checked })
    updateCounts()
    preserveSelectionState()
  }))
  quotas.forEach(field => field.addEventListener('input', () => {
    updateCounts()
    preserveSelectionState()
  }))
  radios.forEach(radio => radio.addEventListener('change', updateMode))
  form.addEventListener('submit', event => {
    if (event.submitter?.value === 'atualizar') preserveSelectionState()
  })

  if (search) search.addEventListener('input', () => {
    const query = search.value.trim().toLocaleLowerCase('pt-BR')
    form.querySelectorAll('[data-company-row]').forEach(row => {
      row.hidden = !row.dataset.companyName.includes(query)
    })
    form.querySelectorAll('.company-group').forEach(group => {
      group.hidden = !group.querySelector('[data-company-row]:not([hidden])')
    })
  })

  updateMode()
  preserveSelectionState()
})()
