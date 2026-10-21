<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import {
		addRotatorCandidate,
		createRotatorAlias,
		deleteRotatorAlias,
		getRotatorAliases,
		removeRotatorCandidate,
		reorderRotatorCandidates,
		updateRotatorAlias
	} from '$lib/apis/rotator';
	import { getOpenAIModels } from '$lib/apis/openai';
	import AdminSettingSection from './AdminSettingSection.svelte';

	const i18n = getContext('i18n');

	let aliases: any[] = [];
	let loading = true;
	let loadError = '';
	let upstreamModels: string[] = [];

	let newAliasId = '';
	let newAliasName = '';
	let creating = false;

	let candidateInput: Record<string, string> = {};
	let busyAlias: string | null = null;

	const loadAliases = async () => {
		loading = true;
		loadError = '';
		try {
			const res = await getRotatorAliases(localStorage.token);
			aliases = res.data ?? [];
		} catch (error: any) {
			loadError = error?.detail ?? error?.message ?? String(error);
			toast.error($i18n.t('Failed to load model rotator aliases'));
			aliases = [];
		} finally {
			loading = false;
		}
	};

	const loadUpstreamModels = async () => {
		try {
			const res = await getOpenAIModels(localStorage.token);
			const list = Array.isArray(res) ? res : (res?.data ?? []);
			const ids: string[] = list.map((m: any) => m?.id).filter(Boolean);
			// The rotator advertises aliases too; those are not upstream candidates.
			upstreamModels = [...new Set(ids)].filter((id) => !aliases.some((a) => a.id === id));
		} catch {
			upstreamModels = [];
		}
	};

	onMount(() => {
		loadAliases().then(loadUpstreamModels);
	});

	const createAlias = async () => {
		const id = newAliasId.trim();
		if (!id) return;
		creating = true;
		try {
			await createRotatorAlias(localStorage.token, {
				id,
				name: newAliasName.trim() || id
			});
			newAliasId = '';
			newAliasName = '';
			await loadAliases();
			toast.success($i18n.t('Alias created'));
		} catch (error: any) {
			toast.error(error?.detail ?? $i18n.t('Failed to create alias'));
		}
		creating = false;
	};

	const removeAlias = async (alias: any) => {
		busyAlias = alias.id;
		try {
			await deleteRotatorAlias(localStorage.token, alias.id);
			await loadAliases();
			toast.success($i18n.t('Alias deleted'));
		} catch (error: any) {
			toast.error(error?.detail ?? $i18n.t('Failed to delete alias'));
		}
		busyAlias = null;
	};

	const renameAlias = async (alias: any, name: string) => {
		const trimmed = (name ?? '').trim();
		if (!trimmed) return;
		busyAlias = alias.id;
		try {
			await updateRotatorAlias(localStorage.token, alias.id, { name: trimmed });
			await loadAliases();
		} catch (error: any) {
			toast.error(error?.detail ?? $i18n.t('Failed to rename alias'));
			await loadAliases();
		}
		busyAlias = null;
	};

	const addCandidate = async (alias: any) => {
		const candidate = (candidateInput[alias.id] ?? '').trim();
		if (!candidate) return;
		busyAlias = alias.id;
		try {
			await addRotatorCandidate(localStorage.token, alias.id, candidate);
			candidateInput[alias.id] = '';
			await loadAliases();
		} catch (error: any) {
			toast.error(error?.detail ?? $i18n.t('Failed to add candidate'));
		}
		busyAlias = null;
	};

	const removeCandidate = async (alias: any, candidateId: string) => {
		busyAlias = alias.id;
		try {
			await removeRotatorCandidate(localStorage.token, alias.id, candidateId);
			await loadAliases();
		} catch (error: any) {
			toast.error(error?.detail ?? $i18n.t('Failed to remove candidate'));
		}
		busyAlias = null;
	};

	const move = async (alias: any, index: number, delta: number) => {
		const priority = [...(alias.priority ?? [])];
		const target = index + delta;
		if (target < 0 || target >= priority.length) return;
		[priority[index], priority[target]] = [priority[target], priority[index]];
		busyAlias = alias.id;
		try {
			await reorderRotatorCandidates(localStorage.token, alias.id, priority);
			await loadAliases();
		} catch (error: any) {
			toast.error(error?.detail ?? $i18n.t('Failed to reorder candidates'));
		}
		busyAlias = null;
	};
</script>

