/** A page-wide limit. Superseded queued work is dropped; an in-flight request
 * keeps its slot until it settles, even if its result is no longer needed. */
const MAX_PREVIEW_REQUESTS = 3;
const pending: (() => void)[] = [];
let inFlight = 0;

export function scheduleVideoPromptPreview<T>(signal: AbortSignal, load: () => Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const start = () => {
      signal.removeEventListener("abort", cancel);
      inFlight++;
      void Promise.resolve().then(load).then(resolve, reject).finally(() => {
        inFlight--;
        pending.shift()?.();
      });
    };
    const cancel = () => {
      const index = pending.indexOf(start);
      if (index !== -1) pending.splice(index, 1);
      reject(new DOMException("Preview no longer needed", "AbortError"));
    };
    if (signal.aborted) { cancel(); return; }
    if (inFlight < MAX_PREVIEW_REQUESTS) start();
    else {
      signal.addEventListener("abort", cancel, { once: true });
      pending.push(start);
    }
  });
}

/** Polling replaces payload objects; invalidate previews on changes to reference media, not identity. */
export function videoPromptReferenceRevision(fingerprints: Record<string, number>): string {
  return JSON.stringify(Object.keys(fingerprints)
    .filter((path) => /^(characters|scenes|props|products|audio)\//.test(path))
    .sort()
    .map((path) => [path, fingerprints[path]]));
}
