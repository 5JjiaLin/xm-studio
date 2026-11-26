import { execFile } from "node:child_process";
import { readFile, writeFile, mkdir, stat, rename, rm } from "node:fs/promises";
import { basename, join } from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";

const [directory, action] = process.argv.slice(2);
const upstream = (name) => import(pathToFileURL(join(directory, name)).href);
const run = promisify(execFile);
const read = async (path) => JSON.parse(await readFile(path, "utf8"));
const write = (path, value) => writeFile(path, JSON.stringify(value));
const version = await read(join(directory, "package.json"));
if (version.version !== "0.4.1") throw new Error("Expected OpenFilm 0.4.1");

async function inspect() {
  const { look } = await upstream("src/look.mjs");
  const input = await read("input.json");
  await mkdir("inspection", { recursive: true });
  for (const shot of input.shots) {
    const facts = JSON.parse((await run("ffprobe", [
      "-v", "error", "-show_streams", "-show_format", "-of", "json", shot.file,
    ])).stdout);
    const video = facts.streams.find((stream) => stream.codec_type === "video");
    const duration = Number(video?.duration ?? facts.format.duration);
    if (!video || !Number.isFinite(duration) || duration <= 0) throw new Error("Invalid source video");
    // The sheet carries OpenFilm's source time labels. Sample densely at both joins.
    const end = Math.max(0, duration - 0.08);
    const moments = [...new Set([0, .06, .16, .33, .50, .67, .84, .94, 1]
      .map((part) => (part * end).toFixed(3)))];
    const contact = await look([shot.file, ...moments], { offline: true });
    if (!contact.ok || !contact.files.length) throw new Error("No inspection frames");
    let speech = null;
    if (facts.streams.some((stream) => stream.codec_type === "audio")) {
      const samples = "inspection/speech.f32";
      await run("ffmpeg", ["-v", "error", "-y", "-i", shot.file, "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", samples]);
      const { stdout } = await run(process.execPath, [join(directory, "src/local-run.mjs"), "asr", "whisper-small", samples]);
      speech = JSON.parse(stdout.trim().split("\n").at(-1));
      if (speech.error) throw new Error(speech.error);
    }
    await write(`inspection/${shot.key}.json`, { duration, frames: contact.files, speech });
  }
}

async function exportDraft(fps, folder) {
  const { createExports } = await upstream("studio/server/exports.mjs");
  const root = process.cwd();
  await mkdir(folder, { recursive: true });
  return await new Promise((resolve, reject) => {
    const api = createExports({
      drafts: () => null,
      onChange(job) {
        if (job.status === "done") resolve();
        else if (["failed", "cancelled"].includes(job.status)) reject(new Error(job.error ?? job.status));
      },
    });
    api.start({ id: basename(root), name: "剪映草稿", path: root }, {
      kind: "nle", format: "jianying", fps, footage: "source", media: "copy",
    }, { folder });
  });
}

async function exists(path) {
  try { await stat(path); return true; }
  catch (error) { if (error.code === "ENOENT") return false; throw error; }
}

if (action === "inspect") await inspect();
else if (action === "deliver") {
  const { fps } = (await read("input.json")).target;
  // The native render and NLE exporter consume the exact same film.html.
  if (!await exists("final.mp4")) {
    const { render } = await upstream("src/render.mjs");
    const result = await render([], { fps, out: ".rendering.mp4", loudness: -14, workers: 2 });
    if (!result.out) throw new Error(result.lines?.join("\n") ?? "Render failed");
    await rename(".rendering.mp4", "final.mp4");
  }
  if (!await exists("draft")) {
    const pending = join(process.cwd(), ".exporting");
    await rm(pending, { recursive: true, force: true }); // Owned staging dir, never a delivered draft.
    await exportDraft(fps, pending);
    await rename(pending, "draft");
  }
  await run("ffmpeg", ["-v", "error", "-y", "-i", "final.mp4", "-frames:v", "1", "-vf", "scale=360:-2", "draft_cover.jpg"]);
} else throw new Error("Unknown engine action");
