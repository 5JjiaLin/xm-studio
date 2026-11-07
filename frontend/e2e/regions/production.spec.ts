import { test, expect, expectAccessible, expectReachableLayout } from "../support/test.ts";
import { waitForEntrance } from "../support/region-helpers.ts";
import { recorded } from "../support/recorded.ts";

const projectFixture = recorded<{ project: Record<string, unknown>; scripts: Record<string, unknown> }>("project-demo.json");
const workflowFixture = recorded<{ content: Record<string, unknown>; target: Record<string, unknown> }>("project-demo-workflow-status.json");
const unit = { unit_id: "E1U1", text: "@[阿明] 说：你好", duration_seconds: 7, note: null };
const readyQuote = { submittable: true, units: [{ unit_id: "E1U1", outcome: "generate", script_duration_seconds: 7, request_duration_seconds: 8, problems: [] }], estimated_total: { CNY: 1.6 }, unpriced_count: 0, confirmed_request_durations: { E1U1: 8 } };
const productionBase = {
  "GET /api/v1/projects/demo/events/stream": { status: 204, body: "" },
  "GET /api/v1/files/demo/renders/final.mp4?v=1": { status: 204, body: "" },
  "GET /api/v1/files/demo/renders/final.mp4?v=2": { status: 204, body: "" },
  "GET /api/v1/files/demo/reference_videos/E1U1.mp4": { status: 204, body: "" },
  "GET /api/v1/projects/demo": { status: 200, body: { ...projectFixture, project: { ...projectFixture.project, content_mode: "drama", generation_mode: "reference_video" } } },
  "GET /api/v1/projects/demo/workflow-status?episode=1": { status: 200, body: { ...workflowFixture, target: { ...workflowFixture.target, source: null }, content: { ...workflowFixture.content, formal_script: "present" } } },
  "GET /api/v1/projects/demo/reference-videos/episodes/1/units": { status: 200, body: { units: [{ ...unit, text: "@[阿明] @[阿红] 说：你好" }], unit_capabilities: {} } },
};

test.beforeEach(({ api }) => {
  api.override({
    "GET /api/v1/projects/demo/production/script-batch": { status: 200, body: { status: "idle", items: [], imports: [] } },
    "GET /api/v1/production/jianying/settings": { status: 200, body: { local: true, settings: { enabled: true, app_path: "/Applications/VideoFusion-macOS.app", draft_root: "/drafts" }, compatibility: { system: "Darwin", architecture: "arm64", version: "11.7.0", build: "11.7.0", ready: true, process: "stopped", verification: "mac_11_7_sample" } } },
    "GET /api/v1/projects/demo/episodes/1/production/jianying": { status: 200, body: { delivery: null } },
    "GET /api/v1/projects/demo/production/costs": { status: 200, body: { episodes: [1, 2].map((episode) => ({ episode, call_count: 0, known_call_count: 0, amounts: {}, unpriced_count: 0, pending_count: 0, reroll_count: 0, reroll_amounts: {}, unidentified_target_count: 0, categories: {} })), unassigned: { call_count: 0, categories: {} } } },
    "POST /api/v1/projects/demo/reference-videos/episodes/1/units/E1U1/prompt-preview": { status: 200, body: { text: "完整视频框架", creative_text: "完整视频框架", declarations: "", references: [], warnings: [], unavailable: null } },
    "GET /api/v1/auth/status": { status: 200, body: { enabled: false } } });
});

test("a referenced character variant has its own image and blocks video preparation until ready", async ({ page, api }) => {
  api.override({ ...productionBase,
    "GET /api/v1/files/demo/characters/base.png": { status: 204, body: "" },
    "GET /api/v1/projects/demo": { status: 200, body: { ...projectFixture, project: { ...projectFixture.project, content_mode: "drama", generation_mode: "reference_video", characters: { "阿明": { description: "主角", character_sheet: "characters/base.png", derivatives: { "战斗装": { description: "换上铠甲", character_sheet: "" } } } }, scenes: {}, props: {} } } },
    "GET /api/v1/projects/demo/reference-videos/episodes/1/units": { status: 200, body: { units: [{ ...unit, pending_authoring: true, text: "@[阿明/战斗装] 走进房间" }], unit_capabilities: {} } },
    "POST /api/v1/projects/demo/characters/阿明/derivatives/战斗装/prompt-preview": { status: 200, body: { text: "以人物原图为参考，换上铠甲", unavailable: null, warnings: [], is_text_form: true } },
  });
  await page.goto("/app/make/demo?step=assets&episode=1");
  const variant = page.getByRole("article", { name: "阿明/战斗装" });
  await expect(variant).toBeVisible();
  await expect(variant.getByRole("textbox", { name: "完整生图提示词" })).toHaveValue("以人物原图为参考，换上铠甲");
  await expect(page.getByText(/1\s*\/\s*2/)).toBeVisible();
  await page.goto("/app/make/demo?step=videos&episode=1");
  await expect(page.getByRole("button", { name: "编写视频提示词" })).toBeDisabled();
});

test("mixed unit assets show sent excluded missing and voice-only references at the reported viewport", async ({ page, api }) => {
  await page.setViewportSize({ width: 835, height: 732 });
  api.override({ ...productionBase,
    "POST /api/v1/projects/demo/reference-videos/episodes/1/units/E1U1/prompt-preview": { status: 200, body: {
      text: null, creative_text: null, declarations: "", references: [], warnings: [], unavailable: "道具图片尚未准备",
      used_assets: [
        { type: "character", name: "阿明", paths: [], request_indices: [1], status: "sent" },
        { type: "scene", name: "黑色石殿", paths: [], request_indices: [], status: "excluded" },
        { type: "prop", name: "银针", paths: [], request_indices: [], status: "missing" },
      ], voice_only: ["画外守卫"],
    } },
  });
  await page.goto("/app/make/demo?step=videos&episode=1");
  const assets = page.getByRole("region", { name: "本单元使用资产" });
  await expect(assets.getByText("角色 · 阿明", { exact: true })).toBeVisible();
  await expect(assets.getByText("场景 · 黑色石殿", { exact: true })).toBeVisible();
  await expect(assets.getByText("道具 · 银针", { exact: true })).toBeVisible();
  await expect(assets.getByText(/画外守卫/)).toBeVisible();
  await expect(page.getByText("道具图片尚未准备", { exact: true })).toBeVisible();
  await expectReachableLayout(page);
});

test("project header downloads the selected project's edit records and defaults to the open project", async ({ page, api }) => {
  api.override({ ...multiBase,
    "GET /api/v1/projects": { status: 200, body: { projects: [
      { name: "demo", title: "短剧项目", episode_count: 2 },
      { name: "archived", title: "归档短剧", production_management: { archived: true } },
    ] } },
  });
  await page.route("**/api/v1/projects/archived/production/feedback/export?include_media=false", (route) => route.fulfill({ contentType: "application/zip", body: "fixture-package" }));
  await page.goto("/app/projects");
  const exportButton = page.getByRole("banner").getByRole("button", { name: "导出修改记录", exact: true });
  await expect(exportButton).toBeVisible();
  await expectReachableLayout(page);
  await exportButton.click();
  const dialog = page.getByRole("dialog", { name: "导出修改记录", exact: true });
  await dialog.getByRole("combobox", { name: "选择项目", exact: true }).selectOption("archived");
  await expect(dialog.getByRole("checkbox", { name: "附上图片与视频", exact: true })).not.toBeChecked();
  await waitForEntrance(dialog);
  await expectAccessible(page);
  await expectReachableLayout(page);
  const downloaded = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "下载 ZIP", exact: true }).click();
  expect((await downloaded).suggestedFilename()).toBe("feedback-archived.zip");
  await expect(dialog).toHaveCount(0);
  await page.getByRole("link", { name: "查看单集", exact: true }).click();
  await expect(page).toHaveURL(/\/app\/projects\/demo$/);
  await exportButton.click();
  await expect(dialog.getByRole("combobox", { name: "选择项目", exact: true })).toHaveValue("demo");
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(dialog).toHaveCount(0);
});

test("feedback export preserves choices after failure and prevents duplicate downloads at the reported viewport", async ({ page, api }) => {
  await page.setViewportSize({ width: 835, height: 732 });
  api.override({ "GET /api/v1/projects": { status: 200, body: { projects: [
    { name: "first", title: "项目一" }, { name: "second", title: "项目二" },
  ] } } });
  let attempts = 0;
  let release!: () => void;
  let arrived!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  const started = new Promise<void>((resolve) => { arrived = resolve; });
  await page.route("**/api/v1/projects/second/production/feedback/export?include_media=true", async (route) => {
    attempts++;
    if (attempts === 1) await route.fulfill({ status: 500, json: { detail: "导出失败，请重试" } });
    else { arrived(); await pending; await route.fulfill({ contentType: "application/zip", body: "fixture-with-media" }); }
  });
  await page.goto("/app/projects");
  await expectReachableLayout(page);
  await page.getByRole("banner").getByRole("button", { name: "导出修改记录", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "导出修改记录", exact: true });
  const project = dialog.getByRole("combobox", { name: "选择项目", exact: true });
  const media = dialog.getByRole("checkbox", { name: "附上图片与视频", exact: true });
  await project.selectOption("second");
  await media.check();
  await dialog.getByRole("button", { name: "下载 ZIP", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("导出失败，请重试");
  await expect(project).toHaveValue("second");
  await expect(media).toBeChecked();
  await expectReachableLayout(page);
  const downloaded = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "下载 ZIP", exact: true }).click();
  await started;
  await expect(dialog.getByRole("button", { name: "导出中…", exact: true })).toBeDisabled();
  await expect(project).toBeDisabled();
  await expect(media).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "关闭", exact: true })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  release();
  expect((await downloaded).suggestedFilename()).toBe("feedback-second.zip");
  await expect(dialog).toHaveCount(0);
  expect(attempts).toBe(2);
});

test("project header keeps feedback export disabled when no projects exist", async ({ page, api }) => {
  api.override({ "GET /api/v1/projects": { status: 200, body: { projects: [] } } });
  await page.goto("/app/projects");
  await expect(page.getByText("暂无内容", { exact: true })).toBeVisible();
  await expect(page.getByRole("banner").getByRole("button", { name: "导出修改记录", exact: true })).toBeDisabled();
});

test("five steps fit the viewport and no platform services load", async ({ page }) => {
  const unrelated: string[] = [];
  page.on("request", (r) => { if (/\/api\/.*(assistant|onboarding|market|official|agent-memory|asset-library)/.test(r.url())) unrelated.push(r.url()); });
  await page.goto("/app/make");
  await expect(page.getByRole("navigation", { name: "制作步骤" }).getByRole("link")).toHaveCount(5);
  await expect(page.getByRole("textbox", { name: "单集剧本" })).toBeVisible();
  await expect(page.getByRole("button", { name: "开始拆解" })).toBeVisible();
  await expectReachableLayout(page);
  await expectAccessible(page);
  expect(unrelated).toEqual([]);
});
test("style choices show both categories and preserve the selected preview on reload", async ({ page }) => {
  await page.goto("/app/make");
  const picker = page.getByRole("combobox", { name: "模板风格" });
  await expect(picker).toHaveValue("live_premium_drama");
  await expect(page.getByRole("img", { name: "精品短剧", exact: true })).toHaveAttribute("src", "/style-thumbnails/live_premium_drama.png");
  await expect(picker.locator("option")).toHaveCount(36);
  await expect(picker.locator("optgroup").nth(0)).toHaveAttribute("label", "AI 真人剧");
  await expect(picker.locator("optgroup").nth(1)).toHaveAttribute("label", "AI 漫剧");
  await expectAccessible(page);
  await picker.selectOption("anim_ghibli");
  await expect(picker).toHaveValue("anim_ghibli");
  await expect(page.getByRole("img", { name: "吉卜力", exact: true })).toHaveAttribute("src", "/style-thumbnails/anim_ghibli.png");
  await page.reload();
  await expect(picker).toHaveValue("anim_ghibli");
  await expectReachableLayout(page);
});

test("invalid draft style falls back and creation submits the selected valid template", async ({ page, api }) => {
  await page.addInitScript(() => sessionStorage.setItem("xm:script-draft", JSON.stringify({ title: "模板验收", text: "阿明说：你好", templateId: "05_live_cinema" })));
  const created: unknown[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/projects") created.push(request.postDataJSON());
  });
  api.override({ ...productionBase,
    "POST /api/v1/projects": { status: 200, body: { name: "demo" } },
    "GET /api/v1/projects/demo": { status: 200, body: { ...projectFixture, project: { ...projectFixture.project, content_mode: "drama", generation_mode: "reference_video", episodes: [{ episode: 1, title: "模板验收", script_file: "scripts/episode_1.json" }] } } },
    "POST /api/v1/projects/demo/episodes": { status: 200, body: { episode: 1 } },
    "POST /api/v1/projects/demo/episodes/1/production/source": { status: 200, body: { needs_confirmation: false } },
    "POST /api/v1/projects/demo/episodes/1/script-plan": { status: 200, body: { batch: { members: [] } } },
  });
  await page.goto("/app/make");
  const picker = page.getByRole("combobox", { name: "模板风格" });
  await expect(picker).toHaveValue("live_premium_drama");
  await picker.selectOption("live_cinema");
  await page.getByRole("button", { name: "开始拆解", exact: true }).click();
  await expect(page).toHaveURL(/\/app\/make\/demo\?step=breakdown&episode=1$/);
  expect(created).toEqual([{ title: "模板验收", content_mode: "drama", generation_mode: "reference_video", aspect_ratio: "9:16", style_template_id: "live_cinema" }]);
});

