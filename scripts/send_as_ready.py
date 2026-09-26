#!/usr/bin/env python3
"""Send each Video_* artifact to Telegram as soon as it appears."""
import glob
import json
import os
import subprocess
import sys
import time

REPO = os.environ["GITHUB_REPOSITORY"]
RUN_ID = os.environ["GITHUB_RUN_ID"]
FORMAT = os.environ.get("FORMAT", "youtube")
BRANDING = os.environ.get("BRANDING", "")
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
if TOKEN:
    os.environ["GH_TOKEN"] = TOKEN

DEADLINE = time.time() + 5 * 3600


def gh_json(path: str):
    out = subprocess.check_output(["gh", "api", path], text=True)
    return json.loads(out)


def list_artifacts():
    data = gh_json(f"repos/{REPO}/actions/runs/{RUN_ID}/artifacts?per_page=100")
    return data.get("artifacts", [])


def list_jobs():
    jobs = []
    page = 1
    while True:
        data = gh_json(
            f"repos/{REPO}/actions/runs/{RUN_ID}/jobs?per_page=100&page={page}"
        )
        batch = data.get("jobs", [])
        if not batch:
            break
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return jobs


def render_jobs_finished():
    render = [
        j
        for j in list_jobs()
        if j.get("name", "").startswith("render-video")
    ]
    if not render:
        return False
    return all(j.get("status") == "completed" for j in render)


def download_artifact(name: str, dest: str):
    subprocess.check_call(
        [
            "gh",
            "run",
            "download",
            RUN_ID,
            "--repo",
            REPO,
            "-n",
            name,
            "-D",
            dest,
        ]
    )


def send_file(path: str, folder: str) -> bool:
    return (
        subprocess.call(
            [
                sys.executable,
                "scripts/telegram_send.py",
                path,
                folder,
                FORMAT,
                BRANDING,
                "n/a",
            ]
        )
        == 0
    )


def main():
    sent = set()
    print("Watching for Video_* artifacts...")
    while time.time() < DEADLINE:
        try:
            arts = list_artifacts()
        except Exception as e:
            print(f"list artifacts error: {e}")
            time.sleep(20)
            continue

        for art in arts:
            name = art.get("name", "")
            if not name.startswith("Video_") or art.get("expired"):
                continue
            if name in sent:
                continue
            folder = name[len("Video_") :]
            dest = f"dl_{name}"
            os.makedirs(dest, exist_ok=True)
            print(f"==== Downloading {name} ====")
            try:
                download_artifact(name, dest)
            except Exception as e:
                print(f"download fail {name}: {e}")
                continue
            mp4s = glob.glob(f"{dest}/**/*.mp4", recursive=True)
            if not mp4s:
                print(f"no mp4 in {name}")
                continue
            print(f"==== Sending {mp4s[0]} ====")
            if send_file(mp4s[0], folder):
                sent.add(name)
                print(f"OK {name}  total_sent={len(sent)}")
                time.sleep(10)
            else:
                print(f"FAIL {name} — will retry")

        try:
            finished = render_jobs_finished()
        except Exception as e:
            print(f"jobs poll error: {e}")
            finished = False

        if finished:
            print("All render jobs completed.")
            time.sleep(15)
            try:
                arts = list_artifacts()
            except Exception:
                arts = []
            pending = [
                a["name"]
                for a in arts
                if a.get("name", "").startswith("Video_")
                and a["name"] not in sent
                and not a.get("expired")
            ]
            if not pending:
                print(f"Done. Sent {len(sent)} videos.")
                break
            print(f"Still pending: {pending}")

        time.sleep(20)
    else:
        print("Timeout waiting for artifacts")
        sys.exit(1)

    if not sent:
        print("No videos sent")
        sys.exit(1)


if __name__ == "__main__":
    main()
