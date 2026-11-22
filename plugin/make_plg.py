#!/usr/bin/env python3
"""Build photo-song-video.plg: one file holding the app (base64 tarball) plus the install scripts.

usage: make_plg.py OUTPUT.plg VERSION [PLUGIN_URL]   (app source: ../src, or $APP_DIR)
"""
import base64
import io
import os
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = Path(os.environ.get("APP_DIR") or HERE.parent / "src")
NAME = "photo-song-video"

out = Path(sys.argv[1])
version = sys.argv[2]
url = sys.argv[3] if len(sys.argv) > 3 else ""


def payload() -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in sorted(APP.rglob("*")):
            if p.is_dir() or "__pycache__" in p.parts:
                continue
            ti = tf.gettarinfo(str(p), arcname=str(p.relative_to(APP)))
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mtime = 0
            with p.open("rb") as fh:
                tf.addfile(ti, fh)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))


setup = (HERE / "psv-setup.sh").read_text().replace("@VERSION@", version)
remove = (HERE / "psv-remove.sh").read_text()
data = payload()

for label, text in (("setup", setup), ("remove", remove), ("payload", data)):
    if "]]>" in text:
        sys.exit("cannot embed %s: contains ]]>" % label)

plugin_attrs = 'name="&name;" author="Built with Claude" version="&version;" min="6.12.0"'
if url:
    plugin_attrs += ' pluginURL="&pluginURL;"'
url_entity = '<!ENTITY pluginURL "%s">\n' % url if url else ""

plg = f"""<?xml version='1.0' standalone='yes'?>
<!DOCTYPE PLUGIN [
<!ENTITY name "{NAME}">
<!ENTITY version "{version}">
{url_entity}<!ENTITY emhttp "/usr/local/emhttp/plugins/&name;">
<!ENTITY flash "/boot/config/plugins/&name;">
]>
<PLUGIN {plugin_attrs}>

<CHANGES>
###{version}
- Fixes the container not being started on a first install, and old setup files being kept on update.
- Builds the app image on the server, adds it to the Docker tab and starts it. Uses the NVIDIA GPU (NVENC) when the Nvidia Driver plugin is installed, otherwise the CPU.
</CHANGES>

<!-- folders. The two files below are removed first because Unraid keeps an inline file that already exists, which would leave an old copy behind on update -->
<FILE Run="/bin/bash">
<INLINE>
mkdir -p &emhttp; &flash;
rm -f &emhttp;/payload.b64 /usr/local/sbin/psv-setup
</INLINE>
</FILE>

<!-- the app, packed as a base64 tarball -->
<FILE Name="&emhttp;/payload.b64" Mode="0644">
<INLINE><![CDATA[
{data}
]]></INLINE>
</FILE>

<!-- command to set up or rebuild by hand: psv-setup, or psv-setup with the force option -->
<FILE Name="/usr/local/sbin/psv-setup" Mode="0755">
<INLINE><![CDATA[{setup}]]></INLINE>
</FILE>

<!-- runs on install, on update, and at every boot (then exits at once because Docker is not up yet) -->
<FILE Run="/bin/bash">
<INLINE>
/usr/local/sbin/psv-setup
</INLINE>
</FILE>

<FILE Run="/bin/bash" Method="remove">
<INLINE><![CDATA[{remove}]]></INLINE>
</FILE>

</PLUGIN>
"""
out.write_text(plg)
print("wrote %s (%d bytes, payload %d bytes base64)" % (out, out.stat().st_size, len(data)))
