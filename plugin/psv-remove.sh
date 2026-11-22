#!/bin/bash
# Photo and Song Video Maker: remove what the plugin set up. Uploaded data is kept.
NAME=photo-song-video
FLASH="${FLASH:-/boot}"

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if docker inspect --type container "$NAME" >/dev/null 2>&1; then
    # ours: created by this plugin, or made from its Docker tab template with its image
    if [ "$(docker inspect --type container -f '{{index .Config.Labels "psv.plugin"}}' "$NAME" 2>/dev/null)" = "1" ] \
        || [ "$(docker inspect --type container -f '{{.Config.Image}}' "$NAME" 2>/dev/null)" = "$NAME:latest" ]; then
      docker rm -f "$NAME" >/dev/null 2>&1 && echo "Removed the container."
    else
      echo "Left the container $NAME alone because this plugin did not create it."
    fi
  fi
  docker rmi "$NAME:latest" >/dev/null 2>&1 && echo "Removed the image."
fi
rm -f "$FLASH/config/plugins/dockerMan/templates-user/my-$NAME.xml"
rm -rf "$FLASH/config/plugins/$NAME" "/usr/local/emhttp/plugins/$NAME"
rm -f /usr/local/sbin/psv-setup
echo "Photo and Song Video Maker was removed. The data folder /mnt/user/appdata/$NAME was left in place."
exit 0
