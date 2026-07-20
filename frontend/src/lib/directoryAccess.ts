export type RuyiFileSystemDirectoryHandle = {
  kind: 'directory'
  name: string
  values: () => AsyncIterableIterator<RuyiFileSystemFileHandle | RuyiFileSystemDirectoryHandle>
  queryPermission: (options?: { mode?: 'read' }) => Promise<'granted' | 'denied' | 'prompt'>
  requestPermission: (options?: { mode?: 'read' }) => Promise<'granted' | 'denied' | 'prompt'>
}

type RuyiFileSystemFileHandle = {
  kind: 'file'
  name: string
  getFile: () => Promise<File>
}

const databaseName = 'ruyi-upload-recovery'
const storeName = 'directory-handles'

export function supportsDirectoryAccess() {
  return typeof window !== 'undefined' && typeof (window as Window & { showDirectoryPicker?: unknown }).showDirectoryPicker === 'function'
}

export async function chooseDirectory() {
  const picker = (window as Window & { showDirectoryPicker?: (options?: { mode?: 'read'; id?: string }) => Promise<RuyiFileSystemDirectoryHandle> }).showDirectoryPicker
  if (!picker) return null
  return picker({ mode: 'read', id: 'ruyi-styling-import' })
}

export async function filesFromDirectory(handle: RuyiFileSystemDirectoryHandle) {
  const files: File[] = []
  await collectFiles(handle, handle.name, files)
  return files
}

async function collectFiles(handle: RuyiFileSystemDirectoryHandle, path: string, files: File[]) {
  for await (const entry of handle.values()) {
    const childPath = `${path}/${entry.name}`
    if (entry.kind === 'directory') {
      await collectFiles(entry, childPath, files)
      continue
    }
    const file = await entry.getFile()
    Object.defineProperty(file, 'relativePath', { configurable: true, value: childPath })
    files.push(file)
  }
}

export async function storeDirectoryHandle(taskId: string, handle: RuyiFileSystemDirectoryHandle) {
  const db = await openDatabase()
  await transactionDone(db, 'readwrite', (store) => store.put(handle, taskId))
  db.close()
}

export async function getDirectoryHandle(taskId: string) {
  const db = await openDatabase()
  const handle = await transactionDone<RuyiFileSystemDirectoryHandle | undefined>(db, 'readonly', (store) => store.get(taskId))
  db.close()
  return handle
}

function openDatabase() {
  return new Promise<IDBDatabase>((resolve, reject) => {
    const request = window.indexedDB.open(databaseName, 1)
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(storeName)) request.result.createObjectStore(storeName)
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

function transactionDone<T = void>(
  db: IDBDatabase,
  mode: IDBTransactionMode,
  action: (store: IDBObjectStore) => IDBRequest<T>,
) {
  return new Promise<T>((resolve, reject) => {
    const transaction = db.transaction(storeName, mode)
    const request = action(transaction.objectStore(storeName))
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
    transaction.onerror = () => reject(transaction.error)
  })
}