test("open works is a simple dialog", async ({ page, api }) => {
  api.override({ "GET /api/v1/projects": { status: 200, body: { projects: [{ name: "work-1", title: "验收短剧", style: "", thumbnail: null, status: {}, last_activity_at: null }] } } });
  await page.goto("/app/make");
  await page.getByRole("button", { name: "打开作品" }).click();
  await expect(page.getByRole("dialog", { name: "打开作品" })).toBeVisible();
  await expect(page.getByRole("link", { name: "验收短剧" })).toHaveAttribute("href", "/app/projects/work-1");
  await expectReachableLayout(page);
});
test("configuration shows three channels and returns to the same step", async ({ page, api }) => {
  api.override({
    "GET /api/v1/projects/work-1": { status: 404, body: { detail: "fixture work" } },
    "GET /api/v1/projects/work-1/workflow-status": { status: 404, body: { detail: "fixture work" } },
    "GET /api/v1/tasks?page_size=200&project_name=work-1": { status: 200, body: { tasks: [], total: 0 } },
    "GET /api/v1/tasks/stats?project_name=work-1": { status: 200, body: { queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 } },
    "GET /api/v1/projects/work-1/events/stream": { status: 204, body: "" },
  });
  await page.addInitScript(() => sessionStorage.setItem("xm:production-return", "/app/make/work-1?step=assets"));
  await page.goto("/app/settings?section=default-models");
  await expect(page.getByRole("heading", { name: "默认模型" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "旁白配音" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "ArcReel", exact: true })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "模型配置" }).getByRole("link")).toHaveCount(3);
  await expectReachableLayout(page);
  await expectAccessible(page);
  // No project requests are needed to verify the saved return destination.
  await page.getByRole("button", { name: "返回制作" }).click();
  await expect(page).toHaveURL(/\/app\/make\/work-1\?step=assets$/);
});
test("removed sections and workspace routes never mount the old platform", async ({ page }) => {
  const unrelated: string[] = [];
  page.on("request", (r) => { if (/\/api\/.*(assistant|onboarding|market|official|agent-memory|asset-library)/.test(r.url())) unrelated.push(r.url()); });
  for (const url of ["/app/assets", "/app/projects/old/settings", "/app/projects/old/episodes/1", "/app/make/old/files"]) {
    await page.goto(url); await expect(page).toHaveURL(/\/app\/make$/);
    await expect(page.getByRole("textbox", { name: "单集剧本" })).toBeVisible();
  }
  await page.goto("/app/settings?section=market");
  await expect(page.getByRole("heading", { name: "默认模型" })).toBeVisible();
  expect(unrelated).toEqual([]);
});

test("video edits save before generation from the page footer without a quote", async ({ page, api }) => {
  const submitted: unknown[] = [];
  page.on("request", (request) => { if (request.method() === "POST" && request.url().endsWith("/production/videos")) submitted.push(request.postDataJSON()); });
  api.override({ ...productionBase,
    "PATCH /api/v1/projects/demo/reference-videos/episodes/1/units/E1U1": { status: 200, body: { unit: { ...unit, text: "@[阿明] 说：欢迎" }, unit_capability: {} } },
    "POST /api/v1/projects/demo/episodes/1/production/videos/preview": { status: 200, body: readyQuote },
    "POST /api/v1/projects/demo/episodes/1/production/videos": { status: 200, body: { episode: 1, status: "active", stage: "videos", action: "videos" } },
  });
  await page.goto("/app/make/demo?step=videos");
  await page.getByRole("textbox", { name: "视频提示词", exact: true }).fill("@[阿明] 说：欢迎");
  await expect(page.getByRole("button", { name: "查看报价", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "确认生成视频", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "有未保存的修改" })).toBeVisible();
  expect(submitted).toEqual([]);
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.getByRole("button", { name: "确认生成视频", exact: true }).click();
  await expect.poll(() => submitted.length).toBe(1);
  expect(submitted[0]).toEqual({});
  await expectReachableLayout(page);
});

test("refreshing a running batch never submits paid generation", async ({ page, api }) => {
  const paid: string[] = [];
  page.on("request", (request) => { if (request.method() === "POST" && /\/production\/(videos|assets)$/.test(request.url())) paid.push(request.url()); });
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "active", stage: "videos", action: "videos", batch_id: "existing-batch", members: [{ unit_id: "E1U1", status: "running" }] } },
  });
  await page.goto("/app/make/demo?step=videos");
  await expect(page.getByRole("button", { name: "确认生成视频", exact: true })).toBeDisabled();
  await page.reload();
  await expect(page.getByRole("button", { name: "确认生成视频", exact: true })).toBeDisabled();
  expect(paid).toEqual([]);
});

test("completed videos stay for review and only a confirmed single clip is regenerated before explicit merge", async ({ page, api }) => {
  let status = "active";
  const generated: unknown[] = [];
  let merged = 0;
  let saved = unit.text;
  api.override({ ...productionBase,
    "GET /api/v1/tasks?page=1&page_size=100": { status: 200, body: { items: [], total: 0 } },
    "GET /api/v1/files/demo/reference_videos/E1U2.mp4": { status: 204, body: "" },
    "POST /api/v1/projects/demo/reference-videos/episodes/1/units/E1U2/prompt-preview": { status: 200, body: { text: "完整视频框架", creative_text: "完整视频框架", declarations: "", references: [], unavailable: null } },
  });
  await page.route("**/api/v1/projects/demo/episodes/1/production", (route) => route.fulfill({ json: {
    episode: 1, status, stage: "videos", action: "videos",
    // An existing cut must not trigger automatic navigation after a new batch completes.
    output: { status: "current", artifact_path: "renders/final.mp4", version: 2 },
    members: ["E1U1", "E1U2"].map((unit_id) => ({ unit_id, status: status === "active" ? "running" : "succeeded" })),
  } }));
  await page.route("**/api/v1/projects/demo/reference-videos/episodes/1/units", (route) => route.fulfill({ json: {
    units: ["E1U1", "E1U2"].map((unit_id) => ({ ...unit, unit_id, text: unit_id === "E1U1" ? saved : unit.text,
      generated_assets: { video_clip: `reference_videos/${unit_id}.mp4` } })), unit_capabilities: {},
  } }));
  await page.route("**/api/v1/projects/demo/reference-videos/episodes/1/units/E1U1", async (route) => {
    const body = route.request().postDataJSON() as { prompt: string };
    saved = body.prompt;
    await route.fulfill({ json: { unit: { ...unit, text: saved } } });
  });
  await page.route("**/api/v1/projects/demo/episodes/1/production/videos", async (route) => {
    generated.push(route.request().postDataJSON()); status = "active";
    await route.fulfill({ json: { episode: 1, status, stage: "videos", action: "videos" } });
  });
  await page.route("**/api/v1/projects/demo/episodes/1/production/render", async (route) => {
    merged++;
    await route.fulfill({ json: { episode: 1, status: "active", stage: "render" } });
  });
  await page.goto("/app/make/demo?step=videos&episode=1");
  const first = page.getByRole("article", { name: "单元 1 · 7 秒", exact: true });
  const second = page.getByRole("article", { name: "单元 2 · 7 秒", exact: true });
  const regenerate = first.getByRole("button", { name: "确认提示词并重新生成当前视频", exact: true });
  const merge = page.getByRole("button", { name: "确认视频并合成成片", exact: true });
  await expect(regenerate).toBeDisabled();
  await expect(merge).toBeDisabled();
  status = "completed";
  await expect(regenerate).toBeEnabled({ timeout: 10000 });
  await expect(merge).toBeEnabled();
  await expect(page).toHaveURL(/step=videos&episode=1$/);
  expect([generated.length, merged]).toEqual([0, 0]);
  await page.reload();
  await expect(regenerate).toBeEnabled();
  await expect(page).toHaveURL(/step=videos&episode=1$/);
  const prompt = first.getByRole("textbox", { name: "视频提示词", exact: true });
  await prompt.fill("更新后的当前视频提示词");
  await expect(regenerate).toBeDisabled();
  await merge.click();
  await expect(page.getByRole("alert").filter({ hasText: "有未保存的修改" })).toBeVisible();
  expect([generated.length, merged]).toEqual([0, 0]);
  await first.getByRole("button", { name: "保存", exact: true }).click();
  await expect(regenerate).toBeEnabled();
  await regenerate.click();
  await expect.poll(() => generated.length).toBe(1);
  expect(generated[0]).toEqual({ unit_ids: ["E1U1"] });
  await expect(regenerate).toBeDisabled();
  await expect(second.getByRole("button", { name: "确认提示词并重新生成当前视频", exact: true })).toBeDisabled();
  await expect(prompt).toBeDisabled();
  await expect(merge).toBeDisabled();
  await expect(second.locator("video")).toHaveAttribute("src", /reference_videos\/E1U2.mp4/);
  status = "completed";
  await expect(regenerate).toBeEnabled({ timeout: 10000 });
  await expect(page).toHaveURL(/step=videos&episode=1$/);
  expect(merged).toBe(0);
  await expectReachableLayout(page);
  await merge.click();
  await expect.poll(() => merged).toBe(1);
  await expect(page).toHaveURL(/step=output&episode=1$/);
  expect(generated).toHaveLength(1);
});

test("production prompts keep a fixed reading area and hide per-unit review/upload controls", async ({ page, api }) => {
  api.override({ ...productionBase });
  await page.goto("/app/make/demo?step=videos");
  const prompt = page.getByRole("textbox", { name: "视频提示词", exact: true });
  const initialHeight = (await prompt.boundingBox())?.height;
  await prompt.fill(Array.from({ length: 30 }, (_, i) => `提示词第 ${i + 1} 行`).join("\n"));
  await expect.poll(async () => (await prompt.boundingBox())?.height).toBe(initialHeight);
  await expect(page.getByRole("textbox", { name: "修改原因（可选）", exact: true })).toHaveCount(0);
  await expect(page.getByLabel("上传替换视频")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "确认提示词并生成当前视频", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "确认生成视频", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "导出修改记录", exact: true })).toHaveCount(0);
  await expectReachableLayout(page);
});

test("asset edits save and regenerate directly without a quote", async ({ page, api }) => {
  const paid: string[] = [];
  page.on("request", (request) => { if (request.method() === "POST" && request.url().includes("/generate/character/")) paid.push(request.url()); });
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo/episodes/1/script-review": { status: 200, body: { episode: 1, status: "confirmed", fingerprint: "confirmed", content: null } },
    "GET /api/v1/projects/demo": { status: 200, body: { ...projectFixture, project: { ...projectFixture.project, content_mode: "drama", generation_mode: "reference_video", characters: { 阿明: { description: "青年" } }, scenes: {}, props: {} } } },
    "PATCH /api/v1/projects/demo/characters/%E9%98%BF%E6%98%8E": { status: 200, body: { success: true } },
    "POST /api/v1/projects/demo/production/assets/preview": { status: 200, body: { estimated_cost: { CNY: 0.2 }, unpriced_count: 0, submittable: true, problems: [] } },
    "POST /api/v1/projects/demo/episodes/1/production/videos/preview": { status: 200, body: readyQuote },
    "POST /api/v1/projects/demo/generate/character/%E9%98%BF%E6%98%8E?episode=1": { status: 200, body: { success: true, task_id: "asset-retry", deduped: false, message: "queued" } },
  });
  await page.goto("/app/make/demo?step=assets");
  await page.getByRole("textbox", { name: "完整生图提示词", exact: true }).fill("穿着白衬衫的青年");
  await page.getByRole("button", { name: "确认提示词并生成资产", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "有未保存的修改" })).toBeVisible();
  expect(paid).toEqual([]);
  await expect(page.getByRole("button", { name: "查看报价", exact: true })).toHaveCount(0);
  await expect(page.getByText("视频报价", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /确认提示词并(重新生成图片|生成图片)/, exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.getByRole("button", { name: /确认提示词并(重新生成图片|生成图片)/, exact: true }).click();
  await expect.poll(() => paid.length).toBe(1);
});

test("only current rendered output has one working download", async ({ page, api }) => {
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "completed", stage: "render", action: "videos", output: { status: "current", artifact_path: "renders/final.mp4", version: 2 } } },
  });
  await page.goto("/app/make/demo?step=output");
  await expect(page.getByRole("link", { name: "下载 MP4", exact: true })).toHaveCount(1);
  await expect(page.getByRole("link", { name: "下载 MP4", exact: true })).toHaveAttribute("href", /renders\/final\.mp4/);
  await page.getByRole("navigation", { name: "制作步骤" }).getByRole("link").nth(3).click();
  await expect(page).toHaveURL(/step=videos&episode=1$/);
  await page.waitForTimeout(3200);
  await expect(page).toHaveURL(/step=videos&episode=1$/);
});

test("stale or missing outputs cannot masquerade as current downloads", async ({ page, api }) => {
  for (const status of ["stale", "missing"]) {
    api.override({ ...productionBase,
      "GET /api/v1/projects/demo/reference-videos/episodes/1/units": { status: 200, body: { units: [{ ...unit, generated_assets: { video_clip: "reference_videos/E1U1.mp4" } }], unit_capabilities: {} } },
      "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "completed", stage: "render", output: { status, artifact_path: "renders/final.mp4", version: status === "missing" ? null : 1 } } },
    });
    await page.goto("/app/make/demo?step=output");
    await expect(page.getByRole("button", { name: "重新合成成片", exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "下载 MP4", exact: true })).toHaveCount(0);
    await expect(page.locator("video")).toHaveCount(status === "stale" ? 1 : 0);
  }
});

