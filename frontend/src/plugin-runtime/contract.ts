/**
 * The published plugin types, checked against the runtime that is actually passed.
 *
 * `types/voxarium_studio-plugin.d.ts` is copied into plugin projects, so nothing in this build
 * imports it and nothing would notice it going stale — which is worse than having no types
 * at all, because a plugin author would believe them. These assignments are the check: the
 * real runtime must satisfy the published one, and a node's real `data` must satisfy the
 * published `PluginNodeData`. Rename or drop a member and `vue-tsc` fails here.
 *
 * One direction only. The published surface is deliberately the *documented subset* — the
 * API client has many more methods than a plugin has any business calling — so "the real
 * thing is at least this" is the guarantee to hold, not equality.
 */

import type { GenericNodeData } from '@/lib/graph'
import type { PluginNodeData, PluginRuntime as Published } from '../../types/voxarium_studio-plugin'

import type { PluginRuntime } from './index'

type Satisfies<Published, Actual extends Published> = Actual

export type RuntimeIsPublished = Satisfies<Published, PluginRuntime>
export type NodeDataIsPublished = Satisfies<PluginNodeData, GenericNodeData>
