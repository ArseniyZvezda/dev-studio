// @vitest-environment jsdom
import { createApp, h, nextTick, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AiDevice, AiExport, AiGrant, AiPolicy, AiRegistration } from '@/api/ai-access'
import { api } from '@/api/client'
import type { createRemoteApiClient, RemoteServerProfile } from '@/api/remote'
import type { ProjectRemoteStatus, Team, TeamMember } from '@/api/types'
import { t } from '@/i18n'
import RemoteAiAccessSettings from './RemoteAiAccessSettings.vue'

vi.mock('@/api/client', () => ({ api: { projectRemote: vi.fn(), teams: vi.fn() } }))
vi.mock('@/api/remote', () => ({ createRemoteApiClient: () => ({ listServers }) }))
vi.mock('@/session/project-session', async importOriginal => {
  const source = await importOriginal<typeof import('@/session/project-session')>()
  const rendererSession = new source.ProjectSession('12345678901234567890123456789012')
  rendererSession.bind({ project_id: rendererSession.projectId!, epoch: '98765432109876543210987654321098' })
  return { ...source, rendererSession }
})

const profileId = '11111111-1111-4111-8111-111111111111'
const serverId = '22222222-2222-4222-8222-222222222222'
const ownerId = '33333333-3333-4333-8333-333333333333'
const requesterId = '44444444-4444-4444-8444-444444444444'
const savedId = '55555555-5555-4555-8555-555555555555'
const draftId = '66666666-6666-4666-8666-666666666666'
const grantId = '77777777-7777-4777-8777-777777777777'
const ownerUserId = '88888888-8888-4888-8888-888888888888'
const requesterUserId = '99999999-9999-4999-8999-999999999999'
const profile: RemoteServerProfile = { profile_id: profileId, url: 'https://remote.example/prefix', revision: 11,
  name: 'Remote', credential_source: 'stored', has_token: true, verification: { state: 'unavailable' } }
const owner: AiRegistration = { profile_id: profileId, url: profile.url, server_instance_id: serverId,
  revision: 1, registration_id: ownerId, user_id: ownerUserId, client_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  display_name: 'Executor', kind: 'studio', publication_enabled: false, has_device_token: true }
const requester: AiDevice = { registration_id: requesterId, user_id: requesterUserId,
  client_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', display_name: 'Other device', kind: 'studio' }
const selected: ProjectRemoteStatus = {
  project_id: '12345678901234567890123456789012', epoch: '98765432109876543210987654321098',
  profile_id: profileId, backend_epoch: 'backend-1', identity_revision: 9, config_fingerprint: 'fingerprint',
  persistent_id: 'team-project', space_instance_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', url: profile.url,
  binding_generation: 7, enabled: false, has_token: true, active_rooms: 0, pending_writes: 0, state: 'disabled', binding_change: null,
}
const draft: AiExport = { enabled: false, display_name: 'Our draft', mirror_state: 'unpublished', target: {
  export_id: draftId, executor_registration_id: ownerId, server_instance_id: serverId,
  persistent_id: selected.persistent_id!, storage_epoch: selected.space_instance_id!, export_revision: 1,
} }
const exactSaved: AiExport = { ...draft, enabled: true, display_name: 'Some saved copy', mirror_state: 'effective',
  target: { ...draft.target, export_id: savedId, export_revision: 5 } }
const digest = 'f'.repeat(64)
const policy: AiPolicy = { target: exactSaved.target, team_id: 'exact-team', member_id: 'exact-agent',
  execution_policy_digest: digest, backend: 'codex', permission_mode: 'review',
  tools: { backend_native_tools_may_include_shell: true }, mcp: { strict: true, project_server_count: 2 },
  limits: { max_turns: 5, max_run_continuations: 3, project_agent_slots: 4, remote_runs_per_export: 2, remote_runs_per_grant: 1 } }
const priorGrant: AiGrant = { target: exactSaved.target, grant_id: grantId, revision: 7, resource_scope: 'created_by_this_grantee',
  grantee_user_id: requesterUserId, grantee_registration_id: requesterId, operations: ['view_status', 'run_task'],
  team_ids: [policy.team_id], member_ids: [policy.member_id], resource_ids: [], execution_policy_digest: 'a'.repeat(64),
  issued_at: 1_000, expires_at: 2_000, revoked_at: null }

const listServers = vi.fn<ReturnType<typeof createRemoteApiClient>['listServers']>()
const views: { app: App; host: HTMLDivElement }[] = []
const calls: { method: string; path: string; body?: unknown }[] = []
const fetchMock = vi.fn<typeof fetch>()
let routes: Record<string, unknown>
const prefix = '/api/ai-access'
const devicePath = `/registrations/${ownerId}`
const savedPath = `/exports/${savedId}`
const draftPath = `/exports/${draftId}`
const grantPath = `/grants/${grantId}`
const previewPath = `${savedPath}/teams/exact-team/members/exact-agent/execution-policy`

function mount(project = false) {
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(RemoteAiAccessSettings, { project }) })
  app.mount(host)
  views.push({ app, host })
  return host
}
async function flush() {
  // Node Response.json(), Vue's queues and local client promises; no live transport.
  for (let index = 0; index < 24; index++) { await Promise.resolve(); await nextTick() }
}
function field<T extends HTMLElement>(host: ParentNode, suffix: string): T {
  const el = host.querySelector<T>(`[data-testid="ai-owner-${suffix}"]`)
  expect(el, suffix).not.toBeNull()
  return el!
}
async function click(host: ParentNode, suffix: string) { field<HTMLButtonElement>(host, suffix).click(); await flush() }
async function set(host: ParentNode, suffix: string, value: string | boolean) {
  const el = field<HTMLSelectElement | HTMLInputElement>(host, suffix)
  if (typeof value === 'boolean') (el as HTMLInputElement).checked = value
  else el.value = value
  el.dispatchEvent(new Event(el.tagName === 'SELECT' || typeof value === 'boolean' ? 'change' : 'input', { bubbles: true }))
  await flush()
}
function writes() { return calls.filter(row => row.method !== 'GET') }
function reply(code: number, detail: string) { return new Response(JSON.stringify({ detail }), { status: code }) }
function enabledProject() { routes['GET /registrations'] = { registrations: [{ ...owner, publication_enabled: true }] } }
async function chooseSaved() {
  enabledProject()
  routes['GET /exports'] = { exports: [exactSaved] }
  const host = mount(true)
  await flush()
  expect(host.querySelector('[data-testid="ai-owner-export"]')).toBeNull()
  await set(host, 'saved', savedId)
  return host
}
async function grantPlan(host: HTMLDivElement, mode: 'new' | 'edit' = 'new') {
  if (mode === 'new') {
    await click(host, 'read-devices')
    await set(host, 'grantee', requesterId)
  } else {
    await click(host, 'read-grants')
    await click(host, 'edit-grant')
  }
  await set(host, 'team', 'exact-team')
  await set(host, 'member', 'exact-agent')
  await set(host, 'op-view_status', true)
}