test("render failure offers local rebuilding without paid video retry", async ({ page, api }) => {
  const posts: string[] = [];
  page.on("request", (request) => { if (request.method() === "POST" && !request.url().endsWith("/prompt-preview")) posts.push(new URL(request.url()).pathname); });
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo/reference-videos/episodes/1/units": { status: 200, body: { units: [{ ...unit, generated_assets: { video_clip: "reference_videos/E1U1.mp4" } }], unit_capabilities: {} } },
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "failed", stage: "render", action: "videos", error: "render failed", members: [{ unit_id: "render-final-cut", status: "failed" }] } },
    "POST /api/v1/projects/demo/episodes/1/production/render": { status: 200, body: { episode: 1, status: "active", stage: "render" } },
  });
  await page.goto("/app/make/demo?step=videos");
  await expect(page.getByRole("button", { name: "重试失败项", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "确认视频并合成成片", exact: true }).click();
  await expect.poll(() => posts.filter((path) => path.endsWith("/production/render")).length).toBe(1);
  expect(posts.filter((path) => path.endsWith("/production/videos"))).toEqual([]);
});

test("failed video retry selects only failed units", async ({ page, api }) => {
  const bodies: unknown[] = [];
  page.on("request", (request) => { if (request.method() === "POST" && request.url().endsWith("/production/videos")) bodies.push(request.postDataJSON()); });
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "failed", stage: "videos", action: "videos", members: [{ unit_id: "E1U1", status: "failed" }, { unit_id: "E1U2", status: "succeeded" }] } },
    "POST /api/v1/projects/demo/episodes/1/production/videos/preview": { status: 200, body: readyQuote },
    "POST /api/v1/projects/demo/episodes/1/production/videos": { status: 200, body: { episode: 1, status: "active", stage: "videos" } },
  });
  await page.goto("/app/make/demo?step=videos");
  await page.getByRole("button", { name: "重试失败项", exact: true }).click();
  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0]).toEqual({ unit_ids: ["E1U1"] });
});

test("breakdown saves edited content before confirming its latest fingerprint", async ({ page, api }) => {
  const calls: Array<{ method: string; body: unknown }> = [];
  let content = { units: [{ ...unit, source_text: "阿明说：你好" }], new_assets: [{ type: "character", name: "阿明", description: "青年", decision: "register", reason: "主角", aliases: [], target: "", asset_name: "", generation_prompt: "" }] };
  let fingerprint = "before-edit";
  api.override({ ...productionBase,
    "POST /api/v1/projects/demo/episodes/1/production/assets": { status: 200, body: { episode: 1, status: "active", stage: "prompts", action: "assets" } },
    "POST /api/v1/projects/demo/episodes/1/production/videos/preview": { status: 200, body: readyQuote },
  });
  await page.route("**/api/v1/projects/demo/episodes/1/script-review**", async (route) => {
    if (route.request().method() === "POST") calls.push({ method: "confirm", body: { fingerprint } });
    if (route.request().method() === "PUT") {
      content = route.request().postDataJSON() as typeof content;
      calls.push({ method: "save", body: content });
      fingerprint = "after-edit";
    }
    await route.fulfill({ json: { episode: 1, content_mode: "drama", status: "pending_review", fingerprint, confirmed_at: null, content, quarantine: null, supported_durations: null, duration_tiers: null, script_overwrite: null } });
  });
  page.on("request", (request) => { if (request.method() === "POST" && /\/production\/(assets|videos)$/.test(request.url())) calls.push({ method: "unexpected-generation", body: request.postDataJSON() }); });
  await page.goto("/app/make/demo?step=breakdown");
  await page.getByRole("textbox", { name: "单元 1 内容", exact: true }).fill("@[阿明] 说：欢迎");
  await page.getByRole("spinbutton", { name: "单元 1 时长", exact: true }).fill("8");
  await page.getByRole("textbox", { name: "完整生图提示词", exact: true }).fill("穿白衬衫的青年");
  await page.getByRole("button", { name: "确认拆解，审查资产提示词", exact: true }).click();
  await expect(page).toHaveURL(/step=assets&episode=1$/);
  expect(calls.map((call) => call.method)).toEqual(["save", "confirm"]);
  expect(content.units[0].text).toBe("@[阿明] 说：欢迎");
  expect(content.units[0].duration_seconds).toBe(8);
  expect(content.new_assets[0].generation_prompt).toBe("穿白衬衫的青年");
  expect(content.new_assets[0].description).toBe("青年");
  expect(calls[1].body).toEqual({ fingerprint: "after-edit" });
});

test("replanning requires a saved draft, reason and explicit confirmation", async ({ page, api }) => {
  await page.setViewportSize({ width: 835, height: 732 });
  const content = { units: [{ ...unit, source_text: "阿明说：你好" }], new_assets: [] };
  let submissions = 0;
  let submittedBody: unknown;
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo/episodes/1/script-review": { status: 200, body: { episode: 1, content_mode: "drama", status: "pending_review", fingerprint: "draft", confirmed_at: null, content, quarantine: null, supported_durations: null, duration_tiers: null, script_overwrite: null } },
  });
  await page.route("**/api/v1/projects/demo/episodes/1/script-plan", async (route) => {
    submittedBody = route.request().postDataJSON();
    submissions++;
    await route.fulfill({ json: { batch: { members: [] } } });
  });
  await page.goto("/app/make/demo?step=breakdown&episode=1");
  const replan = page.getByRole("button", { name: "重新拆解", exact: true });
  await expect(replan).toBeVisible();
  await replan.click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("修改都会丢失");
  const confirm = dialog.getByRole("button", { name: "重新拆解", exact: true });
  const reason = dialog.getByRole("textbox", { name: "重新拆解的原因", exact: true });
  await expect(confirm).toBeDisabled();
  await reason.fill("   ");
  await expect(confirm).toBeDisabled();
  await expectReachableLayout(page);
  await expectAccessible(page);
  await dialog.getByRole("button", { name: "取消" }).click();
  await expect(dialog).toHaveCount(0);
  expect(submissions).toBe(0);
  await page.getByRole("textbox", { name: "单元 1 内容", exact: true }).fill("未保存的修改");
  await replan.click();
  await expect(dialog).toHaveCount(0);
  expect(submissions).toBe(0);
  await page.getByRole("textbox", { name: "单元 1 内容", exact: true }).fill(unit.text);
  await replan.click();
  await reason.fill("  手链脚链归入人物束缚形态，不单独建道具；保留束缚状态。  ");
  await confirm.click();
  await expect.poll(() => submissions).toBe(1);
  expect(submittedBody).toEqual({ replan_reason: "手链脚链归入人物束缚形态，不单独建道具；保留束缚状态。" });
  await expect(dialog).toHaveCount(0);
});

test("failed asset continuation can resume in place without submitting videos", async ({ page, api }) => {
  const paid: string[] = [];
  page.on("request", (request) => { if (request.method() === "POST") paid.push(new URL(request.url()).pathname); });
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo": { status: 200, body: assetsProject },
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "failed", stage: "prompts", action: "assets", error: "failed" } },
    "GET /api/v1/projects/demo/episodes/1/script-review": { status: 200, body: { episode: 1, status: "confirmed", fingerprint: "confirmed", content: null } },
    "POST /api/v1/projects/demo/episodes/1/production/assets": { status: 200, body: { episode: 1, status: "active", stage: "prompts", action: "assets" } },
    "POST /api/v1/projects/demo/episodes/1/production/videos/preview": { status: 200, body: readyQuote },
  });
  await page.goto("/app/make/demo?step=assets");
  await page.getByRole("button", { name: "确认提示词并生成资产", exact: true }).click();
  await expect.poll(() => paid.filter((path) => path.endsWith("/production/assets")).length).toBe(1);
  expect(paid.filter((path) => path.endsWith("/production/videos"))).toEqual([]);
});

const planTask = {
  task_id: "breakdown-task", project_name: "demo", task_type: "text_reference_script_plan", media_type: "text",
  resource_id: "episode-1", resource_type: null, script_file: null, payload: { episode: 1 },
  status: "running", result: {}, error_message: null, cancelled_by: null, provider_id: "text", provider_job_id: null,
  source: "webui", queued_at: "2026-01-01T07:59:00", started_at: "2026-01-01T07:59:10", finished_at: null, updated_at: "2026-01-01T07:59:10",
};
const noReview = { episode: 1, status: "empty", fingerprint: null, content: null, quarantine: null };

test("running breakdown shows visual status and updates to a review table without another submission", async ({ page, api }) => {
  const posts: string[] = [];
  page.on("request", (request) => { if (request.method() === "POST") posts.push(request.url()); });
  api.override({ ...productionBase,
    "GET /api/v1/tasks?page_size=200&project_name=demo": { status: 200, body: { items: [planTask], total: 1 } },
    "GET /api/v1/projects/demo/episodes/1/script-review": { status: 200, body: noReview },
  });
  await page.goto("/app/make/demo?step=breakdown");
  const progress = page.getByRole("region", { name: "拆解进度" });
  await expect(progress.getByRole("status")).toHaveText("正在拆解剧本");
  await expect(progress.getByText("模型拆解剧本与资产", { exact: false })).toBeVisible();
  await expect(progress.getByText("已运行", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "正在拆解剧本", exact: true })).toBeDisabled();
  await expectReachableLayout(page);
  await expectAccessible(page);
  await page.reload();
  await expect(progress.getByRole("status")).toHaveText("正在拆解剧本");
  api.override({
    "GET /api/v1/tasks?page_size=200&project_name=demo": { status: 200, body: { items: [{ ...planTask, status: "succeeded", finished_at: "2026-01-01T08:00:00", updated_at: "2026-01-01T08:00:00" }], total: 1 } },
    "GET /api/v1/projects/demo/episodes/1/script-review": { status: 200, body: { episode: 1, content_mode: "drama", status: "pending_review", fingerprint: "finished-plan", content: { units: [{ ...unit, source_text: "阿明说：你好" }], new_assets: [] }, quarantine: null } },
  });
  await expect(progress.getByRole("status")).toHaveText("拆解完成，请审查", { timeout: 10000 });
  await expect(page.getByRole("table", { name: "单集分解表" })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认拆解，审查资产提示词", exact: true })).toBeEnabled();
  expect(posts).toEqual([]);
});

