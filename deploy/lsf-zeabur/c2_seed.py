"""Append-only synthetic C2 batch, under the caller's existing service lock.

Files, history and embeddings are not one transaction. The durable started marker
is intentionally left after failure: restart refuses partial batches, never heals.
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import frontmatter
from c2_vectors import valid_unit

BASELINE = "ab7a348b11e6c7d8725d8d0c4ad1f0a26e19cec6"
BATCH = "c2-v1"
IDS = [f"c200000000{i:02d}" for i in range(1, 22)]
NAME = ".c2-v1.json"
MODEL = "synthetic-embedding-4-v1"

def require(condition, code):
    if not condition:
        raise RuntimeError(code)

def digest(data):
    return hashlib.sha256(data).hexdigest()

def write_new(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(path.parent)

def sync_dir(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def definitions(today):
    from bucket_manager import BucketManager
    previous = (datetime.fromisoformat(today).date()-timedelta(days=1)).isoformat()
    require(today >= "2026-10-02", "c2_clock_before_history")
    rows = []
    def add(n, name, domain, body, importance=5, date=None, kind="dynamic",
            tags=(), valence=.5, arousal=.3, **extra):
        bid, date = IDS[n-1], date or today
        post = BucketManager._build_bucket_post(
            bid, body, name=name, domain=domain, tags=[BATCH, *tags],
            importance=importance, valence=valence, arousal=arousal,
            bucket_type=kind, created=date+"T00:00:00",
            last_active=date+"T00:00:00", created_date=date,
            pinned=extra.pop("pinned", False), topics=extra.pop("topics", None))
        post.metadata.update(extra)
        folder = {"permanent":"permanent", "archive":"archive", "feel":"feel"}.get(kind, "dynamic")
        rows.append((Path(folder)/BATCH/f"{name}_{bid}.md", post))
    add(1,"C2-NAME-EXACT",["c2-lex"],"C2-LEX-01",8)
    add(2,"C2-NAME-EXACT-NEAR",["c2-lex"],"C2-LEX-02",8)
    add(3,"C2-TAG-TODAY",["c2-lex"],"C2-LEX-03",8,
        tags=["C2-TAG-EXACT","C2-ZERO"],valence=0,arousal=0)
    add(4,"C2-BODY-LOW",["c2-lex"],"C2-BODY-NEEDLE：仅用于正文子串检索。",2)
    add(5,"C2-TAG-YESTERDAY",["c2-lex"],"C2-LEX-05",9,date=previous,
        tags=["C2-TAG-EXACT","C2-ZERO"],valence=1,arousal=1)
    add(6,"C2-OTHER-DOMAIN",["c2-other"],"C2-LEX-06",9,tags=["C2-TAG-EXACT"])
    add(7,"C2-DORMANT",["c2-state"],"C2STATE dormant",dormant=True)
    add(8,"C2-SEALED",["c2-state"],"C2STATE sealed",sealed=1)
    add(9,"C2-COMPRESSED",["c2-state"],"C2STATE compressed：正文保留。",tags=["compressed"])
    add(10,"C2-OLD",["c2-state"],"C2STATE old",
        superseded_by=IDS[10],superseded_at=today+"T00:00:00")
    add(11,"C2-SUCCESSOR",["c2-state"],"C2STATE successor",supersedes=[IDS[9]])
    add(12,"C2-FEEL",[],"C2FEEL synthetic emotion",kind="feel",tags=["C2-FEEL"])
    add(13,"C2-SESSION",["session"],"C2SESSION synthetic archive",
        kind="archive",tags=["C2-SESSION"],topics=["C2-TOPIC"])
    add(14,"C2-PIN",["c2-emerge"],"C2EMERGE synthetic pinned",
        importance=10,kind="permanent",pinned=True)
    add(15,"C2-HISTORY",["c2-history"],"C2HISTORY NEW-V2",date="2026-10-01",
        updated_at="2026-10-02",last_active="2026-10-02T00:00:00")
    for n in range(16,21):
        add(n,f"C2-PAGE-{n}",["c2-page"],f"C2PAGE short {n}")
    add(21,"C2-PAGE-LONG",["c2-page"],"C2PAGE C2-LONG-HEAD\n"+
        "这是一段只用于隔离验收的合成长文。"*400+"\nC2-LONG-TAIL")
    return rows

def source_identity():
    here = Path(__file__).resolve().parent
    source = here.parents[1]
    paths = [here/n for n in ("c2_seed.py","c2_vectors.py","provider_stub.py")]
    paths += [source/n for n in ("bucket_manager.py","embedding_engine.py")]
    return {str(p.relative_to(source)):digest(p.read_bytes()) for p in paths}

def sql_rows(connection, table):
    key = "bucket_id" if table == "embeddings" else "id"
    return connection.execute(f"SELECT * FROM {table} ORDER BY {key}").fetchall()

async def initialize_c2(ob, root, lock):
    root = Path(root)
    require(not lock.closed and (os.fstat(lock.fileno()).st_dev,os.fstat(lock.fileno()).st_ino) ==
            ((root/".service.lock").stat().st_dev,(root/".service.lock").stat().st_ino),
            "c2_caller_lock_required")
    require(root.resolve() == root and not any(p.is_symlink() for p in root.rglob("*")),
            "c2_path_redirected")
    buckets = root/"buckets"
    require(Path(ob.config["buckets_dir"]) == buckets, "c2_root_mismatch")
    marker = root/NAME
    require(not (root/".c2-v1-complete.tmp").exists(), "c2_partial_requires_review")
    identity = dict(format=1,batch=BATCH,baseline=BASELINE,group="L-SF",
                    source_hashes=source_identity())
    db = Path(ob.bucket_mgr.history_db_path)
    engine = ob.bucket_mgr.embedding_engine
    require(engine and engine.enabled and engine.model == MODEL, "c2_embedding_configuration")
    with sqlite3.connect(db) as conn:
        for table, columns in {
            "bucket_history":{"id","bucket_id","old_content","changed_at","change_type"},
            "letters":{"id","content","created_at","session_id","sealed"},
        }.items():
            require({r[1] for r in conn.execute(f"PRAGMA table_info({table})")} == columns,
                    "c2_schema_mismatch")
        if marker.exists():
            state = json.loads(marker.read_text())
            require(all(state.get(k)==v for k,v in identity.items()), "c2_identity_conflict")
            require(state.get("status")=="complete", "c2_partial_requires_review")
            require([r["id"] for r in state["buckets"]]==IDS, "c2_manifest_conflict")
            require(set(state["sql_rows"])=={"letters","bucket_history"} and
                    len(state["sql_rows"]["letters"])==2 and
                    len(state["sql_rows"]["bucket_history"])==1 and
                    [r[0] for r in state["vectors"]]==[bid for bid in IDS if bid != IDS[7]],
                    "c2_incomplete_manifest")
            expected={post["id"]:(path.as_posix(),digest(frontmatter.dumps(post).encode()))
                      for path,post in definitions(state["today"])}
            found=[str(frontmatter.load(p).get("id","")) for p in buckets.rglob("*.md")]
            require(all(found.count(bid)==1 for bid in IDS), "c2_id_collision")
            for row in state["buckets"]:
                require((row["path"],row["file_sha256"])==expected[row["id"]], "c2_manifest_conflict")
                path = buckets/row["path"]
                require(path.resolve().is_relative_to(buckets), "c2_manifest_path")
                require(path.is_file() and digest(path.read_bytes())==row["file_sha256"],
                        "c2_fixture_changed")
            for table, records in state["sql_rows"].items():
                require(table in ("letters","bucket_history"), "c2_manifest_table")
                for row in records:
                    require(conn.execute(f"SELECT * FROM {table} WHERE id=?",(row[0],)).fetchone()==tuple(row),
                            "c2_receipt_changed")
            with sqlite3.connect(engine.db_path) as vectors:
                for row in state["vectors"]:
                    require(vectors.execute("SELECT bucket_id,embedding,model,updated_at FROM embeddings WHERE bucket_id=?",
                                             (row[0],)).fetchone()==tuple(row), "c2_index_changed")
            return dict(initialized=False,today=state["today"],bucket_count=21)

        require(not any((buckets/k/BATCH).exists() for k in ("dynamic","archive","feel","permanent")),
                "c2_directory_collision")
        old_files = {p:digest(p.read_bytes()) for p in buckets.rglob("*.md")}
        require(not any(str(frontmatter.load(p).get("id","")) in IDS for p in old_files),
                "c2_id_collision")
        marks = ",".join("?" for _ in IDS)
        for table, column in (("bucket_history","bucket_id"),("letters","session_id")):
            require(not conn.execute(f"SELECT 1 FROM {table} WHERE {column} IN ({marks}) LIMIT 1",IDS).fetchone(),
                    "c2_sql_collision")
        old_sql = {t:sql_rows(conn,t) for t in ("bucket_history","letters")}
        with sqlite3.connect(engine.db_path) as vectors:
            old_vectors = sql_rows(vectors,"embeddings")
            require(not any(r[0] in IDS for r in old_vectors), "c2_index_collision")
        seed_marker = root/".synthetic-initialized.json"
        seed_bytes = seed_marker.read_bytes()
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        fixtures = definitions(today)
        state = dict(identity,status="started",today=today,buckets=[])
        write_new(marker,json.dumps(state,sort_keys=True).encode())
        for path,post in fixtures:
            target = buckets/path
            target.parent.mkdir(parents=True,exist_ok=True)
            data = frontmatter.dumps(post).encode()
            write_new(target,data)
            state["buckets"].append(dict(id=post["id"],path=path.as_posix(),
                metadata=dict(post.metadata),body_sha256=digest(post.content.encode()),
                body_chars=len(post.content),file_sha256=digest(data)))

        conn.execute("BEGIN IMMEDIATE")
        try:
            hid = conn.execute("INSERT INTO bucket_history(bucket_id,old_content,changed_at,change_type) VALUES (?,?,?,?)",
                (IDS[14],"C2HISTORY OLD-V1","2026-10-02T00:00:00","replace")).lastrowid
            lids = [conn.execute("INSERT INTO letters(content,created_at,session_id,sealed) VALUES (?,?,?,?)",
                (f"C2LETTER {name} synthetic handoff",today+"T00:00:00",IDS[12],sealed)).lastrowid
                for name,sealed in (("PUBLIC",0),("HIDDEN",1))]
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        # Only new, unsealed C2 objects; real SDK -> loopback HTTP -> parser -> store.
        indexed = []
        for _,post in fixtures:
            if post.get("sealed"):
                continue
            require(await engine.generate_and_store(post["id"],post.content), "c2_index_failed")
            require(valid_unit(await engine.get_embedding(post["id"])), "c2_vector_invalid")
            indexed.append(post["id"])
        require(seed_marker.read_bytes()==seed_bytes, "c2_seed_marker_changed")
        require(all(digest(p.read_bytes())==h for p,h in old_files.items()), "c2_prior_bucket_changed")
        for table, records in old_sql.items():
            require(all(conn.execute(f"SELECT * FROM {table} WHERE id=?",(r[0],)).fetchone()==r for r in records),
                    "c2_prior_row_changed")
        state["sql_rows"] = {
            "bucket_history":[list(conn.execute("SELECT * FROM bucket_history WHERE id=?",(hid,)).fetchone())],
            "letters":[list(conn.execute("SELECT * FROM letters WHERE id=?",(i,)).fetchone()) for i in lids]}
        with sqlite3.connect(engine.db_path) as vectors:
            require(all(vectors.execute("SELECT * FROM embeddings WHERE bucket_id=?",(r[0],)).fetchone()==r
                        for r in old_vectors), "c2_prior_vector_changed")
            state["vectors"] = [list(vectors.execute("SELECT bucket_id,embedding,model,updated_at FROM embeddings WHERE bucket_id=?",
                                                      (bid,)).fetchone()) for bid in indexed]
        state.update(status="complete",preserved_prior_files=len(old_files),
                     seed_marker_sha256=digest(seed_bytes))
        temp = root/".c2-v1-complete.tmp"
        write_new(temp,json.dumps(state,ensure_ascii=False,sort_keys=True,indent=2).encode())
        os.replace(temp,marker)
        sync_dir(root)
        return dict(initialized=True,today=today,bucket_count=21)