<div class="space-y-5">
	<AdminSettingSection
		title={$i18n.t('Model Rotator')}
		className="text-xs text-gray-600 dark:text-gray-400"
	>
		<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
			{$i18n.t(
				'An alias is a stable model id that Open WebUI shows to users. The rotator maps it to real upstream models and fails over between them in priority order.'
			)}
		</p>
	</AdminSettingSection>

	<AdminSettingSection title={$i18n.t('New Alias')}>
		<div class="flex flex-col gap-2 sm:flex-row sm:items-end">
			<div class="flex-1">
				<label class="text-xs text-gray-600 dark:text-gray-400" for="rotator-alias-id">
					{$i18n.t('Alias ID')}
				</label>
				<input
					id="rotator-alias-id"
					class="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-700 dark:bg-gray-900"
					placeholder="deepseek-v4.1-flash"
					bind:value={newAliasId}
				/>
			</div>
			<div class="flex-1">
				<label class="text-xs text-gray-600 dark:text-gray-400" for="rotator-alias-name">
					{$i18n.t('Display Name')}
				</label>
				<input
					id="rotator-alias-name"
					class="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-700 dark:bg-gray-900"
					placeholder="Deepseek v4.1 Flash"
					bind:value={newAliasName}
				/>
			</div>
			<button
				class="rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium transition hover:bg-gray-100 disabled:opacity-50 dark:border-gray-700 dark:hover:bg-gray-800"
				disabled={creating || !newAliasId.trim()}
				on:click={createAlias}
			>
				{$i18n.t('Add Alias')}
			</button>
		</div>
	</AdminSettingSection>

	{#if loading}
		<p class="text-xs text-gray-500 dark:text-gray-400">{$i18n.t('Loading...')}</p>
	{:else if loadError}
		<p
			class="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600 dark:bg-red-950 dark:text-red-400"
		>
			{$i18n.t('Failed to load model rotator aliases')}: {loadError}
		</p>
	{:else if aliases.length === 0}
		<p class="text-xs text-gray-500 dark:text-gray-400">
			{$i18n.t('No aliases configured yet.')}
		</p>
	{/if}

	{#each aliases as alias (alias.id)}
		<div class="rounded-xl border border-gray-200 p-3 dark:border-gray-800">
			<div class="flex flex-wrap items-center gap-2">
				<code class="rounded bg-gray-100 px-2 py-1 text-xs dark:bg-gray-900">{alias.id}</code>
				<input
					class="min-w-0 flex-1 rounded-lg border border-gray-300 bg-white px-2 py-1 text-sm dark:border-gray-700 dark:bg-gray-900"
					value={alias.name}
					on:change={(e) => renameAlias(alias, e.currentTarget.value)}
				/>
				<button
					class="rounded-lg border border-red-300 px-2.5 py-1 text-xs text-red-600 transition hover:bg-red-50 disabled:opacity-50 dark:border-red-900 dark:text-red-400 dark:hover:bg-red-950"
					disabled={busyAlias === alias.id}
					on:click={() => removeAlias(alias)}
				>
					{$i18n.t('Delete')}
				</button>
			</div>

			<div class="mt-3 space-y-1.5">
				{#if (alias.priority ?? []).length === 0}
					<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
						{$i18n.t('No fallback models yet. Add at least one candidate.')}
					</p>
				{/if}
				{#each alias.priority ?? [] as candidate, index (candidate)}
					{@const benched = alias.candidates?.find((c: any) => c.id === candidate)?.benched}
					<div class="flex items-center gap-2 rounded-lg bg-gray-50 px-2 py-1.5 dark:bg-gray-900">
						<span class="w-5 text-center text-[0.6875rem] text-gray-400">{index + 1}</span>
						<code class="min-w-0 flex-1 truncate text-xs">{candidate}</code>
						{#if benched}
							<span
								class="rounded bg-amber-100 px-1.5 py-0.5 text-[0.625rem] text-amber-700 dark:bg-amber-950 dark:text-amber-400"
							>
								{$i18n.t('benched')}
							</span>
						{/if}
						<button
							class="rounded px-1.5 py-0.5 text-xs text-gray-500 transition hover:bg-gray-200 disabled:opacity-30 dark:hover:bg-gray-800"
							disabled={index === 0 || busyAlias === alias.id}
							on:click={() => move(alias, index, -1)}
							aria-label={$i18n.t('Move up')}
						>
							▲
						</button>
						<button
							class="rounded px-1.5 py-0.5 text-xs text-gray-500 transition hover:bg-gray-200 disabled:opacity-30 dark:hover:bg-gray-800"
							disabled={index === (alias.priority?.length ?? 0) - 1 || busyAlias === alias.id}
							on:click={() => move(alias, index, 1)}
							aria-label={$i18n.t('Move down')}
						>
							▼
						</button>
						<button
							class="rounded px-1.5 py-0.5 text-xs text-red-500 transition hover:bg-red-100 disabled:opacity-30 dark:hover:bg-red-950"
							disabled={busyAlias === alias.id}
							on:click={() => removeCandidate(alias, candidate)}
							aria-label={$i18n.t('Remove')}
						>
							✕
						</button>
					</div>
				{/each}
			</div>

			<div class="mt-2.5 flex gap-2">
				<input
					class="min-w-0 flex-1 rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-xs dark:border-gray-700 dark:bg-gray-900"
					placeholder="vendor/model-id"
					list="rotator-upstream-models"
					bind:value={candidateInput[alias.id]}
					on:keydown={(e) => e.key === 'Enter' && addCandidate(alias)}
				/>
				<button
					class="rounded-lg border border-gray-300 px-2.5 py-1.5 text-xs font-medium transition hover:bg-gray-100 disabled:opacity-50 dark:border-gray-700 dark:hover:bg-gray-800"
					disabled={busyAlias === alias.id || !(candidateInput[alias.id] ?? '').trim()}
					on:click={() => addCandidate(alias)}
				>
					{$i18n.t('Add Candidate')}
				</button>
			</div>
		</div>
	{/each}

	<datalist id="rotator-upstream-models">
		{#each upstreamModels as model (model)}
			<option value={model}></option>
		{/each}
	</datalist>
</div>
