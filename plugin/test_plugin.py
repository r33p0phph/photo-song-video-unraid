#!/usr/bin/env python3
"""Run the .plg against a fake Unraid root with stub docker/curl/ss/hostname commands."""
import os
import re
import shutil
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path("/tmp/fakeunraid")
STUBS = ROOT / "stubs"
STATE = ROOT / "state"

DOCKER = r'''#!/bin/bash
S="${STATE:?}"
echo "docker $*" >> "$S/calls.log"
cmd="$1"; shift
case "$cmd" in
  info)
    [ -f "$S/docker_down" ] && exit 1
    if [ -f "$S/no_nvidia" ]; then echo " Runtimes: io.containerd.runc.v2 runc"; else echo " Runtimes: io.containerd.runc.v2 nvidia runc"; fi
    exit 0;;
  image)
    [ -f "$S/image" ] || exit 1
    cat "$S/image_version"; exit 0;;
  build)
    label=""; ctx=""
    while [ $# -gt 0 ]; do
      case "$1" in --label) label="${2#psv.version=}"; shift 2;; --build-arg) echo "$2" >> "$S/build_args"; shift 2;; -t) shift 2;; *) ctx="$1"; shift;; esac
    done
    if [ ! -f "$ctx/Dockerfile" ] || [ ! -f "$ctx/app/main.py" ] || [ ! -f "$ctx/unraid-template.xml" ]; then echo "stub: bad build context $ctx"; exit 1; fi
    [ -f "$S/build_fails" ] && { echo "stub: build failed"; exit 1; }
    echo "Step 1/9 : FROM ubuntu:24.04 (stub)"; echo "Successfully built"
    echo 1 > "$S/image"; echo "$label" > "$S/image_version"; exit 0;;
  inspect)
    # Real docker: without --type, a name that is not a container falls back to an image of that name.
    type=""
    if [ "$1" = "--type" ]; then type="$2"; shift 2; fi
    if [ "$1" = "-f" ]; then fmt="$2"; else fmt=""; fi
    if [ ! -f "$S/container" ]; then
      if [ -z "$type" ] && [ -f "$S/image" ]; then exit 0; fi
      exit 1
    fi
    case "$fmt" in
      *Config.Image*) cat "$S/c_image" 2>/dev/null;;
      *net.unraid.docker.managed*) cat "$S/c_managed" 2>/dev/null;;
      *psv.plugin*) cat "$S/c_label" 2>/dev/null;;
      *PortBindings*) cat "$S/c_port" 2>/dev/null;;
      *Config.Env*) cat "$S/c_env" 2>/dev/null;;
    esac
    exit 0;;
  rm) rm -f "$S/container" "$S/c_label" "$S/c_port" "$S/c_env"; exit 0;;
  rmi) rm -f "$S/image" "$S/image_version"; exit 0;;
  run)
    echo "$*" > "$S/run_args"
    port=""; env=""; labelp=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -p) port="${2%%:*}"; shift 2;;
        -e) env="$env$2"$'\n'; shift 2;;
        --label) [ "$2" = "psv.plugin=1" ] && labelp=1; shift 2;;
        *) shift;;
      esac
    done
    echo 1 > "$S/container"; echo "$labelp" > "$S/c_label"; echo "$port" > "$S/c_port"; printf "%s" "$env" > "$S/c_env"
    echo abc123def456; exit 0;;
esac
exit 0
'''

CURL = r'''#!/bin/bash
if [ -f "${STATE}/cpu_encoder" ]; then echo '{"encoder":"cpu","gpu":null,"max_upload_mb":4096,"max_length_seconds":3600}'
else echo '{"encoder":"nvenc","gpu":"NVIDIA GeForce GTX 1080","max_upload_mb":4096,"max_length_seconds":3600}'; fi
'''
SS = r'''#!/bin/bash
echo "State Recv-Q Send-Q Local Address:Port Peer Address:Port"
[ -f "${STATE}/busy_ports" ] && while read -r p; do echo "LISTEN 0 4096 0.0.0.0:$p 0.0.0.0:*"; done < "${STATE}/busy_ports"
exit 0
'''
UPDATE = r'''#!/bin/bash
# Stand-in for Unraid's update_container: recreate the container from its template.
echo "update_container $*" >> "${STATE}/calls.log"
echo "Pulling image: photo-song-video:latest"; echo "Successfully removed container: $1"
echo 1 > "${STATE}/container"; : > "${STATE}/c_label"; echo 8100 > "${STATE}/c_port"
echo dockerman > "${STATE}/c_managed"; echo photo-song-video:latest > "${STATE}/c_image"
exit 0
'''
HOSTNAME = r'''#!/bin/bash
echo "192.168.1.50 "
'''