test("queued breakdown remains visible after refresh and cannot be submitted twice", async ({ page, api }) => {
  api.override({ ...productionBase,
    "GET /api/v1/tasks?page_size=200&project_name=demo": { status: 200, body: { items: [{ ...planTask, status: "queued", started_at: null }], total: 1 } },
    "GET /api/v1/projects/demo/episodes/1/script-review": { status: 200, body: noReview },
  });
  await page.goto("/app/make/demo?step=breakdown");
  await expect(page.getByRole("status")).toHaveText("已排队，等待拆解");
  await expect(page.getByText("已等待", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("status")).toHaveText("已排队，等待拆解");
  await page.getByRole("navigation", { name: "制作步骤" }).getByRole("link").first().click();
  await expect(page.getByRole("button", { name: "正在拆解剧本", exact: true })).toBeDisabled();
});

test("failed breakdown displays the actual error and requires explicit paid retry", async ({ page, api }) => {
  const submitted: string[] = [];
  page.on("request", (request) => { if (request.method() === "POST") submitted.push(new URL(request.url()).pathname); });
  api.override({ ...productionBase,
    "GET /api/v1/tasks?page_size=200&project_name=demo": { status: 200, body: { items: [{ ...planTask, status: "failed", error_message: "模型请求超时", finished_at: "2026-01-01T08:00:00" }], total: 1 } },
    "GET /api/v1/projects/demo/episodes/1/script-review": { status: 200, body: noReview },
    "POST /api/v1/projects/demo/episodes/1/script-plan": { status: 200, body: { batch: { members: [] } } },
  });
  await page.goto("/app/make/demo?step=breakdown");
  await expect(page.getByRole("status")).toHaveText("拆解未完成");
  await expect(page.getByRole("alert")).toHaveText("模型请求超时");
  const retryButton = page.getByRole("button", { name: "重新拆解", exact: true });
  await expect(retryButton).toBeEnabled();
  await waitForEntrance(retryButton);
  await expectReachableLayout(page);
  await expectAccessible(page);
  expect(submitted).toEqual([]);
  await page.getByRole("button", { name: "重新拆解", exact: true }).click();
  const dialog = page.getByRole("alertdialog");
  expect(submitted).toEqual([]);
  await dialog.getByRole("textbox", { name: "重新拆解的原因", exact: true }).fill("上次模型超时，请重新拆解。");
  await dialog.getByRole("button", { name: "重新拆解", exact: true }).click();
  await expect.poll(() => submitted).toEqual(["/api/v1/projects/demo/episodes/1/script-plan"]);
});

test("another episode's planning task does not claim this breakdown is running", async ({ page, api }) => {
  api.override({ ...productionBase,
    "GET /api/v1/tasks?page_size=200&project_name=demo": { status: 200, body: { items: [{ ...planTask, resource_id: "episode-2", payload: { episode: 2 } }], total: 1 } },
    "GET /api/v1/projects/demo/episodes/1/script-review": { status: 200, body: noReview },
  });
  await page.goto("/app/make/demo?step=breakdown");
  await expect(page.getByRole("status")).toHaveText("尚未开始拆解");
  await expect(page.getByRole("button", { name: "确认拆解，审查资产提示词", exact: true })).toBeDisabled();
});

const assetsProject = { ...projectFixture, project: { ...projectFixture.project, content_mode: "drama", generation_mode: "reference_video", characters: { 阿明: { description: "青年" }, 阿红: { description: "少女" } }, scenes: {}, props: {} } };

test("asset prompts must be reviewed before explicit generation and refresh never repeats it", async ({ page, api }) => {
  let started = 0;
  let state = { episode: 1, status: "idle", stage: null as string | null, action: "assets" };
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo": { status: 200, body: assetsProject },
    "GET /api/v1/projects/demo/episodes/1/script-review": { status: 200, body: { episode: 1, status: "confirmed", fingerprint: "confirmed", content: null } },
    "POST /api/v1/projects/demo/episodes/1/production/videos/preview": { status: 200, body: { ...readyQuote, submittable: false } },
  });
  await page.route("**/api/v1/projects/demo/episodes/1/production", (route) => route.fulfill({ json: state }));
  await page.route("**/api/v1/projects/demo/episodes/1/production/assets", (route) => {
    started += 1;
    state = { ...state, status: "active", stage: "assets" };
    return route.fulfill({ json: state });
  });
  await page.route("**/api/v1/tasks?**", (route) => route.fulfill({ json: { items: started ? [
    { ...planTask, task_id: "image-1", task_type: "character", media_type: "image", resource_id: "阿明", status: "running" },
    { ...planTask, task_id: "image-2", task_type: "character", media_type: "image", resource_id: "阿红", status: "queued", started_at: null },
  ] : [], total: started ? 2 : 0 } }));
  await page.goto("/app/make/demo?step=assets");
  await expect(page.getByRole("textbox", { name: "完整生图提示词", exact: true }).first()).not.toHaveValue("");
  expect(started).toBe(0);
  await page.reload();
  await expect(page.getByRole("button", { name: "确认提示词并生成资产", exact: true })).toBeEnabled();
  expect(started).toBe(0);
  await page.getByRole("button", { name: "确认提示词并生成资产", exact: true }).click();
  await expect.poll(() => started).toBe(1);
  await expect(page.getByRole("article", { name: "阿明", exact: true }).getByRole("status").filter({ hasText: "正在生成图片" })).toContainText("正在生成图片");
  await expect(page.getByRole("article", { name: "阿红", exact: true }).getByRole("status")).toContainText("排队中");
  await expect(page.getByRole("button", { name: /确认提示词并(重新生成图片|生成图片)/, exact: true }).first()).toBeDisabled();
  await expect(page.getByText("视频报价", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "查看报价", exact: true })).toHaveCount(0);
  await expectReachableLayout(page);
  await expectAccessible(page);
  await page.reload();
  await expect(page.getByRole("article", { name: "阿明", exact: true }).getByRole("status").filter({ hasText: "正在生成图片" })).toContainText("正在生成图片");
  expect(started).toBe(1);
});

test("completed images are preserved and each card regenerates independently", async ({ page, api }) => {
  let retried = 0;
  const imageTask = { ...planTask, task_id: "old-image", task_type: "character", media_type: "image", resource_id: "阿明", status: "succeeded", finished_at: "2026-01-01T08:00:00", updated_at: "2026-01-01T08:00:00" };
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo": { status: 200, body: { ...assetsProject, project: { ...assetsProject.project, characters: { 阿明: { description: "青年", character_sheet: "characters/a.png" }, 阿红: { description: "少女", character_sheet: "characters/b.png" } } } } },
    "GET /api/v1/files/demo/characters/a.png": { status: 204, body: "" },
    "GET /api/v1/files/demo/characters/b.png": { status: 204, body: "" },
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "completed", stage: "assets", action: "assets" } },
    "POST /api/v1/projects/demo/episodes/1/production/videos/preview": { status: 200, body: readyQuote },
  });
  await page.route("**/api/v1/tasks?**", (route) => route.fulfill({ json: { items: [retried ? { ...imageTask, task_id: "new-image", status: "running", finished_at: null, updated_at: "2026-01-01T08:00:01" } : imageTask], total: 1 } }));
  await page.route("**/api/v1/projects/demo/generate/character/**", (route) => {
    retried += 1;
    return route.fulfill({ json: { success: true, task_id: "new-image", deduped: false } });
  });
  await page.goto("/app/make/demo?step=assets");
  const first = page.getByRole("article", { name: "阿明", exact: true });
  const second = page.getByRole("article", { name: "阿红", exact: true });
  await expect(first.getByRole("status").first()).toContainText("图片已完成");
  expect(retried).toBe(0);
  await first.getByRole("button", { name: /确认提示词并(重新生成图片|生成图片)/, exact: true }).click();
  await expect(first.getByRole("status").first()).toContainText("正在生成图片");
  await expect(first.getByRole("button", { name: /确认提示词并(重新生成图片|生成图片)/, exact: true })).toBeDisabled();
  await expect(second.getByRole("button", { name: /确认提示词并(重新生成图片|生成图片)/, exact: true })).toBeEnabled();
  await expect(first.getByRole("img")).toBeVisible();
  expect(retried).toBe(1);
});

test("failed image stays visible across reload and retries only when clicked", async ({ page, api }) => {
  const calls: string[] = [];
  page.on("request", (request) => { if (request.method() === "POST" && /\/generate\//.test(request.url())) calls.push(new URL(request.url()).pathname); });
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo": { status: 200, body: assetsProject },
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "failed", stage: "assets", action: "assets" } },
    "GET /api/v1/tasks?page_size=200&project_name=demo": { status: 200, body: { items: [{ ...planTask, task_type: "character", media_type: "image", resource_id: "阿明", status: "failed", error_message: "供应商生成失败" }], total: 1 } },
    "POST /api/v1/projects/demo/episodes/1/production/videos/preview": { status: 200, body: { ...readyQuote, submittable: false } },
    "POST /api/v1/projects/demo/generate/character/%E9%98%BF%E6%98%8E?episode=1": { status: 200, body: { success: true, task_id: "retry-image", deduped: false } },
  });
  await page.goto("/app/make/demo?step=assets");
  await page.reload();
  const card = page.getByRole("article", { name: "阿明", exact: true });
  await expect(card.getByRole("status").first()).toContainText("生成未完成");
  await expect(card.getByRole("alert")).toHaveText("供应商生成失败");
  expect(calls).toEqual([]);
  await card.getByRole("button", { name: /确认提示词并(重新生成图片|生成图片)/, exact: true }).click();
  await expect.poll(() => calls).toEqual(["/api/v1/projects/demo/generate/character/%E9%98%BF%E6%98%8E"]);
});


test("video progress follows existing tasks, refreshes without resubmission and shows merging", async ({ page, api }) => {
  const posts: string[] = [];
  page.on("request", (request) => { if (request.method() === "POST" && !request.url().endsWith("/prompt-preview")) posts.push(request.url()); });
  const videoUnits = [unit, { ...unit, unit_id: "E1U2" }, { ...unit, unit_id: "E1U3", generated_assets: { video_clip: "reference_videos/E1U1.mp4" } }, { ...unit, unit_id: "E1U4" }];
  const videoTasks = ["running", "queued", "running", "failed"].map((status, i) => ({ ...planTask, task_id: `video-${i}`, task_type: "reference_video", media_type: "video", resource_id: `E1U${i + 1}`, status, started_at: status === "queued" ? null : planTask.started_at, finished_at: status === "failed" ? "2026-01-01T08:00:00" : null, error_message: status === "failed" ? "供应商生成失败" : null }));
  api.override({ ...productionBase,
    ...Object.fromEntries(videoUnits.map((item) => [`POST /api/v1/projects/demo/reference-videos/episodes/1/units/${item.unit_id}/prompt-preview`, { status: 200, body: { text: "完整视频框架", creative_text: "完整视频框架", declarations: "", references: [], warnings: [], unavailable: null } }])),
    "GET /api/v1/projects/demo/reference-videos/episodes/1/units": { status: 200, body: { units: videoUnits, unit_capabilities: {} } },
    "GET /api/v1/tasks?page_size=200&project_name=demo": { status: 200, body: { items: videoTasks, total: 4 } },
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, action: "videos", status: "active", stage: "videos", batch_id: "existing-videos", members: videoTasks.map((task) => ({ unit_id: task.resource_id, task_id: task.task_id, status: task.status })) } },
  });
  await page.goto("/app/make/demo?step=videos");
  const progress = page.getByRole("region", { name: "视频生成进度" });
  const cards = page.getByRole("article");
  await expect(progress.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
  await expect(progress).toContainText("生成中 2 · 排队 1 · 失败 1 · 待生成 0");
  await expect(cards.nth(0).getByRole("status")).toContainText("正在生成视频");
  await expect(cards.nth(0).getByRole("status")).toContainText("已运行");
  await expect(cards.nth(1).getByRole("status")).toContainText("排队中");
  await expect(cards.nth(1).getByRole("status")).toContainText("已等待");
  await expect(cards.nth(2).locator("video")).toHaveCount(1);
  await expect(cards.nth(2).getByRole("status")).toContainText("正在生成视频");
  await expect(cards.nth(3).getByRole("alert")).toHaveText("供应商生成失败");
  await expectReachableLayout(page);
  await expectAccessible(page);
  await page.reload();
  await expect(progress).toContainText("生成中 2 · 排队 1");
  // The batch endpoint can observe completion before the queue poll catches up.
  api.override({
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, action: "videos", status: "active", stage: "videos", members: videoTasks.map((task) => ({ unit_id: task.resource_id, task_id: task.task_id, status: "succeeded" })) } },
  });
  await expect(progress.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "4", { timeout: 10000 });
  await expect(cards.nth(0).getByRole("status")).toHaveText("视频已完成");
  const completedTasks = videoTasks.map((task) => ({ ...task, status: "succeeded", error_message: null, finished_at: "2026-01-01T08:00:00", updated_at: "2026-01-01T08:00:00" }));
  api.override({
    "GET /api/v1/tasks?page_size=200&project_name=demo": { status: 200, body: { items: completedTasks, total: 4 } },
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, action: "videos", status: "active", stage: "render", members: [{ unit_id: "render-final-cut", status: "running" }] } },
    "GET /api/v1/projects/demo/reference-videos/episodes/1/units": { status: 200, body: { units: videoUnits.map((item) => ({ ...item, generated_assets: { video_clip: "reference_videos/E1U1.mp4" } })), unit_capabilities: {} } },
  });
  await expect(progress.getByRole("status")).toHaveText("视频已就绪，正在合成成片…", { timeout: 10000 });
  await expect(cards.nth(3).getByRole("status")).toContainText("视频已完成", { timeout: 10000 });
  await expect(cards.nth(3).getByRole("alert")).toHaveCount(0);
  expect(posts).toEqual([]);
});

const multiProject = { ...projectFixture, project: { ...projectFixture.project, content_mode: "drama", generation_mode: "reference_video", episodes: [
  { episode: 1, title: "初遇", script_file: "scripts/episode_1.json", script_status: "segmented" },
  { episode: 2, title: "重逢", script_file: "scripts/episode_2.json", script_status: "none" },
] } };
const multiBase = {
  ...productionBase,
  "GET /api/v1/projects/demo": { status: 200, body: multiProject },
  "GET /api/v1/projects/demo/episodes/2/production": { status: 200, body: { episode: 2, status: "idle", stage: null } },
  "GET /api/v1/projects/demo/episodes/2/script-review": { status: 200, body: { episode: 2, status: "empty", content: null } },
  "GET /api/v1/projects/demo/workflow-status?episode=2": { status: 200, body: { ...workflowFixture, target: { episode: 2, source: "source/episode_2.txt" }, content: { ...workflowFixture.content, episode_source: "present", formal_script: "absent" } } },
  "GET /api/v1/projects/demo/source/episode_2.txt": { status: 200, body: "第二集：重逢，人物再次相见。" },
};

test("legacy multi-episode links open episode selection without generation", async ({ page, api }) => {
  const writes: string[] = [];
  page.on("request", (request) => { if (request.method() !== "GET" && request.url().includes("/api/")) writes.push(request.url()); });
  api.override(multiBase);
  await page.goto("/app/make/demo?step=assets");
  await expect(page).toHaveURL(/\/app\/projects\/demo$/);
  await expect(page.getByRole("heading", { name: "第 1 集 · 初遇" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "第 2 集 · 重逢" })).toBeVisible();
  const links = page.getByRole("link", { name: "继续制作", exact: true });
  await expect(links.nth(1)).toHaveAttribute("href", "/app/make/demo?step=script&episode=2");
  await waitForEntrance(page.locator("main"));
  await expectAccessible(page);
  await expectReachableLayout(page);
  expect(writes).toEqual([]);
});

