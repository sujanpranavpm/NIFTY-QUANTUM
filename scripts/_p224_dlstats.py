import json

m = json.load(open("logs/p224_download_manifest.json", encoding="utf-8"))
print("records", len(m))
print("eligible", sum(1 for r in m if r["status"] == "eligible"))
print("bytes_downloaded", sum(r.get("downloaded_bytes", 0) for r in m))
print("sha_match", sum(1 for r in m if r.get("sha256") == r.get("lfs_sha256")))
print("size_match", sum(1 for r in m
                        if r.get("downloaded_bytes") == r.get("published_size")))
print("min_expiry", min(r["expiry"] for r in m))
print("max_expiry", max(r["expiry"] for r in m))
