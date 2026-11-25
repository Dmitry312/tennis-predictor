"""Authenticated CMS delivery; no plaintext sample is written by seal."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
LIMIT = 64*1024*1024


def need(value):
    if not value: raise ValueError("sample_delivery_failed")


def certificate(path, expected):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    data = Path(path).read_bytes()
    need(len(data)<=65536 and re.fullmatch("[a-f0-9]{64}",expected))
    cert = x509.load_pem_x509_certificate(data)
    key = cert.public_key()
    need(isinstance(key,rsa.RSAPublicKey) and key.key_size>=3072 and
         key.public_numbers().e==65537)
    now = datetime.now(timezone.utc)
    need(cert.not_valid_before_utc <= now and cert.not_valid_after_utc > now+timedelta(days=7))
    need(cert.fingerprint(hashes.SHA256()).hex() == expected)


def seal(source, destination, cert, fingerprint):
    certificate(cert,fingerprint)
    data = source.read(LIMIT+1)
    need(0 < len(data) <= LIMIT)
    # AES-GCM provides authentication; OpenSSL emits standard CMS AuthEnvelopedData.
    with tempfile.TemporaryDirectory(prefix="sealed-match-sample-") as tmp:
        target = Path(tmp)/"sample.cms"
        result = subprocess.run(["openssl","cms","-encrypt","-aes-256-gcm","-binary",
            "-outform","DER","-recip",str(cert),"-out",str(target)],
            input=data,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
        need(result.returncode == 0 and 0 < target.stat().st_size <= LIMIT+65536)
        destination = Path(destination)
        need(not destination.exists() and not destination.is_symlink())
        with destination.open("xb") as stream:
            os.chmod(destination,0o600)
            stream.write(target.read_bytes())


def unpack(source, destination, private_key):
    """Decrypt locally, verify authentication, then extract bounded regular files."""
    source, destination = Path(source),Path(destination)
    need(source.stat().st_size <= LIMIT+65536 and not destination.exists())
    with tempfile.TemporaryDirectory(prefix="review-match-sample-") as tmp:
        clear = Path(tmp)/"verified.zip"
        result = subprocess.run(["openssl","cms","-decrypt","-binary","-inform","DER",
            "-in",str(source),"-inkey",str(private_key),"-out",str(clear)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
        # OpenSSL may write bytes before authentication fails; never extract those.
        need(result.returncode==0 and clear.stat().st_size <= LIMIT)
        with zipfile.ZipFile(clear) as z:
            entries = z.infolist()
            need(0 < len(entries) <= 150)
            names = [v.filename for v in entries]
            need(len(names)==len(set(names)) and sum(v.file_size for v in entries)<=LIMIT)
            for value in entries:
                path = PurePosixPath(value.filename)
                need(not path.is_absolute() and ".." not in path.parts and
                     "\\" not in value.filename and not value.is_dir() and
                     (value.external_attr >> 16) & 0o170000 == 0o100000)
                need(value.filename in ("selection.json","acquisition-report.json","catalog/index.json") or
                     re.fullmatch(r"catalog/samples/match_[a-f0-9-]{36}/"
                                  r"(matches|nbbet_details|nbbet_stats|pbp|nbbet_raw|manifest)\.json",
                                  value.filename))
            staged = Path(tmp)/"review"; staged.mkdir(mode=0o700)
            for value in entries:
                path = staged/value.filename
                path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                with path.open("xb") as output: output.write(z.read(value))
            # Reuse existing integrity and content verification before delivery.
            from scripts.export_match_samples import reuse, read_json, inspect_content
            loaded = reuse(staged/"catalog")
            expected = {"selection.json","acquisition-report.json","catalog/index.json"}
            for key in loaded:
                expected |= {"catalog/samples/match_"+key+"/"+name for name in
                    ("matches.json","nbbet_details.json","nbbet_stats.json","pbp.json","nbbet_raw.json","manifest.json")}
            need(set(names) == expected)
            for name in ("selection.json","acquisition-report.json"):
                inspect_content(read_json(staged/name,1024*1024))
            import shutil
            # Move same-filesystem private staging into chosen local directory.
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copytree(staged,destination)


def import_reviewed(source, destination, *, content_reviewed=False, rights_reviewed=False):
    """Explicit local integration; preserves original hashes and synthetic entries."""
    need(content_reviewed and rights_reviewed)
    from scripts import export_match_samples as e
    loaded = e.reuse(Path(source)/"catalog")
    packages = {}
    for key,bundle in loaded.items():
        manifest = dict(bundle["manifest"])
        manifest.pop("files")
        manifest.pop("bundle_digest")
        packages[key] = (bundle["records"],manifest)
    return e.publish(destination,packages)


def main(argv=None):
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("mode",choices=("verify","seal","unpack","import-reviewed"))
        parser.add_argument("--certificate")
        parser.add_argument("--fingerprint")
        parser.add_argument("--output")
        parser.add_argument("--input")
        parser.add_argument("--private-key")
        parser.add_argument("--content-reviewed",action="store_true")
        parser.add_argument("--rights-reviewed",action="store_true")
        args = parser.parse_args(argv)
        if args.mode=="verify": certificate(args.certificate,args.fingerprint)
        elif args.mode=="seal": seal(sys.stdin.buffer,args.output,args.certificate,args.fingerprint)
        elif args.mode=="unpack": unpack(args.input,args.output,args.private_key)
        else: import_reviewed(args.input,args.output,content_reviewed=args.content_reviewed,
                              rights_reviewed=args.rights_reviewed)
        return 0
    except Exception:
        print("sample_delivery_failed",file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
