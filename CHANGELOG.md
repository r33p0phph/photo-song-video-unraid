# Changelog

Newest first. Each release is one plugin version. The Plugins page in Unraid shows this list too.
Add the new entry at the top before building the plugin file with `plugin/make_plg.py`.

## 2026.11.21f
- The public app name and the build label are now kept apart. The page shows the name as its heading and the build label only in a small line at the bottom. `/api/health` reports both, and the image carries the label.
- Install messages read "Photo and Song Video Maker (build ...)".

## 2026.11.21e
- Several pictures make a slideshow. Reorder, rotate or remove them, set seconds per picture, and choose crossfade, slide or hard cut.
- Zoom and pan are now clearly visible (16 percent instead of 12 percent over the whole song) and keep moving back and forth on long pictures.

## 2026.11.21d
- Updating the plugin also recreates a container that was made from the Docker tab, keeping all its settings, so a new image is always picked up.
- Removing the plugin also removes a container made from the Docker tab.

## 2026.11.21c
- The song slot also takes a video and uses only its sound.
- Damaged or cut-short photos open instead of being refused. ffmpeg is a second decoder for pictures the first one cannot read.
- Files the app rejects are written to the container log, with the reason.

## 2026.11.21b
- Fixed updates running the old setup script, because Unraid keeps an inline file that already exists. The two inline files are now removed before each install.

## 2026.11.21a
- Fixed the first install not starting the container. The installer mistook the freshly built image for an existing container of the same name.

## 2026.11.21
- First release. Builds the app image on the server, adds it to the Docker tab and starts it. Uses the NVIDIA GPU (NVENC) when the Nvidia Driver plugin is installed, otherwise the CPU.
