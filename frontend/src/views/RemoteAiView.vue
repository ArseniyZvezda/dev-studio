<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { locale, t, type MessageKey } from '@/i18n'
import { createAiAccessStore, validPaths, validText, validTitle, type AiResourceKind } from '@/stores/ai-access'

const store = createAiAccessStore()
const { registrations, registrationId, targets, targetCursor, directoryBusy, directoryError,
  pin, availability, online, busy, actionError, resources, resourceCursor, resource, scope, live,
  messages, transcriptCursor, transcriptLimit, commandState, cancellationRequested,
  created, receiptsReady, receiptError, unknown, canWrite, waitRevision, permits, canAction } = store

const kind = ref<AiResourceKind>('task')
const team = ref('')
const member = ref('')
const title = ref('')
const brief = ref('')
const paths = ref('')
const message = ref('')
const allowCreateTask = computed(() => permits('create_task'))
const allowCreateChat = computed(() => permits('create_chat'))
const writePaths = computed(() => paths.value.split(/\r?\n/).map((path) => path.trim()).filter(Boolean))
const draftValid = computed(() => !!team.value && !!member.value && validTitle(title.value)
  && (kind.value === 'chat' || (validText(brief.value) && validPaths(writePaths.value))))

function resetDraft() {
  team.value = member.value = title.value = brief.value = paths.value = ''
  kind.value = allowCreateTask.value ? 'task' : 'chat'
}
watch([pin, online], () => { resetDraft(); message.value = '' }, { flush: 'sync' })
watch(scope, () => { message.value = '' }, { flush: 'sync' })
onMounted(() => { void store.refresh() })
onBeforeUnmount(store.dispose)

// Machine codes are only used to pick a fixed string; no untrusted string is a locale key.
const statuses: Record<string, MessageKey> = {
  todo: 'aiAccess.requester.stateTodo', pending: 'aiAccess.requester.statePending',
  in_progress: 'aiAccess.requester.stateInProgress', needs_review: 'aiAccess.requester.stateNeedsReview',
  blocked: 'aiAccess.requester.stateBlocked', done: 'aiAccess.requester.stateDone',
  cancelled: 'aiAccess.requester.stateCancelled', idle: 'aiAccess.requester.stateIdle',
  running: 'aiAccess.requester.stateRunning', cancelling: 'aiAccess.requester.stateCancelling',
  error: 'aiAccess.requester.stateError',
}
function stateLabel(state: string) {
  return Object.hasOwn(statuses, state) ? t(statuses[state]) : t('aiAccess.requester.stateUnknown')
}
function kindLabel(value: AiResourceKind) {
  return t(value === 'task' ? 'aiAccess.requester.kindTask' : 'aiAccess.requester.kindChat')
}
function extra(key: 'receiptUnavailable' | 'created' | 'transcriptLimit' | 'roleUser' | 'roleAgent' | 'roleOther'
  | 'resync' | 'intentStorage' | 'commandAccepted' | 'commandDispatching' | 'commandRejected' | 'commandFailed',
  params?: Record<string, string>) {
  return t(`aiAccess.requester.${key}` as MessageKey, params)
}
function roleLabel(role: string) {
  return extra(role === 'user' ? 'roleUser' : role === 'assistant' ? 'roleAgent' : 'roleOther')
}
const availabilityLabel = computed(() => availability.value === 'resync' ? extra('resync')
  : t({ opening: 'aiAccess.requester.loading', online: 'aiAccess.requester.online', offline: 'aiAccess.requester.offline',
    stale: 'aiAccess.requester.stale', revoked: 'aiAccess.requester.revoked', unavailable: 'aiAccess.requester.unavailable' }[availability.value] as MessageKey))
const commandLabel = computed(() => {
  switch (commandState.value) {
    case 'accepted': return extra('commandAccepted')
    case 'dispatching': return extra('commandDispatching')
    case 'rejected': case 'denied_before_dispatch': return extra('commandRejected')
    case 'failed': return extra('commandFailed')
    case 'completed': return stateLabel('done')
    default: return stateLabel(commandState.value)
  }
})