beforeEach(() => {
  calls.length = 0
  routes = {
    'GET /registrations': { registrations: [] }, 'GET /exports': { exports: [] },
    [`GET /profiles/${profileId}/capabilities`]: { kind: 'supported', code: null, server_instance_id: serverId },
    [`GET ${devicePath}/directory`]: { registrations: [owner, requester] },
    [`GET ${previewPath}`]: policy,
    [`GET ${savedPath}/grants`]: { grants: [{ grant: priorGrant, acked_revision: 7 }] },
  }
  listServers.mockReset().mockResolvedValue({ items: [profile] })
  vi.mocked(api.projectRemote).mockReset().mockResolvedValue(selected)
  vi.mocked(api.teams).mockReset().mockResolvedValue([])
  fetchMock.mockReset().mockImplementation(async (input, init) => {
    const path = String(input)
    if (!path.startsWith(prefix)) throw new Error(`Unexpected fetch: ${path}`)
    const method = init?.method ?? 'GET'
    const url = path.slice(prefix.length)
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
    calls.push({ method, path: url, ...(body === undefined ? {} : { body }) })
    const response = routes[`${method} ${url}`]
    if (response instanceof Response) return response
    if (typeof response === 'function') return response(body)
    if (response === undefined) return reply(500, 'test_route_missing')
    return Response.json(response)
  })
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => {
  for (const { app, host } of views.splice(0)) { app.unmount(); host.remove() }
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Remote AI owner settings', () => {
  it('requires explicit discovery, registration, publication and unregister as distinct exact requests', async () => {
    routes['POST /registrations'] = owner
    routes[`PUT ${devicePath}/publication`] = { ...owner, revision: 2, publication_enabled: true }
    routes[`DELETE ${devicePath}`] = new Response(null, { status: 204 })
    const host = mount()
    await flush()
    expect(calls).toEqual([{ method: 'GET', path: '/registrations' }])
    await set(host, 'profile', profileId)
    expect(host.querySelector('[data-testid="ai-owner-register"]')).toBeNull()
    await click(host, 'discover')
    expect(host.textContent).toContain(profile.url)
    expect(host.textContent).toContain(serverId)
    await set(host, 'device-name', '  Device name  ')
    expect(writes()).toEqual([])
    await click(host, 'register')
    expect(writes()).toEqual([{ method: 'POST', path: '/registrations', body: {
      profile_id: profileId, server_instance_id: serverId, display_name: 'Device name',
    } }])
    expect(host.textContent).toContain(ownerUserId)
    expect(host.textContent).toContain(owner.client_id)
    await click(host, 'device-switch')
    expect(writes().at(-1)).toEqual({ method: 'PUT', path: `${devicePath}/publication`, body: { enabled: true, expected_revision: 1 } })
    await click(host, 'unregister')
    expect(writes()).toHaveLength(2)
    await click(host, 'confirm-unregister')
    expect(writes().at(-1)).toEqual({ method: 'DELETE', path: devicePath, body: { expected_revision: 2 } })
  })

  it('denies registration for an unsupported server and does not follow a profile changed after discovery', async () => {
    routes[`GET /profiles/${profileId}/capabilities`] = { kind: 'unsupported', code: 'not_mounted', server_instance_id: null }
    const host = mount()
    await flush()
    await set(host, 'profile', profileId)
    await click(host, 'discover')
    expect(host.querySelector('[data-testid="ai-owner-register"]')).toBeNull()
    expect(host.textContent).toContain(t('aiAccess.owner.unsupported'))
    routes[`GET /profiles/${profileId}/capabilities`] = { kind: 'supported', code: null, server_instance_id: serverId }
    await click(host, 'discover')
    listServers.mockResolvedValueOnce({ items: [{ ...profile, url: `${profile.url}/changed`, revision: 12 }] })
    await set(host, 'device-name', 'New device')
    await click(host, 'register')
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(t('aiAccess.owner.stale'))
    expect(writes()).toEqual([])
  })

  it('saves only a disabled draft from ordinary Select and publishes it with a separate CAS', async () => {
    enabledProject()
    routes['GET /exports'] = { exports: [exactSaved] } // same catalog ID is intentionally not the draft
    routes['POST /exports'] = draft
    routes[`PUT ${draftPath}/publication`] = { ...draft, enabled: true, mirror_state: 'effective' }
    const host = mount(true)
    await flush()
    expect(api.projectRemote).toHaveBeenCalledTimes(1)
    expect(host.querySelector('[data-testid="ai-owner-export"]')).toBeNull()
    expect(host.textContent).toContain(t('aiAccess.owner.savedExportsNote'))
    await set(host, 'executor', ownerId)
    expect(field(host, 'consent-target').textContent).toContain(ownerUserId)
    expect(field(host, 'consent-target').textContent).toContain(profile.url)
    await set(host, 'draft-name', 'Our draft')
    await click(host, 'create-draft')
    expect(writes()).toHaveLength(0)
    await set(host, 'draft-consent', true)
    await set(host, 'draft-name', 'Renamed draft')
    expect(field<HTMLInputElement>(host, 'draft-consent').checked).toBe(false)
    await set(host, 'draft-consent', true)
    await click(host, 'create-draft')
    expect(writes()).toEqual([{ method: 'POST', path: '/exports', body: {
      registration_id: ownerId, profile_id: profileId, server_instance_id: serverId,
      project_id: selected.project_id, project_epoch: selected.epoch, persistent_id: selected.persistent_id,
      storage_epoch: selected.space_instance_id, identity_revision: selected.identity_revision, display_name: 'Renamed draft',
    } }])
    expect(field(host, 'export').textContent).toContain(t('aiAccess.owner.newExport'))
    expect(field(host, 'export').textContent).toContain(draftId)
    await click(host, 'export-switch')
    expect(writes().at(-1)).toEqual({ method: 'PUT', path: `${draftPath}/publication`, body: { enabled: true, expected_revision: 1 } })
    vi.mocked(api.teams).mockResolvedValue([{ id: policy.team_id, name: 'Our team', members: [
      { id: policy.member_id, name: 'An agent', kind: 'agent' } as TeamMember,
      { id: 'person', name: 'A human', kind: 'human' } as TeamMember,
    ] } as Team])
    await click(host, 'read-devices')
    expect(api.teams).toHaveBeenCalledTimes(1)
    await set(host, 'team', policy.team_id)
    expect(field(host, 'member').textContent).toContain('An agent')
    expect(field(host, 'member').textContent).not.toContain('A human')
  })

  it('drops reviewed draft consent when the binding moves instead of posting to a newer target', async () => {
    enabledProject()
    const host = mount(true)
    await flush()
    await set(host, 'executor', ownerId)
    await set(host, 'draft-name', 'Our draft')
    await set(host, 'draft-consent', true)
    vi.mocked(api.projectRemote).mockResolvedValue({ ...selected, identity_revision: 10 })
    await click(host, 'create-draft')
    expect(writes()).toEqual([])
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(t('aiAccess.owner.stale'))
    await click(host, 'refresh')
    expect(writes()).toEqual([])
    expect(field<HTMLSelectElement>(host, 'executor').value).toBe('')
  })

  it('requires a fresh matching policy and separate consent for an exact least-privilege grant', async () => {
    const host = await chooseSaved()
    expect(field(host, 'export').textContent).toContain(t('aiAccess.owner.savedExport'))
    expect(field(host, 'export').textContent).not.toContain(t('aiAccess.owner.newExport'))
    await grantPlan(host)
    expect(api.teams).not.toHaveBeenCalled() // a saved portable ID is not proof of the local root
    expect(field<HTMLInputElement>(host, 'op-run_task').checked).toBe(false)
    expect(field<HTMLSelectElement>(host, 'scope').value).toBe('created_by_this_grantee')
    await click(host, 'preview')
    expect(writes()).toEqual([])
    expect(field(host, 'policy').textContent).toContain('codex')
    expect(field(host, 'policy').textContent).toContain('review')
    expect(field(host, 'policy').textContent).toContain(t('aiAccess.owner.shellRisk'))
    await set(host, 'grant-consent', true)
    await set(host, 'op-view_transcript', true)
    expect(host.querySelector('[data-testid="ai-owner-save-grant"]')).toBeNull()
    await set(host, 'op-view_transcript', false)
    await click(host, 'preview')
    await set(host, 'grant-consent', true)
    routes[`POST ${savedPath}/grants`] = (body: unknown) => Response.json({ grant: { ...priorGrant, ...body as object }, acked_revision: 7 })
    const before = Date.now()
    await click(host, 'save-grant')
    const body = writes()[0]?.body as { expires_at: number }
    expect(writes()).toEqual([{ method: 'POST', path: `${savedPath}/grants`, body: {
      expected_export_revision: 5, grantee_user_id: requesterUserId, grantee_registration_id: requesterId,
      operations: ['view_status'], team_ids: ['exact-team'], member_ids: ['exact-agent'], execution_policy_digest: digest,
      resource_scope: 'created_by_this_grantee', resource_ids: [], expires_at: expect.any(Number),
    } }])
    expect(body.expires_at).toBeGreaterThanOrEqual(before + 86_400_000)
    expect(body.expires_at).toBeLessThanOrEqual(Date.now() + 86_400_000)
    expect(calls.filter(row => row.path === previewPath)).toHaveLength(2)
  })

  it('sends one exact PUT with old grant CAS and new policy digest, never retrying a stale conflict', async () => {
    const host = await chooseSaved()
    await grantPlan(host, 'edit')
    await click(host, 'preview')
    await set(host, 'grant-consent', true)
    routes[`PUT ${savedPath}/grants/${grantId}`] = reply(409, 'stale_target')
    await click(host, 'save-grant')
    expect(writes()).toEqual([{ method: 'PUT', path: `${savedPath}/grants/${grantId}`, body: {
      expected_revision: 7, expected_export_revision: 5, operations: ['view_status'], team_ids: ['exact-team'],
      member_ids: ['exact-agent'], execution_policy_digest: digest, resource_scope: 'created_by_this_grantee',
      resource_ids: [], expires_at: expect.any(Number),
    } }])
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(t('aiAccess.owner.stale'))
    expect(host.querySelector('[data-testid="ai-owner-save-grant"]')).toBeNull()
    await click(host, 'refresh')
    expect(writes()).toHaveLength(1)
    expect(calls.filter(row => row.path === previewPath)).toHaveLength(1)
  })

  it('rejects a mismatched preview and revokes by the existing grant CAS after explicit refresh', async () => {
    const host = await chooseSaved()
    await grantPlan(host)
    routes[`GET ${previewPath}`] = { ...policy, target: { ...policy.target, export_revision: 6 } }
    await click(host, 'preview')
    expect(host.querySelector('[data-testid="ai-owner-policy"]')).toBeNull()
    expect(writes()).toHaveLength(0)
    await click(host, 'refresh')
    await set(host, 'saved', savedId)
    await click(host, 'read-grants')
    routes[`DELETE ${grantPath}`] = { grant: { ...priorGrant, revision: 8, revoked_at: 1_500 }, acked_revision: 7 }
    await click(host, 'revoke')
    expect(writes()).toEqual([{ method: 'DELETE', path: grantPath, body: { expected_revision: 7 } }])
    expect(host.textContent).toContain(t('aiAccess.owner.grantRevokedLocal'))
  })
})
