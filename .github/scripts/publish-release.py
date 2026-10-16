"""검증한 Release 바이너리를 초안에 첨부한 뒤 공개한다. 인증값은 환경에서만 읽는다."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request


# 토큰이 붙은 요청은 GitHub API와 이 저장소의 업로드 주소로만 보낸다.
def api(method, path, payload=None, *, binary=False):
    url = path if path.startswith("https://") else "https://api.github.com" + path
    parsed = urllib.parse.urlsplit(url)
    upload_path = f"/repos/{repository}/releases/{release_id}/assets"
    if parsed.hostname != "api.github.com" and not (
        parsed.hostname == "uploads.github.com" and parsed.path == upload_path
    ):
        raise RuntimeError("허용하지 않은 GitHub 요청 주소입니다.")
    data = payload if binary else json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/octet-stream" if binary else "application/json",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "MREShell-Release",
    }
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=90) as response:
        body = response.read()
        return json.loads(body) if body else None


# 첨부 파일의 크기와 서버가 계산한 SHA-256을 함께 확인한다.
def matches(asset, content):
    return asset is not None and asset.get("state") == "uploaded" and (
        asset.get("size") == len(content)
        and asset.get("digest") == "sha256:" + hashlib.sha256(content).hexdigest()
    )


repository = os.environ["GITHUB_REPOSITORY"]
release_id = None
if repository != "YiMunDong/MREShell-Release":
    raise RuntimeError("지정된 배포 저장소에서만 실행할 수 있습니다.")
manifest = json.loads(Path("releases/current.json").read_text(encoding="utf-8"))
if manifest["schemaVersion"] != 1 or not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?", manifest["tag"]):
    raise RuntimeError("릴리즈 매니페스트의 스키마 또는 태그가 잘못되었습니다.")
tag = manifest["tag"]
base = f"/repos/{repository}"
notes_path = Path(manifest["notes"])
if notes_path != Path("releases") / f"{tag}.md":
    raise RuntimeError("릴리즈 안내 파일 경로가 태그와 다릅니다.")
notes = notes_path.read_text(encoding="utf-8")

# 이미 공개된 같은 버전은 덮어쓰지 않는다. 실행 파일 해시가 같으면 재실행은 끝낸다.
try:
    release = api("GET", f"{base}/releases/tags/{tag}")
except urllib.error.HTTPError as error:
    if error.code != 404:
        raise
    release = None
if release and not release["draft"]:
    executable = next((a for a in release["assets"] if a["name"] == "MREShell.exe"), None)
    if executable and executable.get("state") == "uploaded" and (
        executable.get("size") == manifest["size"]
        and executable.get("digest") == "sha256:" + manifest["sha256"]
    ):
        print("같은 실행 파일이 이미 공개되어 있습니다: " + release["html_url"])
        raise SystemExit(0)
    raise RuntimeError("이미 공개된 버전의 파일이 매니페스트와 다릅니다. 새 태그를 사용하세요.")

# 업로드한 Git blob은 파일 트리에 넣지 않고 검증된 Release 첨부 파일로만 전달한다.
blob = api("GET", f"{base}/git/blobs/{manifest['blob']}")
if blob.get("encoding") != "base64":
    raise RuntimeError("실행 파일 blob의 인코딩이 예상과 다릅니다.")
executable = base64.b64decode(blob["content"])
if len(executable) != manifest["size"] or hashlib.sha256(executable).hexdigest() != manifest["sha256"]:
    raise RuntimeError("실행 파일의 크기 또는 SHA-256이 검증값과 다릅니다.")
if not executable.startswith(b"MZ"):
    raise RuntimeError("Windows 실행 파일 형식이 아닙니다.")
files = {
    "MREShell.exe": executable,
    "LICENSE.md": Path("LICENSE.md").read_bytes(),
    "THIRD_PARTY_NOTICES.md": Path("THIRD_PARTY_NOTICES.md").read_bytes(),
}
files["SHA256SUMS.txt"] = "".join(
    f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in sorted(files.items())
).encode()

# 첨부 중에는 초안을 유지한다. 중단 뒤 재실행해도 같은 초안과 일치하는 파일을 재사용한다.
if release is None:
    release = api("POST", f"{base}/releases", {
        "tag_name": tag,
        "target_commitish": os.environ["GITHUB_SHA"],
        "name": manifest["name"],
        "body": notes,
        "draft": True,
        "prerelease": manifest["prerelease"],
    })
release_id = release["id"]
upload_url = release["upload_url"].split("{")[0]
existing = {asset["name"]: asset for asset in release["assets"]}
for name, content in files.items():
    asset = existing.get(name)
    if matches(asset, content):
        continue
    if asset:
        api("DELETE", f"{base}/releases/assets/{asset['id']}")
    uploaded = api("POST", upload_url + "?name=" + urllib.parse.quote(name, safe=""), content, binary=True)
    if not matches(uploaded, content):
        raise RuntimeError(f"서버에 첨부된 {name}의 해시가 다릅니다. 초안을 유지합니다.")
    print(f"첨부 검증 완료: {name} ({len(content):,} bytes)")

release = api("PATCH", f"{base}/releases/{release_id}", {
    "name": manifest["name"],
    "body": notes,
    "draft": False,
    "prerelease": manifest["prerelease"],
    "make_latest": "false" if manifest["prerelease"] else "true",
})
assets = {asset["name"]: asset for asset in release["assets"]}
if release["draft"] or not all(matches(assets.get(name), content) for name, content in files.items()):
    raise RuntimeError("공개 릴리즈의 최종 첨부 파일 검증에 실패했습니다.")
print("릴리즈 게시 완료: " + release["html_url"])
