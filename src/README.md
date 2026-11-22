# Photo and Song Video Maker (GPU)

A small web app in a Docker container. Open it from your phone or computer, pick a picture or a video, add a song, and it returns one MP4. Rendering uses ffmpeg with the NVIDIA GPU (NVENC), and falls back to the CPU if no GPU encoder is found.

Options: vertical, square, wide or same-as-source shape, 720p / 1080p / 4K, whole picture with blurred sides or fill-and-crop, slow zoom on pictures, fade in and out, music bars, start point and length for the song, and mixing a video's own sound under the song.

## Requirements

- Unraid with the **Nvidia Driver** plugin (Community Apps), driver 530 or newer. The GPU must not be passed through to a VM.
- Docker with the `nvidia` runtime (the plugin sets this up).

## Install as an Unraid plugin (one link)

If you have the `photo-song-video.plg` file, host it anywhere Unraid can reach, then go to Plugins > Install Plugin, paste its link and press Install. The plugin carries the whole app inside it, so that one file is all that needs hosting. It builds the image on the server (several minutes the first time), adds the container to the Docker tab and starts it. From the Unraid terminal, `plugin install /path/to/photo-song-video.plg` does the same from a local file, and `psv-setup` re-runs the setup by hand (`psv-setup --force` rebuilds). Removing the plugin removes the container and image and keeps your data folder.

The manual steps below do the same thing without the plugin.

## Install on Unraid

1. Install the **Nvidia Driver** plugin, pick a driver, reboot if asked. Open Settings > Nvidia Driver and note the GPU UUID (starts with `GPU-`).
2. Copy this folder to the server, for example to `/mnt/user/appdata/psv-src`.
3. Build the image from the Unraid terminal:

   ```
   cd /mnt/user/appdata/psv-src
   docker build -t photo-song-video .
   ```

4. Start it, using one of these:

   **A. Unraid template.** Copy `unraid-template.xml` to `/boot/config/plugins/dockerMan/templates-user/my-photo-song-video.xml`. On the Docker tab choose Add Container, pick `photo-song-video` from the template list, paste your GPU UUID into `NVIDIA_VISIBLE_DEVICES` (or leave `all`), and Apply.

   **B. docker run.**

   ```
   docker run -d --name photo-song-video --restart unless-stopped \
     --runtime=nvidia \
     -e NVIDIA_VISIBLE_DEVICES=all \
     -e NVIDIA_DRIVER_CAPABILITIES=all \
     -p 8095:8000 \
     -v /mnt/user/appdata/photo-song-video:/data \
     photo-song-video
   ```

   **C. Compose.** `docker compose up -d --build` with the included `docker-compose.yml`.

5. Open `http://SERVER-IP:8095`. The line under the title should read "Rendering on NVIDIA GeForce ... (NVENC)". If it says CPU, see Troubleshooting.

## Settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENCODER` | `auto` | `auto`, `nvenc` or `cpu` |
| `MAX_UPLOAD_MB` | `4096` | Largest upload accepted |
| `MAX_LENGTH_SECONDS` | `3600` | Longest video that can be made |
| `RETENTION_HOURS` | `12` | Finished videos are deleted after this long |
| `MAX_CONCURRENT_JOBS` | `1` | Renders at the same time. GeForce cards limit simultaneous NVENC sessions, so keep this low |

## How it works

- Pictures are decoded with Pillow first (HEIC, WebP, GIF and phone rotation are handled), then handed to ffmpeg as a clean PNG.
- Videos and songs are checked with ffprobe and only common containers are accepted.
- ffmpeg builds the whole picture, blur, zoom, fade, music bars and audio mix as one filter graph, then encodes with `h264_nvenc` (preset p5, constant quality). The filters themselves run on the CPU, the encode runs on the GPU. Output is H.264 plus AAC in an MP4 with the index at the front, so it plays on phones and uploads cleanly.
- If an NVENC attempt fails, the job is retried on the CPU automatically.
- Uploads and results live in `/data/jobs` and are removed after `RETENTION_HOURS`, and on every container start.

## Troubleshooting

- **Badge says CPU.** Run `docker logs photo-song-video | grep -i encoder` and `docker exec photo-song-video ffmpeg -hide_banner -encoders | grep nvenc`.
  - `Cannot load libcuda.so.1`: the container isn't getting the GPU. Check `--runtime=nvidia`, that `NVIDIA_DRIVER_CAPABILITIES` is `all` (or includes `video`), and that the GPU isn't bound to VFIO for a VM.
  - `Driver does not support the required nvenc API version`: the driver is older than 530. Update it in the plugin.
- **Upload fails for big files.** A reverse proxy in front may cap the request size (set `client_max_body_size` in Nginx Proxy Manager). Cloudflare's free plan caps a single upload at 100 MB when traffic goes through their proxy or a tunnel.
- **Video won't open in the preview but downloads fine.** Some browsers can't preview HEIC pictures. The server converts them anyway.

## Security

There is no login. Keep it on your LAN, or put an authentication layer in front of it (Cloudflare Access, Authelia) before publishing it.

## Status

The render pipeline, API and web page were tested end to end on a machine without a GPU or Docker daemon, using the CPU encoder: picture + song, video + song, video's own sound, silent picture, phone-rotated JPEG, bad file, range requests and downloads. The Docker build and the NVENC path itself were not run, because no GPU was available. The ffmpeg arguments for NVENC were accepted by ffmpeg 6.1 up to the point where it tried to open the GPU.
