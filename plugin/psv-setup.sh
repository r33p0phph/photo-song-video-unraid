#!/bin/bash
# Photo and Song Video Maker: set up the Docker image and container on Unraid.
# Runs when the plugin is installed or updated. Safe to run again: psv-setup [--force]
NAME=photo-song-video
VERSION="@VERSION@"
FLASH="${FLASH:-/boot}"
PLUGIN_DIR="${PLUGIN_DIR:-/usr/local/emhttp/plugins/$NAME}"
APPDATA="${APPDATA:-/mnt/user/appdata}"
DATA="$APPDATA/$NAME"
TEMPLATES="$FLASH/config/plugins/dockerMan/templates-user"
IMAGE="$NAME:latest"
FIRST_PORT=8095
FORCE=0
[ "$1" = "--force" ] && FORCE=1

# Unraid runs every plugin again at boot, before Docker is up. Nothing to do then.
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker is not running, so Photo and Song Video Maker was not set up yet."
  echo "Turn Docker on in Settings > Docker, then run psv-setup in the Unraid terminal."
  exit 0
fi

CUR=$(docker image inspect -f '{{index .Config.Labels "psv.version"}}' "$IMAGE" 2>/dev/null)
if [ "$CUR" = "$VERSION" ] && [ "$FORCE" != 1 ]; then
  echo "Photo and Song Video Maker $VERSION is already set up."
  exit 0
fi

echo "Setting up Photo and Song Video Maker $VERSION"

if [ ! -d "$APPDATA" ]; then
  echo "The folder $APPDATA was not found. Check that Docker is set up with an appdata share."
  exit 1
fi

SRC=$(mktemp -d /tmp/psv-src.XXXXXX) || exit 1
trap 'rm -rf "$SRC"' EXIT
if ! base64 -d "$PLUGIN_DIR/payload.b64" | tar -xz -C "$SRC"; then
  echo "Could not unpack the app files."
  exit 1
fi

RUNTIME=""
if docker info 2>/dev/null | grep -qi 'runtimes:.*nvidia'; then
  RUNTIME="--runtime=nvidia"
  echo "NVIDIA runtime found. The GPU will be used for encoding."
else
  echo "NVIDIA runtime not found. Install the Nvidia Driver plugin to use the GPU."
  echo "Until then, videos render on the CPU."
fi

echo
echo "Building the image. This downloads ffmpeg and Python packages, so the first build takes several minutes."
if ! docker build --label "psv.version=$VERSION" -t "$IMAGE" "$SRC"; then
  echo
  echo "The image build failed. Check that the server can reach the internet, then run psv-setup again."
  exit 1
fi

# An earlier container from this plugin is replaced, keeping its port and GPU choice.
# A container with the same name that this plugin did not create is left alone.
GPU="all"
PORT=""
if docker inspect --type container "$NAME" >/dev/null 2>&1; then
  if [ "$(docker inspect --type container -f '{{index .Config.Labels "psv.plugin"}}' "$NAME" 2>/dev/null)" = "1" ]; then
    PORT=$(docker inspect --type container -f '{{with index .HostConfig.PortBindings "8000/tcp"}}{{(index . 0).HostPort}}{{end}}' "$NAME" 2>/dev/null)
    G=$(docker inspect --type container -f '{{range .Config.Env}}{{println .}}{{end}}' "$NAME" 2>/dev/null | sed -n 's/^NVIDIA_VISIBLE_DEVICES=//p' | head -n 1)
    [ -n "$G" ] && GPU="$G"
    docker rm -f "$NAME" >/dev/null 2>&1
  else
    echo
    echo "A container named $NAME that this plugin did not create already exists, so it was left as it is."
    echo "Open it in the Docker tab, choose Edit, then Apply, to recreate it with the new image."
    exit 0
  fi
fi

port_free() { ! ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$"; }
if [ -z "$PORT" ]; then
  PORT=$FIRST_PORT
  while ! port_free "$PORT" && [ "$PORT" -lt $((FIRST_PORT + 20)) ]; do PORT=$((PORT + 1)); done
fi

# Docker tab entry, so the container can be edited there. An existing template is never overwritten.
mkdir -p "$TEMPLATES"
TPL="$TEMPLATES/my-$NAME.xml"
if [ ! -f "$TPL" ]; then
  cp "$SRC/unraid-template.xml" "$TPL"
  sed -i "/Name=\"Web UI port\"/s#>8095</Config>#>$PORT</Config>#" "$TPL"
  sed -i "/Name=\"NVIDIA_VISIBLE_DEVICES\"/s#>all</Config>#>$GPU</Config>#" "$TPL"
fi

mkdir -p "$DATA"
echo
echo "Starting the container on port $PORT"
# $RUNTIME is empty or --runtime=nvidia, so it is left unquoted on purpose.
if ! docker run -d --name "$NAME" --restart unless-stopped $RUNTIME \
    -e NVIDIA_VISIBLE_DEVICES="$GPU" -e NVIDIA_DRIVER_CAPABILITIES=all \
    -p "$PORT:8000" -v "$DATA:/data" \
    --label psv.plugin=1 \
    --label net.unraid.docker.managed=dockerman \
    --label "net.unraid.docker.webui=http://[IP]:[PORT:8000]/" \
    "$IMAGE" >/dev/null; then
  echo "The container could not be started. Run psv-setup again, or add it from the Docker tab."
  exit 1
fi

HEALTH=""
for _ in $(seq 1 40); do
  HEALTH=$(curl -fsS "http://127.0.0.1:$PORT/api/health" 2>/dev/null) && break
  HEALTH=""
  sleep 1
done

ENC=$(echo "$HEALTH" | sed -n 's/.*"encoder":"\([a-z]*\)".*/\1/p')
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
if [ "$ENC" = "nvenc" ]; then
  echo "Done. Encoding runs on the GPU (NVENC)."
elif [ "$ENC" = "cpu" ]; then
  if [ -n "$RUNTIME" ]; then
    echo "Done, but the GPU encoder was not found inside the container, so videos render on the CPU."
    echo "Check that the Nvidia Driver plugin shows your GPU, then run psv-setup --force."
  else
    echo "Done. Videos render on the CPU until the Nvidia Driver plugin is installed."
  fi
else
  echo "The container started but did not answer yet. Give it a few seconds, then open the link below."
fi
echo "Open http://${IP:-YOUR-SERVER-IP}:$PORT in a browser."
exit 0
