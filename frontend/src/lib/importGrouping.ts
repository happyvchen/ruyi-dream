export const SUPPORTED_IMAGE_ACCEPT = 'image/jpeg,image/png,image/webp,image/heic,image/heif,image/avif,.jpg,.jpeg,.png,.webp,.heic,.heif,.avif'
export const SUPPORTED_IMAGE_FILE_PATTERN = /\.(jpe?g|png|webp|heic|heif|avif)$/i
export const SUPPORTED_IMAGE_MIME_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif', 'image/avif'])

export type ImportIssue = {
  code: 'ignored_non_image_files' | 'mixed_directory'
  message: string
  count?: number
  path?: string
}

export type ImportBatch = {
  id: string
  code: string
  files: File[]
  issues?: ImportIssue[]
}

export type ImportGroupingResult = {
  batches: ImportBatch[]
  ignoredFiles: File[]
}

export function isSupportedImageFile(file: File) {
  return SUPPORTED_IMAGE_MIME_TYPES.has(file.type.toLowerCase()) || SUPPORTED_IMAGE_FILE_PATTERN.test(file.name)
}

export function importPath(file: File) {
  return ((file as File & { webkitRelativePath?: string; relativePath?: string }).webkitRelativePath || (file as File & { relativePath?: string }).relativePath || file.name).replace(/^\/+/, '')
}

export function sanitizeClientName(value: string) {
  return value.trim().replace(/[^\u4e00-\u9fa5A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80)
}

export function timestampName(date = new Date()) {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
}

export function formalAssetFiles(files: File[], maxImages: number) {
  return files.filter(isSupportedImageFile).slice(0, maxImages)
}

export function groupImportFiles(files: File[]): ImportGroupingResult {
  const imageFiles = files
    .filter(isSupportedImageFile)
    .sort((a, b) => importPath(a).localeCompare(importPath(b), 'zh-CN', { numeric: true }))
  const ignoredFiles = files.filter((file) => !isSupportedImageFile(file))
  if (!imageFiles.length) return { batches: [], ignoredFiles }

  const paths = imageFiles.map(importPath)
  const hasFolders = paths.some((path) => path.includes('/'))
  if (!hasFolders) {
    return {
      batches: [{
        id: `loose-${Date.now()}`,
        code: timestampName(),
        files: imageFiles,
        issues: ignoredFilesIssue(ignoredFiles),
      }],
      ignoredFiles,
    }
  }

  const filesByDirectory = new Map<string, File[]>()
  const directoriesWithImages = new Set<string>()
  imageFiles.forEach((file) => {
    const directory = pathDirectory(importPath(file))
    directoriesWithImages.add(directory)
    filesByDirectory.set(directory, [...(filesByDirectory.get(directory) ?? []), file])
  })

  const groups = Array.from(filesByDirectory.entries())
    .map(([directory, groupFiles], index) => {
      const issues = [
        ...descendantDirectoryIssues(directory, directoriesWithImages),
        ...mixedDirectoryIssues(directory, directoriesWithImages),
        ...ignoredFilesIssue(ignoredFiles, index === 0),
      ]
      return {
        id: `${directory || 'root'}-${index}`,
        code: sanitizeClientName(pathBasename(directory) || pathBasename(importPath(groupFiles[0]!)) || timestampName()),
        files: groupFiles,
        issues,
      }
    })

  return { batches: groups, ignoredFiles }
}

function ignoredFilesIssue(ignoredFiles: File[], include = true): ImportIssue[] {
  if (!include || !ignoredFiles.length) return []
  return [{
    code: 'ignored_non_image_files',
    count: ignoredFiles.length,
    message: `已忽略 ${ignoredFiles.length} 个非图片文件`,
  }]
}

function mixedDirectoryIssues(directory: string, directoriesWithImages: Set<string>): ImportIssue[] {
  const ancestors = directoryAncestors(directory)
  return ancestors
    .filter((ancestor) => directoriesWithImages.has(ancestor))
    .map((ancestor) => ({
      code: 'mixed_directory' as const,
      path: ancestor,
      message: `${ancestor || '根目录'} 同时包含图片和子目录图片，请确认拆分`,
    }))
}

function descendantDirectoryIssues(directory: string, directoriesWithImages: Set<string>): ImportIssue[] {
  if (!hasDescendantImageDirectory(directory, directoriesWithImages)) return []
  return [{
    code: 'mixed_directory',
    path: directory,
    message: `${directory || '根目录'} 同时包含图片和子目录图片，请确认拆分`,
  }]
}

function hasDescendantImageDirectory(directory: string, directoriesWithImages: Set<string>) {
  const prefix = directory ? `${directory}/` : ''
  return Array.from(directoriesWithImages).some((candidate) => candidate !== directory && candidate.startsWith(prefix))
}

function pathDirectory(path: string) {
  const parts = path.split('/').filter(Boolean)
  return parts.length > 1 ? parts.slice(0, -1).join('/') : ''
}

function pathBasename(path: string) {
  const parts = path.split('/').filter(Boolean)
  return parts.at(-1) ?? ''
}

function directoryAncestors(directory: string) {
  const parts = directory.split('/').filter(Boolean)
  return parts.slice(0, -1).map((_, index) => parts.slice(0, index + 1).join('/'))
}
