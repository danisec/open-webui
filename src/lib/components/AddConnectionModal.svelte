<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { getContext, onMount } from 'svelte';
	const i18n = getContext<any>('i18n');

	import { verifyOpenAIConnection } from '$lib/apis/openai';
	import { verifyOllamaConnection } from '$lib/apis/ollama';

	import Modal from '$lib/components/common/Modal.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Minus from '$lib/components/icons/Minus.svelte';
	import PencilSolid from '$lib/components/icons/PencilSolid.svelte';
	import SensitiveInput from '$lib/components/common/SensitiveInput.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import Tags from './common/Tags.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import Textarea from './common/Textarea.svelte';
	import { normalizeTags } from '$lib/utils/tags';

	export let onSubmit: Function = () => {};
	export let onDelete: Function = () => {};

	export let show = false;
	export let edit = false;

	export let ollama = false;
	export let direct = false;

	export let connection: any = null;

	let url = '';
	let key = '';
	let auth_type = 'bearer';
	let forwardCookies = false;

	let connectionType = 'external';
	let provider = '';
	let prefixId = '';
	let enable = true;
	let apiVersion = '';
	let apiType = ''; // '' = chat completions (default), 'responses' = Responses API

	let headers = '';
	let passthroughParams = '';

	let tags = [];

	let modelId = '';
	let modelIds = [];
	let availableModelIds: string[] = [];

	// Multiple API keys for one connection. Stored as `config.api_keys` (a list)
	// alongside the single legacy key, which stays in the `key` field so existing
	// deployments keep working and the first key remains visible in the row.
	let apiKeys: string[] = [];
	let apiKeyStrategy = 'round_robin';
	let stickyTtl = 300;
	let showApiKeys = false;

	// Optional per-connection proxies. Stored as `config.proxies` (a list) with
	// rotation, because an upstream may rate-limit or ban by source address and
	// the only real remedy is to change the source address. `config.proxy` (the
	// single legacy field) is still written when exactly one entry is present.
	let proxyList: string[] = [];
	let proxyStrategy = 'round_robin';
	let proxyStickyTtl = 300;
	let showProxies = false;

	let loading = false;
	let showDeleteConfirmDialog = false;
	let showAdvanced = false;

	const inputClass =
		'bg-transparent outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700';
	const selectClass =
		'bg-transparent pr-5 outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700';

	const parsePassthroughParams = (value: string) =>
		value
			.split(',')
			.map((param) => param.trim())
			.filter(Boolean);

	const verifyOllamaHandler = async () => {
		// remove trailing slash from url
		url = url.replace(/\/$/, '');

		let _headers = null;

		if (headers) {
			try {
				_headers = JSON.parse(headers);
				if (typeof _headers !== 'object' || Array.isArray(_headers)) {
					_headers = null;
					throw new Error('Headers must be a valid JSON object');
				}
				headers = JSON.stringify(_headers, null, 2);
			} catch (error) {
				toast.error($i18n.t('Headers must be a valid JSON object'));
				return;
			}
		}

		const res = await verifyOllamaConnection(localStorage.token, {
			url,
			key,
			config: {
				auth_type,
				...(_headers ? { headers: _headers } : {})
			}
		}).catch((error) => {
			toast.error(`${error}`);
		});

		if (res) {
			toast.success($i18n.t('Server connection verified'));
		}
	};

	const verifyOpenAIHandler = async () => {
		availableModelIds = [];
		// remove trailing slash from url
		url = url.replace(/\/$/, '');

		let _headers = null;

		if (headers) {
			try {
				_headers = JSON.parse(headers);
				if (typeof _headers !== 'object' || Array.isArray(_headers)) {
					_headers = null;
					throw new Error('Headers must be a valid JSON object');
				}
				headers = JSON.stringify(_headers, null, 2);
			} catch (error) {
				toast.error($i18n.t('Headers must be a valid JSON object'));
				return;
			}
		}

		const res = await verifyOpenAIConnection(
			localStorage.token,
			{
				url,
				key,
				config: {
					auth_type,
					...(!direct && !ollama ? { forward_cookies: forwardCookies } : {}),
					...(provider ? { provider } : {}),
					...(azure ? { azure: true } : {}),
					api_version: apiVersion,
					passthrough_params: parsePassthroughParams(passthroughParams),
					...(_headers ? { headers: _headers } : {})
				}
			},
			direct
		).catch((error) => {
			toast.error(`${error}`);
		});

		const models = Array.isArray(res) ? res : res?.data;
		if (show && !azure && Array.isArray(models)) {
			availableModelIds = [
				...new Set<string>(
					models.map((model) => model?.id).filter((id) => typeof id === 'string' && id.trim())
				)
			];
		}

		if (res) {
			toast.success($i18n.t('Server connection verified'));
		}
	};

	const verifyHandler = () => {
		if (ollama) {
			verifyOllamaHandler();
		} else {
			verifyOpenAIHandler();
		}
	};

	const addModelHandler = () => {
		const newModelId = modelId.trim();

		if (!newModelId) {
			return;
		}

		if (modelIds.includes(newModelId)) {
			toast.error($i18n.t('Model ID is already added'));
			return;
		}

		modelIds = [...modelIds, newModelId];
		modelId = '';
	};

	const submitHandler = async () => {
		loading = true;

		if (!ollama && !url) {
			loading = false;
			toast.error($i18n.t('URL is required'));
			return;
		}

		if (azure) {
			if (!apiVersion) {
				loading = false;
				showAdvanced = true;

				toast.error($i18n.t('API Version is required'));
				return;
			}

			if (!key && !['azure_ad', 'microsoft_entra_id'].includes(auth_type)) {
				loading = false;

				toast.error($i18n.t('Key is required'));
				return;
			}

			if (modelIds.length === 0) {
				loading = false;
				showAdvanced = true;
				toast.error($i18n.t('Deployment names are required for Azure OpenAI'));
				return;
			}
		}

		if (headers) {
			try {
				const _headers = JSON.parse(headers);
				if (typeof _headers !== 'object' || Array.isArray(_headers)) {
					throw new Error('Headers must be a valid JSON object');
				}
				headers = JSON.stringify(_headers, null, 2);
			} catch (error) {
				loading = false;
				toast.error($i18n.t('Headers must be a valid JSON object'));
				return;
			}
		}

		// remove trailing slash from url
		url = url.replace(/\/$/, '');

		// A multi-key list is authoritative when present. The single `key` field
		// mirrors its first entry so the collapsed row, the legacy
		// `OPENAI_API_KEYS` array and every downstream caller that still reads one
		// key keep working unchanged.
		const cleanedKeys = apiKeys.map((k) => k.trim()).filter(Boolean);
		const uniqueKeys = [...new Set(cleanedKeys)];
		if (uniqueKeys.length > 0) {
			key = uniqueKeys[0];
		}

		const connection = {
			url,
			key,
			config: {
				enable: enable,
				tags: tags,
				prefix_id: prefixId,
				model_ids: modelIds,
				connection_type: connectionType,
				auth_type,
				...(!direct && !ollama ? { forward_cookies: forwardCookies } : {}),
				headers: headers ? JSON.parse(headers) : undefined,
				passthrough_params: parsePassthroughParams(passthroughParams),
				...(provider ? { provider } : {}),
				...(!ollama && azure ? { azure: true } : {}),
				...(azure ? { api_version: apiVersion } : {}),
				...(apiType ? { api_type: apiType } : {}),
				// Omitted when there is nothing to rotate, so a single-key connection
				// stores exactly what it did before this feature existed.
				...(uniqueKeys.length > 1 ? { api_keys: uniqueKeys } : {}),
				...(uniqueKeys.length > 1 ? { api_key_strategy: apiKeyStrategy } : {}),
				...(uniqueKeys.length > 1 && apiKeyStrategy === 'sticky'
					? { sticky_ttl: Number(stickyTtl) || 300 }
					: {}),
				// Proxies: a list enables rotation. A single entry is mirrored into
				// the legacy `proxy` field so one-proxy connections store exactly
				// what they did before rotation existed.
				...(() => {
					const cleaned = [...new Set(proxyList.map((p) => p.trim()).filter(Boolean))];
					if (cleaned.length === 0) return {};
					if (cleaned.length === 1) return { proxy: cleaned[0] };
					return {
						proxies: cleaned,
						proxy_strategy: proxyStrategy,
						...(proxyStrategy === 'sticky'
							? { proxy_sticky_ttl: Number(proxyStickyTtl) || 300 }
							: {})
					};
				})()
			}
		};

		await onSubmit(connection);

		loading = false;
		show = false;

		url = '';
		key = '';
		auth_type = 'bearer';
		forwardCookies = false;
		prefixId = '';
		passthroughParams = '';
		showAdvanced = false;
		tags = [];
		modelIds = [];
		headers = '';
		enable = true;
		connectionType = 'external';
		provider = '';
		apiVersion = '';
		apiType = '';
		apiKeys = [];
		apiKeyStrategy = 'round_robin';
		stickyTtl = 300;
		showApiKeys = false;
		proxyList = [];
		proxyStrategy = 'round_robin';
		proxyStickyTtl = 300;
		showProxies = false;
	};

	const init = () => {
		availableModelIds = [];
		forwardCookies = connection?.config?.forward_cookies ?? false;
		// Reset the multi-key state on every open: the modal is reused for different
		// connections, so leaving the previous list here would show one connection's
		// keys while saving another's.
		apiKeys = [];
		apiKeyStrategy = 'round_robin';
		stickyTtl = 300;
		showApiKeys = false;
		proxyList = [];
		proxyStrategy = 'round_robin';
		proxyStickyTtl = 300;
		showProxies = false;
		if (connection) {
			url = connection.url;
			key = connection.key;

			auth_type = connection.config.auth_type ?? 'bearer';
			headers = connection.config?.headers
				? JSON.stringify(connection.config.headers, null, 2)
				: '';

			enable = connection.config?.enable ?? true;
			tags = normalizeTags(connection.config?.tags);
			prefixId = connection.config?.prefix_id ?? '';
			passthroughParams = Array.isArray(connection.config?.passthrough_params)
				? connection.config.passthrough_params.join(', ')
				: (connection.config?.passthrough_params ?? '');
			modelIds = [...new Set(connection.config?.model_ids ?? [])];

			const storedKeys = connection.config?.api_keys;
			if (Array.isArray(storedKeys) && storedKeys.length > 0) {
				apiKeys = [...storedKeys];
			} else if (connection.key) {
				// Seed the list with the single legacy key so the admin can extend it
				// instead of retyping the key they already have.
				apiKeys = [connection.key];
			}
			apiKeyStrategy = connection.config?.api_key_strategy ?? 'round_robin';
			stickyTtl = connection.config?.sticky_ttl ?? 300;
			const storedProxies = connection.config?.proxies;
			if (Array.isArray(storedProxies) && storedProxies.length > 0) {
				proxyList = [...storedProxies];
				showProxies = true;
			} else if (connection.config?.proxy) {
				proxyList = [connection.config.proxy];
			}
			proxyStrategy = connection.config?.proxy_strategy ?? 'round_robin';
			proxyStickyTtl = connection.config?.proxy_sticky_ttl ?? 300;
			showApiKeys = Array.isArray(storedKeys) && storedKeys.length > 1;

			if (ollama) {
				connectionType = connection.config?.connection_type ?? 'local';
			} else {
				connectionType = connection.config?.connection_type ?? 'external';
				provider = connection.config?.provider ?? (connection.config?.azure ? 'azure' : '');
				apiVersion = connection.config?.api_version ?? '';
				apiType = connection.config?.api_type ?? '';
			}
		}
	};

	$: if (show) {
		init();
	}

	$: azure =
		provider === 'azure' ||
		((url.includes('azure.') || url.includes('cognitive.microsoft.com')) &&
			!direct &&
			provider === '' &&
			!/\/openai\/v1(\/|$)/.test(url));

	onMount(() => {
		init();
	});
