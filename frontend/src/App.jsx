import { useEffect, useState } from 'react'
import VaultBrowser from "./components/VaultBrowser";

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

async function api(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const data = await res.json()
  if (!res.ok) {
    throw new Error(data.detail || data.message || 'Request failed')
  }
  return data
}

function JsonBlock({ value }) {
  return <pre className="json-block">{JSON.stringify(value, null, 2)}</pre>
}

function SectionCard({ title, subtitle, children }) {
  return (
    <section className="card">
      <div className="card-header">
        <h2>{title}</h2>
        {subtitle ? <p>{subtitle}</p> : null}
      </div>
      {children}
    </section>
  )
}

export default function App() {
  const [groupName, setGroupName] = useState('demo-group')
  const [algorithm, setAlgorithm] = useState('AES')
  const [keyLength, setKeyLength] = useState(256)
  const [selectedGroup, setSelectedGroup] = useState('demo-group')
  const [offset, setOffset] = useState(0)
  const [state, setState] = useState(null)
  const [health, setHealth] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [lastMessage, setLastMessage] = useState('')
  const [vaultTree, setVaultTree] = useState(null);

  async function refresh() {
    const [stateData, healthData, browserData] = await Promise.all([
      api('/api/state'),
      api('/api/health'),
      api('/api/vault-browser'),
    ])

    setState(stateData.data)
    setHealth(healthData.data)
    setVaultTree(browserData)
  }

  useEffect(() => {
    refresh().catch((err) => setError(err.message))
  }, [])

  async function runAction(action) {
    setBusy(true)
    setError('')
    try {
      const result = await action()
      setLastMessage(result.message)
      await refresh()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const groups = Object.values(state?.memory?.groups || {})

  return (
    <div className="app-shell">
      <header className="hero">
        <div>
          <h1>Vault KMIP Demo</h1>
          <p>
            Demonstrates a Python KMIP client talking to HashiCorp Vault KMIP, with local memory,
            Vault-side visibility, and verbose backend process logs.
          </p>
        </div>
        <button className="secondary" onClick={() => runAction(refresh)} disabled={busy}>
          Refresh
        </button>
      </header>

      {error ? <div className="banner error">{error}</div> : null}
      {lastMessage ? <div className="banner success">{lastMessage}</div> : null}

      <div className="grid two">
        <SectionCard title="Create encryption group" subtitle="Creates KEK, DEK1 and DEK2 via KMIP Create + Activate.">
          <div className="form-grid">
            <label>
              Group name
              <input value={groupName} onChange={(e) => setGroupName(e.target.value)} />
            </label>
            <label>
              Algorithm
              <select value={algorithm} onChange={(e) => setAlgorithm(e.target.value)}>
                <option value="AES">AES</option>
                <option value="HMAC_SHA256">HMAC_SHA256</option>
                <option value="HMAC_SHA384">HMAC_SHA384</option>
                <option value="HMAC_SHA512">HMAC_SHA512</option>
              </select>
            </label>
            <label>
              Key length
              <input
                type="number"
                value={keyLength}
                onChange={(e) => setKeyLength(Number(e.target.value))}
              />
            </label>
          </div>
          <button
            onClick={() =>
              runAction(() =>
                api('/api/groups/create', {
                  method: 'POST',
                  body: JSON.stringify({ group_name: groupName, algorithm, key_length: Number(keyLength) }),
                })
              )
            }
            disabled={busy}
          >
            {busy ? 'Working...' : 'Create group'}
          </button>
        </SectionCard>

        <SectionCard title="Rekey or delete" subtitle="Rekey rotates only the KEK. Delete revokes and destroys tracked objects.">
          <div className="form-grid">
            <label>
              Target group
              <select value={selectedGroup} onChange={(e) => setSelectedGroup(e.target.value)}>
                <option value="">Select a group</option>
                {groups.map((g) => (
                  <option key={g.group_name} value={g.group_name}>
                    {g.group_name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Rekey activation offset seconds
              <input type="number" value={offset} onChange={(e) => setOffset(Number(e.target.value))} />
            </label>
          </div>
          <div className="actions-row">
            <button
              onClick={() =>
                runAction(() =>
                  api(`/api/groups/${selectedGroup}/rekey`, {
                    method: 'POST',
                    body: JSON.stringify({ activation_offset_seconds: Number(offset) }),
                  })
                )
              }
              disabled={busy || !selectedGroup}
            >
              Rekey KEK
            </button>
            <button
              className="danger"
              onClick={() => runAction(() => api(`/api/groups/${selectedGroup}/delete`, { method: 'DELETE' }))}
              disabled={busy || !selectedGroup}
            >
              Delete group
            </button>
          </div>
        </SectionCard>
      </div>

      <div className="grid two">
        <SectionCard title="Backend health" subtitle="Bootstrap configuration and KMIP endpoint details.">
          <JsonBlock value={health} />
        </SectionCard>
        <SectionCard title="Local memory" subtitle="This is the in-process Python state of the demo app.">
          <JsonBlock value={state?.memory?.groups || {}} />
        </SectionCard>
      </div>

      <div className="grid two">
        <SectionCard
          title="Vault-side view"
          subtitle="Derived by issuing KMIP Locate/Get Attributes against Vault using the generated client certificate."
        >
          <JsonBlock value={state?.vault || []} />
        </SectionCard>

        <SectionCard
          title="Vault Browser"
          subtitle="Logical view of KMIP objects stored in Vault."
        >
          <VaultBrowser tree={vaultTree} />
        </SectionCard>
      </div>

      <SectionCard title="Verbose process log" subtitle="Newest entries first. Useful for illustrating the KMIP flow end to end.">
        <div className="log-list">
          {(state?.memory?.logs || []).map((entry, idx) => (
            <div className="log-item" key={`${entry.ts}-${idx}`}>
              <div className="log-meta">
                <span className={`pill ${entry.level.toLowerCase()}`}>{entry.level}</span>
                <span>{entry.ts}</span>
              </div>
              <strong>{entry.message}</strong>
              <JsonBlock value={entry.details} />
            </div>
          ))}
        </div>
      </SectionCard>
    </div>
  )
}
