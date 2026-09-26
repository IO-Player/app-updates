#!/usr/bin/env python3
"""Keep each version-*.json's sha256 in step with the release asset its updateUrl points at.

Release tooling rewrites these manifests without knowing about sha256, so a release can leave the
field missing (the app then installs without verifying) or, worse, carry the previous release's
digest forward, which makes checksum-verifying app builds reject every download. GitHub already
computes each release asset's SHA-256, so this reads it from the API and writes it back.

Writes `changed=true|false` to $GITHUB_OUTPUT when running in Actions.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

MANIFESTS = ["version-tv.json", "version-mobile.json"]
REPO = os.environ.get("GITHUB_REPOSITORY", "IO-Player/app-updates")
TOKEN = os.environ.get("GH_TOKEN", "")
# Release tooling can push the manifest before it finishes uploading the APKs, so wait for them.
ATTEMPTS = int(os.environ.get("ASSET_WAIT_ATTEMPTS", "10"))
WAIT_SECONDS = int(os.environ.get("ASSET_WAIT_SECONDS", "30"))
URL_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/([^/?#]+)$")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")


def api(path):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def asset_digest(tag, name):
    for attempt in range(ATTEMPTS):
        try:
            release = api(f"/repos/{REPO}/releases/tags/{tag}")
            for asset in release.get("assets", []):
                digest = asset.get("digest") or ""
                if asset.get("name") == name and asset.get("state") == "uploaded" and digest.startswith("sha256:"):
                    return digest.split(":", 1)[1].lower()
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        if attempt < ATTEMPTS - 1:
            print(f"  {tag}/{name} isn't uploaded yet, checking again in {WAIT_SECONDS}s")
            time.sleep(WAIT_SECONDS)
    return None


def with_sha256(text, sha):
    # Replace the value in place so the file keeps whatever formatting the release tooling wrote.
    updated, count = re.subn(r'("sha256"\s*:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{sha}"', text, count=1)
    if count:
        return updated
    # No sha256 key: re-serialise with the key placed after updateUrl, copying the file's
    # indentation and "key": value spacing so the diff stays readable.
    data = json.loads(text)
    ordered = {}
    for key, value in data.items():
        ordered[key] = value
        if key == "updateUrl":
            ordered["sha256"] = sha
    ordered.setdefault("sha256", sha)
    indent_match = re.search(r'^([ \t]+)"', text, re.M)
    indent = indent_match.group(1) if indent_match else "    "
    sep_match = re.search(r'"[^"]+"(\s*:\s*)', text)
    sep = sep_match.group(1) if sep_match else ": "
    body = f",\n".join(f'{indent}{json.dumps(k)}{sep}{json.dumps(v)}' for k, v in ordered.items())
    return "{\n" + body + "\n}" + ("\n" if text.endswith("\n") else "")


def main():
    changed = False
    for path in MANIFESTS:
        with open(path, "rb") as fh:
            raw = fh.read()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        data = json.loads(text)
        match = URL_RE.match(data.get("updateUrl", ""))
        if not match or match.group(1).lower() != REPO.lower():
            print(f"::warning file={path}::updateUrl isn't a release download in {REPO}; sha256 left as is")
            continue
        _, tag, name = match.groups()
        digest = asset_digest(tag, name)
        if not digest or not SHA_RE.match(digest):
            print(f"::warning file={path}::release asset {tag}/{name} not found; sha256 left as is")
            continue
        if data.get("sha256") == digest:
            print(f"{path}: sha256 already matches {tag}/{name}")
            continue
        new_text = with_sha256(text, digest)
        if json.loads(new_text).get("sha256") != digest:
            sys.exit(f"{path}: failed to write sha256")
        with open(path, "wb") as fh:
            fh.write((b"\xef\xbb\xbf" if bom else b"") + new_text.encode("utf-8"))
        print(f"{path}: sha256 {data.get('sha256') or '(missing)'} -> {digest} ({tag}/{name})")
        changed = True

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")


if __name__ == "__main__":
    main()