async function create() {
  if (!draftValid.value || !canWrite.value) return
  const current = pin.value
  await store.create(kind.value, team.value, member.value, title.value, locale.value, brief.value, writePaths.value)
  if (pin.value === current && created.value) resetDraft()
}
async function messageAction(action: 'note' | 'send') {
  if (!canAction(action) || !canWrite.value || !validText(message.value)) return
  const previous = scope.value
  const sent = message.value
  await store.act(action, sent)
  if (scope.value === previous && message.value === sent
    && ['accepted', 'dispatching', 'running', 'completed'].includes(commandState.value)) message.value = ''
}
</script>

<template>
  <main class="min-h-0 flex-1 overflow-auto bg-surface text-primary" aria-labelledby="remote-ai-title">
    <div class="mx-auto flex max-w-5xl flex-col gap-5 p-6">
      <header>
        <h1 id="remote-ai-title" class="text-xl font-semibold">{{ t('aiAccess.requester.title') }}</h1>
        <p class="mt-1 text-sm text-muted">{{ t('aiAccess.requester.intro') }}</p>
      </header>

      <section class="panel" aria-labelledby="remote-ai-targets">
        <h2 id="remote-ai-targets">{{ t('aiAccess.requester.targets') }}</h2>
        <div class="flex flex-wrap items-end gap-3">
          <label class="field grow">
            {{ t('aiAccess.requester.registration') }}
            <select :value="registrationId" :disabled="busy" data-testid="requester-registration"
              @change="store.chooseRegistration(($event.target as HTMLSelectElement).value)">
              <option value="">{{ t('aiAccess.requester.chooseRegistration') }}</option>
              <option v-for="reg in registrations" :key="reg.registration_id" :value="reg.registration_id">
                {{ reg.display_name }} — {{ reg.url }} — {{ reg.registration_id }}
              </option>
            </select>
          </label>
          <button type="button" class="button" :disabled="busy || directoryBusy" @click="store.refresh()">
            {{ t('aiAccess.requester.refresh') }}
          </button>
        </div>
        <p v-if="directoryBusy" role="status">{{ t('aiAccess.requester.loading') }}</p>
        <p v-else-if="directoryError" role="alert">{{ t('aiAccess.requester.requestFailed', { code: directoryError }) }}</p>
        <p v-else-if="!registrations.length">{{ t('aiAccess.requester.noRegistrations') }}</p>
        <p v-else-if="registrationId && !targets.length && !pin">{{ t('aiAccess.requester.noTargets') }}</p>
        <ul v-if="targets.length" class="grid gap-2">
          <li v-for="row in targets" :key="row.key" class="row">
            <div class="min-w-0 break-words">
              <p class="font-semibold">{{ row.entry.display_name }} — {{ row.entry.target.persistent_id }}</p>
              <p v-if="row.device">{{ t('aiAccess.requester.device') }}: {{ row.device.display_name }} · {{ row.device.client_id }}</p>
              <p v-else>{{ t('aiAccess.requester.unidentified') }}</p>
              <p v-if="row.device">{{ t('aiAccess.requester.userId') }}: {{ row.device.user_id }}</p>
              <p>{{ t('aiAccess.requester.registrationId') }}: {{ row.entry.target.executor_registration_id }}</p>
              <p>{{ t('aiAccess.requester.grant') }}: {{ row.entry.grant.grant_id }} · {{ t('aiAccess.requester.grantRevision') }}: {{ row.entry.grant.revision }}</p>
              <p>{{ t(row.entry.state === 'online' ? 'aiAccess.requester.online' : 'aiAccess.requester.offline') }}</p>
            </div>
            <button type="button" class="button" data-testid="open-target"
              :disabled="!row.device || !row.entry.executor_epoch || row.entry.state !== 'online' || directoryBusy || busy"
              @click="store.openTarget(row.key)">{{ t('aiAccess.requester.openTarget') }}</button>
          </li>
        </ul>
        <button v-if="targetCursor" type="button" class="button self-start" :disabled="directoryBusy || busy" @click="store.moreTargets()">
          {{ t('aiAccess.requester.more') }}
        </button>
      </section>

      <template v-if="pin">
        <section class="panel" aria-labelledby="remote-ai-pin">
          <h2 id="remote-ai-pin">{{ t('aiAccess.requester.pinned') }} — {{ pin.name }}</h2>
          <p>{{ t('aiAccess.requester.pinHelp') }}</p>
          <p role="status" data-testid="availability" :class="{ 'font-semibold': !online }">{{ availabilityLabel }}</p>
          <dl class="identity">
            <dt>{{ t('aiAccess.requester.target') }}</dt><dd>{{ pin.url }}</dd>
            <dt>{{ t('aiAccess.requester.registration') }}</dt><dd>{{ pin.requester.display_name }} · {{ pin.registrationId }} · {{ pin.requester.user_id }} · {{ pin.requester.client_id }}</dd>
            <dt>{{ t('aiAccess.requester.device') }}</dt><dd>{{ pin.device.display_name }}</dd>
            <dt>{{ t('aiAccess.requester.userId') }}</dt><dd>{{ pin.device.user_id }}</dd>
            <dt>{{ t('aiAccess.requester.clientId') }}</dt><dd>{{ pin.device.client_id }}</dd>
            <dt>{{ t('aiAccess.requester.registrationId') }}</dt><dd>{{ pin.target.executor_registration_id }}</dd>
            <dt>{{ t('aiAccess.requester.serverId') }}</dt><dd>{{ pin.target.server_instance_id }}</dd>
            <dt>{{ t('aiAccess.requester.exportId') }}</dt><dd>{{ pin.target.export_id }}</dd>
            <dt>{{ t('aiAccess.requester.projectId') }}</dt><dd>{{ pin.target.persistent_id }}</dd>
            <dt>{{ t('aiAccess.requester.storageEpoch') }}</dt><dd>{{ pin.target.storage_epoch }}</dd>
            <dt>{{ t('aiAccess.requester.exportRevision') }}</dt><dd>{{ pin.target.export_revision }}</dd>
            <dt>{{ t('aiAccess.requester.grant') }}</dt><dd>{{ pin.grantId }}</dd>
            <dt>{{ t('aiAccess.requester.grantRevision') }}</dt><dd>{{ pin.grantRevision }}</dd>
            <dt>{{ t('aiAccess.requester.executorEpoch') }}</dt><dd>{{ pin.executorEpoch }}</dd>
          </dl>
          <p v-if="actionError === 'snapshot_changed'" role="status">{{ t('aiAccess.requester.snapshotChanged') }}</p>
          <p v-else-if="actionError && !unknown.length" role="alert">{{ t('aiAccess.requester.requestFailed', { code: actionError }) }}</p>
          <p v-if="cancellationRequested" role="status">{{ t('aiAccess.requester.cancelRequested') }}</p>
          <p v-else-if="commandState && !unknown.length" role="status">{{ t('aiAccess.requester.pending', { state: commandLabel }) }}</p>
          <div v-if="unknown.length" class="grid gap-2" data-testid="unknown-receipt">
            <p role="alert" class="font-semibold">{{ t('aiAccess.requester.unknown') }}</p>
            <div v-for="command in unknown" :key="command.id" class="row">
              <p class="break-all">{{ t('aiAccess.requester.receiptId', { id: command.id }) }}</p>
              <button type="button" class="button" :disabled="busy || !online" data-testid="check-receipt"
                @click="store.checkReceipt(command.id)">{{ t('aiAccess.requester.receipts') }}</button>
            </div>
          </div>
          <p v-if="receiptError" role="status">{{ t('aiAccess.requester.requestFailed', { code: receiptError }) }}</p>
          <div v-if="online && !receiptsReady">
            <p role="alert">{{ extra('receiptUnavailable') }}</p>
            <button type="button" class="button" :disabled="busy" @click="store.reloadReceipts()">{{ t('aiAccess.requester.receipts') }}</button>
          </div>
        </section>

        <template v-if="online">
          <section v-if="allowCreateTask || allowCreateChat" class="panel" aria-labelledby="remote-ai-create">
            <h2 id="remote-ai-create">{{ t(kind === 'task' ? 'aiAccess.requester.newTask' : 'aiAccess.requester.newChat') }}</h2>
            <p>{{ extra('intentStorage') }}</p>
            <form class="grid gap-3" @submit.prevent="create">
              <div class="grid gap-3 sm:grid-cols-3">
                <label v-if="allowCreateTask && allowCreateChat" class="field">
                  {{ t('aiAccess.requester.target') }}
                  <select v-model="kind" data-testid="create-kind">
                    <option value="task">{{ kindLabel('task') }}</option><option value="chat">{{ kindLabel('chat') }}</option>
                  </select>
                </label>
                <label class="field">
                  {{ t('aiAccess.requester.team') }}
                  <select v-model="team" required data-testid="create-team">
                    <option value="" disabled>{{ t('aiAccess.requester.choose') }}</option>
                    <option v-for="id in pin.teams" :key="id" :value="id">{{ id }}</option>
                  </select>
                </label>
                <label class="field">
                  {{ t('aiAccess.requester.member') }}
                  <select v-model="member" required data-testid="create-member">
                    <option value="" disabled>{{ t('aiAccess.requester.choose') }}</option>
                    <option v-for="id in pin.members" :key="id" :value="id">{{ id }}</option>
                  </select>
                </label>
              </div>
              <label class="field">{{ t('aiAccess.requester.name') }}<input v-model="title" required maxlength="480" data-testid="create-title"></label>
              <template v-if="kind === 'task'">
                <label class="field">{{ t('aiAccess.requester.brief') }}<textarea v-model="brief" required rows="3" maxlength="16384" data-testid="create-brief" /></label>
                <label class="field">{{ t('aiAccess.requester.paths') }}<textarea v-model="paths" rows="2" maxlength="16448" /></label>
                <p>{{ t('aiAccess.requester.pathsHint') }}</p>
              </template>
              <button type="submit" class="button justify-self-start" data-testid="create" :disabled="!draftValid || !canWrite">
                {{ t(kind === 'task' ? 'aiAccess.requester.createTask' : 'aiAccess.requester.createChat') }}
              </button>
            </form>
            <div v-if="created" class="row" data-testid="created-resource">
              <p>{{ extra('created', { id: created.id }) }}</p>
              <button v-if="permits('view_status')" type="button" class="button" :disabled="busy" data-testid="open-created"
                @click="store.openResource(created.id, created.kind)">{{ t('aiAccess.requester.openResource') }}</button>
            </div>
          </section>

          <section class="panel" aria-labelledby="remote-ai-resources">
            <h2 id="remote-ai-resources">{{ t('aiAccess.requester.resourceList') }}</h2>
            <template v-if="permits('view_status')">
              <button type="button" class="button self-start" :disabled="busy" @click="store.loadResources()">{{ t('aiAccess.requester.refreshResources') }}</button>
              <p v-if="!resources.length && !busy">{{ t('aiAccess.requester.noResources') }}</p>
              <ul v-else class="grid gap-2">
                <li v-for="row in resources" :key="row.resource_id" class="row">
                  <div class="min-w-0 break-words">
                    <p class="font-semibold">{{ kindLabel(row.kind) }} — {{ row.title }}</p>
                    <p>{{ stateLabel(row.status) }} · {{ row.assignee_label }} · {{ row.resource_id }}</p>
                  </div>
                  <button type="button" class="button" data-testid="open-resource" :disabled="busy"
                    @click="store.openResource(row.resource_id, row.kind)">{{ t('aiAccess.requester.openResource') }}</button>
                </li>
              </ul>
              <button v-if="resourceCursor" type="button" class="button self-start" :disabled="busy" @click="store.loadResources(true)">{{ t('aiAccess.requester.more') }}</button>
            </template>
            <p v-else>{{ t('aiAccess.requester.readUnavailable') }}</p>
          </section>

          <section v-if="resource" class="panel" aria-labelledby="remote-ai-resource" data-testid="resource-detail">
            <div class="row">
              <h2 id="remote-ai-resource">{{ kindLabel(resource.kind) }} — {{ resource.title }}</h2>
              <button type="button" class="button" :disabled="busy" @click="store.closeResource()">{{ t('aiAccess.requester.closeResource') }}</button>
            </div>
            <dl class="identity">
              <dt>{{ t('aiAccess.requester.resourceId') }}</dt><dd>{{ resource.resource_id }}</dd>
              <dt>{{ t('aiAccess.requester.assignee') }}</dt><dd>{{ resource.assignee_label }}</dd>
              <dt>{{ t('aiAccess.requester.status') }}</dt><dd>{{ stateLabel(resource.status) }}</dd>
            </dl>
            <p v-if="live" role="status">{{ t('aiAccess.requester.liveMessages') }}</p>
            <p v-if="canAction('start') || canAction('send')" class="font-semibold">{{ t('aiAccess.requester.executionWarning') }}</p>
            <div class="flex flex-wrap gap-2">
              <button v-if="canAction('start')" type="button" class="button" data-testid="start" :disabled="!canWrite || waitRevision !== null"
                @click="store.act('start')">{{ t('aiAccess.requester.start') }}</button>
              <button v-if="canAction('cancel')" type="button" class="button" data-testid="cancel" :disabled="!canWrite || cancellationRequested"
                @click="store.act('cancel')">{{ t('aiAccess.requester.cancel') }}</button>
            </div>
            <form v-if="canAction('note') || canAction('send')" class="grid gap-2" @submit.prevent="messageAction(resource.kind === 'task' ? 'note' : 'send')">
              <p>{{ extra('intentStorage') }}</p>
              <label class="field">{{ t('aiAccess.requester.message') }}<textarea v-model="message" required rows="3" maxlength="16384" data-testid="message" /></label>
              <button type="submit" class="button justify-self-start" data-testid="submit-message"
                :disabled="!canWrite || !validText(message) || (resource.kind === 'chat' && waitRevision !== null)">
                {{ t(resource.kind === 'task' ? 'aiAccess.requester.note' : 'aiAccess.requester.send') }}
              </button>
            </form>
            <div class="grid gap-2">
              <h3>{{ t('aiAccess.requester.transcript') }}</h3>
              <template v-if="permits('view_transcript')">
                <p v-if="!messages.length && !busy">{{ t('aiAccess.requester.emptyTranscript') }}</p>
                <ul v-else class="grid gap-2" data-testid="transcript">
                  <li v-for="item in messages" :key="item.id" class="rounded border border-line p-3">
                    <p class="mb-1 font-semibold">{{ roleLabel(item.role) }}</p>
                    <p v-if="item.omitted">{{ t('aiAccess.requester.omitted') }}</p>
                    <!-- Projection is plain untrusted text, not markdown, HTML, links or images. -->
                    <p v-else class="whitespace-pre-wrap break-words">{{ item.text }}</p>
                    <button v-if="!item.omitted && item.next < item.total && !transcriptLimit" type="button" class="button mt-2"
                      :disabled="busy" @click="store.moreMessage(item.id)">{{ t('aiAccess.requester.showRest') }}</button>
                  </li>
                </ul>
                <p v-if="transcriptLimit" role="status">{{ extra('transcriptLimit') }}</p>
                <button v-else-if="transcriptCursor" type="button" class="button justify-self-start" :disabled="busy"
                  @click="store.moreTranscript()">{{ t('aiAccess.requester.more') }}</button>
              </template>
              <p v-else>{{ t('aiAccess.requester.transcriptUnavailable') }}</p>
            </div>
          </section>
          <p v-if="busy" role="status">{{ t('aiAccess.requester.loading') }}</p>
        </template>
      </template>
    </div>
  </main>
