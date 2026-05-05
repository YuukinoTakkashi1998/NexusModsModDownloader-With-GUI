# NexusModsModDownloader — Unofficial Fork with GUI

A fixed and updated fork of [NexusModsModDownloader by Wedsels](https://github.com/Wedsels/NexusModsModDownloader).

---

## Description

The original program stopped functioning reliably — navigation would fail and downloads would time out without completing. This fork repairs those issues entirely and introduces a graphical user interface for a cleaner and more manageable experience.

I made this because my dorm WiFi is slow and unreliable, which makes downloading mods a frustrating process. So instead of dealing with that, I prepare everything in advance and then download the files later when I’m at university where the internet is much faster. This tool lets me skip manually browsing every mod page when I get there and just run everything at once.

---

## What Changed

- Fixed navigation and download timeout errors present in the original
- Added a graphical user interface (GUI) that allows the user to review and select which mods to download before the process begins
- Cleaner output display during the download process
- Added `Launch.bat` for simplified startup

All input and output methods remain the same as the original program. The GUI is an addition on top of the existing workflow, not a replacement.

---

## Download

[Download the latest release here](https://github.com/YuukinoTakkashi1998/NexusModsModDownloader-With-GUI/releases)

Or clone the repository:

```
git clone https://github.com/YuukinoTakkashi1998/NexusModsModDownloader-With-GUI.git
```

---

## Setup

This program requires the same setup as the original. Please follow the steps below carefully.

1. Download and install [Python](https://www.python.org/downloads/)

2. Download and install [Firefox](https://www.mozilla.org/en-US/firefox/new/)
   - Open Firefox and navigate to `about:profiles` in the address bar
   - Choose a Firefox profile to use
   - Sign in to Nexus Mods using a valid account
   - Generate an API key at the bottom of [this page](https://next.nexusmods.com/settings/api-keys)
   - Optionally, install a content blocker such as uBlock Origin to improve download speed

3. Configure `config.json` with your API key and Firefox profile details

4. Launch the program using `Launch.bat` or by running the Python script directly

---

## Usage

Once launched, either paste the link to a Nexus Mods collection page, provide the path to a mod list text file, or you can browse the file in the GUI. The GUI will display the available mods and allow you to select which ones to download before proceeding.

### Mod List Text File Syntax

The syntax for mod list text files follows the original program's format. The game name goes at the top, followed by mod entries.

The game name can be found in the Nexus Mods URL, for example: `https://www.nexusmods.com/games/eldenring`

| Entry Format | Behaviour |
|---|---|
| `eldenring` | Game name (placed at top of file) |
| `88943` | Download all main files for this mod ID |
| `88943:Textures 4k` | Download only the main file matching this name |
| `88943:Textures 4k:Textures Essentials` | Download the matching main files |
| `88943;Textures LOD;Textures Addon` | Download all main files and these specific optional files |
| `88943:;Textures Optional Grass` | Download only this optional file |
| `88943;` | Download all main files and all optional files |
| `88943:;` | Download only all optional files |
| `https://direct.download.link/file/1234/download` | Download the file at this direct link |

An example mod list file is included in the repository.

---

## Screenshot

![GUI Screenshot](/gui.png)

---

## Credits

All original credit goes to [Wedsels](https://github.com/Wedsels), the author of the original NexusModsModDownloader.

This fork was developed with the assistance of Claude (Anthropic) and GitHub Copilot.
Basically it entirely Vibe-Coded

---

## Disclaimer

Use this program at your own risk. This is an unofficial, independently maintained fork and is not affiliated with Nexus Mods or the original author. If the original author wishes for this repository to be taken down, it will be removed upon request.
