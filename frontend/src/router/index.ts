/**
 * Static routes for the built-in sections; plugin pages are added at runtime by
 * `plugin-runtime/loader.ts` once the registry has been fetched (spec §9.3).
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/explorer' },
  {
    path: '/explorer',
    name: 'explorer',
    component: () => import('@/views/ExplorerView.vue'),
  },
  {
    path: '/pipelines',
    name: 'pipelines',
    component: () => import('@/views/PipelinesView.vue'),
  },
  {
    path: '/pipelines/:id',
    name: 'pipeline-editor',
    component: () => import('@/views/PipelineEditorView.vue'),
    props: true,
  },
  {
    // Dedicated workspace with a pinned Producer and live task conversations (§10.11).
    path: '/producer',
    name: 'producer',
    component: () => import('@/views/ProducerView.vue'),
  },
  {
    // The board. `?assign=<member id>` opens the composer for that member — a deep link
    // into "give this person something to do", kept now that the org chart's own Assign
    // button is gone, since it costs nothing and Teams is deliberately not a place that
    // creates work.
    path: '/tasks',
    name: 'tasks',
    component: () => import('@/views/TasksView.vue'),
  },
  {
    path: '/teams',
    name: 'teams',
    component: () => import('@/views/TeamsView.vue'),
  },
  {
    // What the team's work has cost — every task, archived ones included (§10.4).
    path: '/statistics',
    name: 'statistics',
    component: () => import('@/views/StatisticsView.vue'),
  },
  {
    // Not `/docs`: FastAPI serves its Swagger UI there, and it wins on a full page load,
    // so a reload or a bookmark would land on the API explorer instead of this view.
    path: '/documents',
    name: 'docs',
    component: () => import('@/views/DocsView.vue'),
  },
  {
    // Full-page access to the same conversations as the floating chat (§10.9).
    path: '/chats',
    name: 'chats',
    component: () => import('@/views/ChatsView.vue'),
  },
  {
    // The whole corpus as a live graph, filling the page (block-editor spec §5.1.1). A sibling
    // record rather than a child of `/documents`, so the rail marks one row, not both.
    path: '/documents/graph',
    name: 'docs-graph',
    component: () => import('@/views/DocsGraphView.vue'),
  },
  {
    // `voxarium_studio/automations.yaml` — what the project does by itself: a trigger and an action
    // each, run by the backend off its own event bus (§6.5).
    path: '/automations',
    name: 'automations',
    component: () => import('@/views/AutomationsView.vue'),
  },
  {
    // Which version control system the project is under, and what the Studio does with
    // it — one page over `VcsBackend`, git or otherwise (§9.6).
    path: '/vcs',
    name: 'vcs',
    component: () => import('@/views/VcsView.vue'),
  },
  {
    // `voxarium_studio/skills/` — folders of instructions the team's agents can be given (§10.10).
    path: '/skills',
    name: 'skills',
    component: () => import('@/views/SkillsView.vue'),
  },
  {
    // `.mcp.json` at the project root — which MCP servers the project's agents get (§10.7).
    path: '/mcp',
    name: 'mcp',
    component: () => import('@/views/McpView.vue'),
  },
  {
    path: '/plugins',
    name: 'plugins',
    component: () => import('@/views/PluginsView.vue'),
  },
  {
    // Managed-project settings are separate from application preferences (§5.2).
    path: '/project-settings',
    name: 'project-settings',
    component: () => import('@/views/ProjectSettingsView.vue'),
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

/**
 * Keep `?token=` out of the address bar.
 *
 * `api/auth.captureTokenFromUrl()` stashes the token and scrubs the URL, but this module
 * is evaluated when `main.ts` imports it — before that call — so the router's first
 * navigation reads the original location and puts the query back. Dropping it here runs
 * after the token has been stored, and as a redirect on the initial navigation it
 * replaces rather than pushes, so the credential never reaches session history.
 */
router.beforeEach((to) => {
  if (to.query.token === undefined) return true
  const { token: _discarded, ...rest } = to.query
  return { path: to.path, query: rest, hash: to.hash, replace: true }
})
