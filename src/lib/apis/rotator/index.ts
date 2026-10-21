import { WEBUI_API_BASE_URL } from '$lib/constants';

export type RotatorCandidate = {
	id: string;
	benched: boolean;
};

export type RotatorAlias = {
	id: string;
	name: string;
	priority: string[];
	candidates: RotatorCandidate[];
};

const request = async (token: string, method: string, path: string, body?: unknown) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/rotator${path}`, {
		method,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: body === undefined ? undefined : JSON.stringify(body)
	}).then(async (res) => {
		if (!res.ok) throw await res.json();
		return res.json();
	});

	return res;
};

export const getRotatorAliases = async (token: string) => {
	const res = await request(token, 'GET', '/aliases');
	return { data: res?.aliases ?? [], error: null };
};

export const createRotatorAlias = async (
	token: string,
	alias: { id: string; name: string; priority?: string[] }
) => {
	const res = await request(token, 'POST', '/aliases', alias);
	return { data: res, error: null };
};

export const updateRotatorAlias = async (
	token: string,
	aliasId: string,
	payload: { name?: string; priority?: string[] }
) => {
	const res = await request(token, 'PUT', `/aliases/${encodeURIComponent(aliasId)}`, payload);
	return { data: res, error: null };
};

export const deleteRotatorAlias = async (token: string, aliasId: string) => {
	const res = await request(token, 'DELETE', `/aliases/${encodeURIComponent(aliasId)}`);
	return { data: res, error: null };
};

export const addRotatorCandidate = async (token: string, aliasId: string, candidateId: string) => {
	const res = await request(token, 'POST', `/aliases/${encodeURIComponent(aliasId)}/candidates`, {
		id: candidateId
	});
	return { data: res, error: null };
};

export const removeRotatorCandidate = async (
	token: string,
	aliasId: string,
	candidateId: string
) => {
	const res = await request(token, 'DELETE', `/aliases/${encodeURIComponent(aliasId)}/candidates`, {
		id: candidateId
	});
	return { data: res, error: null };
};

export const reorderRotatorCandidates = async (
	token: string,
	aliasId: string,
	priority: string[]
) => {
	const res = await request(token, 'PUT', `/aliases/${encodeURIComponent(aliasId)}/priority`, {
		priority
	});
	return { data: res, error: null };
};
