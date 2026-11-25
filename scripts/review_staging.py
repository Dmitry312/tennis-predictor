"""Private server staging. No DB, S3, Git or external network operations."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time

from scripts import export_match_samples as e
from scripts.sample_delivery import unpack_archive, LIMIT

TTL = 24*60*60
MAX_PENDING = 2


def ticket(value):
    e.need(type(value)is str and re.fullmatch(r"gh-[0-9]{1,20}-[0-9]{1,5}",value),"stage_ticket")
    return value


def review(value):
    e.need(type(value)is dict and set(value)=={"content_reviewed","rights_reviewed",
        "content_evidence","rights_evidence","archive_sha256"},"publication_review_required")
    e.need(value["content_reviewed"] is True and value["rights_reviewed"] is True,
           "publication_review_required")
    e.need(all(type(value[k])is str and 1<=len(value[k])<=512
               for k in ("content_evidence","rights_evidence")),"publication_review_required")
    e.need(type(value["archive_sha256"])is str and
           re.fullmatch("[a-f0-9]{64}",value["archive_sha256"]),"stage_digest")
    e.inspect_content(value)
    return value


def owned(path, directory=True):
    value=path.lstat()
    e.need(not stat.S_ISLNK(value.st_mode) and value.st_uid==os.geteuid() and
           stat.S_IMODE(value.st_mode)==(0o700 if directory else 0o600) and
           (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)),
           "private_stage_required")


@contextmanager
def locked(root=None):
    root=Path(root) if root is not None else Path("/tmp")/("tennis-match-review-"+str(os.geteuid()))
    if not root.exists():
        root.mkdir(mode=0o700)
    owned(root)
    lock=root/".lock"
    e.need(not lock.is_symlink(),"private_stage_required")
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"r+") as stream:
        owned(lock,False)
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield root


def store(data, stage_ticket, source_sha, *, root=None):
    stage_ticket=ticket(stage_ticket)
    e.need(re.fullmatch("[a-f0-9]{40}",source_sha) and 0<len(data)<=LIMIT,"stage_invalid")
    with locked(root) as base:
        e.need(not (base/stage_ticket).exists() and
               len([p for p in base.iterdir() if p.is_dir()])<MAX_PENDING,"stage_capacity")
        with tempfile.TemporaryDirectory(prefix=".stage-",dir=base) as temp:
            temp=Path(temp)
            unpack_archive(data,temp/"review")
            report=e.read_json(temp/"review"/"acquisition-report.json",1024*1024)
            e.need(report.get("source_commit")==source_sha,"stage_source_sha")
            with (temp/"payload.zip").open("xb") as stream:
                os.chmod(temp/"payload.zip",0o600)
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            metadata={"ticket":stage_ticket,"archive_sha256":e.sha(data),"source_sha":source_sha,
                      "created_at":int(time.time()),"exported_count":report["exported_count"]}
            with (temp/"stage.json").open("xb") as stream:
                os.chmod(temp/"stage.json",0o600)
                stream.write(e.dumps(metadata))
            # Atomic directory move; the TemporaryDirectory context removes the empty old path.
            destination=base/stage_ticket
            os.rename(temp,destination)
            temp.mkdir(mode=0o700)
    return metadata


def release(stage_ticket, digest, source_sha, approval, *, root=None):
    approval=review(approval)
    e.need(approval["archive_sha256"]==digest,"stage_digest")
    with locked(root) as base:
        stage=base/ticket(stage_ticket)
        owned(stage); owned(stage/"stage.json",False); owned(stage/"payload.zip",False)
        metadata=e.read_json(stage/"stage.json",4096)
        e.need(metadata.get("ticket")==stage_ticket and metadata.get("source_sha")==source_sha and
               metadata.get("archive_sha256")==digest,"stage_changed")
        age=time.time()-metadata["created_at"]
        e.need(0<=age<=TTL,"stage_expired")
        with (stage/"payload.zip").open("rb") as stream: data=stream.read(LIMIT+1)
        e.need(len(data)<=LIMIT and e.sha(data)==digest,"stage_changed")
        # Validate immutable bytes again; the human approval is bound to those exact bytes.
        with tempfile.TemporaryDirectory(prefix="recheck-match-stage-") as tmp:
            fresh=Path(tmp)/"review"
            unpack_archive(data,fresh)
            inspected=stage/"review"
            owned(inspected)
            actual=set()
            for path in inspected.rglob("*"):
                e.need(not path.is_symlink(),"stage_review_changed")
                if path.is_file(): actual.add(path.relative_to(inspected))
            expected={path.relative_to(fresh) for path in fresh.rglob("*") if path.is_file()}
            e.need(actual==expected,"stage_review_changed")
            for relative in sorted(expected):
                old=inspected/relative
                new=fresh/relative
                e.need(old.stat().st_size==new.stat().st_size and old.read_bytes()==new.read_bytes(),
                       "stage_review_changed")
        return data


def cleanup(stage_ticket, *, root=None):
    with locked(root) as base:
        stage=base/ticket(stage_ticket)
        if stage.exists():
            owned(stage)
            shutil.rmtree(stage)