test("selected episode source survives reload and all step links preserve identity", async ({ page, api }) => {
  api.override(multiBase);
  await page.goto("/app/make/demo?step=script&episode=2");
  await expect(page.getByRole("textbox", { name: "单集剧本", exact: true })).toHaveValue("第二集：重逢，人物再次相见。");
  await expect(page.getByRole("navigation", { name: "制作步骤" }).getByRole("link").nth(4)).toHaveAttribute("href", "/app/make/demo?step=output&episode=2");
  await page.reload();
  await expect(page.getByRole("textbox", { name: "单集剧本", exact: true })).toHaveValue("第二集：重逢，人物再次相见。");
  await page.getByRole("link", { name: "项目管理", exact: true }).click();
  await page.getByRole("link", { name: "继续制作", exact: true }).nth(0).click();
  await expect(page).toHaveURL(/episode=1$/);
  await expect(page.getByText("第二集：重逢，人物再次相见。", { exact: true })).toHaveCount(0);
  await page.getByRole("link", { name: "项目管理", exact: true }).click();
  await page.getByRole("link", { name: "继续制作", exact: true }).nth(1).click();
  await expect(page.getByRole("textbox", { name: "单集剧本", exact: true })).toHaveValue("第二集：重逢，人物再次相见。");
});

test("episode create rename reorder archive and restore use stable episode identities", async ({ page, api }) => {
  let project = structuredClone(multiProject);
  const calls: string[] = [];
  api.override(multiBase);
  await page.route("**/api/v1/projects/demo", (route) => route.fulfill({ json: project }));
  await page.route("**/api/v1/projects/demo/episodes", async (route) => {
    calls.push("create");
    project.project.episodes.push({ episode: 3, title: (route.request().postDataJSON() as { title: string }).title, script_file: "scripts/episode_3.json", script_status: "none" });
    await route.fulfill({ json: { episode: 3 } });
  });
  await page.route("**/api/v1/projects/demo/episodes/2", async (route) => {
    calls.push("rename:2"); project.project.episodes.find((e) => e.episode === 2)!.title = (route.request().postDataJSON() as { title: string }).title;
    await route.fulfill({ json: { success: true } });
  });
  await page.route("**/api/v1/projects/demo/episodes/2/move", async (route) => {
    calls.push(`move:2:${(route.request().postDataJSON() as { after: number | null }).after}`);
    project.project.episodes.reverse(); await route.fulfill({ json: { success: true } });
  });
  await page.route("**/api/v1/projects/demo/episodes/2/production/archive", async (route) => {
    const archived = (route.request().postDataJSON() as { archived: boolean }).archived;
    calls.push(`archive:2:${archived}`);
    project = { ...project, project: { ...project.project, production_management: { archived_episodes: archived ? [2] : [] } } } as typeof project;
    await route.fulfill({ json: { success: true } });
  });
  api.override({ "GET /api/v1/projects/demo/episodes/3/production": { status: 200, body: { episode: 3, status: "idle", stage: null } } });
  await page.goto("/app/projects/demo");
  const second = page.getByRole("article").nth(1);
  await second.getByRole("button", { name: "改名", exact: true }).click();
  await page.getByRole("textbox", { name: "名称", exact: true }).fill("第二集新标题");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByRole("heading", { name: "第 2 集 · 第二集新标题" })).toBeVisible();
  await page.getByRole("button", { name: "上移 第二集新标题", exact: true }).click();
  await expect(page.getByRole("article").first().getByRole("heading")).toHaveText("第 1 集 · 第二集新标题");
  await page.getByRole("article").first().getByRole("button", { name: "归档", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(1);
  await page.getByRole("button", { name: "查看归档", exact: true }).click();
  await page.getByRole("button", { name: "恢复", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(0);
  await page.getByRole("button", { name: "返回当前列表", exact: true }).click();
  await page.getByRole("button", { name: "新建单集", exact: true }).click();
  await page.getByRole("textbox", { name: "名称", exact: true }).fill("第三集");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(3);
  expect(calls).toEqual(["rename:2", "move:2:null", "archive:2:true", "archive:2:false", "create"]);
});

test("project management creates an empty project without model calls and restores archives", async ({ page, api }) => {
  let archived = false;
  const writes: string[] = [];
  api.override({ ...multiBase, "GET /api/v1/files/demo/characters/阿明.png": { status: 204, body: "" } });
  await page.route("**/api/v1/projects", async (route) => {
    if (route.request().method() === "POST") {
      writes.push("create-project");
      await route.fulfill({ json: { name: "demo", success: true } });
    } else await route.fulfill({ json: { projects: [{ name: "demo", title: "短剧项目", episode_count: 2, thumbnail: "/api/v1/files/demo/characters/阿明.png", last_activity_at: null, production_management: { archived } }] } });
  });
  await page.route("**/api/v1/projects/demo/production/archive", async (route) => {
    archived = (route.request().postDataJSON() as { archived: boolean }).archived; writes.push(`project-archive:${archived}`);
    await route.fulfill({ json: { success: true } });
  });
  page.on("request", (request) => { if (/script-plan|production\/(assets|videos|render)$/.test(request.url()) && request.method() === "POST") writes.push("unexpected-generation"); });
  await page.goto("/app/projects");
  await expect(page.getByText("共 2 集", { exact: true })).toBeVisible();
  await expect(page.getByRole("article").locator("img")).toHaveAttribute("src", "/api/v1/files/demo/characters/阿明.png");
  await waitForEntrance(page.locator("main"));
  await expectAccessible(page);
  await expectReachableLayout(page);
  await page.getByRole("button", { name: "归档", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(0);
  await page.getByRole("button", { name: "查看归档", exact: true }).click();
  await page.getByRole("button", { name: "恢复", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(0);
  await page.getByRole("button", { name: "返回当前列表", exact: true }).click();
  await page.getByRole("button", { name: "新建项目", exact: true }).click();
  await page.getByRole("textbox", { name: "作品名", exact: true }).fill("空白项目");
  await page.getByRole("button", { name: "创建项目", exact: true }).click();
  await expect(page).toHaveURL(/\/app\/projects\/demo$/);
  expect(writes).toEqual(["project-archive:true", "project-archive:false", "create-project"]);
});

test("a delayed previous episode response cannot replace the newly selected episode", async ({ page, api }) => {
  api.override(multiBase);
  let release!: () => void;
  let arrived!: () => void;
  const delayed = new Promise<void>((resolve) => { release = resolve; });
  const started = new Promise<void>((resolve) => { arrived = resolve; });
  await page.route("**/api/v1/projects/demo/episodes/1/script-review", async (route) => {
    arrived(); await delayed;
    await route.fulfill({ json: { episode: 1, status: "pending_review", content: { unexpected_previous_episode: true } } });
  });
  await page.goto("/app/make/demo?step=script&episode=1");
  await started;
  await page.getByRole("link", { name: "项目管理", exact: true }).click();
  await page.getByRole("link", { name: "继续制作", exact: true }).nth(1).click();
  await expect(page.getByRole("textbox", { name: "单集剧本", exact: true })).toHaveValue("第二集：重逢，人物再次相见。");
  release();
  await expect(page).toHaveURL(/episode=2$/);
  await expect(page.getByRole("textbox", { name: "单集剧本", exact: true })).toHaveValue("第二集：重逢，人物再次相见。");
  await expect(page.getByRole("link", { name: "下载 MP4", exact: true })).toHaveCount(0);
});


test("cost is read-only on output and includes rerolls within the known subtotal", async ({ page, api }) => {
  const posts: string[] = [];
  page.on("request", (r) => { if (r.method() === "POST") posts.push(r.url()); });
  const category = { call_count: 3, known_call_count: 2, amounts: { CNY: 4 }, unpriced_count: 1, pending_count: 0, reroll_count: 1, reroll_amounts: { CNY: 1 }, unidentified_target_count: 0 };
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo/production/costs": { status: 200, body: { episodes: [{ ...category, episode: 1, categories: { image: category } }], unassigned: { call_count: 0, categories: {} } } },
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "completed", stage: "render", action: "videos", output: { status: "current", artifact_path: "renders/final.mp4", version: 2 } } },
  });
  await page.goto("/app/make/demo?step=output&episode=1");
  const costs = page.getByRole("region", { name: "累计制作成本（按调用记录核算）" });
  await expect(costs.locator(":scope > p").getByText("已知小计 · CNY 4.00", { exact: true })).toBeVisible();
  await costs.getByText("展开调用明细", { exact: true }).click();
  await expect(costs.getByText("其中重做/重试 1 次 · CNY 1.00", { exact: true })).toBeVisible();
  await expect(costs.getByText("1 次缺少价格，未计入金额", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "刷新成本", exact: true }).click();
  expect(posts).toEqual([]);
  await expectReachableLayout(page);
  await expectAccessible(page);
  await page.getByRole("link", { name: "4 视频", exact: true }).click();
  await expect(costs).toHaveCount(0);
  await expect(page.getByRole("button", { name: "查看报价", exact: true })).toHaveCount(0);
});


test("DOCX extraction is editable and a failed save retries the same target", async ({ page, api }) => {
  let createdProjects = 0, createdEpisodes = 0, saves = 0;
  const bodies: string[] = [];
  api.override({ ...productionBase,
    "POST /api/v1/production/source-preview": { status: 200, body: { text: "小明：你好。", filename: "episode.docx" } },
    "POST /api/v1/projects/demo/episodes/1/script-plan": { status: 200, body: { batch: { members: [] } } },
  });
  await page.route("**/api/v1/projects", async (route) => { if (route.request().method() === "POST") { createdProjects++; await route.fulfill({ json: { name: "demo" } }); } else await route.fallback(); });
  await page.route("**/api/v1/projects/demo/episodes", async (route) => { createdEpisodes++; await route.fulfill({ json: { episode: 1 } }); });
  await page.route("**/api/v1/projects/demo/episodes/1/production/source", async (route) => { bodies.push(route.request().postData() || ""); saves++; await route.fulfill({ status: saves === 1 ? 500 : 200, json: saves === 1 ? { detail: "保存失败，请重试" } : { needs_confirmation: false } }); });
  await page.goto("/app/make");
  await page.getByRole("textbox", { name: "作品名" }).fill("Word 剧本");
  await page.locator("#script-upload").setInputFiles({ name: "episode.docx", mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", buffer: Buffer.from("served by actual extraction contract fixture") });
  await expect(page.getByRole("textbox", { name: "单集剧本" })).toHaveValue("小明：你好。");
  await page.getByRole("textbox", { name: "单集剧本" }).fill("小明：修改后的对白。");
  await page.getByRole("button", { name: "开始拆解", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("保存失败");
  await expect(page.getByRole("textbox", { name: "单集剧本" })).toHaveValue("小明：修改后的对白。");
  await page.getByRole("button", { name: "开始拆解", exact: true }).click();
  await expect(page).toHaveURL(/step=breakdown&episode=1$/);
  expect([createdProjects, createdEpisodes, saves]).toEqual([1, 1, 2]);
  expect(bodies[1]).toContain("修改后的对白");
});

test("manual clip and caption edits persist and export locally", async ({ page, api }) => {
  const recordedEdit = recorded<import("../../src/types/production.js").ProductionEdit>("project-demo-production-edit.json");
  let edit = { ...recordedEdit, media: { E1U1: { version: 1, duration_us: 2000000 }, E1U2: { version: 1, duration_us: 2000000 } }, subtitle_drafts: {}, issues: [] };
  let savedOperations: Record<string, unknown>[] = [];
  const posts: string[] = [];
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "completed", stage: "render", timeline_id: edit.timeline_id, output: { status: "current", artifact_path: "renders/final.mp4", version: 2 } } },
    "POST /api/v1/projects/demo/episodes/1/production/render": { status: 200, body: { episode: 1, status: "active", stage: "render" } },
  });
  await page.route("**/api/v1/projects/demo/episodes/1/production/edit", async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON() as { base_revision: number; operations: Record<string, unknown>[] };
      expect(body.base_revision).toBe(edit.revision);
      savedOperations = body.operations;
      const clips = structuredClone(edit.clips);
      for (const op of body.operations) {
        const clip = clips.find((c) => c.id === op.clip)!;
        if (op.op === "set_subtitles") clip.subtitles = op.subtitles as typeof clip.subtitles;
        if (op.op === "set_volume") clip.source_volume = Number(op.volume);
        if (op.op === "set_trim") { const trim = op.trim as { source_in: number; source_out: number }; clip.trim = { in_us: trim.source_in * 1e6, out_us: trim.source_out * 1e6, basis_version: 1 }; }
        if (op.op === "set_included") clip.included = Boolean(op.included);
        if (op.op === "move") { clips.splice(clips.indexOf(clip), 1); clips.splice(op.after ? clips.findIndex((c) => c.id === op.after) + 1 : 0, 0, clip); }
      }
      edit = { ...edit, revision: edit.revision + 1, clips };
    }
    await route.fulfill({ json: edit });
  });
  page.on("request", (request) => { if (request.method() === "POST" && !request.url().endsWith("/prompt-preview")) posts.push(new URL(request.url()).pathname); });
  await page.goto("/app/make/demo?step=output&episode=1");
  await page.getByRole("button", { name: "编辑成片", exact: true }).click();
  await page.getByRole("button", { name: "增加字幕", exact: true }).click();
  await page.getByRole("textbox", { name: "字幕 1", exact: true }).fill("人工修改的字幕");
  await page.getByRole("spinbutton", { name: "开始秒数 1", exact: true }).fill("0.2");
  await page.getByRole("spinbutton", { name: "结束秒数 1", exact: true }).fill("0.8");
  await page.getByRole("combobox", { name: "位置 1", exact: true }).selectOption("top");
  await page.getByRole("button", { name: "静音", exact: true }).click();
  await page.getByRole("spinbutton", { name: "出点（源视频秒）", exact: true }).fill("1");
  await page.getByRole("button", { name: "下移", exact: true }).first().click();
  await page.getByRole("button", { name: "保存剪辑草稿", exact: true }).click();
  await expect.poll(() => edit.revision).toBe(recordedEdit.revision + 1);
  expect(savedOperations.map((op) => op.op)).toEqual(expect.arrayContaining(["move", "set_trim", "set_volume", "set_subtitles"]));
  await page.reload();
  await page.getByRole("button", { name: "编辑成片", exact: true }).click();
  await page.getByRole("button", { name: "E1U1", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "字幕 1", exact: true })).toHaveValue("人工修改的字幕");
  await expect(page.getByRole("spinbutton", { name: "出点（源视频秒）", exact: true })).toHaveValue("1");
  await expectReachableLayout(page);
  await waitForEntrance(page.getByRole("main"));
  await expectAccessible(page);
  await page.getByRole("textbox", { name: "字幕 1", exact: true }).fill("直接保存并导出的字幕");
  await page.getByRole("button", { name: "保存并重新导出", exact: true }).click();
  await expect.poll(() => posts.includes("/api/v1/projects/demo/episodes/1/production/render")).toBe(true);
  expect(posts.filter((path) => /\/production\/(videos|assets)$/.test(path))).toEqual([]);
});