</script>

<Modal size="sm" bind:show>
	<div>
		<div class=" flex justify-between dark:text-gray-100 px-5 pt-4 pb-1.5">
			<h1 class="text-lg font-medium self-center font-primary">
				{#if edit}
					{$i18n.t('Edit Connection')}
				{:else}
					{$i18n.t('Add Connection')}
				{/if}
			</h1>
			<button
				class="self-center"
				aria-label={$i18n.t('Close modal')}
				on:click={() => {
					show = false;
				}}
			>
				<XMark className={'size-5'} />
			</button>
		</div>

		<div class="flex flex-col md:flex-row w-full px-4 pb-4 md:space-x-4 dark:text-gray-200">
			<div class=" flex flex-col w-full sm:flex-row sm:justify-center sm:space-x-6">
				<form
					class="flex flex-col w-full"
					on:submit={(e) => {
						e.preventDefault();
						submitHandler();
					}}
				>
					<div class="px-1">
						{#if !direct}
							<div class="flex gap-2">
								<div class="flex w-full justify-between items-center">
									<div class=" text-xs text-gray-500">{$i18n.t('Connection Type')}</div>

									<div class="">
										<button
											on:click={() => {
												connectionType = connectionType === 'local' ? 'external' : 'local';
											}}
											type="button"
											class=" text-xs text-gray-700 dark:text-gray-300"
										>
											{#if connectionType === 'local'}
												{$i18n.t('Local')}
											{:else}
												{$i18n.t('External')}
											{/if}
										</button>
									</div>
								</div>
							</div>
						{/if}

						<div class="flex gap-2 mt-1.5">
							<div class="flex flex-col w-full">
								<label
									for="url-input"
									class={`mb-0.5 text-xs text-gray-500
								`}>{$i18n.t('URL')}</label
								>

								<div class="flex-1">
									<input
										id="url-input"
										class={`w-full text-sm ${inputClass}`}
										type="text"
										bind:value={url}
										placeholder={$i18n.t('API Base URL')}
										autocomplete="off"
										list={ollama ? undefined : 'suggestions'}
										required
									/>

									{#if !ollama}
										<datalist id="suggestions">
											<option value="https://api.openai.com/v1" />
											<option value="https://api.anthropic.com/v1" />
											<option value="https://generativelanguage.googleapis.com/v1beta/openai" />
											<option value="https://api.mistral.ai/v1" />
											<option value="https://api.groq.com/openai/v1" />
											<option value="https://openrouter.ai/api/v1" />
											<option value="https://api.x.ai/v1" />
										</datalist>
									{/if}
								</div>
							</div>

							<Tooltip content={$i18n.t('Verify Connection')} className="self-end -mb-1">
								<button
									class="self-center p-1 bg-transparent hover:bg-gray-100 dark:hover:bg-gray-850 rounded-lg transition"
									on:click={() => {
										verifyHandler();
									}}
									type="button"
									aria-label={$i18n.t('Verify Connection')}
								>
									<svg
										xmlns="http://www.w3.org/2000/svg"
										viewBox="0 0 20 20"
										fill="currentColor"
										aria-hidden="true"
										class="w-4 h-4"
									>
										<path
											fill-rule="evenodd"
											d="M15.312 11.424a5.5 5.5 0 01-9.201 2.466l-.312-.311h2.433a.75.75 0 000-1.5H3.989a.75.75 0 00-.75.75v4.242a.75.75 0 001.5 0v-2.43l.31.31a7 7 0 0011.712-3.138.75.75 0 00-1.449-.39zm1.23-3.723a.75.75 0 00.219-.53V2.929a.75.75 0 00-1.5 0V5.36l-.31-.31A7 7 0 003.239 8.188a.75.75 0 101.448.389A5.5 5.5 0 0113.89 6.11l.311.31h-2.432a.75.75 0 000 1.5h4.243a.75.75 0 00.53-.219z"
											clip-rule="evenodd"
										/>
									</svg>
								</button>
							</Tooltip>

							<div class="flex flex-col shrink-0 self-end">
								<label class="sr-only" for="toggle-connection"
									>{$i18n.t('Toggle whether current connection is active.')}</label
								>
								<Tooltip content={enable ? $i18n.t('Enabled') : $i18n.t('Disabled')}>
									<Switch id="toggle-connection" bind:state={enable} />
								</Tooltip>
							</div>
						</div>

						<div class="flex gap-2 mt-2">
							<div class="flex flex-col w-full">
								<label for="select-bearer-or-session" class={`text-xs text-gray-500`}
									>{$i18n.t('Auth')}</label
								>

								<div class="flex gap-2">
									<div class="flex-shrink-0 self-start">
										<select
											id="select-bearer-or-session"
											class={`w-full text-sm ${selectClass}`}
											bind:value={auth_type}
										>
											<option value="none">{$i18n.t('None')}</option>
											<option value="bearer">{$i18n.t('Bearer')}</option>

											{#if !ollama}
												<option value="session">{$i18n.t('Session')}</option>
												{#if !direct}
													<option value="system_oauth">{$i18n.t('OAuth')}</option>
													<option value="microsoft_entra_id">{$i18n.t('Entra ID')}</option>
												{/if}
											{/if}
										</select>
									</div>

									<div class="flex flex-1 items-center">
										{#if auth_type === 'bearer'}
											<SensitiveInput
												bind:value={key}
												placeholder={$i18n.t('API Key')}
												required={false}
											/>
										{:else if auth_type === 'none'}
											<div class={`text-xs self-center translate-y-[1px] text-gray-500`}>
												{$i18n.t('No authentication')}
											</div>
										{:else if auth_type === 'session'}
											<div class={`text-xs self-center translate-y-[1px] text-gray-500`}>
												{$i18n.t('Forwards system user session credentials to authenticate')}
											</div>
										{:else if auth_type === 'system_oauth'}
											<div class={`text-xs self-center translate-y-[1px] text-gray-500`}>
												{$i18n.t('Forwards system user OAuth access token to authenticate')}
											</div>
										{:else if ['azure_ad', 'microsoft_entra_id'].includes(auth_type)}
											<div class={`text-xs self-center translate-y-[1px] text-gray-500`}>
												{$i18n.t('Uses DefaultAzureCredential to authenticate')}
											</div>
										{/if}
									</div>
								</div>
							</div>
						</div>

						{#if auth_type === 'bearer' && !ollama && !direct}
							<!-- Multiple keys on one connection, rotated per request. -->
							<div class="mt-2">
								<button
									type="button"
									class="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition"
									on:click={() => (showApiKeys = !showApiKeys)}
									aria-expanded={showApiKeys}
								>
									<svg
										xmlns="http://www.w3.org/2000/svg"
										viewBox="0 0 20 20"
										fill="currentColor"
										class="w-3 h-3 transition-transform {showApiKeys ? 'rotate-90' : ''}"
									>
										<path
											fill-rule="evenodd"
											d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
											clip-rule="evenodd"
										/>
									</svg>
									{$i18n.t('Multiple API Keys')}
									{#if apiKeys.filter((k) => k.trim()).length > 1}
										<span
											class="ml-1 rounded-full bg-gray-100 px-1.5 text-[10px] text-gray-600 dark:bg-gray-800 dark:text-gray-300"
										>
											{apiKeys.filter((k) => k.trim()).length}
										</span>
									{/if}
								</button>

								{#if showApiKeys}
									<div class="mt-1.5">
										<p class="mb-1 text-xs text-gray-500">
											{$i18n.t(
												'Rotate between these keys. Leave empty to use the single key above.'
											)}
										</p>

										{#each apiKeys as _apiKey, apiKeyIdx}
											<div class="flex items-center gap-1">
												<SensitiveInput
													bind:value={apiKeys[apiKeyIdx]}
													placeholder={$i18n.t('API Key')}
													required={false}
												/>
												<button
													type="button"
													aria-label={$i18n.t('Remove key')}
													class="shrink-0 p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
													on:click={() => {
														apiKeys = apiKeys.filter((_, i) => i !== apiKeyIdx);
													}}
												>
													<Minus strokeWidth="2" className="size-3.5" />
												</button>
											</div>
										{/each}

										<button
											type="button"
											class="mt-1 flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
											on:click={() => (apiKeys = [...apiKeys, ''])}
										>
											<Plus className="size-3.5" strokeWidth="2" />
											{$i18n.t('Add key')}
										</button>

										{#if apiKeys.filter((k) => k.trim()).length > 1}
											<div class="mt-2 flex flex-row justify-between items-center w-full">
												<label for="api-key-strategy" class="text-xs text-gray-500">
													{$i18n.t('Rotation')}
												</label>
												<select
													id="api-key-strategy"
													bind:value={apiKeyStrategy}
													class="text-xs text-gray-700 dark:text-gray-300 bg-transparent pr-5 outline-hidden"
												>
													<option value="round_robin">
														{$i18n.t('Round Robin')}
													</option>
													<option value="sticky">
														{$i18n.t('Sticky')}
													</option>
												</select>
											</div>

											{#if apiKeyStrategy === 'sticky'}
												<div class="mt-2 flex gap-2 items-center">
													<div class="flex flex-col w-full">
														<label for="sticky-ttl-input" class="mb-0.5 text-xs text-gray-500">
															{$i18n.t('Sticky window (seconds)')}
														</label>
														<input
															id="sticky-ttl-input"
															class={`w-full text-sm ${inputClass}`}
															type="number"
															min="0"
															bind:value={stickyTtl}
															placeholder="300"
															autocomplete="off"
														/>
													</div>
												</div>
												<p class="mt-1 text-xs text-gray-500">
													{$i18n.t(
														'One key is reused for this long before rotating. Good for upstream prompt caching.'
													)}
												</p>
											{/if}
										{/if}
									</div>
								{/if}
							</div>
						{/if}

						{#if !ollama && !direct}
							<!-- Per-connection proxies. Only this connection's outbound
							     requests use them; other connections are unaffected.
							     A list enables rotation, which is the only remedy for an
							     upstream that rate-limits by source address. -->
							<div class="mt-3">
								<div class="flex items-center justify-between">
									<label
										for="connection-proxy"
										class="mb-0.5 block text-xs font-medium text-gray-500 dark:text-gray-400"
									>
										{$i18n.t('Proxy (optional)')}
									</label>
									{#if proxyList.filter((p) => p.trim()).length > 1}
										<span
											class="rounded-full bg-gray-100 px-1.5 text-[10px] text-gray-600 dark:bg-gray-800 dark:text-gray-300"
										>
											{proxyList.filter((p) => p.trim()).length}
										</span>
									{/if}
								</div>

								{#if !showProxies}
									<!-- Single proxy: the simple path, unchanged. -->
									<input
										id="connection-proxy"
										class={`w-full text-sm ${inputClass}`}
										type="text"
										bind:value={proxyList[0]}
										placeholder="socks5://user:pass@host:1080"
										autocomplete="off"
										spellcheck="false"
									/>
								{:else}
									<div class="space-y-1">
										{#each proxyList as _proxy, proxyIdx}
											<div class="flex items-center gap-1">
												<input
													class={`w-full text-sm ${inputClass}`}
													type="text"
													bind:value={proxyList[proxyIdx]}
													placeholder="socks5://user:pass@host:1080"
													autocomplete="off"
													spellcheck="false"
													aria-label={$i18n.t('Proxy')}
												/>
												<button
													type="button"
													aria-label={$i18n.t('Remove proxy')}
													class="shrink-0 p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
													on:click={() => {
														proxyList = proxyList.filter((_, i) => i !== proxyIdx);
													}}
												>
													<Minus strokeWidth="2" className="size-3.5" />
												</button>
											</div>
										{/each}
									</div>
								{/if}

								<div class="mt-1 flex items-center gap-3">
									<button
										type="button"
										class="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
										on:click={() => {
											showProxies = true;
											if (proxyList.length === 0) proxyList = [''];
											else proxyList = [...proxyList, ''];
										}}
									>
										<Plus className="size-3.5" strokeWidth="2" />
										{$i18n.t('Add proxy')}
									</button>
									{#if showProxies}
										<button
											type="button"
											class="text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
											on:click={() => {
												showProxies = false;
												proxyList = proxyList.slice(0, 1);
											}}
										>
											{$i18n.t('Use a single proxy')}
										</button>
									{/if}
								</div>

								{#if proxyList.filter((p) => p.trim()).length > 1}
									<div class="mt-2 flex flex-row justify-between items-center w-full">
										<label for="proxy-strategy" class="text-xs text-gray-500">
											{$i18n.t('Proxy rotation')}
										</label>
										<select
											id="proxy-strategy"
											bind:value={proxyStrategy}
											class="text-xs text-gray-700 dark:text-gray-300 bg-transparent pr-5 outline-hidden"
										>
											<option value="round_robin">{$i18n.t('Round Robin')}</option>
											<option value="sticky">{$i18n.t('Sticky')}</option>
										</select>
									</div>

									{#if proxyStrategy === 'sticky'}
										<div class="mt-2 flex gap-2 items-center">
											<div class="flex flex-col w-full">
												<label for="proxy-sticky-ttl" class="mb-0.5 text-xs text-gray-500">
													{$i18n.t('Sticky window (seconds)')}
												</label>
												<input
													id="proxy-sticky-ttl"
													class={`w-full text-sm ${inputClass}`}
													type="number"
													min="0"
													bind:value={proxyStickyTtl}
													placeholder="300"
													autocomplete="off"
												/>
											</div>
										</div>
									{/if}
								{/if}

								<p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
									{$i18n.t(
										'Route only this connection through a proxy. Supports socks5://, socks4://, http:// and https://. Add more than one to rotate between source addresses.'
									)}
								</p>
							</div>
						{/if}

						{#if !ollama && !direct}
							<div class="flex flex-row justify-between items-center w-full mt-1">
								<label
									for="api-type-toggle"
									class={`mb-0.5 text-xs text-gray-500
							`}>{$i18n.t('API Type')}</label
								>

								<div>
									<button
										on:click={() => {
											apiType = apiType === 'responses' ? '' : 'responses';
										}}
										type="button"
										id="api-type-toggle"
										class=" text-xs text-gray-700 dark:text-gray-300"
									>
										{#if apiType === 'responses'}
											{$i18n.t('Responses')}
										{:else}
											{$i18n.t('Chat Completions')}
										{/if}
									</button>
								</div>
							</div>
						{/if}

						<div class="flex items-center justify-between">
							<button
								type="button"
								class="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition mt-2"
								on:click={() => (showAdvanced = !showAdvanced)}
							>
								<svg
									xmlns="http://www.w3.org/2000/svg"
									viewBox="0 0 20 20"
									fill="currentColor"
									class="w-3 h-3 transition-transform {showAdvanced ? 'rotate-90' : ''}"
								>
									<path
										fill-rule="evenodd"
										d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
										clip-rule="evenodd"
									/>
								</svg>
								{$i18n.t('Advanced')}
							</button>
						</div>

						{#if showAdvanced}
							{#if !direct && !ollama}
								<div class="flex items-center justify-between gap-3 mt-2">
									<div>
										<label for="forward-cookies" class="text-xs text-gray-500">
											{$i18n.t('Forward cookies')}
										</label>
										<p class="text-xs text-gray-500">
											{$i18n.t('Forward cookies from your Open WebUI request to this server.')}
										</p>
									</div>
									<Switch
										id="forward-cookies"
										ariaLabel={$i18n.t('Forward cookies')}
										bind:state={forwardCookies}
									/>
								</div>
							{/if}

							{#if !direct}
								<div class="flex gap-2 mt-2">
									<div class="flex flex-col w-full">
										<label
											for="headers-input"
											class={`mb-0.5 text-xs text-gray-500
								`}>{$i18n.t('Headers')}</label
										>

										<div class="flex-1">
											<Tooltip
												content={$i18n.t(
													'Enter additional headers in JSON format (e.g. {"X-Custom-Header": "value"}'
												)}
											>
												<Textarea
													className="w-full text-sm outline-hidden"
													bind:value={headers}
													placeholder={$i18n.t('Enter additional headers in JSON format')}
													required={false}
													minSize={30}
												/>
											</Tooltip>
										</div>
									</div>
								</div>
							{/if}

							{#if !ollama && !direct}
								<div class="flex gap-2 mt-2">
									<div class="flex flex-col w-full">
										<label
											for="allowed-passthrough-params-input"
											class={`mb-0.5 text-xs text-gray-500
								`}>{$i18n.t('Passthrough params')}</label
										>

										<div class="flex-1">
											<Tooltip
												content={$i18n.t(
													'Comma-separated top-level request parameters this upstream may receive without translation. Use * to allow all captured passthrough params.'
												)}
											>
												<input
													class={`w-full text-sm ${inputClass}`}
													type="text"
													id="allowed-passthrough-params-input"
													bind:value={passthroughParams}
													placeholder={$i18n.t('thinking, output_config')}
													autocomplete="off"
												/>
											</Tooltip>
										</div>
									</div>
								</div>
							{/if}

							<div class="flex gap-2 mt-2">
								<div class="flex flex-col w-full">
									<label
										for="prefix-id-input"
										class={`mb-0.5 text-xs text-gray-500
								`}>{$i18n.t('Prefix ID')}</label
									>

									<div class="flex-1">
										<Tooltip
											content={$i18n.t(
												'Prefix ID is used to avoid conflicts with other connections by adding a prefix to the model IDs - leave empty to disable'
											)}
										>
											<input
												class={`w-full text-sm ${inputClass}`}
												type="text"
												id="prefix-id-input"
												bind:value={prefixId}
												placeholder={$i18n.t('Prefix ID')}
												autocomplete="off"
											/>
										</Tooltip>
									</div>
								</div>
							</div>

							{#if !ollama && !direct}
								<div class="flex flex-row justify-between items-center w-full mt-2">
									<label
										for="provider-select"
										class={`mb-0.5 text-xs text-gray-500
								`}>{$i18n.t('Provider')}</label
									>

									<div>
										<select
											id="provider-select"
											bind:value={provider}
											class="text-xs text-gray-700 dark:text-gray-300 bg-transparent pr-5 outline-hidden"
										>
											<option value="">{$i18n.t('Default')}</option>
											<option value="azure">{$i18n.t('Azure OpenAI')}</option>
											<option value="llama.cpp">{$i18n.t('llama.cpp')}</option>
											<option value="lmstudio">{$i18n.t('LM Studio')}</option>
											<option value="litellm">{$i18n.t('LiteLLM')}</option>
										</select>
									</div>
								</div>
							{/if}

							{#if azure}
								<div class="flex gap-2 mt-2">
									<div class="flex flex-col w-full">
										<label
											for="api-version-input"
											class={`mb-0.5 text-xs text-gray-500
										`}>{$i18n.t('API Version')}</label
										>

										<div class="flex-1">
											<input
												id="api-version-input"
												class={`w-full text-sm ${inputClass}`}
												type="text"
												bind:value={apiVersion}
												placeholder={$i18n.t('API Version')}
												autocomplete="off"
												required
											/>
										</div>
									</div>
								</div>
							{/if}

							<div class="flex flex-col w-full mt-2">
								<div class="mb-1 flex justify-between">
									<div
										class={`mb-0.5 text-xs text-gray-500
								`}
									>
										{$i18n.t('Model IDs')}
									</div>
								</div>

								{#if modelIds.length > 0}
									<ul class="flex flex-col">
										{#each modelIds as modelId, modelIdx}
											<li class=" flex gap-2 w-full justify-between items-center">
												<div class=" text-sm flex-1 py-1 rounded-lg">
													{modelId}
												</div>
												<div class="shrink-0">
													<button
														aria-label={$i18n.t(`Remove {{MODELID}} from list.`, {
															MODELID: modelId
														})}
														type="button"
														on:click={() => {
															modelIds = modelIds.filter((_, idx) => idx !== modelIdx);
														}}
													>
														<Minus strokeWidth="2" className="size-3.5" />
													</button>
												</div>
											</li>
										{/each}
									</ul>
								{:else}
									<div
										class={`text-gray-500 text-xs text-center py-2 px-10
								`}
									>
										{#if ollama}
											{$i18n.t(
												'Leave empty to include all models from "{{url}}/api/tags" endpoint',
												{
													url: url
												}
											)}
										{:else if azure}
											{$i18n.t('Deployment names are required for Azure OpenAI')}
											<!-- {$i18n.t('Leave empty to include all models from "{{url}}" endpoint', {
											url: `${url}/openai/deployments`
										})} -->
										{:else}
											{$i18n.t('Leave empty to include all models from "{{url}}/models" endpoint', {
												url: url
											})}
										{/if}
									</div>
								{/if}
							</div>

							<div class="flex items-center">
								<label class="sr-only" for="add-model-id-input">{$i18n.t('Add a model ID')}</label>
								<input
									class={`w-full text-sm ${inputClass} ${modelId ? '' : 'text-gray-500'}`}
									bind:value={modelId}
									id="add-model-id-input"
									placeholder={$i18n.t('Add a model ID')}
									list={availableModelIds.length ? 'model-id-suggestions' : undefined}
									on:keydown={(event) => {
										if (event.key === 'Enter') event.preventDefault();
									}}
								/>
								{#if availableModelIds.length}
									<datalist id="model-id-suggestions">
										{#each availableModelIds.filter((id) => !modelIds.includes(id)) as id}
											<option value={id}></option>
										{/each}
									</datalist>
								{/if}

								<div>
									<button
										type="button"
										aria-label={$i18n.t('Add')}
										on:click={() => {
											addModelHandler();
										}}
									>
										<Plus className="size-3.5" strokeWidth="2" />
									</button>
								</div>
							</div>

							<div class="flex gap-2 mt-2">
								<div class="flex flex-col w-full">
									<div
										class={`mb-0.5 text-xs text-gray-500
								`}
									>
										{$i18n.t('Tags')}
									</div>

									<div class="flex-1 mt-0.5">
										<Tags
											bind:tags
											on:add={(e) => {
												tags = [
													...tags,
													{
														name: e.detail
													}
												];
											}}
											on:delete={(e) => {
												tags = tags.filter((tag) => tag.name !== e.detail);
											}}
										/>
									</div>
								</div>
							</div>
						{/if}
					</div>

					<div class="flex justify-between items-center pt-3 text-sm font-medium">
						<div>
							{#if edit}
								<button
									class="px-1 py-1.5 text-sm font-medium text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 hover:underline transition"
									type="button"
									on:click={() => {
										showDeleteConfirmDialog = true;
									}}
								>
									{$i18n.t('Delete')}
								</button>
							{/if}
						</div>

						<button
							class="flex h-7 shrink-0 items-center justify-center gap-1.5 rounded-lg bg-gray-900 px-2.5 text-xs font-normal text-white transition hover:bg-black disabled:opacity-60 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white disabled:cursor-not-allowed"
							type="submit"
							disabled={loading}
						>
							{$i18n.t('Save')}

							{#if loading}
								<span class="shrink-0">
									<Spinner className="size-3" />
								</span>
							{/if}
						</button>
					</div>
				</form>
			</div>
		</div>
	</div>
</Modal>

<ConfirmDialog
	bind:show={showDeleteConfirmDialog}
	message={$i18n.t(
		'Are you sure you want to delete this connection? This action cannot be undone.'
	)}
	confirmLabel={$i18n.t('Delete')}
	on:confirm={() => {
		onDelete();
		show = false;
	}}
/>
