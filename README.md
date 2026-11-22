# Photo and Song Video Maker for Unraid

Pick a picture or a video, add a song, get one video back. A small web app that renders with ffmpeg on your NVIDIA GPU (NVENC), or on the CPU if no GPU is available. Installs as an Unraid plugin from a link.

## Names and versions

The public name and the build label are kept apart.

| What | Value | Where it shows |
| --- | --- | --- |
| Public name | Photo and Song Video Maker | The page title and heading, install messages, this README |
| Internal id | `photo-song-video` | Plugin file, container, image, appdata folder, labels |
| Build label | `2026.11.21f` (date, then a letter for a second build that day) | Plugin version, image label `psv.version`, `/api/health`, the small line at the bottom of the page, and `CHANGELOG.md` |

The build label never goes into the public name. To release: add an entry to the top of `CHANGELOG.md`, then run `python3 plugin/make_plg.py photo-song-video.plg <build label> <plugin link>`.

## Install

In Unraid, open **Plugins**, then **Install Plugin**, paste this link and press **Install**:

```
https://raw.githubusercontent.com/r33p0phph/photo-song-video-unraid/main/photo-song-video.plg
```

The first install builds the app image on your server, so it takes several minutes and needs internet access. When it finishes, the install window prints the address to open (port 8095, or the next free port). The app also shows up in the Docker tab.

Before installing:

- Docker must be turned on (Settings > Docker).
- For GPU encoding, install the **Nvidia Driver** plugin (driver 530 or newer). Without it, videos render on the CPU.

## What the plugin does

- Builds the `photo-song-video` image from the source in this repo (the whole app is packed inside the `.plg`).
- Adds a template to the Docker tab and starts the container with your appdata folder at `/mnt/user/appdata/photo-song-video`.
- Adds a `psv-setup` command. Run it in the Unraid terminal to repeat the setup, or `psv-setup --force` to rebuild.
- Removing the plugin removes the container and image and keeps your data folder.

## Security

The app has no login. Keep it on your home network, or put an authentication layer in front of it before publishing it.

## Layout

| Path | Contents |
| --- | --- |
| `photo-song-video.plg` | The plugin file Unraid installs |
| `src/` | The app: FastAPI server, ffmpeg pipeline, web page, Dockerfile. See `src/README.md` |
| `plugin/` | Install and remove scripts, the script that builds the `.plg`, and a test that runs the plugin against a simulated Unraid |

To rebuild the plugin after changing `src/`: `python3 plugin/make_plg.py photo-song-video.plg VERSION PLUGIN_URL`.

## Status

The install, update and remove scripts were tested against a simulated Unraid and Docker. The app itself was tested end to end on the CPU encoder. The NVENC path and a real install on Unraid have not been run yet.