def reset(flags=()):
    shutil.rmtree(ROOT, ignore_errors=True)
    for d in ("stubs", "state", "boot/config/plugins", "usr/local/sbin", "usr/local/emhttp/plugins", "mnt/user/appdata"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    for name, body in (("docker", DOCKER), ("curl", CURL), ("ss", SS), ("hostname", HOSTNAME)):
        p = STUBS / name
        p.write_text(body)
        p.chmod(0o755)
    (ROOT / "update_container").write_text(UPDATE)
    (ROOT / "update_container").chmod(0o755)
    for f in flags:
        (STATE / f).write_text("1\n")


def env():
    e = dict(os.environ)
    e.update({
        "PATH": "%s:%s" % (STUBS, e["PATH"]),
        "STATE": str(STATE),
        "FLASH": str(ROOT / "boot"),
        "PLUGIN_DIR": str(ROOT / "usr/local/emhttp/plugins/photo-song-video"),
        "APPDATA": str(ROOT / "mnt/user/appdata"),
        "UPDATE_CONTAINER": str(ROOT / "update_container"),
    })
    return e


def mapped(text):
    return text.replace("/usr/local/", "%s/usr/local/" % ROOT).replace("/boot/", "%s/boot/" % ROOT)


def run_plg(plg, method="install"):
    """Mimic the Unraid plugin engine: process FILE entries in order."""
    out = []
    code = 0
    for f in ET.parse(plg).getroot().findall("FILE"):
        if f.get("Method", "install") != method:
            continue
        inline = f.find("INLINE")
        text = inline.text if inline is not None and inline.text else ""
        name = f.get("Name")
        if name:
            dest = Path(mapped(name))
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Unraid keeps an inline file that already exists, so model that.
            if not dest.exists():
                dest.write_text(text)
                dest.chmod(int(f.get("Mode", "0644"), 8))
        if f.get("Run"):
            p = subprocess.run(["bash", "-c", mapped(text)], env=env(), capture_output=True, text=True)
            out.append(p.stdout + p.stderr)
            code = p.returncode
            if code != 0:
                break
    return code, "\n".join(out)


def calls():
    p = STATE / "calls.log"
    return p.read_text() if p.exists() else ""


fails = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else "   <- " + detail))
    if not cond:
        fails.append(label)


plg1, plg2 = sys.argv[1], sys.argv[2]
V1 = re.search(r'ENTITY version "([^"]+)"', open(plg1).read()).group(1)
V2 = re.search(r'ENTITY version "([^"]+)"', open(plg2).read()).group(1)
TPL = ROOT / "boot/config/plugins/dockerMan/templates-user/my-photo-song-video.xml"

# 1. fresh install with NVIDIA runtime
reset()
code, out = run_plg(plg1)
run = (STATE / "run_args").read_text() if (STATE / "run_args").exists() else ""
check("1 fresh install exits 0", code == 0, out[-300:])
check("1 image built with version label", (STATE / "image_version").read_text().strip() == V1)
check("1 build label passed to the image build", (STATE / "build_args").read_text().strip() == "PSV_BUILD=" + V1)
check("1 container uses nvidia runtime and port 8095", "--runtime=nvidia" in run and "-p 8095:8000" in run, run)
check("1 plugin labels set", "psv.plugin=1" in run and "net.unraid.docker.managed=dockerman" in run)
check("1 template written with port 8095", TPL.exists() and ">8095</Config>" in TPL.read_text())
check("1 reports GPU encoding and a URL", "GPU (NVENC)" in out and "http://192.168.1.50:8095" in out, out[-400:])
check("1 psv-setup starts with a shebang", (ROOT / "usr/local/sbin/psv-setup").read_text().startswith("#!/bin/bash"))
print("\n--- install output ---\n" + out + "---------------------\n")