test("breakdown adds a fully prompted asset and removes visual references without losing dialogue", async ({ page, api }) => {
  let content = { units: [{ ...unit, text: "@[阿明]走入画面。@[阿明]：{你好。}", source_text: "阿明：你好。" }], new_assets: [{ type: "character", name: "阿明", description: "青年", decision: "register", reason: "主角", aliases: [], target: "", asset_name: "", generation_prompt: "" }] };
  api.override({ ...productionBase, "GET /api/v1/projects/demo": { status: 200, body: { ...projectFixture, project: { ...projectFixture.project, generation_mode: "reference_video", characters: {}, scenes: {}, props: {} } } } });
  await page.route("**/api/v1/projects/demo/episodes/1/script-review**", async (route) => {
    if (route.request().method() === "PUT") content = route.request().postDataJSON() as typeof content;
    await route.fulfill({ json: { episode: 1, status: "pending_review", fingerprint: "draft", content, quarantine: null } });
  });
  await page.goto("/app/make/demo?step=breakdown&episode=1");
  await page.getByRole("button", { name: "从本集移除", exact: true }).click();
  await expect(page.getByText(/引用位置：E1U1/)).toBeVisible();
  await page.getByRole("button", { name: "从本集移除", exact: true }).last().click();
  await expect(page.getByRole("textbox", { name: "单元 1 内容", exact: true })).toHaveValue("阿明走入画面。@[阿明]：{你好。}");
  await page.getByRole("button", { name: "增加资产", exact: true }).last().click();
  await page.getByRole("textbox", { name: "名称（登记后引用自动更新）", exact: true }).fill("信封");
  await expect(page.getByRole("textbox", { name: /资产描述/ })).toHaveCount(0);
  const prompt = page.getByRole("textbox", { name: "完整生图提示词", exact: true });
  await expect(prompt).toBeEnabled();
  await prompt.fill("完整人工框架：信封，白底三视图，油画风，不要水印。");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => content.new_assets[0]?.generation_prompt).toBe("完整人工框架：信封，白底三视图，油画风，不要水印。");
  expect(content.units[0].text).toContain("@[新增资产1]");
  expect(content.units[0].text).toContain("{你好。}");
  expect(content.new_assets[0].asset_name).toBe("信封");
  await expectReachableLayout(page);
});

test("video prompt edits save the visible unit text", async ({ page, api }) => {
  const preview = { text: "@图片1 是<阿明>的参考图。\n\n原始完整框架", declarations: "@图片1 是<阿明>的参考图。", creative_text: "原始完整框架", references: [], warnings: [], unavailable: null, is_text_form: true, parameters: { model: "fixture" } };
  let patch: Record<string, unknown> | null = null;
  api.override({ ...productionBase, "POST /api/v1/projects/demo/reference-videos/episodes/1/units/E1U1/prompt-preview": { status: 200, body: preview } });
  await page.route("**/api/v1/projects/demo/reference-videos/episodes/1/units/E1U1", async (route) => {
    patch = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({ json: { unit: { ...unit, text: patch.prompt }, unit_capability: {} } });
  });
  await page.goto("/app/make/demo?step=videos&episode=1");
  const creative = page.locator("#video-prompt-E1U1");
  await creative.fill("@[阿明]完整自定义正文");
  await expect(page.getByText("最终视频提示词（与实发请求一致）", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => patch?.prompt).toBe("@[阿明]完整自定义正文");
  expect((patch as Record<string, unknown> | null)?.generation_prompt).toBe("");
});

test("manual video text survives body edits and reload until explicitly restored", async ({ page, api }) => {
  let savedUnit = { ...unit, text: "@[阿明]抬头。@[阿明]{你好}", generation_prompt: "人工近景、风格和限制，保留这段" };
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && !request.url().endsWith("/prompt-preview")) writes.push(new URL(request.url()).pathname);
  });
  api.override(productionBase);
  await page.route("**/api/v1/projects/demo/reference-videos/episodes/1/units", (route) => route.fulfill({ json: { units: [savedUnit], unit_capabilities: {} } }));
  await page.route("**/api/v1/projects/demo/reference-videos/episodes/1/units/E1U1", async (route) => {
    const patch = route.request().postDataJSON() as { prompt: string; generation_prompt: string };
    savedUnit = { ...savedUnit, text: patch.prompt, generation_prompt: patch.generation_prompt };
    await route.fulfill({ json: { unit: savedUnit, unit_capability: {} } });
  });
  await page.goto("/app/make/demo?step=videos&episode=1");
  const manual = savedUnit.generation_prompt;
  await expect(page.locator("#video-prompt-E1U1")).toHaveValue(manual);
  await page.locator("#video-prompt-E1U1").fill("更新后的人工创作段");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => savedUnit.generation_prompt).toBe("更新后的人工创作段");
  expect(savedUnit.text).toBe("@[阿明]抬头。@[阿明]{你好}");
  await page.reload();
  await expect(page.locator("#video-prompt-E1U1")).toHaveValue("更新后的人工创作段");
  await page.getByRole("button", { name: "恢复默认框架", exact: true }).click();
  await expect(page.locator("#video-prompt-E1U1")).toHaveValue("@[阿明]抬头。@[阿明]{你好}");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => savedUnit.generation_prompt).toBe("");
  expect(savedUnit.text).toBe("@[阿明]抬头。@[阿明]{你好}");
  expect(writes).toEqual([]);
  await expectReachableLayout(page);
  await expectAccessible(page);
});

test("existing breakdown assets persist full prompts and rename only after dirty references are saved", async ({ page, api }) => {
  let name = "阿明";
  let asset = { description: "青年", generation_prompt: "" };
  let content = { units: [{ ...unit, text: "@[阿明]：{你好。}", source_text: "阿明：你好。" }], new_assets: [] };
  api.override({ ...productionBase, "POST /api/v1/projects/demo/production/asset-prompt": { status: 200, body: { text: "默认完整框架", aspect_ratio: "16:9", references: [], system_message: null, affected_episodes: [1, 2] } } });
  await page.route("**/api/v1/projects/demo", (route) => route.fulfill({ json: { ...projectFixture, project: { ...projectFixture.project, generation_mode: "reference_video", characters: { [name]: asset }, scenes: {}, props: {} } } }));
  await page.route("**/api/v1/projects/demo/episodes/1/script-review**", async (route) => {
    if (route.request().method() === "PUT") content = route.request().postDataJSON() as typeof content;
    await route.fulfill({ json: { episode: 1, status: "pending_review", fingerprint: JSON.stringify(content), content, quarantine: null } });
  });
  await page.route("**/api/v1/projects/demo/characters/*", async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    asset = { ...asset, ...route.request().postDataJSON() as Partial<typeof asset> };
    await route.fulfill({ json: { success: true } });
  });
  await page.route("**/api/v1/projects/demo/characters/*/rename", async (route) => {
    const body = route.request().postDataJSON() as { new_name: string; dry_run: boolean };
    const old = name;
    if (!body.dry_run) {
      name = body.new_name;
      content = { ...content, units: content.units.map((u) => ({ ...u, text: u.text.replaceAll(`@[${old}]`, `@[${name}]`) })) };
    }
    await route.fulfill({ json: { success: true, dry_run: body.dry_run, old_name: old, new_name: body.new_name, episodes: 2, references: 3, files: 0 } });
  });
  await page.goto("/app/make/demo?step=breakdown&episode=1");
  const card = page.getByRole("article", { name: "阿明", exact: true });
  await card.getByText("修改提示词", { exact: true }).click();
  await expect(card.getByText(/第 1、2 集/)).toBeVisible();
  await expect(card.getByRole("textbox", { name: "资产描述", exact: true })).toHaveCount(0);
  await card.getByRole("textbox", { name: "完整生图提示词", exact: true }).fill("人工完整框架，白衣青年三视图，不要水印");
  await expect(card.getByRole("button", { name: "重命名", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "确认拆解，审查资产提示词", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "有未保存的修改" })).toBeVisible();
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => asset.generation_prompt).toBe("人工完整框架，白衣青年三视图，不要水印");
  const shot = page.getByRole("textbox", { name: "单元 1 内容", exact: true });
  await shot.fill("@[阿明]走入房间。@[阿明]：{你好。}");
  await expect(card.getByRole("button", { name: "重命名", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "保存", exact: true }).last().click();
  await card.getByRole("button", { name: "重命名", exact: true }).click();
  await card.getByRole("textbox", { name: "重命名", exact: true }).fill("小明");
  await card.getByRole("textbox", { name: "重命名", exact: true }).press("Enter");
  const dialog = page.getByRole("alertdialog");
  await expect(dialog.getByText(/2 集共 3 处引用/)).toBeVisible();
  await dialog.getByRole("button", { name: "重命名", exact: true }).click();
  await expect(page.getByRole("article", { name: "小明", exact: true })).toBeVisible();
  await expect(shot).toHaveValue("@[小明]走入房间。@[小明]：{你好。}");
  await page.reload();
  const saved = page.getByRole("article", { name: "小明", exact: true });
  await saved.getByText("修改提示词", { exact: true }).click();
  await expect(saved.getByRole("textbox", { name: "资产描述", exact: true })).toHaveCount(0);
  expect(asset.description).toBe("青年");
  await expect(saved.getByRole("textbox", { name: "完整生图提示词", exact: true })).toHaveValue("人工完整框架，白衣青年三视图，不要水印");
  await expectReachableLayout(page);
  await expectAccessible(page);
});


test("blank episode opens without reading a missing source and preserves the draft through first save", async ({ page, api }) => {
  const text = "新单集：小明说你好。";
  let saved = false;
  const reads: string[] = [];
  api.override(productionBase);
  await page.addInitScript((draft) => sessionStorage.setItem("xm:script-draft:demo:1", draft), text);
  await page.route("**/api/v1/projects/demo/workflow-status**", (route) => route.fulfill({ json: { ...workflowFixture, target: { ...workflowFixture.target, source: "source/episode_1.txt" }, content: { ...workflowFixture.content, formal_script: "absent", episode_source: saved ? "present" : "absent" } } }));
  await page.route("**/api/v1/projects/demo/source/episode_1.txt", async (route) => {
    reads.push(route.request().url());
    await route.fulfill({ status: saved ? 200 : 404, contentType: "text/plain", body: saved ? text : "文件不存在" });
  });
  await page.route("**/api/v1/projects/demo/episodes/1/production/source", async (route) => {
    const body = route.request().postData() || "";
    expect(body).toContain('name="expected_text"');
    expect(body).toContain(text);
    saved = true;
    await route.fulfill({ json: { success: true, applied: true, needs_confirmation: false, affected_episodes: [] } });
  });
  await page.goto("/app/make/demo?step=script&episode=1");
  await expect(page.getByRole("textbox", { name: "单集剧本", exact: true })).toHaveValue(text);
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(reads).toEqual([]);
  await expect(page.getByLabel("上传 DOCX / TXT / Markdown")).toHaveAttribute("accept", /\.docx/);
  await page.getByRole("button", { name: "保存剧本", exact: true }).click();
  await expect.poll(() => saved).toBe(true);
  await page.reload();
  await expect(page.getByRole("textbox", { name: "单集剧本", exact: true })).toHaveValue(text);
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(reads.length).toBeGreaterThan(0);
});