</template>

<style scoped>
.panel { display: flex; flex-direction: column; gap: .75rem; padding: 1rem; font-size: .875rem;
  border: 1px solid var(--vxs-border); border-radius: var(--vxs-radius-lg); }
.panel h2, .panel h3 { overflow-wrap: anywhere; font-weight: 600; }
.panel h2 { font-size: 1rem; }
.row { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: .5rem;
  border: 1px solid var(--vxs-border); border-radius: var(--vxs-radius-md); padding: .75rem; }
.field { display: grid; gap: .25rem; }
.field input, .field select, .field textarea, .button {
  border: 1px solid var(--vxs-border); border-radius: var(--vxs-radius-md);
}
.field input, .field select, .field textarea {
  min-width: 0; background: var(--vxs-bg-surface); color: var(--vxs-text-primary); padding: .5rem;
}
.button { padding: .375rem .75rem; font-weight: 500; }
.button:hover:not(:disabled) { background: var(--vxs-bg-hover); }
.button:disabled { cursor: not-allowed; opacity: .5; }
.identity { display: grid; gap: .25rem 1rem; }
.identity dt { font-weight: 500; }
.identity dd { overflow-wrap: anywhere; }
@media (min-width: 640px) { .identity { grid-template-columns: max-content minmax(0, 1fr); } }
</style>