# 2. same version again (boot with Docker up): nothing happens
before = calls()
code, out = run_plg(plg1)
new = calls()[len(before):]
check("2 re-run is a no-op", code == 0 and "docker build" not in new and "docker run" not in new and "already set up" in out, out + new)

# 3. boot with Docker down
Path(STATE / "docker_down").write_text("1")
before = calls()
code, out = run_plg(plg1)
check("3 Docker down exits 0 with a message", code == 0 and "Docker is not running" in out, out)
(STATE / "docker_down").unlink()

# 4. upgrade: user edited the template, container is on port 8100 with a GPU UUID
TPL.write_text(TPL.read_text() + "<!-- user edit -->")
(STATE / "c_port").write_text("8100\n")
(STATE / "c_env").write_text("NVIDIA_VISIBLE_DEVICES=GPU-1234\nNVIDIA_DRIVER_CAPABILITIES=all\n")
code, out = run_plg(plg2)
run = (STATE / "run_args").read_text()
check("4 upgrade rebuilds with the new version", (STATE / "image_version").read_text().strip() == V2, out[-300:])
check("4 upgrade replaced the setup script", V2 in (ROOT / "usr/local/sbin/psv-setup").read_text())
check("4 upgrade keeps port and GPU choice", "-p 8100:8000" in run and "NVIDIA_VISIBLE_DEVICES=GPU-1234" in run, run)
check("4 template edits are kept", "<!-- user edit -->" in TPL.read_text())

# 5. port 8095 and 8096 busy, no NVIDIA runtime
reset(["no_nvidia", "cpu_encoder"])
(STATE / "busy_ports").write_text("8095\n8096\n")
code, out = run_plg(plg1)
run = (STATE / "run_args").read_text()
check("5 picks the next free port", "-p 8097:8000" in run and ">8097</Config>" in TPL.read_text(), run)
check("5 no runtime flag and a CPU message", "--runtime" not in run and "CPU" in out, out[-300:])

# 6. a container with this name that the plugin did not create
reset()
(STATE / "container").write_text("1")
(STATE / "c_label").write_text("\n")
code, out = run_plg(plg1)
check("6 foreign container left alone", code == 0 and "docker run" not in calls() and "left as it is" in out, out)

# 7. build failure
reset(["build_fails"])
code, out = run_plg(plg1)
check("7 build failure is reported and fails the install", code != 0 and "build failed" in out.lower(), out)

# 9. container made from the Docker tab (template, same image, no plugin label): recreated with Unraid's own update
reset()
for f, v in (("container", "1"), ("c_label", "\n"), ("c_managed", "dockerman\n"), ("c_image", "photo-song-video:latest\n"), ("c_port", "8100\n")):
    (STATE / f).write_text(v)
code, out = run_plg(plg1)
check("9 template container recreated by update_container, no docker run",
      code == 0 and "update_container photo-song-video" in calls() and "docker run" not in calls(), out + calls())
check("9 reports the encoder and the container's port", "GPU (NVENC)" in out and ":8100" in out, out[-300:])

# 10. same, but Unraid's update script is missing: left alone with instructions
reset()
for f, v in (("container", "1"), ("c_label", "\n"), ("c_managed", "dockerman\n"), ("c_image", "photo-song-video:latest\n")):
    (STATE / f).write_text(v)
(ROOT / "update_container").unlink()
code, out = run_plg(plg1)
check("10 no update script: container left alone", code == 0 and "docker run" not in calls() and "left as it is" in out, out)

# 8. remove
reset()
run_plg(plg1)
code, out = run_plg(plg1, method="remove")
check("8 remove deletes container, image, template, folders",
      code == 0 and not (STATE / "container").exists() and not (STATE / "image").exists()
      and not TPL.exists() and not (ROOT / "usr/local/sbin/psv-setup").exists(), out)
check("8 data folder is kept", (ROOT / "mnt/user/appdata/photo-song-video").exists())

print("\n%d check(s) failed" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
