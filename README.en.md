# XM_Studio

A local short-drama production tool organized around a single-episode workflow:

**Project → Episode → Script → Breakdown → Assets → Videos → Final Cut**

Projects and production feedback stay on each tester's machine. Team members can record changes to prompts, asset images, and video units, then export the records for offline review.

## Project Guides

- [Production team testing guide](docs/production-mvp/team-testing.md)

## Run on Windows

The ZIP contains source code. Use **Windows PowerShell** for the commands below, and finish each command successfully before continuing.

### 1. Install prerequisites

Install [Node.js](https://nodejs.org/) 24.12 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/). uv will download Python 3.12 when needed. Alternatively, install the tools with WinGet:

```powershell
winget install --exact --id OpenJS.NodeJS.LTS --source winget
winget install --exact --id astral-sh.uv --source winget
```

Close and reopen PowerShell, then verify:

```powershell
node --version
npm.cmd --version
uv --version
```

### 2. Download and extract

On the [repository page](https://github.com/5JjiaLin/xm-studio), choose **Code → Download ZIP**, then use **Extract All**. Open PowerShell inside the extracted `xm-studio-main` folder containing `pyproject.toml`, `frontend`, and `server`. For example, adjust this path to your extraction location:

```powershell
Set-Location "$HOME\Downloads\xm-studio-main"
```

### 3. Install dependencies and build once

Run each command from the project root. Internet access is required to download dependencies.

```powershell
uv sync --python 3.12 --no-dev
npm.cmd install --global pnpm@10.33.2
Set-Location frontend
pnpm.cmd install --frozen-lockfile
pnpm.cmd build
Set-Location ..
```

Make sure the build succeeds and you are back in the project root before starting the service.

### 4. Start the service

Run these commands from the project root every time you start:

```powershell
$env:AUTH_ENABLED = "false"
$env:ARCREEL_DATA_DIR = "projects"
$env:ARCREEL_PROFILE_DIR = "agent_runtime_profile"
uv run --no-dev uvicorn server.app:app --host 127.0.0.1 --port 1242
```

After `Application startup complete`, open **[http://127.0.0.1:1242/app/projects](http://127.0.0.1:1242/app/projects)**.

This command listens only on the local machine and disables login. Keep the PowerShell window open while using the app; press `Ctrl+C` to stop. On subsequent runs, enter the project folder and repeat this step; dependency installation and building are only needed initially or after relevant updates.

### 5. Configure models and create a video

1. Open model configuration, enter your provider, API key, base URL, and models, then test the connection. Configure text, image, and video capabilities as needed.
2. Create a project and episode, paste or upload a script, then follow Script → Breakdown → Assets → Videos → Final Cut.
3. Review and save complete prompts before explicitly generating or regenerating media. Model calls use your configured provider's credits.
4. Before team testing, save your tester code, batch, and sample in the test records panel. Export a ZIP using the projects page's edit-record export button and send it to the coordinator. See the [team testing guide](docs/production-mvp/team-testing.md).

Project media and edit records stay in the local `projects/` directory. API keys are saved through local model configuration. Keep the project folder to continue later; back up local configuration and `projects/` before changing computers or replacing the source folder.

### Troubleshooting

| Issue | Action |
| --- | --- |
| A tool is not found | Install it, reopen PowerShell, and check its version. |
| PowerShell blocks `npm.ps1` or `pnpm.ps1` | Use `npm.cmd` and `pnpm.cmd` as shown above; no execution-policy change is needed. |
| Page is unavailable or the frontend is missing | Ensure the build succeeded and `frontend/dist/index.html` exists, then restart the service. |
| Port 1242 is in use | Stop the earlier instance, or use port 1243 in both the startup command and browser URL. |
| Dependency downloads fail | Check your network connection and retry the failed installation command. |

These instructions were checked against the repository configuration and startup code. A complete production run on a Windows machine remains to be verified by the testing team.

## Continue editing in Jianying

After confirming the selected videos, save the clip order, trims, volume, and subtitles on the **Final Cut** page, then click **Create draft in Jianying**. The draft keeps separate video clips and editable subtitle segments.

1. Expand the local Jianying settings, confirm the application and draft-root paths, then save and enable. If the draft index is missing, create an empty draft in Jianying and quit before checking the paths again.
2. Create the draft to copy media and prepare the saved timeline. If Jianying is running, save your work and quit normally, then choose continue/retry in XM.
3. After registration, open Jianying and find the draft by the name displayed in XM. Continue editing and export from Jianying.
4. After changing the timeline in XM, create a new version. Existing delivered drafts and manual edits are preserved. Page loads, refreshes, and video completion do not register drafts automatically.

MP4 rendering and draft delivery have independent states. Jianying **11.7.0 on Apple Silicon Mac** has been tested; Windows adaptation and automated checks are implemented, but **native opening, editing, and export remain unverified**. Automatic registration requires XM and Jianying on the same computer. Remote deployments provide a media-inclusive package; relocating media paths and registering it on the destination computer are manual steps.

## License

See [LICENSE](LICENSE), [NOTICE](NOTICE), and [LICENSES](LICENSES/) for license terms and third-party notices.