for (const entry of ["footer", "navigation"] as const) {
  test(`confirming assets via ${entry} writes prompts once and waits for explicit video generation`, async ({ page, api }) => {
    let pending = true;
    let authored = 0;
    let generated = 0;
    api.override({ ...productionBase,
      "GET /api/v1/files/demo/characters/a.png": { status: 204, body: "" },
      "GET /api/v1/projects/demo": { status: 200, body: { ...assetsProject, project: { ...assetsProject.project, characters: { 阿明: { description: "青年", character_sheet: "characters/a.png" } } } } },
      "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "idle", stage: null } },
    });
    await page.route("**/api/v1/tasks?**", (route) => route.fulfill({ json: { items: authored ? [{ ...planTask, task_id: "prompt-task", task_type: "text_episode_script", status: pending ? "running" : "succeeded" }] : [], total: authored ? 1 : 0 } }));
    await page.route("**/api/v1/projects/demo/reference-videos/episodes/1/units", (route) => route.fulfill({ json: { units: [{ ...unit, pending_authoring: pending, text: pending ? "@[阿明]：{你好。}" : "@[阿明]面向镜头微笑。@[阿明]：{你好。}" }], unit_capabilities: {} } }));
    await page.route("**/api/v1/projects/demo/episodes/1/prompt-authoring", async (route) => {
      authored++;
      expect(route.request().postDataJSON()).toEqual({});
      await route.fulfill({ json: { batch: { batch_id: "text-only", members: [{ task_id: "prompt-task" }] } } });
    });
    await page.route("**/api/v1/projects/demo/episodes/1/production/videos", async (route) => {
      generated++;
      await route.fulfill({ json: { episode: 1, status: "active", stage: "videos", action: "videos" } });
    });
    await page.goto("/app/make/demo?step=assets&episode=1");
    await expect(page.getByRole("button", { name: "确认资产并编写视频提示词", exact: true })).toBeEnabled();
    if (entry === "footer") await page.getByRole("button", { name: "确认资产并编写视频提示词", exact: true }).click();
    else await page.getByRole("navigation").getByRole("link", { name: "4 视频", exact: true }).click();
    await expect(page).toHaveURL(/step=videos&episode=1$/);
    await expect(page.getByRole("status").filter({ hasText: "正在编写视频提示词" })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "视频提示词", exact: true })).toHaveCount(0);
    expect([authored, generated]).toEqual([1, 0]);
    await expect(page.getByRole("button", { name: "确认生成视频", exact: true })).toBeDisabled();
    await page.reload();
    await expect(page.getByRole("status").filter({ hasText: "正在编写视频提示词" })).toBeVisible();
    expect([authored, generated]).toEqual([1, 0]);
    pending = false;
    await expect(page.getByRole("button", { name: "确认生成视频", exact: true })).toBeEnabled();
    await expect(page.getByRole("textbox", { name: "视频提示词", exact: true })).toHaveValue("@[阿明]面向镜头微笑。@[阿明]：{你好。}");
    await expect(page.getByRole("button", { name: "编写视频提示词", exact: true })).toHaveCount(0);
    expect([authored, generated]).toEqual([1, 0]);
    await page.reload();
    await expect(page.getByRole("textbox", { name: "视频提示词", exact: true })).toHaveValue("@[阿明]面向镜头微笑。@[阿明]：{你好。}");
    await page.getByRole("navigation").getByRole("link", { name: "3 资产", exact: true }).click();
    await page.getByRole("button", { name: "进入视频提示词审查", exact: true }).click();
    await expect(page.getByRole("button", { name: "确认生成视频", exact: true })).toBeEnabled();
    expect([authored, generated]).toEqual([1, 0]);
    await page.getByRole("button", { name: "确认生成视频", exact: true }).click();
    await expect.poll(() => generated).toBe(1);
    expect(authored).toBe(1);
  });
}


const promptAuthoringBase = {
  ...productionBase,
  "GET /api/v1/files/demo/characters/a.png": { status: 204, body: "" },
  "GET /api/v1/projects/demo": { status: 200, body: { ...assetsProject, project: { ...assetsProject.project, characters: { 阿明: { description: "青年", character_sheet: "characters/a.png" } } } } },
  "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "idle", stage: null } },
};

test("failed prompt writing survives refresh and only retries on request", async ({ page, api }) => {
  let authored = 0;
  let pending = true;
  api.override(promptAuthoringBase);
  await page.route("**/api/v1/tasks?**", (route) => route.fulfill({ json: { items: authored ? [{ ...planTask, task_id: `prompt-${authored}`, task_type: "text_episode_script", status: pending ? "failed" : "succeeded", error_message: pending ? "模型请求超时" : null }] : [], total: authored ? 1 : 0 } }));
  await page.route("**/api/v1/projects/demo/reference-videos/episodes/1/units", (route) => route.fulfill({ json: { units: [{ ...unit, pending_authoring: pending }], unit_capabilities: {} } }));
  await page.route("**/api/v1/projects/demo/episodes/1/prompt-authoring", async (route) => {
    authored++;
    if (authored > 1) pending = false;
    await route.fulfill({ json: { batch: { batch_id: `batch-${authored}`, members: [{ task_id: `prompt-${authored}` }] } } });
  });
  await page.goto("/app/make/demo?step=assets&episode=1");
  await page.getByRole("button", { name: "确认资产并编写视频提示词", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("模型请求超时");
  await page.reload();
  await expect(page.getByRole("alert")).toContainText("模型请求超时");
  expect(authored).toBe(1);
  await expect(page.getByRole("textbox", { name: "视频提示词", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "编写视频提示词", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "视频提示词", exact: true })).toHaveValue(unit.text);
  expect(authored).toBe(2);
});

test("unsaved asset prompts block confirming assets and writing video prompts", async ({ page, api }) => {
  let authored = 0;
  api.override({ ...promptAuthoringBase,
    "GET /api/v1/projects/demo/reference-videos/episodes/1/units": { status: 200, body: { units: [{ ...unit, pending_authoring: true }], unit_capabilities: {} } },
  });
  await page.route("**/api/v1/projects/demo/episodes/1/prompt-authoring", async (route) => { authored++; await route.abort(); });
  await page.goto("/app/make/demo?step=assets&episode=1");
  await page.getByRole("textbox", { name: "完整生图提示词", exact: true }).fill("保留人物容貌，改为深色衣服");
  await page.getByRole("button", { name: "确认资产并编写视频提示词", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("未保存");
  await expect(page).toHaveURL(/step=assets&episode=1$/);
  await page.getByRole("navigation").getByRole("link", { name: "4 视频", exact: true }).click();
  await expect(page).toHaveURL(/step=assets&episode=1$/);
  expect(authored).toBe(0);
});

test("late prompt submission stays on the destination after leaving assets", async ({ page, api }) => {
  api.override({ ...promptAuthoringBase,
    "GET /api/v1/projects/demo/reference-videos/episodes/1/units": { status: 200, body: { units: [{ ...unit, pending_authoring: true }], unit_capabilities: {} } },
  });
  let release!: () => void;
  let arrived!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  const started = new Promise<void>((resolve) => { arrived = resolve; });
  await page.route("**/api/v1/projects/demo/episodes/1/prompt-authoring", async (route) => {
    arrived(); await pending;
    await route.fulfill({ json: { batch: { batch_id: "text-only", members: [] } } });
  });
  await page.goto("/app/make/demo?step=assets&episode=1");
  await page.getByRole("button", { name: "确认资产并编写视频提示词", exact: true }).click();
  await started;
  await page.getByRole("banner").getByRole("link", { name: "项目管理", exact: true }).click();
  await expect(page).toHaveURL(/\/app\/projects\/demo$/);
  release();
  await expect(page.getByText("任务已提交，将在后台继续处理。", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/app\/projects\/demo$/);
});

test("character angle selection saves the actual visual reference without changing dialogue", async ({ page, api }) => {
  const sheet = "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="900"><rect width="400" height="900" fill="red"/></svg>');
  const character = { description: "青年", character_sheet: sheet, derivatives: { "视角-正面特写": { description: "正面特写", character_sheet: sheet } } };
  let savedUnit = { ...unit, text: "@[阿明]抬头。@[阿明]{你好}", generation_prompt: "" };
  const patches: unknown[] = [];
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo": { status: 200, body: { ...assetsProject, project: { ...assetsProject.project, characters: { 阿明: character } } } },
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "idle", stage: null } },
  });
  await page.route("**/reference-videos/episodes/1/units", (route) => route.fulfill({ json: { units: [savedUnit], unit_capabilities: {} } }));
  await page.route("**/reference-videos/episodes/1/units/E1U1", async (route) => {
    const body = route.request().postDataJSON() as { prompt: string; generation_prompt: string }; patches.push(body);
    savedUnit = { ...savedUnit, text: body.prompt, generation_prompt: body.generation_prompt };
    await route.fulfill({ json: { unit: savedUnit, unit_capability: {} } });
  });
  await page.goto("/app/make/demo?step=videos&episode=1");
  await page.getByRole("combobox", { name: "阿明 · 人物参考角度" }).selectOption("视角-正面特写");
  const prompt = page.locator("#video-prompt-E1U1");
  await expect(prompt).toHaveValue("@[阿明/视角-正面特写]抬头。@[阿明]{你好}");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => patches.length).toBe(1);
  expect(patches[0]).toEqual({ prompt: savedUnit.text, generation_prompt: "" });
  await page.reload();
  await expect(page.getByRole("combobox", { name: "阿明 · 人物参考角度" })).toHaveValue("视角-正面特写");
  const generate = page.getByRole("button", { name: "确认生成视频", exact: true });
  await expect(generate).toBeEnabled();
  await expect(generate).toHaveCSS("opacity", "1");
  await expectReachableLayout(page);
  await expectAccessible(page);
});

test("character angle crop uploads only the selected pixels with no generation call", async ({ page, api }) => {
  const sheet = "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900"><rect width="400" height="900" fill="red"/><rect x="400" width="400" height="900" fill="green"/><rect x="800" width="400" height="900" fill="blue"/><rect x="1200" width="400" height="900" fill="white"/></svg>');
  let registered = false;
  let uploaded: Buffer | null = null;
  const paid: string[] = [];
  page.on("request", (r) => { if (r.method() === "POST" && /\/(generate|production\/(assets|videos))($|\/)/.test(new URL(r.url()).pathname)) paid.push(r.url()); });
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo": { status: 200, body: { ...assetsProject, project: { ...assetsProject.project, characters: { 阿明: { description: "青年", character_sheet: sheet } } } } },
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "idle", stage: null } },
  });
  await page.route("**/characters/*/derivatives", async (route) => {
    if (route.request().method() === "POST") { registered = true; await route.fulfill({ json: { success: true } }); }
    else await route.fulfill({ json: { success: true, derivatives: registered ? { "视角-正面特写": { description: "" } } : {} } });
  });
  await page.route("**/derivatives/*/upload", async (route) => {
    const body = route.request().postDataBuffer()!;
    const start = body.indexOf(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
    const end = body.indexOf(Buffer.from([73, 69, 78, 68]), start) + 8;
    uploaded = body.subarray(start, end);
    await route.fulfill({ json: { success: true, path: "characters/derivatives/阿明/视角-正面特写.png", version: 1, asset_fingerprints: {} } });
  });
  await page.goto("/app/make/demo?step=assets&episode=1");
  await page.getByRole("button", { name: "保存人物角度图", exact: true }).click();
  await page.getByRole("combobox", { name: "保存为哪个角度" }).selectOption("3");
  await expectReachableLayout(page);
  await expectAccessible(page);
  await page.getByRole("button", { name: "确认裁切并保存此角度" }).click();
  await expect.poll(() => !!uploaded).toBe(true);
  const encoded = uploaded!.toString("base64");
  const pixels = await page.evaluate(async (base64) => {
    const image = new Image(); image.src = `data:image/png;base64,${base64}`; await image.decode();
    const canvas = document.createElement("canvas"); canvas.width = image.width; canvas.height = image.height;
    const context = canvas.getContext("2d")!; context.drawImage(image, 0, 0);
    return { width: image.width, height: image.height, pixel: Array.from(context.getImageData(10, 10, 1, 1).data) };
  }, encoded);
  expect(pixels).toEqual({ width: 400, height: 900, pixel: [255, 255, 255, 255] });
  expect(registered).toBe(true);
  expect(paid).toEqual([]);
});

test("creative prompt typing retains focus and preview failures stay visible", async ({ page, api }) => {
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo/reference-videos/episodes/1/units": { status: 200, body: { units: [{ ...unit, text: "原始框架" }] } },
  });
  let calls = 0;
  let fail = false;
  await page.route("**/api/v1/projects/demo/reference-videos/episodes/1/units/E1U1/prompt-preview", async (route) => {
    calls++;
    const body = route.request().postDataJSON() as { generation_prompt?: string };
    await route.fulfill(fail ? { status: 500, json: { detail: "预览暂不可用" } } : { json: { text: body.generation_prompt || "原始框架", creative_text: "原始框架", declarations: "", references: [], unavailable: null } });
  });
  await page.goto("/app/make/demo?step=videos&episode=1");
  const creative = page.getByRole("textbox", { name: "视频提示词", exact: true });
  await expect(creative).toHaveValue("原始框架");
  await creative.focus();
  await creative.evaluate((node: HTMLTextAreaElement) => node.setSelectionRange(node.value.length, node.value.length));
  for (const key of ["a", "b", "c"]) {
    const before = calls;
    await creative.press(key);
    await expect.poll(() => calls).toBeGreaterThan(before);
    await expect(creative).toBeFocused();
  }
  await expect(creative).toHaveValue("原始框架abc");
  await creative.fill("原始框架");
  await expect(creative).toHaveValue("原始框架");
  fail = true;
  await creative.press("d");
  await expect(page.getByRole("alert").filter({ hasText: "预览暂不可用" })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认提示词并生成当前视频", exact: true })).toBeDisabled();
  await expect(creative).toBeVisible();
});

