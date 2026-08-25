/**
 * Turning a drop or a folder picker into a flat list of files with their paths.
 *
 * A dropped *folder* is not in `DataTransfer.files` at all — only
 * `DataTransferItem.webkitGetAsEntry()` reaches its contents, and only by walking it.
 * The prefix is non-standard and universally implemented; the standard replacement
 * (`getAsFileSystemHandle`) is Chromium-only, so this is the portable path.
 */

export interface DroppedFile {
  /** Path relative to what was dropped, e.g. `docs/img/logo.png`. */
  relativePath: string;
  file: File;
}

export interface CollectResult {
  files: DroppedFile[];
  /** More files were found than the cap allowed, and the extra ones were left out. */
  truncated: boolean;
}

/** Files one drop may carry. A bound on the work, not a judgement about real trees. */
export const MAX_DROPPED_FILES = 500;

/**
 * Bytes one file may carry, checked here before anything is sent.
 *
 * Must match ``MAX_UPLOAD_TOTAL_BYTES`` in
 * `nanoinfra/webui/workspace_upload_ws.py`, and a Python test asserts the two agree
 * rather than trusting this comment. Checked client-side as well so an oversized
 * file is a sentence on screen instead of a refusal after the bytes were read.
 */
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

/**
 * Bytes per frame. A file larger than this is sent in several.
 *
 * Sized against the transport, not against the file: `max_message_bytes` defaults to
 * 36 MB and base64 inflates by 4/3, so 8 MB of file is a ~11 MB frame — inside that
 * with room for an operator who lowered it. Chunking is what took the frame size out
 * of the limit a person sees.
 */
export const UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024;

// The DOM lib does not type the webkit entry API, and these are the three members
// this module touches. Declared narrowly rather than reaching for `any`.
interface FileSystemEntryLike {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
}
interface FileEntryLike extends FileSystemEntryLike {
  file: (onSuccess: (file: File) => void, onError?: (error: unknown) => void) => void;
}
interface DirectoryEntryLike extends FileSystemEntryLike {
  createReader: () => {
    readEntries: (
      onSuccess: (entries: FileSystemEntryLike[]) => void,
      onError?: (error: unknown) => void,
    ) => void;
  };
}

function entryFile(entry: FileEntryLike): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

/**
 * Every child of *directory*.
 *
 * `readEntries` answers at most 100 entries per call and signals the end with an
 * empty batch, so a single call silently truncates any directory past that — which
 * is the classic way a folder upload loses files without erroring.
 */
async function readAllEntries(directory: DirectoryEntryLike): Promise<FileSystemEntryLike[]> {
  const reader = directory.createReader();
  const all: FileSystemEntryLike[] = [];
  for (;;) {
    const batch = await new Promise<FileSystemEntryLike[]>((resolve, reject) =>
      reader.readEntries(resolve, reject),
    );
    if (batch.length === 0) return all;
    all.push(...batch);
  }
}

async function walk(
  entry: FileSystemEntryLike,
  prefix: string,
  out: DroppedFile[],
  maxFiles: number,
): Promise<boolean> {
  if (out.length >= maxFiles) return true;
  const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name;
  if (entry.isFile) {
    out.push({ relativePath, file: await entryFile(entry as FileEntryLike) });
    return out.length >= maxFiles;
  }
  if (!entry.isDirectory) return false;
  // An empty directory contributes no file, and so is not created. Uploading a tree
  // is uploading its files; a folder that holds nothing holds nothing to send.
  for (const child of await readAllEntries(entry as DirectoryEntryLike)) {
    if (await walk(child, relativePath, out, maxFiles)) return true;
  }
  return false;
}

/**
 * The files in a drop, including everything inside any dropped folder.
 *
 * Falls back to `DataTransfer.files` where the entry API is missing — which loses
 * folders, because without it the browser never offered them.
 */
export async function collectDroppedFiles(
  transfer: DataTransfer,
  maxFiles: number = MAX_DROPPED_FILES,
): Promise<CollectResult> {
  const items = transfer.items ? Array.from(transfer.items) : [];
  const entries = items
    .map((item) =>
      typeof (item as DataTransferItem & { webkitGetAsEntry?: unknown }).webkitGetAsEntry
      === "function"
        ? (item.webkitGetAsEntry() as FileSystemEntryLike | null)
        : null,
    )
    .filter((entry): entry is FileSystemEntryLike => entry !== null);

  if (entries.length === 0) return filesFromList(transfer.files, maxFiles);

  const out: DroppedFile[] = [];
  let truncated = false;
  for (const entry of entries) {
    if (await walk(entry, "", out, maxFiles)) {
      truncated = true;
      break;
    }
  }
  return { files: out, truncated };
}

/**
 * The files from an `<input type="file">`, keeping the tree a `webkitdirectory`
 * pick reports in `webkitRelativePath`.
 */
export function filesFromList(list: FileList | null, maxFiles: number = MAX_DROPPED_FILES): CollectResult {
  const all = list ? Array.from(list) : [];
  const files = all.slice(0, maxFiles).map((file) => ({
    // `webkitRelativePath` is `<picked folder>/…` for a directory pick and empty for
    // a plain file pick, so this covers both without asking which one happened.
    relativePath: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
    file,
  }));
  return { files, truncated: all.length > files.length };
}
