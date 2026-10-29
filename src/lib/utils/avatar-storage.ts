/**
 * Persist a user-picked avatar locally so it survives a reload.
 *
 * The model never leaves the device: it is stored in IndexedDB, not uploaded.
 * `File` objects are structured-cloneable, so the archive is written as-is and
 * unpacked again on the next load, which avoids caching derived blobs whose
 * object URLs would have to be tracked and revoked.
 *
 * IndexedDB is used rather than `localStorage` because the archive is tens of
 * megabytes and `localStorage` caps out around 5 MB.
 */

const DB_NAME = 'open-webui-avatar';
const STORE = 'models';
const KEY = 'live2d';

function open(): Promise<IDBDatabase> {
	return new Promise((resolve, reject) => {
		const request = indexedDB.open(DB_NAME, 1);
		request.onupgradeneeded = () => {
			const db = request.result;
			if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
		};
		request.onsuccess = () => resolve(request.result);
		request.onerror = () => reject(request.error);
	});
}

/** Returns the stored archive, or null when none was saved. */
export async function loadStoredAvatar(): Promise<File | null> {
	if (typeof indexedDB === 'undefined') return null;
	let db: IDBDatabase | undefined;
	try {
		db = await open();
		return await new Promise<File | null>((resolve, reject) => {
			const request = db!.transaction(STORE, 'readonly').objectStore(STORE).get(KEY);
			request.onsuccess = () => {
				const value = request.result;
				resolve(value instanceof File ? value : null);
			};
			request.onerror = () => reject(request.error);
		});
	} catch {
		// Private browsing and quota exhaustion both land here; the picker still
		// works for the current session.
		return null;
	} finally {
		db?.close();
	}
}

export async function storeAvatar(file: File): Promise<boolean> {
	if (typeof indexedDB === 'undefined') return false;
	let db: IDBDatabase | undefined;
	try {
		db = await open();
		await new Promise<void>((resolve, reject) => {
			const request = db!.transaction(STORE, 'readwrite').objectStore(STORE).put(file, KEY);
			request.onsuccess = () => resolve();
			request.onerror = () => reject(request.error);
		});
		return true;
	} catch {
		return false;
	} finally {
		db?.close();
	}
}

export async function clearStoredAvatar(): Promise<void> {
	if (typeof indexedDB === 'undefined') return;
	let db: IDBDatabase | undefined;
	try {
		db = await open();
		await new Promise<void>((resolve, reject) => {
			const request = db!.transaction(STORE, 'readwrite').objectStore(STORE).delete(KEY);
			request.onsuccess = () => resolve();
			request.onerror = () => reject(request.error);
		});
	} catch {
		// Nothing to clear.
	} finally {
		db?.close();
	}
}