test("replanning confirms the listed overwrite revision and refreshes conflicts before retry", async ({ page, api }) => {
  const content = { units: [{ ...unit, source_text: "阿明说：你好" }], new_assets: [] };
  const writes: string[] = [];
  let revision = "sha256-v1:first";
  let confirmed = false;
  api.override({ ...productionBase });
  await page.route("**/api/v1/projects/demo/episodes/1/script-review**", async (route) => {
    if (route.request().method() === "POST") {
      writes.push((route.request().postDataJSON() as { overwrite_revision: string }).overwrite_revision);
      if (writes.length === 1) {
        revision = "sha256-v1:second";
        await route.fulfill({ status: 409, json: { detail: "正式脚本已变化" } }); return;
      }
      confirmed = true;
    }
    await route.fulfill({ json: { episode: 1, status: confirmed ? "confirmed" : "pending_review", fingerprint: "plan", content, quarantine: null,
      script_overwrite: { revision, text: revision.endsWith("first") ? "将覆盖原有 1 个视频" : "将覆盖更新后的 2 个视频", entries: [], storyboard_count: 0, video_count: 2, narration_audio_count: 0, end_frame_count: 0, grid_member_count: 0, grid_count: 0 } } });
  });
  await page.goto("/app/make/demo?step=breakdown&episode=1");
  const submit = page.getByRole("button", { name: "确认拆解，审查资产提示词", exact: true });
  await submit.click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("将覆盖原有 1 个视频");
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  expect(writes).toEqual([]);
  await submit.click();
  await dialog.getByRole("button", { name: "覆盖并确认", exact: true }).click();
  await expect(dialog).toContainText("将覆盖更新后的 2 个视频");
  expect(writes).toEqual(["sha256-v1:first"]);
  await dialog.getByRole("button", { name: "覆盖并确认", exact: true }).click();
  await expect(page).toHaveURL(/step=assets&episode=1$/);
  expect(writes).toEqual(["sha256-v1:first", "sha256-v1:second"]);
});

test("late video submission stays on the destination after leaving the episode", async ({ page, api }) => {
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo": { status: 200, body: { ...assetsProject, project: { ...assetsProject.project, characters: {} } } },
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "idle", stage: null } },
  });
  let release!: () => void;
  let arrived!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  const started = new Promise<void>((resolve) => { arrived = resolve; });
  await page.route("**/api/v1/projects/demo/episodes/1/production/videos", async (route) => {
    arrived(); await pending;
    await route.fulfill({ json: { episode: 1, status: "active", stage: "videos", action: "videos" } });
  });
  await page.goto("/app/make/demo?step=videos&episode=1");
  await page.getByRole("button", { name: "确认生成视频", exact: true }).click();
  await started;
  await page.getByRole("banner").getByRole("link", { name: "项目管理", exact: true }).click();
  await expect(page).toHaveURL(/\/app\/projects\/demo$/);
  release();
  await expect(page.getByText("任务已提交，将在后台继续处理。", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/app\/projects\/demo$/);
});


test("Jianying handoff is explicit, resumes after exit and keeps MP4 and new versions independent", async ({ page, api }) => {
  const edit = recorded<import("../../src/types/production.js").ProductionEdit>("project-demo-production-edit.json");
  let delivery: import("../../src/types/jianying-delivery.js").JianyingDelivery | null = null;
  const submitted: Record<string, unknown>[] = [];
  let fail = false;
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo/episodes/1/production": { status: 200, body: { episode: 1, status: "completed", stage: "render", timeline_id: edit.timeline_id, output: { status: "current", artifact_path: "renders/final.mp4", version: 2 } } },
    "GET /api/v1/projects/demo/episodes/1/production/edit": { status: 200, body: edit },
  });
  await page.route("**/api/v1/projects/demo/episodes/1/production/jianying", async (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      submitted.push(body);
      if (fail) { fail = false; await route.fulfill({ status: 409, json: { detail: "索引已变化，请重试" } }); return; }
      delivery = { delivery_id: body.new_version ? "second" : "first", draft_id: body.new_version ? "draft-two" : "draft-one", folder: "测试剪映草稿", revision: 1, local: true, error: null, state: delivery ? "delivered" : "preparing" };
    }
    await route.fulfill({ json: { delivery } });
  });
  await page.goto("/app/make/demo?step=output&episode=1");
  const card = page.getByRole("region", { name: "剪映草稿", exact: true });
  await expect(card.getByRole("button", { name: "在剪映建立草稿", exact: true })).toBeEnabled();
  await card.getByRole("button", { name: "刷新状态", exact: true }).click();
  expect(submitted).toHaveLength(0);
  await card.getByRole("button", { name: "在剪映建立草稿", exact: true }).click();
  await expect(card.getByRole("status")).toContainText("正在准备");
  delivery = { ...delivery!, state: "waiting_for_exit" };
  await expect(card.getByRole("status")).toContainText("退出剪映");
  fail = true;
  await card.getByRole("button", { name: "继续登记 / 重试", exact: true }).click();
  await expect(card.getByRole("alert")).toContainText("索引已变化");
  await expect(page.getByRole("link", { name: "下载 MP4", exact: true })).toBeVisible();
  await card.getByRole("button", { name: "继续登记 / 重试", exact: true }).click();
  await expect(card.getByRole("button", { name: "打开剪映", exact: true })).toBeVisible();
  await card.getByRole("button", { name: "建立新版本", exact: true }).click();
  await expect.poll(() => submitted.length).toBe(4);
  expect(submitted[3]).toMatchObject({ new_version: true, previous_id: "first", local: true });
  await page.reload();
  await expect(card.getByRole("status")).toContainText("已在剪映草稿箱建立");
  expect(submitted).toHaveLength(4);
  await expectReachableLayout(page);
});

test("timeline problems keep manual text editable and block generation until explicitly restored", async ({ page, api }) => {
  const text = "【0-7s】\n@[阿明]抬眼，停住。";
  let saved = { ...unit, text, prompt_format_version: "timeline_v1", generation_prompt: "旧人工完整框架" };
  const writes: string[] = [];
  page.on("request", (request) => { if (request.method() === "POST" && !request.url().endsWith("/prompt-preview")) writes.push(new URL(request.url()).pathname); });
  api.override({ ...productionBase,
    "GET /api/v1/projects/demo/reference-videos/episodes/1/units": { status: 200, body: { units: [saved] } },
  });
  await page.route("**/api/v1/projects/demo/reference-videos/episodes/1/units/E1U1/prompt-preview", async (route) => {
    const body = route.request().postDataJSON() as { prompt: string; generation_prompt?: string };
    const manual = !!body.generation_prompt;
    await route.fulfill({ json: { text: body.generation_prompt || body.prompt, creative_text: body.generation_prompt || body.prompt, declarations: "", references: [], warnings: [], unavailable: null, prompt_source: manual ? "manual" : "default", prompt_format_version: "timeline_v1", generation_allowed: !manual, problems: manual ? [{ code: "timeline_missing", blocking: true, message: "第 1 行（人工创作段）：缺少时间轴，请补充后再生成。", params: { line: 1, source: "generation_prompt" } }] : [] } });
  });
  await page.route("**/api/v1/projects/demo/reference-videos/episodes/1/units/E1U1", async (route) => {
    const body = route.request().postDataJSON() as { prompt: string; generation_prompt: string };
    saved = { ...saved, text: body.prompt, generation_prompt: body.generation_prompt };
    api.override({ "GET /api/v1/projects/demo/reference-videos/episodes/1/units": { status: 200, body: { units: [saved] } } });
    await route.fulfill({ json: { unit: saved } });
  });
  await page.goto("/app/make/demo?step=videos&episode=1");
  const card = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "单元 1 · 7 秒", exact: true }) });
  const creative = card.getByRole("textbox", { name: "视频提示词", exact: true });
  await expect(creative).toHaveValue("旧人工完整框架");
  await expect(card.getByRole("alert")).toContainText("缺少时间轴");
  await expect(page.getByRole("button", { name: "确认生成视频", exact: true })).toBeDisabled();
  await card.getByRole("button", { name: "定位到第 1 行", exact: true }).click();
  await expect(creative).toBeFocused();
  await card.getByRole("button", { name: "恢复默认框架", exact: true }).click();
  await expect(creative).toHaveValue(text);
  await expect(card.getByRole("alert")).toHaveCount(0);
  await card.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => saved.generation_prompt).toBe("");
  await expect(page.getByRole("button", { name: "确认生成视频", exact: true })).toBeEnabled();
  expect(writes).toEqual([]);
  await expectReachableLayout(page);
  await expectAccessible(page);
});

test("whole script import previews boundaries and saves without automatic breakdown", async ({ page, api }) => {
  api.override(multiBase);
  const text = "第一集 开始\n甲说你好。🙂\n第二集 继续\n甲换上盔甲。";
  let saved = false;
  let starts = 0;
  type Parts = Array<{ start_line: number; number: number; title: string }>;
  const previews: Array<{ text: string; boundaries?: Parts }> = [];
  await page.route("**/production/source-preview", (route) => route.fulfill({ json: { text, filename: "整部.txt" } }));
  await page.route("**/production/whole-script/preview", async (route) => {
    const request = route.request().postDataJSON() as typeof previews[number];
    previews.push(request);
    const parts = request.boundaries ?? [{ start_line: 1, number: 1, title: "第一集 开始" }, { start_line: 3, number: 2, title: "第二集 继续" }];
    await route.fulfill({ json: { text: request.text, fingerprint: "full-source", project_basis: "project-revision", episodes: parts.map((part) => ({ ...part, start: 0, end: text.length, characters: text.length, head: "正文预览", tail: "保留原文" })), warnings: [], errors: [] } });
  });
  await page.route("**/production/whole-script/import", async (route) => {
    expect(route.request().postData()).toContain("手动修订标题");
    saved = true;
    await route.fulfill({ json: { import_id: "import-1", episodes: [3, 4], fingerprint: "full-source" } });
  });
  await page.route("**/production/script-batch", async (route) => {
    if (route.request().method() === "POST") starts++;
    await route.fulfill({ json: { status: "idle", items: [], imports: saved ? [{ import_id: "import-1", episodes: [3, 4], fingerprint: "full-source" }] : [] } });
  });
  await page.goto("/app/projects/demo");
  await page.getByRole("button", { name: "上传整部剧本", exact: true }).click();
  await page.locator("#whole-script-file").setInputFiles({ name: "整部.txt", mimeType: "text/plain", buffer: Buffer.from(text) });
  await expect(page.getByRole("textbox", { name: "整部剧本原文", exact: true })).toHaveValue(text);
  await page.getByRole("button", { name: "识别分集", exact: true }).click();
  await expect(page.getByLabel("原集号", { exact: true })).toHaveCount(2);
  await page.locator("#boundary-title-1").fill("手动修订标题");
  await expect(page.getByRole("button", { name: "确认分集并保存全部", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "校验调整并预览", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认分集并保存全部", exact: true })).toBeEnabled();
  await waitForEntrance(page.locator("main"));
  await expectAccessible(page);
  await expectReachableLayout(page);
  await page.getByRole("button", { name: "确认分集并保存全部", exact: true }).click();
  await expect(page.getByRole("button", { name: "一键拆解全部集", exact: true })).toBeEnabled();
  expect(previews[1].boundaries?.[1].title).toBe("手动修订标题");
  expect(starts).toBe(0);
});

test("whole script batch starts once survives refresh and finishes at review links", async ({ page, api }) => {
  api.override(multiBase);
  let starts = 0;
  let state: { status: string; import_id?: string; items: Array<{ episode: number; status: string }>; imports: Array<{ import_id: string; episodes: number[]; fingerprint: string }> } = { status: "idle", items: [], imports: [{ import_id: "import-1", episodes: [1, 2], fingerprint: "full" }] };
  await page.route("**/production/script-batch", async (route) => {
    if (route.request().method() === "POST") {
      starts++;
      state = { ...state, status: "active", import_id: "import-1", items: [{ episode: 1, status: "active" }, { episode: 2, status: "pending" }] };
    }
    await route.fulfill({ json: state });
  });
  await page.goto("/app/projects/demo");
  await page.getByRole("button", { name: "一键拆解全部集", exact: true }).click();
  await expect(page.getByText("正在逐集拆解 · 0/2", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "新建单集", exact: true })).toBeDisabled();
  await page.reload();
  await expect(page.getByText("正在逐集拆解 · 0/2", { exact: true })).toBeVisible();
  expect(starts).toBe(1);
  state = { ...state, status: "completed", items: [{ episode: 1, status: "completed" }, { episode: 2, status: "completed" }] };
  await expect(page.getByText("全部拆解完成，请逐集审查 · 2/2", { exact: true })).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole("link", { name: "第 2 集 · 待审查", exact: true })).toHaveAttribute("href", "/app/make/demo?step=breakdown&episode=2");
  await expectReachableLayout(page);
  expect(starts).toBe(1);
});

test("unknown whole script submission remains read-only after refresh", async ({ page, api }) => {
  api.override({ ...multiBase, "GET /api/v1/projects/demo/production/script-batch": { status: 200, body: { status: "unknown", import_id: "import-1", items: [{ episode: 1, status: "unknown" }], imports: [] } } });
  const writes: string[] = [];
  page.on("request", (request) => { if (request.method() !== "GET" && request.url().includes("/api/")) writes.push(request.url()); });
  await page.goto("/app/projects/demo");
  await expect(page.getByText("提交结果待核对，不会重复提交 · 0/1", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("提交结果待核对，不会重复提交 · 0/1", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "继续未完成集", exact: true })).toHaveCount(0);
  expect(writes).toEqual([]);
});
